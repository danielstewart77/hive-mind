"""One mind server per container, running every harness.

A conversation runs on a harness — claude, codex or dsh — and can be switched
to another mid-conversation, so a container that served one harness module
would be a mind that could never leave the harness it was built on. This
process mounts all three adapters and routes each session by the harness its
spawn payload names (``harness``), its terminal attach names (the ``harness``
query) and its pane rotation names (``harness`` in the body). Each adapter
keeps its own session table, thread map and process handling exactly as it
does when run alone: nothing here reaches into one adapter's state on another's
behalf, and the session id → harness map below is the only state this module
adds.

Beside the routing it owns the parts of the wire contract that belong to the
mind as a whole rather than to one harness:

* ``GET /harnesses`` — which harnesses this mind can offer and, for each one
  it cannot, why. A harness is offered only with its CLI present, its hooks
  configured and its login in place; the checks are a list a deployment can
  replace.
* ``POST /handover`` — the outgoing conversation rendered for the incoming
  harness, read off the old harness's own transcript (``minds.transcript``).
* ``GET /models?harness=`` — one harness's listing from the inference proxy.

Selected by the container's ``command``; ``MIND_NAME`` picks the mind folder,
as it does for each adapter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.hive_logging import configure_logging, install_fastapi_logging, log_event
from minds import (
    files_api,
    github_token_api,
    models_api,
    runtime_api,
    skills_api,
    surface_token_api,
    transcript,
)
from minds.harness import claude_cli, codex_cli, dsh_cli
from minds.proactive import make_proactive_router
from minds.pty_attach import PaneAdapter, install_pty_attach

MIND_NAME = claude_cli.MIND_NAME
MIND_DIR = claude_cli.MIND_DIR
RUNTIME_PATH = claude_cli.RUNTIME_PATH
NAME: str = claude_cli.NAME
MIND_ID: str = claude_cli.MIND_ID

log = configure_logging(f"hive-mind.minds.{MIND_NAME}")

#: Each harness's adapter module, by bare name.
ADAPTERS: dict[str, Any] = {"claude": claude_cli, "codex": codex_cli, "dsh": dsh_cli}

#: Which adapter holds each live session. A session not in here is looked for
#: in every adapter's own table, which is what a mind that restarted under a
#: running conversation needs.
HARNESS_OF: dict[str, str] = {}

app = FastAPI(title=f"Mind: {NAME}", docs_url=None, redoc_url=None, openapi_url=None)
install_fastapi_logging(app, log, f"mind:{NAME}")

# Only the claude adapter has an idle stream to drain into this buffer; the
# per-turn harnesses leave it empty, as they do when run alone.
app.include_router(make_proactive_router(
    claude_cli.PROACTIVE_BUFFER, os.environ.get("COMMS_BEARER_TOKEN") or None,
))


def default_harness() -> str:
    """The harness a new conversation starts on, read from the file now.

    Read per call rather than at import: the console writes it while this
    process serves, and the next new conversation is what it is for.
    """
    try:
        loaded = runtime_api.load_runtime(RUNTIME_PATH)
    except ValueError:
        loaded = claude_cli.RUNTIME
    return runtime_api.harness_name(loaded.get("harness")) or "claude"


def _live_runtime() -> dict:
    try:
        return runtime_api.load_runtime(RUNTIME_PATH)
    except ValueError:
        return claude_cli.RUNTIME


# ---------------------------------------------------------------------------
# A conversation's model window, for the rotation threshold
# ---------------------------------------------------------------------------

#: How long a model's window is trusted before the proxy is asked again. A
#: window changes when the proxy's operator edits it, not per turn, and asking
#: on every spawn would put the proxy between a message and its reply.
WINDOW_TTL_SECONDS = 600.0

_WINDOWS: dict[tuple[str, str], tuple[float, int | None]] = {}


async def conversation_env(harness: str, model: str) -> dict[str, str]:
    """Environment naming this conversation's own model window.

    The rotation hook sizes its threshold as a percentage of a window, and the
    window it has always read is the file's — the default model's. A
    conversation switched to another model (or another harness) has that
    model's room, not the default's, so every process a conversation runs in
    is told its window, and the threshold that window gives at this mind's
    percentage. Zero means nobody has measured the model, which the hook reads
    as "fall back", never as a window of no tokens.
    """
    key = (harness, model)
    cached = _WINDOWS.get(key)
    if cached is not None and time.monotonic() - cached[0] < WINDOW_TTL_SECONDS:
        window = cached[1]
    else:
        try:
            window = await models_api.context_window(RUNTIME_PATH, harness, model)
        except Exception:  # noqa: BLE001 — a spawn must not fail for a listing
            window = None
        _WINDOWS[key] = (time.monotonic(), window)
    env = {"HIVE_HARNESS": harness, "HIVE_MODEL_CONTEXT_WINDOW": str(window or 0)}
    try:
        percent = int(_live_runtime().get("rotation_threshold_percent"))
    except (TypeError, ValueError):
        percent = 0
    if window and percent > 0:
        env["HIVE_ROTATION_THRESHOLD_TOKENS"] = str(window * percent // 100)
    return env


# ---------------------------------------------------------------------------
# Which harnesses this mind can offer
# ---------------------------------------------------------------------------

def _cli_check(harness: str) -> str | None:
    """None when the harness's CLI is installed here, else why not."""
    if harness == "dsh":
        launcher = dsh_cli.DSH_BIN
        if os.sep in launcher:
            return None if Path(launcher).is_file() else f"dsh launcher {launcher} not found"
        return None if shutil.which(launcher) else f"{launcher} not on PATH"
    return None if shutil.which(harness) else f"{harness} CLI not on PATH"


