# Plan: Refactor Agent Creation with Claude Code SDK

## Context

The current `workflow_create_agent` tool uses a multi-step LangGraph state machine that calls OpenAI 5+ times, then asks the user for approval at each stage (code, libraries, name). It has a known bug where the second LangGraph interrupt (libraries approval) is silently dropped in the terminal's resume path.

The goal is to replace this with **Claude Code invoked via the Python SDK**. Claude Code can see the full project, read existing agents as examples, install dependencies, test, and iterate — producing better tools more autonomously. Output is streamed in real-time.

## Architecture Overview

```
User: "create a tool that gets stock prices"
  ↓
create_agent_with_claude_code() [workflows/create_agent.py]
  ↓
invoke_claude_code() [services/claude_code.py]
  ├─ Background thread: asyncio.run(async SDK query)
  ├─ Queue bridges async → sync generator
  ├─ Permission requests: print prompt → input() → return decision
  └─ Output chunks: put in queue → yield to terminal
  ↓
discover_tools(['agents', ...])  ← register new tool immediately
  ↓
"✅ Tool registered!"
```

## Files to Modify / Create

| File | Action | Notes |
|------|--------|-------|
| `requirements.txt` | Add `claude-agent-sdk` | Official Anthropic SDK for Python |
| `services/claude_code.py` | **NEW** | Sync wrapper for async SDK |
| `workflows/create_agent.py` | Replace tool | Remove LangGraph, add new `@tool` |
| `terminal_app.py` | Update output loop | Print chunks in real-time |

---

## 1. `requirements.txt`

Add one line:
```
claude-agent-sdk
```

---

## 2. `services/claude_code.py` (new file)

Follow the `services/speech.py` pattern: try/except import, `CLAUDE_CODE_AVAILABLE` flag, graceful fallback.

```python
import asyncio
import os
import queue
import threading
from typing import Generator, Optional

try:
    from claude_agent_sdk import query, ClaudeAgentOptions
    CLAUDE_CODE_AVAILABLE = True
except ImportError:
    CLAUDE_CODE_AVAILABLE = False

_SENTINEL = object()  # end-of-stream marker

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def invoke_claude_code(
    prompt: str,
    system_prompt: str = None,
    permission_mode: str = "default",
) -> Generator[str, None, None]:
    """Invoke Claude Code and yield text output synchronously.

    Bridges async claude-agent-sdk to a sync generator using a background
    thread + queue. Permission requests are handled interactively via
    stdout/stdin.
    """
    if not CLAUDE_CODE_AVAILABLE:
        yield "❌ claude-agent-sdk not installed. Run: pip install claude-agent-sdk"
        return

    output_queue: queue.Queue = queue.Queue()

    async def _run() -> None:
        try:
            options = ClaudeAgentOptions(
                cwd=PROJECT_ROOT,
                system_prompt=system_prompt,
                permission_mode=permission_mode,
            )
            async for message in query(prompt=prompt, options=options):
                # AssistantMessage has .content list of blocks with .text
                if hasattr(message, "content"):
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            output_queue.put(block.text)
        except Exception as e:
            output_queue.put(f"\n❌ Claude Code error: {e}")
        finally:
            output_queue.put(_SENTINEL)

    thread = threading.Thread(target=lambda: asyncio.run(_run()), daemon=True)
    thread.start()

    while True:
        item = output_queue.get()
        if item is _SENTINEL:
            break
        yield item

    thread.join()
```

**Permission handling**: With `permission_mode="default"`, Claude Code's CLI subprocess asks for permission. Since the SDK communicates with the subprocess, the prompts surface as messages in the stream. For the initial implementation this is documented but we use `"default"` mode which lets Claude Code's built-in permission system handle it (user sees prompts from the subprocess via inherited terminal I/O or SDK messages).

**Note**: If the SDK does not expose permission prompts through the message stream (they stay inside the subprocess), we can change to `"acceptEdits"` to auto-accept file writes and restrict Bash commands via `allowed_tools`. This is a refinement to make after testing.

---

## 3. `workflows/create_agent.py` — replace the tool

Remove the entire LangGraph-based implementation (all state machine, nodes, `create_agent_workflow()`, `generate_code`, `get_user_feedback`, etc.). Replace with a single clean `@tool` function.

Keep the imports from `agents/maker.py` as a fallback path.

