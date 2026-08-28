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
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from minds.harness.empty_turn_diagnostic import compose_empty_turn_diagnostic
from minds.proactive import make_proactive_router
from minds.pty_attach import (
    TmuxTerminals,
    install_pty_attach,
    mirror_turn,
    seeded_pane_command,
)
from minds.pty_attach import teardown as teardown_pty
from minds import files_api, models_api, runtime_api, skills_api
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

# CODEX_HOME is codex's canonical knob and the container already exports it;
# runtime.yaml is the fallback for a bare invocation.
CODEX_HOME = Path(
    os.environ.get("CODEX_HOME")
    or RUNTIME.get("runtime_config_dir")
    or str(MIND_DIR / ".codex")
)

app = FastAPI(title=f"Mind: {NAME}", docs_url=None, redoc_url=None, openapi_url=None)
install_fastapi_logging(app, log, f"mind:{NAME}")

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
    # Ollama is reached through the inference proxy, never as a bare daemon —
    # the proxy owns the provider table and meters per mind, and it answers
    # 401 without a bearer key. So the token is always declared. This used to
    # be conditional on a key being visible in the runtime env or the ambient
    # environment, which made the emitted config depend on which machine
    # composed it: the same mind produced different auth on a host that
    # happened to export OPENAI_API_KEY for something else.
    return [
        "-c",
        f'model_provider="{provider_key}"',
        "-c",
        f'model_providers.{provider_key}.name="{NAME.capitalize()} Ollama"',
        "-c",
        f'model_providers.{provider_key}.base_url="{base_url}"',
        "-c",
        f'model_providers.{provider_key}.env_key="OPENAI_API_KEY"',
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
    asyncio.ensure_future(runtime_api.registration_loop(
        RUNTIME_PATH, mind_name=MIND_NAME, mind_id=MIND_ID, log=log
    ))
    log.info("%s ready (mind_id=%s, codex_home=%s)", NAME, MIND_ID, CODEX_HOME)


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
        log_event(log, "session.created", mind_id=MIND_ID, mind_name=NAME,
                  session_id=sid, model=model, conversation_id=resume_sid or None,
                  harness_thread_id=THREADS.get(sid))
        return {"session_id": sid, "mind_id": MIND_ID, "name": NAME, "status": "running", "model": model}
    except Exception as exc:
        log.exception("Failed to create session for %s", NAME)
        return JSONResponse({"error": str(exc)}, status_code=500)


_ROLLOUT_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.IGNORECASE,
)


def _existing_rollout_paths() -> set[Path]:
    sessions_dir = CODEX_HOME / "sessions"
    if not sessions_dir.exists():
        return set()
    return set(sessions_dir.rglob("*.jsonl"))


def _rollout_exists(thread_id: str) -> bool:
    """A thread id is only resumable if its rollout is on *this* CODEX_HOME.

    A thread can outlive the container that minted it — a redeploy onto a
    fresh volume, a migration to a new host — while THREADS still points at
    it. `codex resume` on a missing rollout dies within a second of the pty
    starting it, which reads identically to a hung terminal.
    """
    for path in _existing_rollout_paths():
        match = _ROLLOUT_UUID_RE.search(path.name)
        if match and match.group(1).lower() == thread_id.lower():
            return True
    return False


TERMINALS = TmuxTerminals(NAME, PROJECT_DIR)


def _report_thread(session_id: str, thread_id: str) -> None:
    """Tell hive-comms which provider-native thread belongs to this session.

    The gateway is the durable home for the mapping: THREADS lives in this
    process and dies with the container, while a browser tile reattaching
    after a redeploy is handed ``harness_sid`` from the session row.
    """
    base_url = (NS_URL or "").rstrip("/")
    if not base_url:
        return
    token = os.environ.get("COMMS_BEARER_TOKEN", "")
    request = urllib.request.Request(
        f"{base_url}/sessions/{session_id}/harness-state",
        data=json.dumps({"harness_sid": thread_id}).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"gateway returned HTTP {response.status}")
    except (OSError, urllib.error.URLError, RuntimeError) as exc:
        log.warning("Failed to report codex thread %s for session %s: %s",
                    thread_id, session_id, exc)