#: The hooks a harness needs before a conversation may run on it: memory and
#: rotation on Stop, per-turn context on UserPromptSubmit. A conversation on a
#: harness without them is one whose memory stops being written and which
#: grows until the harness's own compaction is all that is left.
REQUIRED_STOP_HOOKS = ("auto_remember", "rotation_check")


def _hook_commands(config: dict) -> dict[str, list[str]]:
    """Every hook command by event, from Claude's or Codex's hook block."""
    events: dict[str, list[str]] = {}
    hooks = config.get("hooks") if isinstance(config, dict) else None
    for event, groups in (hooks or {}).items() if isinstance(hooks, dict) else []:
        for group in groups if isinstance(groups, list) else []:
            for hook in (group or {}).get("hooks") or []:
                if isinstance(hook, dict) and hook.get("command"):
                    events.setdefault(event, []).append(str(hook["command"]))
    return events


def _hook_config(harness: str) -> tuple[dict | None, str]:
    """The harness's hook configuration and where it was read from."""
    try:
        if harness == "claude":
            path = claude_cli.CONFIG_DIR / "settings.json"
            return json.loads(path.read_text()), str(path)
        if harness == "codex":
            path = codex_cli.CODEX_HOME / "config.toml"
            return tomllib.loads(path.read_text()), str(path)
        named = os.environ.get("DSH_HOOKS_CONFIG", "")
        if not named:
            return None, "DSH_HOOKS_CONFIG"
        return json.loads(Path(named).read_text()), named
    except (OSError, ValueError) as exc:
        return None, f"{exc}"


def _hooks_check(harness: str) -> str | None:
    config, source = _hook_config(harness)
    if config is None:
        return f"no hook configuration ({source})"
    commands = _hook_commands(config)
    stop = " ".join(commands.get("Stop", []))
    missing = [name for name in REQUIRED_STOP_HOOKS if name not in stop]
    if not commands.get("UserPromptSubmit"):
        missing.append("a UserPromptSubmit hook")
    return f"hooks missing in {source}: {', '.join(missing)}" if missing else None


