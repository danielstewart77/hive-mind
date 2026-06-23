"""Codex CLI harness template — provider-agnostic.

Spawns one Codex subprocess per turn (`codex exec --json
--dangerously-bypass-approvals-and-sandbox`) and stores the `thread_id`
so subsequent turns resume the same Codex thread. The provider is chosen
in ``runtime.yaml``: ``provider: openai`` (or any non-ollama value) runs
Codex against its native backend, while ``provider: ollama`` injects a
per-mind ``model_provider`` override pointing at the configured base URL.
The live minds Nagatha and Mordecai run this shape on OpenAI; Bilby runs
it on Ollama. One template, both paths.

The system prompt is composed upstream by hive-comms and shipped as
``system_prompt_blocks`` in the spawn payload — this module does not
compose anything locally.

When the model emits its tool call in a dialect Codex does not parse
(Llama 3 sentinels, Mistral [TOOL_CALLS] prose, an improvised JSON
schema, etc.), Codex files the text as ``agent_reasoning`` and closes
the turn with no ``agent_message``. To stop that surfacing as the
generic "mind stream closed with no text output" placeholder, the relay
yields a synthetic assistant frame containing
``compose_empty_turn_diagnostic`` output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def compose_empty_turn_diagnostic(
    last_reasoning_text: str,
    last_other_item_type: str,
) -> str:
    """Build the diagnostic body for a turn that produced no agent_message.

    Inputs are best-effort observations from the relay loop:
      - last_reasoning_text: text from the most recent agent_reasoning item,
        if any. Empty string if none was seen.
      - last_other_item_type: type of the most recent non-agent_message
        item.completed event, if any (e.g. "command_execution"). Empty
        string if none was seen.
    """
    parts = ["Mind produced no agent message this turn."]
    if last_reasoning_text:
        parts.append(
            "The model emitted text on the reasoning channel instead, which "
            "usually means it tried to call a tool in a dialect Codex does "
            "not parse. Raw reasoning text follows:"
        )
        parts.append(last_reasoning_text)
    elif last_other_item_type:
        parts.append(
            f"Last item type Codex received was '{last_other_item_type}'."
        )
    else:
        parts.append(
            "No reasoning or other content was captured. Check the rollout "
            "JSONL under .codex/sessions for details."
        )
    return "\n\n".join(parts)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hive-mind.minds.MIND_NAME")

HERE = Path(__file__).parent
PROJECT_DIR = Path("/usr/src/app")

RUNTIME = yaml.safe_load((HERE / "runtime.yaml").read_text())
NAME: str = RUNTIME["name"]
MIND_ID: str = RUNTIME["mind_id"]
DEFAULT_MODEL: str = RUNTIME["default_model"]
PROVIDER: str = RUNTIME["provider"]
RUNTIME_ENV: dict[str, Any] = RUNTIME.get("env", {}) or {}

NS_URL = os.environ.get("HIVE_MIND_SERVER_URL", "http://server:8420")

CODEX_HOME = Path(RUNTIME.get("runtime_config_dir", f"/usr/src/app/minds/{NAME}/.codex"))

app = FastAPI(title=f"Mind: {NAME}")

# session_id -> {"system_prompt": str, "thread_id": str | None, "model": str}
SESSIONS: dict[str, dict] = {}


def _setup_codex_home() -> None:
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    os.environ["CODEX_HOME"] = str(CODEX_HOME)


_setup_codex_home()


async def _fetch_secrets_on_startup() -> None:
    _ENV_MAP = {"gh_oauth_token": "GH_TOKEN", "mcp_auth_token": "MCP_AUTH_TOKEN"}
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(
                f"{NS_URL}/secrets/scopes/{NAME}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return
                scopes = await resp.json()
                secret_keys = scopes.get("secret_keys", []) or []
            for key in secret_keys:
                try:
                    async with http.get(
                        f"{NS_URL}/secrets/{key}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            value = data.get("value")
                            if value:
                                env_name = _ENV_MAP.get(key, key.upper())
                                os.environ[env_name] = value
                                log.info("Secret %s loaded into %s", key, env_name)
                except Exception:
                    log.debug("Could not fetch secret %s", key)
    except Exception:
        log.debug("Could not connect to NS for secrets")


def _provider_args() -> list[str]:
    if PROVIDER != "ollama":
        return []

    base_url = str(
        RUNTIME_ENV.get("OLLAMA_BASE_URL")
        or RUNTIME_ENV.get("OPENAI_BASE_URL")
        or "http://localhost:11434/v1"
    ).rstrip("/")

    provider_key = f"{NAME}_ollama"
    return [
        "-c",
        f'model_provider="{provider_key}"',
        "-c",
        f'model_providers.{provider_key}.name="{NAME.capitalize()} Ollama"',
        "-c",
        f'model_providers.{provider_key}.base_url="{base_url}"',
    ]


async def _reap_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Kill the codex subprocess group and wait for it to exit.

    codex is a node wrapper that spawns a rust binary as its child. Killing
    only the node parent would orphan the rust child to PID 1 (us). Spawning
    with start_new_session=True puts both in their own process group; killpg
    on SIGKILL takes them both down. Safe to call when proc is None or already
    exited.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        log.warning("codex pid %s did not exit within 5s of SIGKILL", proc.pid)


@app.on_event("startup")
async def _startup() -> None:
    await _fetch_secrets_on_startup()
    log.info("%s ready (mind_id=%s, default_model=%s, codex_home=%s)",
             NAME, MIND_ID, DEFAULT_MODEL, CODEX_HOME)


@app.get("/health")
async def health() -> dict:
    return {"name": NAME, "mind_id": MIND_ID, "ok": True, "sessions": len(SESSIONS)}


@app.get("/sessions")
async def list_sessions() -> list[dict]:
    return [
        {"id": sid, "mind_id": MIND_ID, "model": s.get("model", "unknown"), "status": "running"}
        for sid, s in SESSIONS.items()
    ]


@app.post("/sessions")
async def create_session(req: Request) -> Any:
    body = await req.json()
    sid = body.get("session_id") or str(uuid4())
    model = body.get("model") or DEFAULT_MODEL
    resume_sid = body.get("resume_sid")
    system_prompt_blocks = body.get("system_prompt_blocks") or ""
    surface_prompt = body.get("surface_prompt")
    # Spawn-env metadata for the rotation hook. The hook reads these from
    # the codex subprocess env to attribute the rotation summary to the
    # right (mind_id, client_ref) row in NS's session_memory table.
    client_ref = body.get("client_ref") or ""
    owner_type = body.get("owner_type") or ""
    owner_ref = body.get("owner_ref") or ""
    try:
        if system_prompt_blocks and surface_prompt:
            full_prompt = f"{system_prompt_blocks}\n\n{surface_prompt}"
        elif surface_prompt:
            full_prompt = surface_prompt
        else:
            full_prompt = system_prompt_blocks
        SESSIONS[sid] = {
            "system_prompt": full_prompt,
            "thread_id": resume_sid,
            "model": model,
            "client_ref": client_ref,
            "owner_type": owner_type,
            "owner_ref": owner_ref,
        }
        log.info("%s session %s initialised (model=%s resume=%s)",
                 NAME, sid, model, resume_sid or "new")
        return {"session_id": sid, "mind_id": MIND_ID, "name": NAME, "status": "running", "model": model}
    except Exception as exc:
        log.exception("Failed to create session for %s", NAME)
        return JSONResponse({"error": str(exc)}, status_code=500)


async def _run_codex_turn(sid: str, content: str, images: list[dict] | None) -> Any:
    state = SESSIONS.get(sid)
    if state is None:
        yield {"type": "result", "is_error": True}
        return

    thread_id = state.get("thread_id")
    if thread_id:
        cmd = [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            state["model"],
            *_provider_args(),
            "resume",
            thread_id,
            "-",
        ]
        stdin_content = content
    else:
        cmd = [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            state["model"],
            *_provider_args(),
            "-",
        ]
        stdin_content = f"{state['system_prompt']}\n\n---\n\n{content}"

    if images:
        log.warning("%s session %s: image input not supported, ignoring", NAME, sid)

    log.info("%s session %s: spawning codex turn (thread=%s)", NAME, sid, thread_id or "new")

    env = os.environ.copy()
    env.update({k: str(v) for k, v in RUNTIME_ENV.items()})
    # Per-spawn metadata for the rotation_check Stop hook. The hook reads
    # these to attribute the rotation summary to the right (mind_id,
    # client_ref) row in NS's session_memory table. Empty values stay
    # unset so the hook can detect the no-op case ("missing mind_id/
    # client_ref in env") instead of writing under a fake key.
    if state.get("client_ref"):
        env["CLIENT_REF"] = state["client_ref"]
    if state.get("owner_type"):
        env["OWNER_TYPE"] = state["owner_type"]
    if state.get("owner_ref"):
        env["OWNER_REF"] = state["owner_ref"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
        env=env,
        cwd=str(PROJECT_DIR),
        start_new_session=True,
    )
    state["proc"] = proc
    proc.stdin.write(stdin_content.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    current_thread_id = thread_id

    # Track whether the model produced any assistant text this turn, plus the
    # most recent reasoning text and the most recent non-agent_message item
    # type. If the turn closes without an agent_message, the relay synthesises
    # a diagnostic assistant frame from these so the operator sees what the
    # model actually emitted instead of dead air.
    saw_agent_message = False
    last_reasoning_text = ""
    last_other_item_type = ""

    def _empty_turn_frame() -> dict:
        return {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": compose_empty_turn_diagnostic(
                            last_reasoning_text, last_other_item_type
                        ),
                    }
                ],
            },
        }

    async for raw_line in proc.stdout:
        line = raw_line.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")
        yield {
            "type": "codex_event",
            "session_id": sid,
            "event": event,
            "_observer_only": True,
        }

        if etype == "thread.started":
            current_thread_id = event.get("thread_id")
            state["thread_id"] = current_thread_id
        elif etype == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")
            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    saw_agent_message = True
                    yield {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": text}],
                        },
                    }
            elif item_type in ("agent_reasoning", "reasoning"):
                text = item.get("text", "") or item.get("content", "")
                if text:
                    last_reasoning_text = text
            elif item_type:
                last_other_item_type = item_type
        elif etype == "turn.completed":
            if not saw_agent_message:
                yield _empty_turn_frame()
            await _reap_proc(proc)
            state["proc"] = None
            yield {
                "type": "result",
                "session_id": current_thread_id,
                "stop_reason": "end_turn",
                "is_error": False,
            }
            return
        elif etype == "turn.failed":
            error_msg = event.get("error", {}).get("message", "Unknown error")
            log.error("%s session %s: turn failed: %s", NAME, sid, error_msg)
            # Clear the cached thread_id so the next turn starts fresh
            # rather than resuming a thread Codex still has an unanswered
            # turn sitting in (otherwise the next message gets the failed
            # turn's response — the operator ends up one turn behind).
            state["thread_id"] = None
            if not saw_agent_message:
                yield _empty_turn_frame()
            await _reap_proc(proc)
            state["proc"] = None
            yield {"type": "result", "is_error": True}
            return

    # Stream ended without turn.completed or turn.failed — treat as
    # incomplete. Same reasoning as turn.failed: don't leave thread_id
    # dirty or the next turn will resume into a broken thread.
    state["thread_id"] = None
    if not saw_agent_message:
        yield _empty_turn_frame()
    await _reap_proc(proc)
    state["proc"] = None
    yield {"type": "result", "session_id": current_thread_id, "is_error": False}


@app.post("/sessions/{sid}/message")
async def send_message(sid: str, req: Request) -> Any:
    body = await req.json()
    content = body.get("content", "")
    images = body.get("images")
    if sid not in SESSIONS:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)

    # Turn-bleed guard. Codex CLI spawns a fresh subprocess per turn, but two
    # concurrent calls would race against the same state["thread_id"] — both
    # trying to resume the same Codex thread. Refuse to start a second turn
    # while one is in flight; caller retries.
    sess = SESSIONS[sid]
    if sess.get("in_flight"):
        return JSONResponse(
            {"error": "Turn in progress, retry shortly"},
            status_code=409,
        )
    sess["in_flight"] = True

    async def stream() -> Any:
        try:
            async for event in _run_codex_turn(sid, content, images):
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            # Clear in_flight on every exit path: normal completion, generator
            # cancellation (client disconnect), or exception. Without this, an
            # abandoned stream would lock the session out of all future turns.
            sess["in_flight"] = False
            # Reap the codex subprocess on cancellation. Without this, an SSE
            # client disconnect (broker timeout, etc.) leaves the codex node +
            # rust child running until they crash on their own, leaking file
            # descriptors. The _run_codex_turn exit paths clear state["proc"]
            # on normal completion, so this only fires when the generator was
            # cancelled mid-stream.
            leftover = sess.get("proc")
            if leftover is not None:
                await _reap_proc(leftover)
                sess["proc"] = None

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/sessions/{sid}/interrupt")
async def interrupt_session(sid: str) -> Any:
    if sid not in SESSIONS:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)
    return {"ok": True, "session_id": sid, "message": "codex_per_turn"}


@app.delete("/sessions/{sid}")
async def kill_session(sid: str) -> dict:
    sess = SESSIONS.pop(sid, None)
    if sess is not None:
        await _reap_proc(sess.get("proc"))
    log.info("Killed %s session %s", NAME, sid)
    return {"session_id": sid, "status": "closed"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
