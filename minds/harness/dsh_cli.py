"""dsh harness — in-container FastAPI service for one mind.

Runs as the sole process inside the mind's container. The mind is selected by
the ``MIND_NAME`` env var (set in the mind's ``container/compose.yaml``);
everything mind-specific comes from ``minds/<MIND_NAME>/runtime.yaml``.

dsh runs one process per turn, like codex and unlike claude — but unlike
codex it takes the conversation id it is handed, through the
``@hive/dsh-headless-resumable`` surface in the harness tree. So the gateway's
conversation id *is* the harness session's id here, there is no second
provider-native id to carry, and the two spawn shapes are the same
conversation at different ages: ``--session-id`` for its first process and
``--resume`` for every one after, decided by whether dsh has that session on
disk. ``minds/pty_attach.claude_conversation_flags`` makes the same decision
the same way for claude.

The turn comes back as one line of JSON on stdout — the session it ran in, how
it stopped, the assistant text, an error code when there was one, and the tool
traffic — because an exit status can carry none of that and the tool traffic is
the measurement this harness is being evaluated on.

The system prompt is composed by hive-comms and shipped as
``system_prompt_blocks`` in the spawn payload; this module composes nothing.
It travels in a file rather than in argv, because a composed prompt plus a
user message runs past ``MAX_ARG_STRLEN`` (128 KiB), which caps one argv entry
however much room the whole command line has.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from minds.proactive import make_proactive_router
from minds import models_api, runtime_api
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

#: DSH_HOME is dsh's own knob: profiles, sessions and credentials live under it.
DSH_HOME = Path(
    os.environ.get("DSH_HOME")
    or RUNTIME.get("runtime_config_dir")
    or str(MIND_DIR / ".dsh")
)

#: The profile whose bundle layers mount the resumable surface. A profile is
#: the only thing that composes a dsh process, so naming the wrong one is a
#: mind with no runner rather than a mind with the wrong options.
DSH_PROFILE = str(RUNTIME.get("dsh_profile") or "hive")

#: The launcher. A container links the bind-mounted tree's own bin; a bare
#: invocation can point at it directly.
DSH_BIN = str(os.environ.get("DSH_BIN") or RUNTIME.get("dsh_bin") or "dsh")

app = FastAPI(title=f"Mind: {NAME}", docs_url=None, redoc_url=None, openapi_url=None)
install_fastapi_logging(app, log, f"mind:{NAME}")

# session_id -> {"system_prompt": str, "model": str, "proc": Process | None, ...}
SESSIONS: dict[str, dict] = {}

# Proactive delivery endpoint. A per-turn harness has no idle stream to drain,
# so the buffer stays empty and GET /proactive returns []. Mounted anyway so
# this mind's bot polls it uniformly with the claude-CLI minds.
PROACTIVE_BUFFER: list[dict] = []
PROACTIVE_TOKEN = os.environ.get("COMMS_BEARER_TOKEN") or None
app.include_router(make_proactive_router(PROACTIVE_BUFFER, PROACTIVE_TOKEN))

#: One path segment of a dsh session artifact: everything outside this set is
#: escaped as ``~XXXX``.
_SAFE_SEGMENT_CHAR = re.compile(r"[A-Za-z0-9._-]")


def _setup_dsh_home() -> None:
    try:
        DSH_HOME.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Off-container (test collection on the host) the container-absolute
        # path has no parent to create. dsh would fail loudly on spawn; a
        # missing directory at import time is not itself the failure.
        log.warning("Could not create DSH_HOME at %s", DSH_HOME)
    os.environ["DSH_HOME"] = str(DSH_HOME)


_setup_dsh_home()


def _encode_segment(raw: str) -> str:
    """Encode one session id the way dsh's JSONL backend does.

    Mirrors ``encodeSegment`` in
    ``packages/session/session-persistence-jsonl/src/format.ts``: safe
    characters pass through, everything else — ``~`` included, so the escape
    is injective — becomes ``~`` plus four uppercase hex digits.
    """
    if raw == "":
        raise ValueError("cannot encode an empty path segment")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for ch in raw:
        if ch != "~" and _SAFE_SEGMENT_CHAR.fullmatch(ch):
            out.append(ch)
        else:
            out.append("~" + format(ord(ch), "04X"))
    return "".join(out)


def _project_key(cwd: str) -> str:
    """The project directory name dsh groups a session under.

    Mirrors ``projectKey``: runs of ``/``, ``\\`` and ``:`` collapse to one
    ``-``, unsafe code units take the same ``~XXXX`` escape, leading dashes are
    stripped, and the result is wrapped in ``--`` and bounded.
    """
    if cwd == "":
        raise ValueError("cannot encode an empty project path")
    readable = []
    separator_run = False
    for ch in cwd:
        if ch in "/\\:":
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif ch != "~" and _SAFE_SEGMENT_CHAR.fullmatch(ch):
            readable.append(ch)
            separator_run = False
        else:
            readable.append("~" + format(ord(ch), "04X"))
            separator_run = False
    slug = "".join(readable).lstrip("-") or "root"
    return f"--{slug[:251]}--"


def _session_persisted(session_id: str) -> bool:
    """Whether dsh holds this conversation on disk.

    On-disk truth, not in-process state: a mind restarted mid-conversation has
    an empty :data:`SESSIONS` and must still resume rather than create, and a
    volume restored without its sessions must create rather than resume.
    """
    directory = (
        DSH_HOME / "sessions" / _project_key(str(PROJECT_DIR)) / _encode_segment(session_id)
    )
    return directory.is_dir()


def _conversation_flags(session_id: str) -> list[str]:
    """Flags binding one dsh process to one conversation id.

    A session on disk means the conversation has spoken before, so continue it.
    Nothing on disk means this is its first process, so declare the id. Either
    way the id is the gateway's. The surface refuses to substitute one branch
    for the other, which is why the choice is made here and made from disk.
    """
    if _session_persisted(session_id):
        return ["--resume", session_id]
    return ["--session-id", session_id]


def _model_env(model: str) -> dict[str, str]:
    """The model, its provider route and that route's endpoint, for one spawn.

    The hive profile defaults nothing: these are what it reads. The model is
    the one the gateway resolved for this session from the mind's broker row,
    and the route and endpoint come from the mind's own ``runtime.yaml``.
    """
    base_url = str(
        RUNTIME_ENV.get("OLLAMA_BASE_URL")
        or RUNTIME_ENV.get("OPENAI_BASE_URL")
        or RUNTIME_ENV.get("ANTHROPIC_BASE_URL")
        or ""
    ).rstrip("/")
    env = {
        "DSH_MODEL": model,
        "DSH_PROVIDER": str(RUNTIME.get("dsh_provider_route") or "hive-proxy"),
        "DSH_PROXY_BASE_URL": base_url,
    }
    window = RUNTIME.get("context_window")
    if window:
        env["DSH_MODEL_CONTEXT_WINDOW"] = str(window)
    return env


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


async def _reap_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Kill the dsh process group and wait for it to exit.

    The launcher is a node process that spawns tool and code-runtime children.
    Killing only the parent would orphan them to PID 1 (us). Spawning with
    ``start_new_session`` puts the whole turn in its own process group;
    ``killpg`` takes it down together. Safe when proc is None or already gone.
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
        log.warning("dsh pid %s did not exit within 5s of SIGKILL", proc.pid)


@app.on_event("startup")
async def _startup() -> None:
    await _fetch_secrets_on_startup()
    asyncio.ensure_future(runtime_api.registration_loop(
        RUNTIME_PATH, mind_name=MIND_NAME, mind_id=MIND_ID, log=log
    ))
    log.info("%s ready (mind_id=%s, dsh_home=%s, profile=%s)",
             NAME, MIND_ID, DSH_HOME, DSH_PROFILE)


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
    # No default, on either field. The gateway mints the conversation id when
    # it writes the session row and resolves the model from this mind's broker
    # row; a mind inventing either has lost the one it was supposed to use,
    # and in dsh's case an invented id is a conversation nobody can find again.
    sid = str(body.get("session_id") or "").strip()
    if not sid:
        return JSONResponse(
            {"error": "session_id required — this mind does not mint conversation ids"},
            status_code=400,
        )
    model = str(body.get("model") or "").strip()
    if not model:
        return JSONResponse(
            {"error": "model required — the gateway resolves it per session"},
            status_code=400,
        )
    system_prompt_blocks = body.get("system_prompt_blocks") or ""
    surface_prompt = body.get("surface_prompt")
    # Spawn-env metadata for the rotation hook, which reads it to attribute the
    # rotation summary to the right (mind_id, client_ref) row.
    client_ref = body.get("client_ref") or ""
    owner_type = body.get("owner_type") or ""
    owner_ref = body.get("owner_ref") or ""
    if system_prompt_blocks and surface_prompt:
        full_prompt = f"{system_prompt_blocks}\n\n{surface_prompt}"
    else:
        full_prompt = surface_prompt or system_prompt_blocks
    SESSIONS[sid] = {
        "system_prompt": full_prompt,
        "model": model,
        "proc": None,
        "client_ref": client_ref,
        "owner_type": owner_type,
        "owner_ref": owner_ref,
    }
    log.info("%s session %s initialised (model=%s persisted=%s)",
             NAME, sid, model, _session_persisted(sid))
    log_event(log, "session.created", mind_id=MIND_ID, mind_name=NAME,
              session_id=sid, model=model, conversation_id=sid)
    return {"session_id": sid, "mind_id": MIND_ID, "name": NAME,
            "status": "running", "model": model}


def _parse_report(lines: list[str]) -> dict | None:
    """The turn report out of a process's stdout, or None if it wrote none.

    The surface writes exactly one line of JSON carrying ``sessionId``. Any
    other stdout — a plugin's own chatter, a warning from node — is passed
    over rather than mistaken for the report, and a process that wrote no
    report at all says so by returning None.
    """
    # Scanned from the end: nothing else writes a report-shaped line, so this
    # only ever matters if the surface someday writes more than one, and the
    # last word is the right one then. No test distinguishes the two
    # directions, because nothing available can produce a second report.
    for line in reversed(lines):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "sessionId" in parsed:
            return parsed
    return None


def _no_text_diagnostic(report: dict) -> str:
    """What to show for a turn that completed without assistant text.

    A blank reply reads as the mind being broken. What the turn actually did —
    how it stopped, and what it did with its tools — is the thing worth
    showing, because on a local model the usual cause is tool calls emitted in
    a dialect the harness could not parse.
    """
    traffic = report.get("traffic") or {}
    parts = [f"No assistant text this turn (dsh stopped on: {report.get('outcome', 'unknown')})."]
    emitted = traffic.get("emitted") or 0
    if emitted:
        parts.append(
            f"The model emitted {emitted} tool call(s): "
            f"{traffic.get('succeeded') or 0} succeeded, "
            f"{traffic.get('failed') or 0} failed, "
            f"{traffic.get('unanswered') or 0} went unanswered."
        )
        by_tool = traffic.get("callsByTool") or {}
        if by_tool:
            parts.append("By tool: " + ", ".join(f"{k}={v}" for k, v in sorted(by_tool.items())))
    else:
        parts.append("It emitted no tool calls either.")
    error = report.get("error")
    if error:
        parts.append(f"Error: {error.get('code')}: {error.get('message')}")
    return "\n\n".join(parts)


async def _run_dsh_turn(sid: str, content: str, images: list[dict] | None) -> Any:
    state = SESSIONS.get(sid)
    if state is None:
        yield {"type": "result", "is_error": True}
        return

    # The composed prompt rides in on the conversation's first turn, the way it
    # does for codex: the surface submits the task as a user message, and a
    # system prompt submitted to nothing reaches no transcript.
    flags = _conversation_flags(sid)
    task = content if flags[0] == "--resume" else f"{state['system_prompt']}\n\n---\n\n{content}"

    if images:
        log.warning("%s session %s: image input not supported, ignoring", NAME, sid)

    env = os.environ.copy()
    env.update({k: str(v) for k, v in RUNTIME_ENV.items()})
    env.update(_model_env(state["model"]))
    env["DSH_HOME"] = str(DSH_HOME)
    for key, name in (("client_ref", "CLIENT_REF"), ("owner_type", "OWNER_TYPE"),
                      ("owner_ref", "OWNER_REF")):
        if state.get(key):
            env[name] = state[key]

    # The task travels in a file: MAX_ARG_STRLEN caps one argv entry at 128 KiB
    # regardless of total command-line room, and a composed prompt plus a turn
    # goes past it. The surface deletes nothing, so this process owns the file.
    handle, task_path = tempfile.mkstemp(prefix=f"dsh-turn-{sid}-", suffix=".txt")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(task)

        cmd = [DSH_BIN, "--profile", DSH_PROFILE, *flags, "--task-file", task_path]
        log.info("%s session %s: spawning dsh turn (%s)", NAME, sid, flags[0])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=10 * 1024 * 1024,
            env=env,
            cwd=str(PROJECT_DIR),
            start_new_session=True,
        )
        state["proc"] = proc

        lines: list[str] = []
        if proc.stdout is not None:
            async for raw_line in proc.stdout:
                lines.append(raw_line.decode(errors="replace"))

        stderr = b""
        if proc.stderr is not None:
            stderr = await proc.stderr.read()
        await proc.wait()
        state["proc"] = None
    finally:
        try:
            os.unlink(task_path)
        except OSError:
            pass

    report = _parse_report(lines)
    if report is None:
        # A process that wrote no report did not complete a turn, whatever its
        # exit status says. Reporting it as an empty success would make a
        # crashed harness indistinguishable from a model with nothing to say.
        detail = stderr.decode(errors="replace").strip()[-2000:]
        log.error("%s session %s: dsh wrote no turn report (rc=%s) %s",
                  NAME, sid, proc.returncode, detail)
        yield {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{
                "type": "text",
                "text": "The harness exited without reporting a turn"
                        f" (exit {proc.returncode})."
                        + (f"\n\n{detail}" if detail else ""),
            }]},
        }
        yield {"type": "result", "session_id": sid, "stop_reason": "no-report",
               "is_error": True}
        return

    yield {"type": "dsh_report", "session_id": sid, "report": report,
           "_observer_only": True}

    text = str(report.get("text") or "")
    yield {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": text if text else _no_text_diagnostic(report)}
        ]},
    }

    outcome = str(report.get("outcome") or "unknown")
    traffic = report.get("traffic") or {}
    error = report.get("error")
    # `completed` is the only outcome that is not a failure. Everything else —
    # a refusal, an error, max-tokens, a reason this harness has not grown yet
    # — is reported under its own name rather than flattened, because the stop
    # reason is the measurement and `max-tokens` in particular is the context
    # ceiling rather than a crash.
    log_event(log, "turn.completed", mind_id=MIND_ID, mind_name=NAME, session_id=sid,
              model=state["model"], stop_reason=outcome,
              error_code=(error or {}).get("code"),
              tools_emitted=traffic.get("emitted"),
              tools_succeeded=traffic.get("succeeded"),
              tools_failed=traffic.get("failed"),
              tools_unanswered=traffic.get("unanswered"))
    result: dict[str, Any] = {
        "type": "result",
        "session_id": str(report.get("sessionId") or sid),
        "stop_reason": outcome,
        "traffic": traffic,
        "is_error": outcome != "completed",
    }
    if error:
        result["error_code"] = error.get("code")
        result["error"] = error.get("message")
    yield result


@app.post("/sessions/{sid}/message")
async def send_message(sid: str, req: Request) -> Any:
    body = await req.json()
    content = body.get("content", "")
    images = body.get("images")
    if sid not in SESSIONS:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)

    # Turn-bleed guard. Two concurrent turns on one conversation would be two
    # dsh processes resuming one session store, which is how a transcript ends
    # up with two interleaved turns and neither one's history intact.
    sess = SESSIONS[sid]
    if sess.get("in_flight"):
        return JSONResponse({"error": "Turn in progress, retry shortly"}, status_code=409)
    sess["in_flight"] = True

    async def stream() -> Any:
        try:
            async for event in _run_dsh_turn(sid, content, images):
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            # Every exit path: completion, client disconnect, exception. An
            # abandoned stream must not lock the conversation out of its next
            # turn, and must not leave a dsh process group running.
            sess["in_flight"] = False
            leftover = sess.get("proc")
            if leftover is not None:
                await _reap_proc(leftover)
                sess["proc"] = None

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/sessions/{sid}/interrupt")
async def interrupt_session(sid: str) -> Any:
    if sid not in SESSIONS:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)
    return {"ok": True, "session_id": sid, "message": "dsh_per_turn"}


@app.post("/sessions/{sid}/release")
async def release_session(sid: str, surface: str) -> Any:
    """Stop one live surface. dsh's session lives on disk, so nothing is lost.

    A terminal release is refused as unsupported rather than reported as a
    release of nothing: this harness ships no interactive CLI, and answering
    "released: false" would send the operator looking for a pane that could
    never have existed.
    """
    if surface == "terminal":
        return JSONResponse(
            {"error": "this harness has no interactive terminal",
             "session_id": sid, "surface": surface, "supported": False},
            status_code=501,
        )
    if surface != "stream":
        return JSONResponse({"error": "surface must be stream"}, status_code=400)
    sess = SESSIONS.pop(sid, None)
    if sess is not None:
        await _reap_proc(sess.get("proc"))
    return {"session_id": sid, "surface": surface, "released": sess is not None}


@app.websocket("/sessions/{sid}/attach-pty")
async def attach_pty(websocket: Any) -> None:
    """Refuse a terminal attach as unsupported, with a code of its own.

    A pre-accept close presents to the gateway as HTTP 403, which is also what
    a mind with no pty route at all answers — so the gateway would read "this
    harness cannot host a terminal" as "this mind's image is broken" and send
    the operator off to rebuild it. The handshake is accepted and closed on
    4417, distinct from 4415 (attach refused) and 4416 (credential refused).
    """
    await websocket.accept()
    await websocket.close(code=4417, reason="this harness has no interactive terminal")


@app.delete("/sessions/{sid}")
async def kill_session(sid: str) -> dict:
    sess = SESSIONS.pop(sid, None)
    if sess is not None:
        await _reap_proc(sess.get("proc"))
    log.info("Killed %s session %s", NAME, sid)
    log_event(log, "session.closed", mind_id=MIND_ID, mind_name=NAME, session_id=sid)
    return {"session_id": sid, "status": "closed"}


# The console's model picker and the runtime writer are harness-agnostic: both
# read this mind's own runtime.yaml. The skills and files pages are not mounted
# yet — this harness reads its skills from a third directory nobody has taught
# skills_sync about, and a route reporting the wrong one is worse than no route.
runtime_api.install_runtime_routes(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
models_api.install_models_route(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info",
                log_config=None, access_log=False)


if __name__ == "__main__":
    main()
