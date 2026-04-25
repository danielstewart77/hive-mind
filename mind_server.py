"""Minimal mind container server.

Runs inside each mind's container. Manages the harness subprocess
(claude CLI, codex CLI, SDK) via the mind's implementation.py.

Does NOT contain: broker, mind registry, secret storage, HITL,
session database, or any nervous system component. Sessions are
tracked in-memory only.
"""

import asyncio
import importlib
import json
import logging
import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("mind-server")

MIND_ID = os.environ.get("MIND_ID")
if not MIND_ID:
    log.error("MIND_ID environment variable is required")
    sys.exit(1)

PROJECT_DIR = Path(__file__).resolve().parent

# Load this mind's implementation
try:
    impl = importlib.import_module(f"minds.{MIND_ID}.implementation")
    log.info("Loaded implementation for mind: %s", MIND_ID)
except ImportError:
    log.error("No implementation found for mind: %s", MIND_ID)
    sys.exit(1)

app = FastAPI(title=f"Mind Server: {MIND_ID}")

# In-memory session tracking — no database
_sessions: dict[str, dict] = {}  # session_id -> {"proc": process, "model": str, ...}

# NS gateway URL — for secrets API calls
_NS_URL = os.environ.get("HIVE_MIND_SERVER_URL", "http://server:8420")

# Per-mind config directory (tmpfs, writable)
_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/home/hivemind/.claude"))

# Host credentials (read-only mount from host's ~/.claude)
_HOST_CREDS = Path("/mnt/host-claude/.credentials.json")

# Mind-specific skills source (from minds/<name>/.claude/ in the project)
_MIND_SKILLS_SRC = PROJECT_DIR / "minds" / MIND_ID / ".claude"


def _setup_config_dir():
    """Set up the per-mind CLAUDE_CONFIG_DIR.

    The config dir is bind-mounted from minds/<name>/.claude/ (read-write).
    Skills and agents are already there. We only need to copy the host's
    auth credentials into it.
    """
    import shutil

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(_CONFIG_DIR)

    # Copy host credentials for auth
    target_creds = _CONFIG_DIR / ".credentials.json"
    if _HOST_CREDS.exists():
        if not target_creds.exists() or _HOST_CREDS.stat().st_mtime > target_creds.stat().st_mtime:
            shutil.copy2(str(_HOST_CREDS), str(target_creds))
            target_creds.chmod(0o600)
            log.info("Copied credentials to %s", target_creds)
    else:
        log.warning("Host credentials not found at %s — mind will need manual auth", _HOST_CREDS)

    # Log what skills are available
    skills_dir = _CONFIG_DIR / "skills"
    if skills_dir.exists():
        skill_count = len([d for d in skills_dir.iterdir() if d.is_dir()])
        log.info("Mind %s has %d skills available", MIND_ID, skill_count)
    else:
        log.warning("No skills directory found at %s", skills_dir)


# Run config setup immediately (before FastAPI starts, before harness spawns)
_setup_config_dir()


