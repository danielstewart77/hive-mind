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
import logging
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
    claude_conversation_flags,
    install_pty_attach,
    open_pty_process,
)
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

app = FastAPI(title=f"Mind: {NAME}")

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


def _spawn_pty(
    *, session_id: str, model: str, conversation_id: str, cols: int, rows: int
) -> tuple[Any, int]:
    """Put an interactive `claude` TUI under a pty for the web terminal.

    Distinct from `_spawn_proc`: no `-p`, no stream-json — those are
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

    env = os.environ.copy()
    env.update({k: str(v) for k, v in RUNTIME_ENV.items()})
    proc, master_fd = open_pty_process(
        cmd, env=env, cwd=str(PROJECT_DIR), cols=cols, rows=rows
    )
    log.info("Spawned %s pty session=%s pid=%d model=%s conversation=%s",
             NAME, session_id, proc.pid, model, conversation_id)
    return proc, master_fd


install_pty_attach(app, mind_name=NAME, spawn=_spawn_pty)


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
    log.info("%s ready (mind_id=%s, default_model=%s)", NAME, MIND_ID, DEFAULT_MODEL)


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
        return {"session_id": sid, "mind_id": MIND_ID, "name": NAME, "status": "running", "model": model}
    except Exception as exc:
        log.exception("Failed to create session for %s", NAME)
        return JSONResponse({"error": str(exc)}, status_code=500)


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
                    if event.get("type") == "result":
                        cs = event.get("session_id")
                        if cs:
                            sess["resume_sid"] = cs
                        break
                except json.JSONDecodeError:
                    continue
        finally:
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
    return {"session_id": sid, "status": "closed"}


def main() -> None:
    import uvicorn
    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