def _env_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = str(claude_cli.RUNTIME_ENV.get(name) or os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _login_check(harness: str) -> str | None:
    """None when the harness has a credential to log in with."""
    if harness == "claude":
        if (claude_cli.CONFIG_DIR / ".credentials.json").is_file() or _env_value(
            ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        ):
            return None
        return "claude has no login: no .credentials.json and no API token"
    if harness == "codex":
        if (codex_cli.CODEX_HOME / "auth.json").is_file() or _env_value(("OPENAI_API_KEY",)):
            return None
        return "codex has no login: no auth.json and no OPENAI_API_KEY"
    if dsh_cli._first_env(dsh_cli._PROXY_KEY_SOURCES):
        return None
    return "dsh has no proxy key: " + ", ".join(dsh_cli._PROXY_KEY_SOURCES) + " all unset"


#: The checks a harness must pass to be offered, in the order their reasons
#: are worth reading. Replaceable: a deployment whose login lives elsewhere
#: swaps the one check rather than forking the route.
CHECKS: list[tuple[str, Callable[[str], str | None]]] = [
    ("cli", _cli_check),
    ("hooks", _hooks_check),
    ("login", _login_check),
]


def harness_report() -> dict:
    """Every harness, whether it can be offered, and if not, why not."""
    rows = []
    for name in ADAPTERS:
        reasons = []
        for _label, check in CHECKS:
            try:
                reason = check(name)
            except Exception as exc:  # noqa: BLE001 — a broken check is a reason
                reason = f"check failed: {exc}"
            if reason:
                reasons.append(reason)
        rows.append({"name": name, "available": not reasons, "reason": "; ".join(reasons)})
    return {"harnesses": rows, "default": default_harness()}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _adapter_for(sid: str):
    """The adapter holding this session, or None."""
    name = HARNESS_OF.get(sid)
    if name is not None:
        return ADAPTERS[name]
    for name, adapter in ADAPTERS.items():
        if sid in adapter.SESSIONS:
            HARNESS_OF[sid] = name
            return adapter
    return None


@app.on_event("startup")
async def _startup() -> None:
    # One fetch serves every adapter: they share this process's environment.
    await claude_cli._fetch_secrets_on_startup()
    # After the shared fetch, never before: this mind's own GitHub token wins
    # over the hive's. Never fatal, like every other thing on this path.
    try:
        github_token_api.adopt_stored_token()
    except Exception:  # noqa: BLE001
        log.exception("could not adopt this mind's own GitHub token")
    asyncio.ensure_future(runtime_api.registration_loop(
        RUNTIME_PATH, mind_name=MIND_NAME, mind_id=MIND_ID, log=log
    ))
    log.info("%s ready (mind_id=%s, harnesses=%s, default=%s)",
             NAME, MIND_ID, ",".join(ADAPTERS), default_harness())


@app.get("/health")
async def health() -> dict:
    sessions = sum(len(adapter.SESSIONS) for adapter in ADAPTERS.values())
    return {"name": NAME, "mind_id": MIND_ID, "ok": True, "sessions": sessions}


@app.get("/sessions")
async def list_sessions() -> list[dict]:
    return [
        {"id": sid, "mind_id": MIND_ID, "model": state.get("model", "unknown"),
         "harness": name, "status": "running"}
        for name, adapter in ADAPTERS.items()
        for sid, state in adapter.SESSIONS.items()
    ]


@app.post("/sessions")
async def create_session(req: Request) -> Any:
    body = await req.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    harness = runtime_api.harness_name(body.get("harness")) or default_harness()
    adapter = ADAPTERS.get(harness)
    if adapter is None:
        return JSONResponse(
            {"error": f"{harness} is not a harness this mind runs: " + ", ".join(ADAPTERS)},
            status_code=400,
        )
    sid = str(body.get("session_id") or "")
    previous = HARNESS_OF.get(sid)
    if sid and previous and previous != harness:
        # The gateway kills before it respawns on a switch; a session still
        # held by its old adapter here is one whose kill never arrived, and
        # leaving it would put two harnesses on one conversation.
        await ADAPTERS[previous].kill_session(sid)
    model = str(body.get("model") or "").strip()
    if model:
        body = {**body, "conversation_env": await conversation_env(harness, model)}
    response = await adapter.start_session(body)
    if sid and not isinstance(response, JSONResponse):
        HARNESS_OF[sid] = harness
        log_event(log, "session.routed", mind_id=MIND_ID, mind_name=NAME,
                  session_id=sid, harness=harness, model=model or None)
    if isinstance(response, dict):
        response = {**response, "harness": harness}
    return response


@app.post("/sessions/{sid}/message")
async def send_message(sid: str, req: Request) -> Any:
    adapter = _adapter_for(sid)
    if adapter is None:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)
    return await adapter.send(sid, await req.json())


@app.post("/sessions/{sid}/interrupt")
async def interrupt_session(sid: str) -> Any:
    adapter = _adapter_for(sid)
    if adapter is None:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)
    return await adapter.interrupt_session(sid)