async def _fetch_secrets_on_startup():
    """Fetch all scoped secrets from the NS and inject into environment.

    Queries the NS for which secrets this mind is allowed to access,
    then fetches each one. No hardcoded secret list — the scoping
    policy in the NS determines what's available.
    """
    import aiohttp

    # Map secret key names to environment variable names
    _ENV_MAP = {
        "gh_oauth_token": "GH_TOKEN",
        "mcp_auth_token": "MCP_AUTH_TOKEN",
    }

    try:
        async with aiohttp.ClientSession() as http:
            # 1. Get the list of secrets this mind is scoped for
            async with http.get(
                f"{_NS_URL}/secrets/scopes/{MIND_ID}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    log.debug("Could not fetch secret scopes (status=%d)", resp.status)
                    return
                scopes = await resp.json()
                secret_keys = scopes.get("secret_keys", [])

            if not secret_keys:
                log.debug("No secrets scoped for mind %s", MIND_ID)
                return

            # 2. Fetch each scoped secret
            for key in secret_keys:
                try:
                    async with http.get(
                        f"{_NS_URL}/secrets/{key}",
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

            log.info("Loaded %d secrets for mind %s", len(secret_keys), MIND_ID)
    except Exception:
        log.debug("Could not connect to NS for secrets (NS may not be ready yet)")


@app.on_event("startup")
async def startup_secrets():
    await _fetch_secrets_on_startup()


@app.get("/health")
async def health():
    return {"mind_id": MIND_ID, "status": "ok", "sessions": len(_sessions)}


@app.get("/sessions")
async def list_sessions():
    return [
        {"id": sid, "mind_id": MIND_ID, "model": s.get("model", "unknown"), "status": "running"}
        for sid, s in _sessions.items()
    ]


@app.post("/sessions")
async def create_session(request: Request):
    body = await request.json()
    session_id = body.get("session_id", str(uuid4()))
    model = body.get("model", "sonnet")
    resume_sid = body.get("resume_sid")
    surface_prompt = body.get("surface_prompt")
    autopilot = body.get("autopilot", False)
    allowed_directories = body.get("allowed_directories")
    prompt_files = body.get("prompt_files")

    try:
        # Import config and registry from the project (read-only mount)
        build_base_prompt = None
        registry = None
        config_obj = None
        mcp_config = ""

        try:
            from core.sessions import _build_base_prompt
            build_base_prompt = _build_base_prompt
        except ImportError:
            pass

        try:
            from config import config as _config
            config_obj = _config
            registry = _config.model_registry if hasattr(_config, 'model_registry') else None
            if registry is None:
                from core.models import ModelRegistry, Provider
                providers = {}
                for pname, pdata in _config.providers.items():
                    env_overrides = pdata.get("env", {}) if isinstance(pdata, dict) else {}
                    providers[pname] = Provider(name=pname, env_overrides=env_overrides)
                registry = ModelRegistry(providers=providers, static_models=_config.models)
            mcp_config_path = PROJECT_DIR / ".mcp.container.json"
            if not mcp_config_path.exists():
                mcp_config_path = PROJECT_DIR / ".mcp.json"
            mcp_config = str(mcp_config_path) if mcp_config_path.exists() else ""
        except ImportError:
            pass

        proc = await impl.spawn(
            session_id=session_id,
            model=model,
            autopilot=autopilot,
            resume_sid=resume_sid,
            surface_prompt=surface_prompt,
            allowed_directories=allowed_directories,
            mind_id=MIND_ID,
            build_base_prompt=build_base_prompt,
            mcp_config=mcp_config,
            registry=registry,
            config_obj=config_obj,
            prompt_files=prompt_files,
        )

        _sessions[session_id] = {
            "proc": proc,
            "model": model,
            "resume_sid": resume_sid,
        }

        log.info("Session %s created for %s (model=%s)", session_id, MIND_ID, model)
        return {"session_id": session_id, "mind_id": MIND_ID, "status": "running", "model": model}

    except Exception as exc:
        log.exception("Failed to create session for %s", MIND_ID)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/sessions/{session_id}/message")
async def send_message(session_id: str, request: Request):
    body = await request.json()
    content = body.get("content", "")

    session = _sessions.get(session_id)
    if not session:
        return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)

    proc = session["proc"]

    # Check if implementation has a custom send function (SDK/Codex minds)
    if hasattr(impl, "send"):
        # SDK implementations (Bilby) require the async generator to be consumed
        # in the same task that created it (anyio cancel scope constraint).
        # Collect all events first, then stream them out.
        collected_events = []
        async for event in impl.send(session_id, content, db=None):
            collected_events.append(event)
            # Capture claude_sid for session resumption
            if event.get("type") == "result" and event.get("session_id"):
                session["resume_sid"] = event["session_id"]

        async def stream_collected():
            for event in collected_events:
                yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(stream_collected(), media_type="text/event-stream")

    # CLI-based minds: write to stdin, read from stdout
    if not proc or not proc.stdin or proc.returncode is not None:
        return JSONResponse({"error": "Process not running"}, status_code=500)

    # Send the message as a stream-json input
    msg = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": content}]},
    })
    proc.stdin.write(msg.encode() + b"\n")
    await proc.stdin.drain()

    async def stream_response():
        async for line in proc.stdout:
            decoded = line.decode().strip()
            if not decoded:
                continue
            yield f"data: {decoded}\n\n"
            try:
                event = json.loads(decoded)
                if event.get("type") == "result":
                    # Capture claude_sid for session resumption
                    if event.get("session_id"):
                        session["resume_sid"] = event["session_id"]
                    break
            except json.JSONDecodeError:
                continue

    return StreamingResponse(stream_response(), media_type="text/event-stream")


@app.post("/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    session = _sessions.get(session_id)
    if not session:
        return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)

    proc = session.get("proc")
    if proc is None or (hasattr(proc, "returncode") and proc.returncode is not None):
        return {"ok": True, "session_id": session_id, "message": "nothing_running"}

    proc.send_signal(signal.SIGINT)
    log.info("Sent SIGINT to session %s", session_id)
    return {"ok": True, "session_id": session_id}


@app.delete("/sessions/{session_id}")
async def kill_session(session_id: str):
    session = _sessions.pop(session_id, None)
    if not session:
        return JSONResponse({"error": f"Session {session_id} not found"}, status_code=404)

    proc = session.get("proc")
    try:
        if hasattr(impl, "kill"):
            # SDK/Codex minds have a custom kill that takes session_id
            if "session_id" in impl.kill.__code__.co_varnames:
                await impl.kill(session_id)
            else:
                await impl.kill(proc)
        elif proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    except Exception:
        log.exception("Error killing session %s", session_id)

    log.info("Session %s killed for %s", session_id, MIND_ID)
    return {"session_id": session_id, "status": "closed"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
