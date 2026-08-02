"""Claude CLI harness — in-container FastAPI service for one mind.

Runs as the sole process inside the mind's container. The mind is selected
by the ``MIND_NAME`` env var (set in the mind's ``container/compose.yaml``);
everything mind-specific comes from ``minds/<MIND_NAME>/runtime.yaml``.
Owns the Claude CLI subprocesses for the mind's sessions. An Ollama-backed
mind is the same harness with ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``
set in ``runtime.yaml``'s ``env`` block.

The system prompt is composed by hive-comms (soul, standing rules,
decay-weighted recent memory, session-memory carry-forward) and shipped as
``system_prompt_blocks`` in the spawn payload — this module composes nothing
locally.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from minds.proactive import idle_drain, make_proactive_router
from minds.pty_attach import (
    TmuxTerminals,
    claude_conversation_flags,
    ensure_tui_first_run_flags,
    install_pty_attach,
    mirror_turn,
    seeded_pane_command,
)
from minds.pty_attach import teardown as teardown_pty
from minds import runtime_api, skills_api
from core.hive_logging import configure_logging, install_fastapi_logging, log_event

MIND_NAME = os.environ.get("MIND_NAME", "example")
MINDS_ROOT = Path(__file__).resolve().parent.parent
MIND_DIR = MINDS_ROOT / MIND_NAME
PROJECT_DIR = Path("/usr/src/app")

log = configure_logging(f"hive-mind.minds.{MIND_NAME}")

RUNTIME_PATH = MIND_DIR / "runtime.yaml"
RUNTIME = yaml.safe_load(RUNTIME_PATH.read_text())
NAME: str = RUNTIME["name"]
MIND_ID: str = RUNTIME["mind_id"]
PROVIDER: str = RUNTIME["provider"]
RUNTIME_ENV: dict[str, Any] = RUNTIME.get("env", {}) or {}

NS_URL = os.environ.get("HIVE_MIND_SERVER_URL", "http://server:8420")

CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/home/hivemind/.claude"))
HOST_CREDS = Path("/mnt/host-claude/.credentials.json")
SKIP_HOST_CREDENTIALS = os.environ.get("SKIP_HOST_CREDENTIALS", "").lower() in {
    "1", "true", "yes", "on",
}

_MCP_CONTAINER = PROJECT_DIR / ".mcp.container.json"
_MCP_DEFAULT = PROJECT_DIR / ".mcp.json"
# Empty string -> the --mcp-config flag is omitted from the spawn cmd.
# Both .mcp.* files are deployment-local (gitignored); a missing file is a
# valid configuration (no MCP tools wired in), not a fatal error.
if _MCP_CONTAINER.exists():
    MCP_CONFIG = str(_MCP_CONTAINER)
elif _MCP_DEFAULT.exists():
    MCP_CONFIG = str(_MCP_DEFAULT)
else:
    MCP_CONFIG = ""

app = FastAPI(title=f"Mind: {NAME}", docs_url=None, redoc_url=None, openapi_url=None)
install_fastapi_logging(app, log, f"mind:{NAME}")

# session_id -> {"proc": Process, "model": str, "resume_sid": str | None}
SESSIONS: dict[str, dict] = {}

# Proactive (unsolicited) delivery. The idle drain appends {"chat_id", "text"}
# items here while no request is in flight; this mind's Telegram bot polls
# GET /proactive to drain and deliver them. See minds/proactive.py.
PROACTIVE_BUFFER: list[dict] = []
# Shared bearer for the /proactive endpoint — the same token the backend
# already trusts on the internal docker network.
PROACTIVE_TOKEN = os.environ.get("COMMS_BEARER_TOKEN") or None
app.include_router(make_proactive_router(PROACTIVE_BUFFER, PROACTIVE_TOKEN))


# ---------------------------------------------------------------------------
# Setup — config dir + host credential sync
# ---------------------------------------------------------------------------

def _sync_mind_config_assets() -> None:
    """Copy safe mind-local Claude config assets into CONFIG_DIR."""
    src = MIND_DIR / ".claude"
    if not src.exists():
        return
    try:
        if CONFIG_DIR.resolve() == src.resolve():
            log.info("Skipping mind config sync: CLAUDE_CONFIG_DIR (%s) is the source itself", CONFIG_DIR)
            return
    except (OSError, RuntimeError):
        pass
    allowed = {".claude.json", "agents", "hooks", "projects", "settings.json", "skills"}
    for child in src.iterdir():
        if child.name not in allowed:
            continue
        target = CONFIG_DIR / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True, ignore_dangling_symlinks=True)
        else:
            shutil.copy2(child, target)


def _setup_config_dir() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Off-container (test collection on the host) the container-absolute
        # path has no parent to create. The CLI would fail loudly on spawn; a
        # missing directory at import time is not itself the failure.
        log.warning("Could not create CLAUDE_CONFIG_DIR at %s", CONFIG_DIR)
        return
    os.environ["CLAUDE_CONFIG_DIR"] = str(CONFIG_DIR)
    _sync_mind_config_assets()
    target_creds = CONFIG_DIR / ".credentials.json"
    if SKIP_HOST_CREDENTIALS:
        log.info("Skipping host credential sync for %s", NAME)
    elif HOST_CREDS.exists():
        if not target_creds.exists() or HOST_CREDS.stat().st_mtime > target_creds.stat().st_mtime:
            shutil.copy2(str(HOST_CREDS), str(target_creds))
            target_creds.chmod(0o600)
            log.info("Copied credentials to %s", target_creds)
    else:
        log.warning("Host credentials not found at %s — mind will need manual auth", HOST_CREDS)
    skills_dir = CONFIG_DIR / "skills"
    if skills_dir.exists():
        skill_count = len([d for d in skills_dir.iterdir() if d.is_dir()])
        log.info("Mind %s has %d skills available", NAME, skill_count)


_setup_config_dir()


# ---------------------------------------------------------------------------
# Secrets fetch on startup
# ---------------------------------------------------------------------------

async def _fetch_secrets_on_startup() -> None:
    """Fetch all scoped secrets from the NS and inject into environment."""
    _ENV_MAP = {"gh_oauth_token": "GH_TOKEN", "mcp_auth_token": "MCP_AUTH_TOKEN"}
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(
                f"{NS_URL}/secrets/scopes/{NAME}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    log.debug("Could not fetch secret scopes (status=%d)", resp.status)
                    return
                scopes = await resp.json()
                secret_keys = scopes.get("secret_keys", []) or []
            if not secret_keys:
                log.debug("No secrets scoped for mind %s", NAME)
                return
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
            log.info("Loaded %d secrets for mind %s", len(secret_keys), NAME)
    except Exception:
        log.debug("Could not connect to NS for secrets (NS may not be ready yet)")


# ---------------------------------------------------------------------------
# Harness — Claude CLI spawn / kill
# ---------------------------------------------------------------------------

async def _spawn_proc(
    session_id: str,
    *,
    model: str,
    autopilot: bool,
    resume_sid: str | None,
    surface_prompt: str | None,
    allowed_directories: list[str] | None,
    is_group_session: bool,
    owner_type: str | None = None,
    system_prompt_blocks: str | None = None,
    client_ref: str = "",
    owner_ref: str = "",
) -> asyncio.subprocess.Process:
    blocks = system_prompt_blocks or ""
    if blocks and surface_prompt:
        full_prompt = f"{blocks}\n\n{surface_prompt}"
    elif surface_prompt:
        full_prompt = surface_prompt
    else:
        full_prompt = blocks

    cmd = [
        "claude", "-p",
        "--verbose",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--model", model,
        "--append-system-prompt", full_prompt,
    ]
    if MCP_CONFIG:
        cmd.extend(["--mcp-config", MCP_CONFIG])
    for d in allowed_directories or []:
        cmd.extend(["--allowedDirectory", d])
    if resume_sid:
        cmd.extend(claude_conversation_flags(resume_sid, PROJECT_DIR))

    env = os.environ.copy()
    env.update({k: str(v) for k, v in RUNTIME_ENV.items()})
    if is_group_session:
        env["HIVEMIND_GROUP_SESSION"] = "1"
    if owner_type == "scheduler":
        env["HIVEMIND_SCHEDULED_TASK"] = "1"
    # Per-spawn metadata for the rotation_check Stop hook. Empty values
    # stay unset so the hook can detect the no-op case instead of
    # writing under a fake key.
    if client_ref:
        env["HIVEMIND_CLIENT_REF"] = client_ref
    if owner_type:
        env["HIVEMIND_OWNER_TYPE"] = owner_type
    if owner_ref:
        env["HIVEMIND_OWNER_REF"] = owner_ref

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
        env=env,
        cwd=str(PROJECT_DIR),
    )
    asyncio.create_task(_drain_stderr(proc, session_id))
    log.info(
        "Spawned %s session=%s pid=%d model=%s resume=%s base_url=%s",
        NAME, session_id, proc.pid, model, resume_sid or "new",
        env.get("ANTHROPIC_BASE_URL", "anthropic"),
    )
    return proc


TERMINALS = TmuxTerminals(NAME, PROJECT_DIR)


def _terminal_argv(model: str, conversation_id: str) -> list[str]:
    """The interactive `claude` that runs inside the tmux pane.

    Distinct from `_spawn_proc`'s argv: no `-p`, no stream-json — those are
    print-mode flags and disable the TUI (slash commands, tab completion,
    Ctrl+C). Resuming needs no `--append-system-prompt`; that was set on the
    conversation's first turn. Both processes append to the same conversation
    file; only one writes at a time.
    """
    cmd = [
        "claude",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    if MCP_CONFIG:
        cmd.extend(["--mcp-config", MCP_CONFIG])
    cmd.extend(claude_conversation_flags(conversation_id, PROJECT_DIR))
    return cmd


def _rotation_argv(model: str, new_claude_sid: str) -> list[str]:
    """The interactive `claude` a rotation respawns the pane onto.

    Always `--session-id`, never `--resume`: rotation starts a fresh harness
    conversation under the same hive session, and it opens holding a summary
    of the one it replaced. The summary itself rides in via
    ``seeded_pane_command``.
    """
    cmd = [
        "claude",
        "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions",
        "--model", model,
    ]
    if MCP_CONFIG:
        cmd.extend(["--mcp-config", MCP_CONFIG])
    cmd.extend(["--session-id", new_claude_sid])
    return cmd


def _pane_env(
    client_ref: str | None, owner_type: str | None, owner_ref: str | None
) -> dict[str, str]:
    """Environment the pane needs that the tmux server can't have inherited.

    The tmux server was started before this conversation existed, so the
    per-session values ride on `-e` per pane. Without them the Stop hook's
    rotation check finds no client ref and bails on every fire, so a terminal
    conversation never rotates and just grows until Claude's own native
    compaction is the only thing left to intervene.
    """
    env = {k: str(v) for k, v in RUNTIME_ENV.items()}
    # A tile is one session on one conversation; the harness's agent view is
    # a second, conflicting session picker inside it, re-hosting the
    # conversation in a nested pty at a geometry nobody asked for.
    env["CLAUDE_CODE_DISABLE_AGENT_VIEW"] = "1"
    # A pty spawn is the web terminal by definition — no gateway derivation
    # needed. Per-turn hooks read this to tell the model which surface a turn
    # arrived on.
    env["HIVE_SURFACE"] = "terminal"
    if client_ref:
        env["HIVEMIND_CLIENT_REF"] = client_ref
    if owner_type:
        env["HIVEMIND_OWNER_TYPE"] = owner_type
    if owner_ref:
        env["HIVEMIND_OWNER_REF"] = owner_ref
    return env


def _spawn_pty(
    *, session_id: str, model: str, conversation_id: str, cols: int, rows: int,
    harness_sid: str | None = None, client_ref: str | None = None,
    owner_type: str | None = None, owner_ref: str | None = None,
    system_prompt: str = "",
) -> tuple[Any, int]:
    """Attach a pty to this session's interactive `claude`, starting it if needed.

    The TUI itself lives in a tmux session named for the hive session and
    outlives every viewer; what this returns is a tmux *client* running in a
    pty of the caller's geometry. Calling it again for a session that already
    has a terminal attaches a second client to the same `claude` rather than
    starting a rival one.

    ``conversation_id`` is the gateway's, never the harness's — the mind does
    not mint conversation ids, so a terminal attach is never "new": it either
    resumes a transcript that exists or pins the harness to the id this
    conversation will have from its first word.
    """
    del harness_sid  # claude adopts the gateway's id; nothing else to track
    ensure_tui_first_run_flags(CONFIG_DIR, str(PROJECT_DIR))
    pane_env = _pane_env(client_ref, owner_type, owner_ref)
    # ``system_prompt`` is a carry-forward comms is still holding: a rotation
    # seeded this conversation and no turn ever landed on it, so the seed has
    # to be applied again or the context the rotation composed is gone.
    # ``start`` no-ops on a live terminal, so a reattach never re-seeds.
    TERMINALS.start(
        session_id,
        seeded_pane_command(
            _terminal_argv(model, conversation_id),
            system_prompt,
            CONFIG_DIR / "rotation-seeds" / f"{conversation_id}.txt",
            seed_flag="--append-system-prompt",
        ),
        env_overrides=pane_env, cols=cols, rows=rows,
    )
    proc, master_fd = TERMINALS.attach(
        session_id, env_overrides=pane_env, cols=cols, rows=rows,
    )
    log.info("Attached %s terminal session=%s pid=%d model=%s conversation=%s",
             NAME, session_id, proc.pid, model, conversation_id)
    log_event(log, "session.pty.spawned", mind_id=MIND_ID, mind_name=NAME,
              session_id=session_id, process_id=proc.pid, model=model,
              conversation_id=conversation_id)
    return proc, master_fd


def _rotate_pty(
    *, session_id: str, new_claude_sid: str, model: str = "", system_prompt: str = "",
    client_ref: str | None = None, owner_type: str | None = None,
    owner_ref: str | None = None,
) -> bool:
    """Start a fresh harness conversation in a live terminal, in place.

    A rotation replaces the *conversation*, not the session and not the
    terminal. The hive session id is permanent — it is what every surface,
    label and ledger row is keyed to — so nothing here renames anything. The
    pane's process is respawned onto a new conversation seeded with the
    carry-forward, and the attached tmux client (and therefore the pty, the
    websocket and the browser tile above it) is never disturbed.
    """
    if not TERMINALS.alive(session_id):
        log.info("No live terminal for session %s — nothing to rotate in place",
                 session_id)
        return False

    if not model:
        log.warning("Refusing to rotate session %s: no model to carry over",
                    session_id)
        return False

    argv = seeded_pane_command(
        _rotation_argv(model, new_claude_sid),
        system_prompt,
        CONFIG_DIR / "rotation-seeds" / f"{new_claude_sid}.txt",
        seed_flag="--append-system-prompt",
    )
    TERMINALS.respawn(
        session_id, argv,
        env_overrides=_pane_env(client_ref, owner_type, owner_ref),
    )
    log.info("Rotated the conversation in terminal %s onto %s (seed=%d chars)",
             TERMINALS.session_name(session_id), new_claude_sid, len(system_prompt))
    log_event(log, "session.pty.rotated", mind_id=MIND_ID, mind_name=NAME,
              session_id=session_id, conversation_id=new_claude_sid)
    return True


install_pty_attach(app, mind_name=NAME, terminals=TERMINALS,
                   spawn=_spawn_pty, rotate=_rotate_pty)
runtime_api.install_runtime_routes(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
skills_api.install_skills_routes(app, harness="claude_cli", mind_id=MIND_ID, log=log)


async def _drain_stderr(proc: asyncio.subprocess.Process, session_id: str) -> None:
    if proc.stderr is None:
        return
    async for line in proc.stderr:
        text = line.decode().strip()
        if text:
            log.warning("subprocess stderr: session=%s line=%s", session_id, text[:200])


async def _kill_proc(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    await _fetch_secrets_on_startup()
    asyncio.ensure_future(runtime_api.registration_loop(
        RUNTIME_PATH, mind_name=MIND_NAME, mind_id=MIND_ID, log=log
    ))
    log.info("%s ready (mind_id=%s)", NAME, MIND_ID)


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
    # No default. A spawn that arrives without a model has already lost the
    # one the gateway resolved from this mind's broker row, and quietly
    # substituting a house favourite is how that goes unnoticed for weeks.
    model = str(body.get("model") or "").strip()
    if not model:
        return JSONResponse(
            {"error": "model required — the gateway resolves it per session"},
            status_code=400,
        )
    resume_sid = body.get("resume_sid")
    surface_prompt = body.get("surface_prompt")
    allowed_directories = body.get("allowed_directories")
    autopilot = bool(body.get("autopilot", False))
    is_group_session = bool(body.get("is_group_session", False))
    owner_type = body.get("owner_type")
    system_prompt_blocks = body.get("system_prompt_blocks")
    client_ref = body.get("client_ref") or ""
    owner_ref = body.get("owner_ref") or ""
    try:
        proc = await _spawn_proc(
            sid,
            model=model,
            autopilot=autopilot,
            resume_sid=resume_sid,
            surface_prompt=surface_prompt,
            allowed_directories=allowed_directories,
            is_group_session=is_group_session,
            owner_type=owner_type,
            system_prompt_blocks=system_prompt_blocks,
            client_ref=client_ref,
            owner_ref=owner_ref,
        )
        session = {
            "proc": proc,
            "model": model,
            "resume_sid": resume_sid,
            # client_ref is the surface's chat id (Telegram chat_id). Persist it
            # so unsolicited (proactive) turns can be routed back to the user.
            "chat_id": client_ref,
            # Serialises stdout access between the live request reader
            # (send_message) and the background idle drain.
            "stdout_lock": asyncio.Lock(),
            "in_flight": False,
        }
        SESSIONS[sid] = session
        # Start the per-session idle drain so unsolicited assistant output is
        # buffered for proactive Telegram delivery instead of stranded in the
        # subprocess stdout buffer.
        if getattr(proc, "stdout", None) is not None:
            session["drain_task"] = asyncio.ensure_future(
                idle_drain(session, PROACTIVE_BUFFER)
            )
        log.info("%s session %s initialised (model=%s resume=%s prompt_source=%s)",
                 NAME, sid, model, resume_sid or "new",
                 "comms" if system_prompt_blocks else "local")
        log_event(log, "session.created", mind_id=MIND_ID, mind_name=NAME,
                  session_id=sid, model=model, conversation_id=resume_sid or None,
                  prompt_source="comms" if system_prompt_blocks else "local")
        return {"session_id": sid, "mind_id": MIND_ID, "name": NAME, "status": "running", "model": model}
    except Exception as exc:
        log.exception("Failed to create session for %s", NAME)
        return JSONResponse({"error": str(exc)}, status_code=500)


def _assistant_texts(event: dict) -> list[str]:
    """The text of every ``text`` content block in an assistant event.

    Deterministically ignores non-assistant events (system, result,
    stream_event deltas) and non-text blocks (tool_use, tool_result).
    """
    if event.get("type") != "assistant":
        return []
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [
        block["text"] for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ]


@app.post("/sessions/{sid}/message")
async def send_message(sid: str, req: Request) -> Any:
    body = await req.json()
    content = body.get("content", "")
    sess = SESSIONS.get(sid)
    if not sess:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)

    # Turn-bleed guard. If a previous turn's stream was abandoned mid-response
    # (client disconnect, voice timeout, etc.), the underlying claude subprocess
    # kept generating, and its output is still buffered in proc.stdout. A second
    # message arriving now would write to stdin and immediately read the
    # previous turn's queued result. Refuse to start a second turn while one is
    # in flight; caller retries.
    if sess.get("in_flight"):
        return JSONResponse(
            {"error": "Turn in progress, retry shortly"},
            status_code=409,
        )

    proc: asyncio.subprocess.Process = sess["proc"]
    if not proc or not proc.stdin or proc.returncode is not None:
        return JSONResponse({"error": "Process not running"}, status_code=500)

    images = body.get("images") or []
    content_blocks: list[dict] = [{"type": "text", "text": content}]
    for img in images:
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("media_type", "image/jpeg"),
                "data": img["data"],
            },
        })
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
    })
    sess["in_flight"] = True
    proc.stdin.write(msg.encode() + b"\n")
    await proc.stdin.drain()

    async def stream() -> Any:
        stdout_lock = sess.get("stdout_lock")
        spoken: list[str] = []
        try:
            # Hold the stdout lock for the whole read so the idle drain can
            # never consume this turn's output concurrently.
            if stdout_lock is not None:
                await stdout_lock.acquire()
            async for line in proc.stdout:
                decoded = line.decode().strip()
                if not decoded:
                    continue
                yield f"data: {decoded}\n\n"
                try:
                    event = json.loads(decoded)
                    spoken.extend(_assistant_texts(event))
                    if event.get("type") == "result":
                        cs = event.get("session_id")
                        if cs:
                            sess["resume_sid"] = cs
                        break
                except json.JSONDecodeError:
                    continue
        finally:
            # A tile open on this session showed none of the above — its
            # harness process wasn't involved in the turn at all.
            mirror_turn(sid, mind_name=NAME, assistant_texts=spoken,
                        user_text=content, surface="chat")
            if stdout_lock is not None and stdout_lock.locked():
                stdout_lock.release()
            # Clear in_flight on every exit path: normal completion, generator
            # cancellation (client disconnect), or exception. Without this, an
            # abandoned stream would lock the session out of all future turns.
            sess["in_flight"] = False

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/sessions/{sid}/interrupt")
async def interrupt_session(sid: str) -> Any:
    sess = SESSIONS.get(sid)
    if not sess:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)
    proc: asyncio.subprocess.Process = sess.get("proc")
    if proc is None or proc.returncode is not None:
        return {"ok": True, "session_id": sid, "message": "nothing_running"}
    try:
        proc.send_signal(signal.SIGINT)
    except ProcessLookupError:
        pass
    log.info("Sent SIGINT to session %s", sid)
    return {"ok": True, "session_id": sid}


@app.post("/sessions/{sid}/release")
async def release_session(sid: str, surface: str) -> Any:
    """Stop one live surface without forgetting the conversation."""
    if surface == "terminal":
        released = teardown_pty(sid)
    elif surface == "stream":
        sess = SESSIONS.pop(sid, None)
        released = sess is not None
        if sess is not None:
            drain_task = sess.get("drain_task")
            if drain_task is not None and not drain_task.done():
                drain_task.cancel()
            await _kill_proc(sess.get("proc"))
    else:
        return JSONResponse({"error": "surface must be terminal or stream"}, status_code=400)
    return {"session_id": sid, "surface": surface, "released": released}


@app.delete("/sessions/{sid}")
async def kill_session(sid: str) -> dict:
    sess = SESSIONS.pop(sid, None)
    # The terminal is a separate process from the tracked subprocess, so a
    # kill has to reach both or the TUI outlives its own conversation.
    teardown_pty(sid)
    if not sess:
        return {"session_id": sid, "status": "closed"}
    # Cancel the per-session idle drain so no orphan task keeps reading stdout.
    drain_task = sess.get("drain_task")
    if drain_task is not None and not drain_task.done():
        drain_task.cancel()
    await _kill_proc(sess.get("proc"))
    log.info("Killed %s session %s", NAME, sid)
    log_event(log, "session.closed", mind_id=MIND_ID, mind_name=NAME, session_id=sid)
    return {"session_id": sid, "status": "closed"}


def main() -> None:
    import uvicorn
    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", log_config=None, access_log=False)


if __name__ == "__main__":
    main()