def _watch_for_new_thread_in_background(session_id: str, before: set[Path]) -> None:
    """Report the thread id codex mints for a bare (thread-less) terminal.

    A fresh terminal launches without `resume` — there is no thread yet, so
    there is nothing to pass, and `app-server`'s `thread/start` mints an id
    without ever writing the rollout `codex resume` needs. codex writes that
    file under CODEX_HOME/sessions on the user's first real turn; this polls
    for it (there is no JSON event stream on an interactive TUI, unlike
    `_run_codex_turn`'s `thread.started`) and, once it appears, extracts the
    thread id from its name, stores it in THREADS and reports it to the
    gateway so a later reattach resumes the real conversation instead of
    starting a second one. Gives up once the tmux session ends with nothing
    ever typed.
    """
    def _watch() -> None:
        while TERMINALS.alive(session_id):
            for path in _existing_rollout_paths() - before:
                match = _ROLLOUT_UUID_RE.search(path.name)
                if match:
                    thread_id = match.group(1)
                    THREADS[session_id] = thread_id
                    log.info(
                        "%s session %s: discovered thread %s from new rollout %s",
                        NAME, session_id, thread_id, path,
                    )
                    _report_thread(session_id, thread_id)
                    return
            time.sleep(1.0)

    threading.Thread(
        target=_watch, daemon=True, name=f"codex-thread-watch-{session_id}"
    ).start()


def _terminal_argv(model: str, thread_id: str | None) -> list[str]:
    """The interactive `codex` that runs inside the tmux pane.

    Resumes a known thread (`codex resume <id>`) when one exists and has a
    rollout on disk; a fresh terminal launches bare `codex` instead (see
    `_watch_for_new_thread_in_background`). A rotation's carry-forward rides
    in as codex's positional opening turn — this harness has no
    ``--append-system-prompt`` — but not from here; see
    ``seeded_pane_command``.
    """
    cmd = [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model", model,
        *_provider_args(),
    ]
    if thread_id:
        cmd += ["resume", thread_id]
    return cmd


def _pane_env(
    client_ref: str | None, owner_type: str | None, owner_ref: str | None
) -> dict[str, str]:
    """Environment the pane needs that the tmux server can't have inherited.

    The tmux server was started before this conversation existed, so the
    per-session values ride on `-e` per pane. Without CLIENT_REF the Stop
    hook's rotation check bails on every fire and a terminal conversation
    never rotates at all — it just grows until the harness's own compaction
    is the only thing left.
    """
    env = {k: str(v) for k, v in RUNTIME_ENV.items()}
    env["CODEX_HOME"] = str(CODEX_HOME)
    # A pty spawn is the web terminal by definition — no gateway derivation
    # needed. Per-turn hooks read this to tell the model which surface a turn
    # arrived on.
    env["HIVE_SURFACE"] = "terminal"
    if client_ref:
        env["CLIENT_REF"] = client_ref
    if owner_type:
        env["OWNER_TYPE"] = owner_type
    if owner_ref:
        env["OWNER_REF"] = owner_ref
    return env


def _resumable_thread(session_id: str, harness_sid: str | None) -> str | None:
    """This session's codex thread, if one exists and is resumable here.

    ``harness_sid`` is the gateway's copy and wins over the in-process map,
    which a container restart empties. Either can outlive its rollout — a
    redeploy onto a fresh volume, a migration to a new host — and `codex
    resume` on a missing rollout dies within a second of tmux starting it,
    which reads identically to a hung terminal. So check the disk before
    trusting either source.
    """
    if harness_sid:
        THREADS[session_id] = harness_sid
    thread_id = THREADS.get(session_id)
    if thread_id and not _rollout_exists(thread_id):
        log.warning(
            "Discarding stale thread %s for session %s — no matching rollout "
            "under %s", thread_id, session_id, CODEX_HOME,
        )
        THREADS.pop(session_id, None)
        return None
    return thread_id