@app.post("/sessions/{sid}/release")
async def release_session(sid: str, surface: str) -> Any:
    adapter = _adapter_for(sid) or ADAPTERS[default_harness()]
    return await adapter.release_session(sid, surface)


@app.delete("/sessions/{sid}")
async def kill_session(sid: str, forget_thread: bool = False) -> dict:
    """End the session on every adapter, not just the one recorded.

    Each adapter's kill is idempotent, and asking all three is what makes a
    kill land on a mind that restarted under the conversation and so no
    longer knows which harness held it. ``forget_thread`` also drops codex's
    thread for the session: a switch leaving codex must not leave a thread a
    later switch back would resume instead of opening on its handover.
    """
    HARNESS_OF.pop(sid, None)
    await claude_cli.kill_session(sid)
    await codex_cli.kill_session(sid, forget_thread=forget_thread)
    await dsh_cli.kill_session(sid)
    return {"session_id": sid, "status": "closed"}


@app.get("/harnesses")
async def get_harnesses(request: Request) -> Any:
    """Which harnesses a conversation here may run on, and why not the rest."""
    denied = runtime_api.authorize_admin(request)
    if denied is not None:
        return denied
    return harness_report()


@app.post("/handover")
async def handover(request: Request) -> Any:
    """The named (outgoing) harness's conversation, rendered for the next one.

    A conversation with no transcript on disk has never had a turn: it is
    empty, not unreadable, and hands over as the summary alone or nothing —
    its harness would declare the id fresh on its next spawn anyway. A
    transcript that exists and cannot be read is different: with no summary
    to stand in for it, the switch would lose the conversation it was moving,
    so it is refused and the old harness keeps running.
    """
    denied = runtime_api.authorize_admin(request)
    if denied is not None:
        return denied
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    harness = runtime_api.harness_name(body.get("harness"))
    adapter = ADAPTERS.get(harness)
    if adapter is None:
        return JSONResponse({"error": f"{harness or 'no harness'} is not a harness"},
                            status_code=400)
    summary = str(body.get("summary") or "")
    try:
        budget = int(body.get("budget_bytes") or transcript.MAX_HANDOVER_BYTES)
    except (TypeError, ValueError):
        budget = transcript.MAX_HANDOVER_BYTES
    try:
        path = adapter.transcript_path(str(body.get("claude_sid") or ""),
                                       str(body.get("harness_sid") or "") or None)
        blocks = transcript.READERS[harness](path) if path is not None else []
    except transcript.Unreadable as exc:
        log_event(log, "session.handover.unreadable", level=logging.WARNING,
                  mind_id=MIND_ID, harness=harness, error=str(exc))
        if not summary.strip():
            return JSONResponse({"detail": "unreadable"}, status_code=422)
        blocks = []
    text = transcript.render(blocks, summary=summary, budget_bytes=budget)
    log_event(log, "session.handover.rendered", mind_id=MIND_ID, harness=harness,
              blocks=len(blocks), bytes=len(text.encode("utf-8")))
    return {"text": text}


install_pty_attach(
    app, mind_name=NAME, mind_dir=MIND_DIR,
    adapters={
        name: PaneAdapter(adapter.TERMINALS, adapter._spawn_pty, adapter._rotate_pty)
        for name, adapter in ADAPTERS.items()
    },
    default_harness=default_harness,
    conversation_env=conversation_env,
)
runtime_api.install_session_guard(app, mind_dir=MIND_DIR)
runtime_api.install_runtime_routes(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
models_api.install_models_route(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
surface_token_api.install_surface_token_routes(app, mind_id=MIND_ID, log=log)
github_token_api.install_github_token_routes(app, mind_id=MIND_ID, log=log)
# The skills and files pages act on the harness whose directories they read.
# Until they take the harness per request they answer for the default one,
# which is what each harness module mounted for itself when it ran alone.
_PAGES_HARNESS = {"claude": "claude_cli", "codex": "codex_cli"}.get(default_harness())
if _PAGES_HARNESS:
    skills_api.install_skills_routes(app, harness=_PAGES_HARNESS, mind_id=MIND_ID, log=log)
    files_api.install_files_routes(app, harness=_PAGES_HARNESS, mind_id=MIND_ID, log=log)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info",
                log_config=None, access_log=False)


if __name__ == "__main__":
    main()
