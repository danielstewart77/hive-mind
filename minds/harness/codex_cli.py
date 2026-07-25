"""Codex CLI harness — in-container FastAPI service for one mind.

Runs as the sole process inside the mind's container. The mind is selected
by the ``MIND_NAME`` env var (set in the mind's ``container/compose.yaml``);
everything mind-specific comes from ``minds/<MIND_NAME>/runtime.yaml``.

The Codex CLI runs one subprocess per turn. The session table holds
metadata only (system prompt + last thread_id). On the first turn the
system prompt is folded into stdin; subsequent turns use
``codex exec resume <thread_id>``. An Ollama-backed mind sets
``provider: ollama`` in ``runtime.yaml`` (base URL from its ``env`` block).

The system prompt is composed by hive-comms and shipped as
``system_prompt_blocks`` in the spawn payload — this module composes
nothing locally.
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

from minds.harness.empty_turn_diagnostic import compose_empty_turn_diagnostic
from minds.proactive import make_proactive_router
from minds.pty_attach import PtyUnavailable, install_pty_attach, open_pty_process
from minds.pty_attach import teardown as teardown_pty

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

MIND_NAME = os.environ.get("MIND_NAME", "example")
MINDS_ROOT = Path(__file__).resolve().parent.parent
MIND_DIR = MINDS_ROOT / MIND_NAME
PROJECT_DIR = Path("/usr/src/app")

log = logging.getLogger(f"hive-mind.minds.{MIND_NAME}")

RUNTIME = yaml.safe_load((MIND_DIR / "runtime.yaml").read_text())
NAME: str = RUNTIME["name"]
MIND_ID: str = RUNTIME["mind_id"]
DEFAULT_MODEL: str = RUNTIME["default_model"]
PROVIDER: str = RUNTIME["provider"]
RUNTIME_ENV: dict[str, Any] = RUNTIME.get("env", {}) or {}

NS_URL = os.environ.get("HIVE_MIND_SERVER_URL", "http://server:8420")

# CODEX_HOME is codex's canonical knob and the container already exports it;
# runtime.yaml is the fallback for a bare invocation.
CODEX_HOME = Path(
    os.environ.get("CODEX_HOME")
    or RUNTIME.get("runtime_config_dir")
    or str(MIND_DIR / ".codex")
)

app = FastAPI(title=f"Mind: {NAME}")

# session_id -> {"system_prompt": str, "thread_id": str | None, "model": str}
SESSIONS: dict[str, dict] = {}

# session_id -> codex thread id, surviving the session dict itself.
#
# Codex is the one harness that will not take a conversation id it was
# handed: `codex exec` mints its own thread and reports it on the first
# event, and there is no flag to declare one up front. The gateway's
# conversation id is therefore the *session's* identity, not the thread's,
# and the mapping between them can only live here. Keeping it outside
# SESSIONS means a respawn of the same session (idle eviction, a gateway
# restart) rejoins its thread instead of silently starting a second one.
THREADS: dict[str, str] = {}

# Proactive delivery endpoint. Codex minds run one subprocess per turn
# (stdin closed, no idle stream), so there is no unsolicited output to drain —
# the buffer stays empty and GET /proactive returns []. The endpoint is still
# mounted so this mind's bot can poll it uniformly with the Claude-CLI minds.
PROACTIVE_BUFFER: list[dict] = []
PROACTIVE_TOKEN = os.environ.get("COMMS_BEARER_TOKEN") or None
app.include_router(make_proactive_router(PROACTIVE_BUFFER, PROACTIVE_TOKEN))


def _setup_codex_home() -> None:
    try:
        CODEX_HOME.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Off-container (test collection on the host) the container-absolute
        # path has no parent to create. Codex would fail loudly on spawn; a
        # missing directory at import time is not itself the failure.
        log.warning("Could not create CODEX_HOME at %s", CODEX_HOME)
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
    args = [
        "-c",
        f'model_provider="{provider_key}"',
        "-c",
        f'model_providers.{provider_key}.name="{NAME.capitalize()} Ollama"',
        "-c",
        f'model_providers.{provider_key}.base_url="{base_url}"',
    ]
    if RUNTIME_ENV.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        # The "ollama" endpoint may really be a metering proxy in front of
        # Ollama, which authenticates by bearer key. Codex only sends one when
        # the provider declares env_key; bare Ollama needs no auth, so the
        # declaration is gated on a key actually being present.
        args += ["-c", f'model_providers.{provider_key}.env_key="OPENAI_API_KEY"']
    return args


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
    # The gateway's conversation id. Codex cannot adopt it — see THREADS —
    # so it is never passed to the CLI; this session's thread is whatever
    # codex minted for it, if it has spoken at all.
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
            "thread_id": THREADS.get(sid),
            "model": model,
            "client_ref": client_ref,
            "owner_type": owner_type,
            "owner_ref": owner_ref,
        }
        log.info("%s session %s initialised (model=%s conversation=%s thread=%s)",
                 NAME, sid, model, resume_sid or "none", THREADS.get(sid) or "new")
        return {"session_id": sid, "mind_id": MIND_ID, "name": NAME, "status": "running", "model": model}
    except Exception as exc:
        log.exception("Failed to create session for %s", NAME)
        return JSONResponse({"error": str(exc)}, status_code=500)


def _spawn_pty(
    *, session_id: str, model: str, conversation_id: str, cols: int, rows: int
) -> tuple[Any, int]:
    """Put an interactive `codex` TUI on this session's thread under a pty.

    ``conversation_id`` is the gateway's and means nothing to codex, so the
    thread comes from THREADS. A session that has never taken a turn has no
    thread; starting one here would fork a second conversation the gateway
    knows nothing about, so the attach is refused instead.
    """
    del conversation_id  # codex mints its own ids; see THREADS
    thread_id = THREADS.get(session_id)
    if not thread_id:
        raise PtyUnavailable("this conversation has not started yet — send a message first")

    cmd = [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model", model,
        *_provider_args(),
        "resume", thread_id,
    ]
    env = os.environ.copy()
    env.update({k: str(v) for k, v in RUNTIME_ENV.items()})
    proc, master_fd = open_pty_process(
        cmd, env=env, cwd=str(PROJECT_DIR), cols=cols, rows=rows
    )
    log.info("Spawned %s pty session=%s pid=%d model=%s thread=%s",
             NAME, session_id, proc.pid, model, thread_id)
    return proc, master_fd


install_pty_attach(app, mind_name=NAME, spawn=_spawn_pty)


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
            if current_thread_id:
                THREADS[sid] = current_thread_id
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
            # turn's response — the user ends up one turn behind).
            state["thread_id"] = None
            THREADS.pop(sid, None)
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
    THREADS.pop(sid, None)
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


@app.post("/sessions/{sid}/release")
async def release_session(sid: str, surface: str) -> Any:
    """Stop one live surface while retaining Codex's resumable thread id."""
    if surface == "terminal":
        released = teardown_pty(sid)
    elif surface == "stream":
        sess = SESSIONS.pop(sid, None)
        released = sess is not None
        if sess is not None:
            await _reap_proc(sess.get("proc"))
    else:
        return JSONResponse({"error": "surface must be terminal or stream"}, status_code=400)
    return {"session_id": sid, "surface": surface, "released": released}


@app.delete("/sessions/{sid}")
async def kill_session(sid: str) -> dict:
    sess = SESSIONS.pop(sid, None)
    # The terminal is a separate process from the per-turn subprocess, so a
    # kill has to reach both or the TUI outlives its own conversation.
    teardown_pty(sid)
    THREADS.pop(sid, None)
    if sess is not None:
        await _reap_proc(sess.get("proc"))
    log.info("Killed %s session %s", NAME, sid)
    return {"session_id": sid, "status": "closed"}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