```python
import os
from typing import Generator, Optional
from agent_tooling import tool, discover_tools
from utilities.messages import get_last_user_message
from services.claude_code import invoke_claude_code, CLAUDE_CODE_AVAILABLE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _build_system_prompt() -> str:
    """Build system prompt from CLAUDE.md for context."""
    claude_md_path = os.path.join(PROJECT_ROOT, "CLAUDE.md")
    try:
        with open(claude_md_path) as f:
            claude_md = f.read()
    except Exception:
        claude_md = ""
    return f"""You are creating a new Python agent/tool for the hive_mind system.

## Project Conventions (from CLAUDE.md):
{claude_md}

## Your Task:
Based on the user's requirements, create a new tool file in the agents/ directory.
Requirements:
- Filename: agents/<descriptive_snake_case_name>.py
- Decorator: @tool(tags=["agent"]) from agent_tooling
- Last param: messages: list[dict[str, str]] = None
- Docstring: explain what the tool does and WHEN an LLM should call it (< 1024 chars)
- Return type: Generator[str, None, None] using yield
- Type hints + string→type coercion on all params
- Use completions_streaming from utilities.openai_tools for formatted LLM responses
- Check agents/ for existing similar tools before creating
- Install any new pip dependencies if needed
"""


@tool(tags=["agent"])
def create_agent_with_claude_code(
    messages: Optional[list[str]] = None,
) -> Generator[str, None, None]:
    """Create a new agent/tool for the hive_mind system using Claude Code.

    Called when the user asks to create, build, or add a new tool, agent, skill,
    or capability. Claude Code autonomously generates the code, checks dependencies,
    and saves the file to agents/. The tool is immediately available after creation.
    """
    last_user_message = get_last_user_message(messages)
    if not last_user_message:
        yield "❌ No requirements provided. Please describe the tool you want to create."
        return

    if not CLAUDE_CODE_AVAILABLE:
        yield "❌ claude-agent-sdk not installed. Run: pip install claude-agent-sdk"
        return

    yield "🤖 Invoking Claude Code to create your tool...\n"

    system_prompt = _build_system_prompt()

    yield from invoke_claude_code(
        prompt=last_user_message,
        system_prompt=system_prompt,
    )

    # After Claude Code finishes, reload tools so the new agent is immediately available
    discover_tools(["agents", "workflows", "utilities"])
    yield "\n✅ Tool registered and ready to use!"
```

---

## 4. `terminal_app.py` — real-time streaming output

Currently `process_message` accumulates all generator chunks then `output_response` prints the whole block. This means users see nothing during long Claude Code operations.

**Change**: print chunks as they arrive in both the first-run path (line ~142) and resume path (line ~87).

```python
# process_message — first run path (replace lines ~142-143)
print("\n🤖 Assistant: ", end="", flush=True)
for partial in response_stream:
    chunk = str(partial)
    print(chunk, end="", flush=True)
    full_response += chunk
print()  # trailing newline

# Remove the call to output_response() in main() since we already printed above
# Keep full_response return value for voice mode
```

Apply the same pattern to the workflow resume path (lines ~87-93).

The `output_response()` function call in `main()` at line 536 needs to be conditional: only call it for voice mode, not for terminal output (since we already streamed it).

---

## Verification

1. **Install**: `pip install claude-agent-sdk`
2. **Test in terminal**: Run `python terminal_app.py`, ask "create a tool that converts temperatures"
3. **Verify**: Claude Code's output streams in real-time; new file appears in `agents/`; `discover_tools` registers it; follow-up message can use the tool immediately
4. **Permissions**: Verify that file write prompts are handled (either auto-accepted for agents/ writes, or shown interactively)
5. **Fallback**: If `claude-agent-sdk` not installed, verify helpful error message is shown

## Open Questions / Follow-up

- **Permission surfacing**: Needs testing — how does `permission_mode="default"` behave when Claude Code is running in SDK subprocess mode? May need to switch to hooks-based approach or `"acceptEdits"` after seeing behavior.
- **Old LangGraph code**: Removed entirely in this plan. Could keep as fallback if SDK unavailable, but adds complexity. Recommend removing cleanly.
- **Model selection**: The `query()` call uses Claude Code's default model. We could pass `model` parameter if we want to control which Claude model creates the agent.