def _spawn_pty(
    *, session_id: str, model: str, conversation_id: str, cols: int, rows: int,
    harness_sid: str | None = None, client_ref: str | None = None,
    owner_type: str | None = None, owner_ref: str | None = None,
    system_prompt: str = "",
) -> tuple[Any, int]:
    """Attach a pty to this session's interactive `codex`, starting it if needed.

    The TUI lives in a tmux session named for the hive session and outlives
    every viewer; what this returns is a tmux *client* running in a pty of
    the caller's geometry. Calling it again for a session that already has a
    terminal attaches a second client to the same `codex` rather than
    starting a rival one.

    ``conversation_id`` is the gateway's and means nothing to codex, which
    mints its own ids — the thread comes from ``harness_sid`` or THREADS.
    """
    del conversation_id  # codex mints its own ids; see THREADS
    thread_id = _resumable_thread(session_id, harness_sid)
    pane_env = _pane_env(client_ref, owner_type, owner_ref)

    fresh = not TERMINALS.alive(session_id) and not thread_id
    before = _existing_rollout_paths() if fresh else set()
    # ``system_prompt`` is a carry-forward comms is still holding: a rotation
    # seeded this conversation and no turn ever landed on it. Codex has no
    # system-prompt flag, so it rides in as the opening turn — the same
    # channel a rotation uses. ``start`` no-ops on a live terminal, so a
    # reattach never re-seeds.
    TERMINALS.start(
        session_id,
        seeded_pane_command(
            _terminal_argv(model, thread_id),
            system_prompt,
            CODEX_HOME / "rotation-seeds" / f"{session_id}.txt",
        ),
        env_overrides=pane_env, cols=cols, rows=rows,
    )
    if fresh:
        _watch_for_new_thread_in_background(session_id, before)

    proc, master_fd = TERMINALS.attach(
        session_id, env_overrides=pane_env, cols=cols, rows=rows,
    )
    log.info("Attached %s terminal session=%s pid=%d model=%s thread=%s",
             NAME, session_id, proc.pid, model, thread_id or "new")
    log_event(log, "session.pty.spawned", mind_id=MIND_ID, mind_name=NAME,
              session_id=session_id, process_id=proc.pid, model=model,
              harness_thread_id=thread_id or None)
    return proc, master_fd


def _rotate_pty(
    *, session_id: str, new_claude_sid: str, model: str = "", system_prompt: str = "",
    client_ref: str | None = None, owner_type: str | None = None,
    owner_ref: str | None = None,
) -> bool:
    """Start a fresh codex thread in a live terminal, in place.

    A rotation replaces the *conversation*, not the session and not the
    terminal: the hive session id is permanent, so nothing is renamed and the
    attached client — and therefore the pty, the websocket and the browser
    tile above it — is never disturbed.

    Codex cannot be handed a thread id, so the new thread starts bare and the
    same watcher a fresh terminal uses reports the id codex writes on the
    first turn. The carry-forward rides in as codex's opening prompt, which
    is the only channel this harness has for it.
    """
    del new_claude_sid  # symmetry with the claude harness; codex mints its own
    if not TERMINALS.alive(session_id):
        log.info("No live terminal for session %s — nothing to rotate in place",
                 session_id)
        return False

    # The old thread id must go: it belongs to the conversation being
    # replaced, and a later reattach that resumed it would undo the rotation.
    if not model:
        log.warning("Refusing to rotate session %s: no model to carry over",
                    session_id)
        return False

    THREADS.pop(session_id, None)
    before = _existing_rollout_paths()

    argv = seeded_pane_command(
        _terminal_argv(model, None),
        system_prompt,
        CODEX_HOME / "rotation-seeds" / f"{session_id}.txt",
    )
    TERMINALS.respawn(
        session_id, argv,
        env_overrides=_pane_env(client_ref, owner_type, owner_ref),
    )
    _watch_for_new_thread_in_background(session_id, before)
    log.info("Rotated the conversation in terminal %s (seed=%d chars)",
             TERMINALS.session_name(session_id), len(system_prompt))
    log_event(log, "session.pty.rotated", mind_id=MIND_ID, mind_name=NAME,
              session_id=session_id)
    return True


install_pty_attach(app, mind_name=NAME, terminals=TERMINALS,
                   spawn=_spawn_pty, rotate=_rotate_pty)
runtime_api.install_runtime_routes(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
skills_api.install_skills_routes(app, harness="codex_cli", mind_id=MIND_ID, log=log)
files_api.install_files_routes(app, harness="codex_cli", mind_id=MIND_ID, log=log)
models_api.install_models_route(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)


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
                    # A tile open on this session showed none of this — its
                    # own codex process wasn't involved in the turn at all.
                    mirror_turn(sid, mind_name=NAME, assistant_texts=[text],
                                user_text=content, surface="chat")
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
    log_event(log, "session.closed", mind_id=MIND_ID, mind_name=NAME, session_id=sid)
    return {"session_id": sid, "status": "closed"}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", log_config=None, access_log=False)


if __name__ == "__main__":
    main()
