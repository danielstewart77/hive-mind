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
import importlib
import json
import logging
import os
import shutil
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

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
from minds.proactive import make_proactive_router
from minds.pty_attach import PaneAdapter, install_pty_attach

MIND_NAME = os.environ.get("MIND_NAME", "example")
MIND_DIR = Path(__file__).resolve().parent / MIND_NAME
RUNTIME_PATH = MIND_DIR / "runtime.yaml"
_BOOT_RUNTIME = runtime_api.load_runtime(RUNTIME_PATH)
NAME: str = _BOOT_RUNTIME["name"]
MIND_ID: str = _BOOT_RUNTIME["mind_id"]

log = configure_logging(f"hive-mind.minds.{MIND_NAME}")

#: Where each harness's adapter lives.
ADAPTER_MODULES = {
    "claude": "minds.harness.claude_cli",
    "codex": "minds.harness.codex_cli",
    "dsh": "minds.harness.dsh_cli",
}


def load_adapters(modules: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Import each adapter, keeping the ones that load and why the rest did not.

    One adapter that fails to import — a mind's file missing a field the
    adapter reads at import, say — must cost that harness and nothing else: a
    mind server that would not start over it takes every conversation down
    with it.
    """
    loaded: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for name, module in modules.items():
        try:
            loaded[name] = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 — any failure is this harness's
            failed[name] = f"{type(exc).__name__}: {exc}"
            log.exception("The %s adapter failed to load", name)
    return loaded, failed


#: Each harness's adapter module, by bare name, and the ones that would not load.
ADAPTERS, LOAD_FAILURES = load_adapters(ADAPTER_MODULES)

#: Which adapter holds each live session. A session not in here is looked for
#: in every adapter's own table, which is what a mind that restarted under a
#: running conversation needs.
HARNESS_OF: dict[str, str] = {}

app = FastAPI(title=f"Mind: {NAME}", docs_url=None, redoc_url=None, openapi_url=None)
install_fastapi_logging(app, log, f"mind:{NAME}")

# Only the claude adapter has an idle stream to drain into this buffer; the
# per-turn harnesses leave it empty, as they do when run alone.
app.include_router(make_proactive_router(
    getattr(ADAPTERS.get("claude"), "PROACTIVE_BUFFER", []),
    os.environ.get("COMMS_BEARER_TOKEN") or None,
))


def default_harness() -> str:
    """The harness a new conversation starts on, read from the file now.

    Read per call rather than at import: the console writes it while this
    process serves, and the next new conversation is what it is for.
    """
    return runtime_api.harness_name(_live_runtime().get("harness")) or "claude"


def _live_runtime() -> dict:
    try:
        return runtime_api.load_runtime(RUNTIME_PATH)
    except ValueError:
        return _BOOT_RUNTIME


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
    percentage. A model nobody has measured gets neither, and the hook falls
    back to the file.
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
    env = {"HIVE_HARNESS": harness}
    # Omitted, not zero, when nobody has measured the model: a hook reading
    # zero would have to know it means "fall back", and one that forgot would
    # rotate on every turn.
    if window:
        env["HIVE_MODEL_CONTEXT_WINDOW"] = str(window)
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

def _home_check(harness: str) -> str | None:
    """None when the harness has a home of this mind's own to run in.

    Never a fallback to the user's own ``~/.codex`` or ``~/.dsh``: those are
    another login and another set of threads. Claude's config directory is
    the container's, declared by its compose file.
    """
    adapter = ADAPTERS[harness]
    if not hasattr(adapter, "home_declared") or adapter.home_declared():
        return None
    return f"no {harness} home declared for this mind"


def _cli_check(harness: str) -> str | None:
    """None when the harness's CLI is installed here, else why not."""
    if harness == "dsh":
        launcher = ADAPTERS["dsh"].DSH_BIN
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
            path = ADAPTERS["claude"].CONFIG_DIR / "settings.json"
            return json.loads(path.read_text()), str(path)
        if harness == "codex":
            path = ADAPTERS["codex"].CODEX_HOME / "config.toml"
            return tomllib.loads(path.read_text()), str(path)
        # The same rule the adapter hands its bridge: the environment's file,
        # else the mind's own under DSH_HOME.
        named = ADAPTERS["dsh"].hooks_config()
        if not named:
            return None, f"DSH_HOOKS_CONFIG or {ADAPTERS['dsh'].DSH_HOME / 'hooks.json'}"
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
    declared = _BOOT_RUNTIME.get("env") or {}
    for name in names:
        value = str(declared.get(name) or os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _login_check(harness: str) -> str | None:
    """None when the harness has a credential to log in with."""
    if harness == "claude":
        if (ADAPTERS["claude"].CONFIG_DIR / ".credentials.json").is_file() or _env_value(
            ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        ):
            return None
        return "claude has no login: no .credentials.json and no API token"
    if harness == "codex":
        if (ADAPTERS["codex"].CODEX_HOME / "auth.json").is_file() or _env_value(("OPENAI_API_KEY",)):
            return None
        return "codex has no login: no auth.json and no OPENAI_API_KEY"
    # dsh logs in to the proxy, which takes both halves: an endpoint and a key.
    dsh = ADAPTERS["dsh"]
    missing = []
    if not dsh._first_env(dsh._PROXY_KEY_SOURCES):
        missing.append("proxy key (" + ", ".join(dsh._PROXY_KEY_SOURCES) + ")")
    if not dsh._first_env(dsh._PROXY_URL_SOURCES):
        missing.append("proxy URL (" + ", ".join(dsh._PROXY_URL_SOURCES) + ")")
    return "dsh has no login: no " + " and no ".join(missing) if missing else None


#: The checks a harness must pass to be offered, in the order their reasons
#: are worth reading. Replaceable: a deployment whose login lives elsewhere
#: swaps the one check rather than forking the route.
CHECKS: list[tuple[str, Callable[[str], str | None]]] = [
    ("home", _home_check),
    ("cli", _cli_check),
    ("hooks", _hooks_check),
    ("login", _login_check),
]


#: The harnesses whose spawn answers before any process runs: one turn later
#: is too late for the gateway to put a failed switch back. Claude's spawn
#: starts its process, which fails where it can be seen.
PREFLIGHT_HARNESSES = ("codex", "dsh")


async def preflight(harness: str, model: str) -> str | None:
    """Why this harness cannot run this model here, or None.

    Its home, CLI and login, and the model in that harness's listing. A
    listing that comes back empty says nothing — the proxy unreachable, or a
    mind whose codex logs in by its own account — so only a listing that names
    other models and not this one refuses.
    """
    reasons = []
    for label, check in CHECKS:
        if label == "hooks":
            continue
        try:
            reason = check(harness)
        except Exception as exc:  # noqa: BLE001
            reason = f"check failed: {exc}"
        if reason:
            reasons.append(reason)
    if not reasons:
        try:
            names = {row.get("name") for row in
                     await models_api.build_catalog(RUNTIME_PATH, harness=harness)}
        except Exception:  # noqa: BLE001 — a listing failure is not a refusal
            names = set()
        if names and model not in names:
            reasons.append(f"{harness} does not offer {model} to this mind")
    return "; ".join(reasons) or None


def harness_report() -> dict:
    """Every harness, whether it can be offered, and if not, why not."""
    rows = []
    for name in runtime_api.HARNESSES:
        if name not in ADAPTERS:
            reason = LOAD_FAILURES.get(name, "no adapter")
            rows.append({"name": name, "available": False,
                         "reason": f"adapter failed to load: {reason}"})
            continue
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
    fetcher = next(iter(ADAPTERS.values()), None)
    if fetcher is not None:
        await fetcher._fetch_secrets_on_startup()
    # After the shared fetch, never before: this mind's own GitHub token wins
    # over the hive's. Never fatal, like every other thing on this path.
    try:
        github_token_api.adopt_stored_token()
    except Exception:  # noqa: BLE001
        log.exception("could not adopt this mind's own GitHub token")
    asyncio.ensure_future(runtime_api.registration_loop(
        RUNTIME_PATH, mind_name=MIND_NAME, mind_id=MIND_ID, log=log
    ))
    # The skills render pass, once per start: every harness finds its copies
    # current before the first conversation. Off the loop and never fatal — a
    # render that falls over costs stale skills, not a mind that will not boot.
    check_at_start = getattr(skills_api, "check_at_start", None)
    if check_at_start is not None:
        try:
            await asyncio.to_thread(check_at_start)
        except Exception:  # noqa: BLE001
            log.exception("The skills render pass failed at start")
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
    model = str(body.get("model") or "").strip()
    if harness in PREFLIGHT_HARNESSES:
        # Answered now, while the gateway can still undo a switch: a codex or
        # dsh spawn holds nothing until the first turn, and a 200 here would
        # report a session that is going to fail on its first word.
        refusal = await preflight(harness, model)
        if refusal:
            log_event(log, "session.refused", level=logging.WARNING, mind_id=MIND_ID,
                      session_id=sid or None, harness=harness, reason=refusal)
            return JSONResponse({"error": refusal}, status_code=503)
    previous = HARNESS_OF.get(sid)
    if sid and previous and previous != harness:
        # The gateway kills before it respawns on a switch; a session still
        # held by its old adapter here is one whose kill never arrived, and
        # leaving it would put two harnesses on one conversation.
        await ADAPTERS[previous].kill_session(sid)
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
    for name, adapter in ADAPTERS.items():
        if name == "codex":
            await adapter.kill_session(sid, forget_thread=forget_thread)
        else:
            await adapter.kill_session(sid)
    return {"session_id": sid, "status": "closed"}


async def release_idle_chat(sid: str) -> str | None:
    """End an idle chat process before a terminal opens on the conversation.

    One live harness process per conversation: a pane and a chat process on
    one transcript is two writers. A turn in flight is never torn down for a
    pane — the reason returned refuses the attach, and the turn goes on. The
    handover an idle chat process was holding needs no moving: the pane takes
    it from comms' carry-forward, which outlives both.
    """
    for adapter in ADAPTERS.values():
        state = adapter.SESSIONS.get(sid)
        if state is not None and state.get("in_flight"):
            return "a chat turn is still running for this conversation"
    for adapter in ADAPTERS.values():
        if sid in adapter.SESSIONS:
            await adapter.release_session(sid, "stream")
            HARNESS_OF.pop(sid, None)
    return None


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

    A conversation with no transcript on disk and no turns behind it
    (``had_turns``) is empty: it hands over as the summary alone, or nothing,
    so a conversation is switchable before its first turn. A transcript
    missing after turns were had, or one that exists and cannot be read, is
    unreadable: it hands over the summary alone, and with no summary either
    the switch would lose the conversation it was moving, so it is refused
    and the old harness keeps running.
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
    prior = str(body.get("prior_handover") or "")
    try:
        budget = int(body.get("budget_bytes") or transcript.MAX_HANDOVER_BYTES)
    except (TypeError, ValueError):
        budget = transcript.MAX_HANDOVER_BYTES
    had_turns = bool(body.get("had_turns"))

    def read() -> list[dict]:
        # Off the event loop: a rollout search walks CODEX_HOME and a dsh log
        # is decompressed, and this loop serves every session on the mind.
        path = adapter.transcript_path(str(body.get("claude_sid") or ""),
                                       str(body.get("harness_sid") or "") or None,
                                       session_id=str(body.get("session_id") or ""))
        if path is None:
            if had_turns:
                raise transcript.Unreadable("no transcript on disk after turns were had")
            return []
        return transcript.READERS[harness](path)

    try:
        blocks = await asyncio.to_thread(read)
    except transcript.Unreadable as exc:
        log_event(log, "session.handover.unreadable", level=logging.WARNING,
                  mind_id=MIND_ID, harness=harness, error=str(exc))
        if not summary.strip() and not prior.strip():
            return JSONResponse({"detail": "unreadable"}, status_code=422)
        blocks = []
    text = transcript.render(blocks, summary=summary, budget_bytes=budget,
                             prior_handover=prior)
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
    before_attach=release_idle_chat,
)
runtime_api.install_session_guard(app, mind_dir=MIND_DIR)
runtime_api.install_runtime_routes(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
models_api.install_models_route(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
surface_token_api.install_surface_token_routes(app, mind_id=MIND_ID, log=log)
github_token_api.install_github_token_routes(app, mind_id=MIND_ID, log=log)


@app.post("/skills/check")
async def skills_check(request: Request) -> Any:
    """Run the skills render pass: merge in-place edits, render everywhere.

    The gateway calls it before a switch so the incoming harness finds the
    mind's skills and agents already in its own form. Refused, never faked,
    where the render pass is not installed: a 200 that rendered nothing would
    let a switch go ahead on stale copies with every surface reporting fine.
    """
    denied = runtime_api.authorize_admin(request)
    if denied is not None:
        return denied
    check_all = getattr(skills_api, "check_all", None)
    if check_all is None:
        return JSONResponse({"error": "this mind has no skills render pass"},
                            status_code=501)
    try:
        return await asyncio.to_thread(check_all)
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _pages_app(harness: str) -> FastAPI:
    """The skills and files pages for one harness's directories."""
    pages = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    skills_api.install_skills_routes(pages, harness=f"{harness}_cli", mind_id=MIND_ID, log=log)
    files_api.install_files_routes(pages, harness=f"{harness}_cli", mind_id=MIND_ID, log=log)
    return pages


#: The skills and files pages, one set per harness: every harness reads its
#: own skills and hooks directories, and a request names whose (``?harness=``),
#: the default harness's when it names none.
PAGES = {name: _pages_app(name) for name in ADAPTERS}


class _PagesByHarness:
    """Send `/skills` and `/files` requests to the named harness's pages."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if (path == "/skills" or path.startswith(("/skills/", "/files/"))) \
                and path != "/skills/check":
            query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
            name = runtime_api.harness_name((query.get("harness") or [""])[0]) \
                or default_harness()
            pages = PAGES.get(name)
            if pages is None:
                response = JSONResponse({"error": f"no {name} pages on this mind"},
                                        status_code=404)
                await response(scope, receive, send)
                return
            await pages(scope, receive, send)
            return
        await self.inner(scope, receive, send)


app.add_middleware(_PagesByHarness)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info",
                log_config=None, access_log=False)


if __name__ == "__main__":
    main()
