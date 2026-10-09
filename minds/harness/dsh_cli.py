"""dsh harness — in-container FastAPI service for one mind.

Runs as the sole process inside the mind's container. The mind is selected by
the ``MIND_NAME`` env var (set in the mind's ``container/compose.yaml``);
everything mind-specific comes from ``minds/<MIND_NAME>/runtime.yaml``.

dsh runs one process per chat turn and one persistent process for a browser
terminal. Unlike codex it takes the conversation id it is handed, through the
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
from minds import github_token_api, models_api, runtime_api, surface_token_api
from minds.pty_attach import (
    PtyUnavailable,
    TmuxTerminals,
    install_pty_attach,
    teardown as teardown_pty,
)
from core.hive_logging import configure_logging, install_fastapi_logging, log_event

MIND_NAME = os.environ.get("MIND_NAME", "example")
MINDS_ROOT = Path(__file__).resolve().parent.parent
MIND_DIR = MINDS_ROOT / MIND_NAME
PROJECT_DIR = Path("/usr/src/app")
#: The directory a harness turn runs in. The mind's own tree is the default and
#: is wrong for a mind under test: a model writing a relative path lands in the
#: hive's checkout rather than in its own work area, and the writes are
#: scattered where its next turn will not find them. A deployment that mounts a
#: work area names it here, and nothing else about the spawn changes.
SPAWN_DIR = Path(os.environ.get("DSH_SPAWN_DIR", str(PROJECT_DIR)))

log = configure_logging(f"hive-mind.minds.{MIND_NAME}")

RUNTIME_PATH = MIND_DIR / "runtime.yaml"
RUNTIME = yaml.safe_load(RUNTIME_PATH.read_text())
NAME: str = RUNTIME["name"]
MIND_ID: str = RUNTIME["mind_id"]
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
#:
#: The boot value is kept for the pty pane, which caches its argv; the chat
#: path resolves it per turn through `dsh_profile()` so an edit from the
#: settings panel takes effect when the next turn spawns rather than at the
#: next container start — a profile that is not installed must fail where the
#: operator can still see what they typed.
DSH_PROFILE = str(RUNTIME.get("dsh_profile") or "hive")

#: The default when a mind's file names none.
DEFAULT_TURN_TIMEOUT_SECONDS = 1800.0


def turn_timeout(runtime: dict) -> float | None:
    """How long one turn may run, or None for no bound at all.

    Zero is a deliberate choice and not a missing value: a mind whose work is
    one dispatch lasting hours would rather risk a hung request than be killed
    mid-build. It is reported as `None` because that is what `asyncio.timeout`
    takes for "do not arm one", so the caller has no branch to forget.

    The bound is the only automatic recovery from a model request that hangs
    rather than fails. Without it such a turn holds the conversation open,
    `in_flight` set and `/health` still reporting ok, until the gateway's own
    socket read expires and reports a stalled model as a mind that is
    unreachable — a remedy aimed at the network for a fault in neither.
    """
    raw = runtime.get("turn_timeout_seconds")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_TURN_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TURN_TIMEOUT_SECONDS
    return None if seconds <= 0 else seconds


def live_runtime() -> dict:
    """This mind's runtime.yaml as it is on disk right now.

    The module-level `RUNTIME` is this mind's identity and its plumbing, read
    once because none of it can change under a running process. The three
    settings below are different: the console writes them into the same file
    while this process is serving, and a value read at import would mean every
    edit waited on a container restart — which is the per-instance fiddling the
    settings panel exists to end. A failed read falls back to the boot copy: a
    half-written file must not take a turn down.
    """
    try:
        loaded = yaml.safe_load(RUNTIME_PATH.read_text())
    except (OSError, yaml.YAMLError):
        return RUNTIME
    return loaded if isinstance(loaded, dict) else RUNTIME

#: The launcher. A container links the bind-mounted tree's own bin; a bare
#: invocation can point at it directly.
DSH_BIN = str(os.environ.get("DSH_BIN") or RUNTIME.get("dsh_bin") or "dsh")

TERMINALS = TmuxTerminals(NAME, SPAWN_DIR)

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
    for unit in _utf16_units(raw):
        ch = chr(unit)
        if ch != "~" and _SAFE_SEGMENT_CHAR.fullmatch(ch):
            out.append(ch)
        else:
            out.append("~" + format(unit, "04X"))
    return "".join(out)


def _utf16_units(raw: str) -> list[int]:
    """The string as UTF-16 code units, which is what JavaScript iterates.

    ``charCodeAt`` walks code units, so an astral character is two escapes
    (``~D83D~DE00``) and not one (``~1F600``). A Python loop over characters
    would spell the same id differently and look in a directory dsh never wrote.
    """
    encoded = raw.encode("utf-16-le", errors="surrogatepass")
    return [encoded[i] | (encoded[i + 1] << 8) for i in range(0, len(encoded), 2)]


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


def _session_persisted(conversation_id: str) -> bool:
    """Whether dsh holds this conversation on disk.

    On-disk truth, not in-process state: a mind restarted mid-conversation has
    an empty :data:`SESSIONS` and must still resume rather than create, and a
    volume restored without its sessions must create rather than resume.

    The test is the **log**, not its directory, because that is dsh's own test
    (``findLog``) and because the directory is created before the first log is
    written. A turn killed in that window would otherwise leave an empty
    directory that every later turn reads as "resumable", and dsh refuses a
    resume it cannot load — permanently, since nothing cleans the directory up.

    The project key is built from the *resolved* working directory: dsh records
    ``process.cwd()``, which node returns with symlinks resolved, so a spawn cwd
    naming a symlinked path would be filed under a key this probe never looks in.
    """
    directory = (
        DSH_HOME / "sessions" / _project_key(_spawn_cwd()) / _encode_segment(conversation_id)
    )
    return (directory / "session.jsonl.zstd").exists() or (directory / "session.jsonl").exists()


def _spawn_cwd() -> str:
    """The working directory a turn runs in, as dsh will record it."""
    try:
        return str(SPAWN_DIR.resolve())
    except OSError:
        # A path that cannot be resolved is still the one we will hand the
        # spawn; dsh will record whatever it resolves to and the probe will
        # agree with itself either way.
        return str(SPAWN_DIR)


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


def _terminal_context_file(context: str) -> Path | None:
    """Write one process-owned opening context for the interactive runner.

    Whitespace is nothing. dsh refuses a context file it cannot make a turn
    out of, and a refusal here is a dead pane: ``rotate-pty`` has already
    answered ``rotated: true`` and the gateway has already written the
    successor's id, so the rotation is recorded against a pane that exited.
    Writing no file instead brings the pane up unseeded, which the session row
    can recover.
    """
    if not context.strip():
        return None
    directory = DSH_HOME / "terminal-context"
    directory.mkdir(parents=True, exist_ok=True)
    handle, raw_path = tempfile.mkstemp(prefix="context-", suffix=".txt", dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(context)
        path.chmod(0o600)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _terminal_argv(
    conversation_id: str, context_file: Path | None = None, *,
    context_as_turn: bool = False,
) -> list[str]:
    """The persistent DSH prompt loop hosted by one tmux pane.

    ``context_as_turn`` is how a staged rotation's seed gets answered. The
    runner queues an opening context by default, which is right for the
    standing context a fresh terminal opens on; a rotation's seed carries the
    message the user typed into the conversation this pane replaced, so queuing
    it would leave their question in the transcript with no reply coming.
    """
    cmd = [DSH_BIN, "--profile", DSH_PROFILE, *_conversation_flags(conversation_id),
           "--interactive"]
    if context_file is not None:
        cmd.extend(["--context-file", str(context_file)])
        if context_as_turn:
            cmd.append("--context-as-turn")
    return cmd


def _pane_env(
    model: str, client_ref: str | None, owner_type: str | None, owner_ref: str | None,
) -> dict[str, str]:
    """Environment the tmux pane needs for this conversation and model."""
    env = {k: str(v) for k, v in RUNTIME_ENV.items()}
    env.update(_model_env(model))
    env["DSH_HOME"] = str(DSH_HOME)
    env["DSH_PERMISSION_MODE"] = _permission_mode()
    env["HIVE_SURFACE"] = "terminal"
    # The pane prints its own prompt and banner, and the interactive surface
    # ships to every dsh mind — so it reads the name from here rather than
    # carrying one mind's name in code every other install would be lying with.
    env["MIND_NAME"] = NAME
    if client_ref:
        env["CLIENT_REF"] = client_ref
    if owner_type:
        env["OWNER_TYPE"] = owner_type
    if owner_ref:
        env["OWNER_REF"] = owner_ref
    return env


def _spawn_pty(
    *, session_id: str, model: str, conversation_id: str, cols: int, rows: int,
    harness_sid: str | None = None, client_ref: str | None = None,
    owner_type: str | None = None, owner_ref: str | None = None,
    system_prompt: str = "",
) -> tuple[Any, int]:
    """Attach to this session's DSH terminal, starting its pane if absent."""
    del harness_sid
    state = SESSIONS.get(session_id)
    if state is not None and state.get("in_flight"):
        raise PtyUnavailable("a chat turn is still running for this conversation")

    pane_env = _pane_env(model, client_ref, owner_type, owner_ref)
    context_file: Path | None = None
    if not TERMINALS.alive(session_id):
        opening_context = system_prompt
        if not opening_context and not _session_persisted(conversation_id) and state is not None:
            opening_context = str(state.get("system_prompt") or "")
        context_file = _terminal_context_file(opening_context)
    try:
        TERMINALS.start(
            session_id,
            _terminal_argv(conversation_id, context_file),
            env_overrides=pane_env,
            cols=cols,
            rows=rows,
        )
    except Exception:
        if context_file is not None:
            context_file.unlink(missing_ok=True)
        raise
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
    user_prompt: str = "", client_ref: str | None = None,
    owner_type: str | None = None, owner_ref: str | None = None,
) -> bool:
    """Respawn a live pane onto a fresh gateway-owned DSH conversation."""
    if not TERMINALS.alive(session_id):
        return False
    if not model:
        log.warning("Refusing to rotate session %s: no model to carry over", session_id)
        return False

    seed = user_prompt or system_prompt
    context_file = _terminal_context_file(seed)
    try:
        TERMINALS.respawn(
            session_id,
            # Keyed to the file, not to the seed: a whitespace-only seed wrote
            # no file, and asking the runner to answer a context that is not
            # there is refused — which would kill the pane this rotation is
            # supposed to keep alive.
            _terminal_argv(new_claude_sid, context_file,
                           context_as_turn=bool(user_prompt) and context_file is not None),
            env_overrides=_pane_env(model, client_ref, owner_type, owner_ref),
        )
    except Exception:
        if context_file is not None:
            context_file.unlink(missing_ok=True)
        raise
    log.info("Rotated DSH terminal %s onto %s", session_id, new_claude_sid)
    log_event(log, "session.pty.rotated", mind_id=MIND_ID, mind_name=NAME,
              session_id=session_id, conversation_id=new_claude_sid)
    return True


#: The permission mode every spawn runs under, unless the mind names another.
#:
#: dsh's base bundle pins a fresh session to ``workspace-write`` with an
#: ``ask`` approval policy, and a per-turn harness has nobody to ask: there is
#: no pane, no prompt and no channel the request could travel on. So the
#: escalation is refused, and what reaches the operator is a shell call failing
#: ``SANDBOX_UNAVAILABLE`` and a write outside the spawn's own cwd failing
#: ``FS_SANDBOX_DENIED`` — reported as capabilities the model does not have,
#: when they are walls the deployment put up. A mind's boundary is its
#: container and the directories mounted into it, which is the same decision
#: the claude and codex minds already run under. A mind that wants confinement
#: anyway names ``permission_mode`` in its own ``runtime.yaml``.
DEFAULT_PERMISSION_MODE = "danger-full-access"

#: What the profile's ``apiKeyEnv`` names. Every mind's env block spells its
#: proxy credential differently; the route resolves one name, so the adapter
#: translates rather than asking every mind to be rewritten.
PROXY_KEY_ENV = "HIVE_PROXY_KEY"

#: The env names a mind's own block may carry its proxy credential under.
_PROXY_KEY_SOURCES = ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DSH_API_KEY")

#: And its endpoint.
_PROXY_URL_SOURCES = ("OLLAMA_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL")


def _permission_mode() -> str:
    """The sandbox and approval mode this mind's spawns run under."""
    return str(RUNTIME.get("permission_mode") or DEFAULT_PERMISSION_MODE)


# How often a turn in flight puts a byte on the gateway's socket. Comfortably
# inside comms' own no-data cap, which is ten minutes.
HEARTBEAT_SECONDS = 120.0



def dsh_profile() -> str:
    """The profile this turn's spawn composes itself from, read per turn."""
    return str(live_runtime().get("dsh_profile") or "hive")


def _goal_rounds() -> int:
    """How many goal rounds one dispatch to this mind may be driven for.

    Declared per mind in ``runtime.yaml`` rather than guessed per turn, because
    only the mind's own configuration knows what it is for. A chat mind leaves
    it unset and every turn is one turn, which is what a person talking to it
    expects. A mind whose job is a long build names a number, and then its own
    decision to stop ends a round instead of the job — a small model treats the
    first natural pause as the end of the work, and no prompt wording fixes
    that reliably.
    """
    raw = live_runtime().get("goal_rounds")
    try:
        rounds = int(raw)
    except (TypeError, ValueError):
        return 1
    return rounds if rounds > 1 else 1


def _stop_on_failed_call() -> bool:
    """Whether a failed tool call should end this mind's dispatch.

    A mind under test while the harness is still growing tools wants this on:
    the first failure is the result, the rounds after it are the same model
    working around the same gap, and the fix cannot reach a process that loaded
    its tool registry at spawn. Every other mind wants it off, which is the
    default — a failed call mid-conversation is something the model works
    around, not a reason to end the turn.

    Every failure counts, not only the ones the harness refuses outright. A
    `create` that wanted an empty file and a path spelled a second way both
    arrive as a tool failing at its job, and both are tools we owe the model.
    The report says which side turned the call away, so the run names where to
    look without costing the rounds that would have found out.
    """
    return bool(live_runtime().get("stop_on_failed_call") is True)


def _first_env(names: tuple[str, ...]) -> str:
    """The first of these names this mind's env block or environment carries.

    A container gets its key from compose; a bare-metal mind gets it from the
    ``env:`` block its spawns already apply. Reading both means one adapter
    serves either deployment.
    """
    for name in names:
        value = str(RUNTIME_ENV.get(name) or os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _model_env(model: str) -> dict[str, str]:
    """The model, its provider route, that route's endpoint and its credential.

    The hive profile defaults nothing: these are what it reads. The model is
    the one the gateway resolved for this session from the mind's broker row;
    the endpoint and credential come from the mind's own ``runtime.yaml``.

    The credential is not optional. The inference proxy answers 401 without a
    bearer key, and ``llm-pi-ai`` refuses outright rather than falling back
    when a profile names a credential reference that resolves to nothing — so
    an unmapped key is every turn of this mind failing at its first model
    request.
    """
    env = {
        "DSH_MODEL": model,
        "DSH_PROXY_BASE_URL": _first_env(_PROXY_URL_SOURCES).rstrip("/"),
    }
    key = _first_env(_PROXY_KEY_SOURCES)
    if key:
        env[PROXY_KEY_ENV] = key
    # Not optional either. The profile's one provider route lists no models, so
    # this single number sizes every model the route ever serves — including the
    # ones a subagent names that nobody configured — and compaction reads it as
    # the ceiling. Omitting it would hand the harness a fallback nobody picked,
    # so the harness refuses the boot; refusing here instead names the field and
    # the file the operator actually edits.
    window = RUNTIME.get("context_window")
    if not window:
        raise RuntimeError(
            "runtime.yaml needs context_window: it sizes every model this mind's "
            "provider route serves, and compaction reads it as the ceiling"
        )
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
    # After the shared fetch, never before: this mind's own GitHub token
    # wins over the hive's, and `GH_TOKEN` from that fetch would
    # otherwise shadow it on every spawn.
    github_token_api.adopt_stored_token()
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
    # The conversation id, minted by comms when it wrote the session row and
    # carried on every spawn as `resume_sid`. It is not the row's own id: the
    # row is permanent and a rotation replaces the conversation under it, so a
    # harness running under the row id would resume the context a rotation
    # exists to drop and would report an id the gateway does not recognise.
    conversation_id = str(body.get("resume_sid") or "").strip()
    if not sid or not conversation_id:
        return JSONResponse(
            {"error": "session_id and resume_sid required — this mind mints no conversation ids"},
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
    # A re-POST of an existing session is ordinary: comms respawns on its own
    # restart, mid-turn, because its process table is empty while this process
    # and its running dsh turn are not. Assigning a fresh dict would clear
    # `in_flight` and the process handle, and the next message would put a
    # second dsh process on the same session log — whose colliding event
    # sequence numbers make the log unloadable, at which point dsh refuses
    # both resume and create and the conversation is dead for good. So the
    # declared fields are updated and the turn's own state is left alone.
    state = SESSIONS.setdefault(sid, {})
    state.update({
        "system_prompt": full_prompt,
        "conversation_id": conversation_id,
        "model": model,
        "client_ref": client_ref,
        "owner_type": owner_type,
        "owner_ref": owner_ref,
    })
    state.setdefault("proc", None)
    log.info("%s session %s initialised (model=%s conversation=%s persisted=%s)",
             NAME, sid, model, conversation_id, _session_persisted(conversation_id))
    log_event(log, "session.created", mind_id=MIND_ID, mind_name=NAME,
              session_id=sid, model=model, conversation_id=conversation_id)
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
        if isinstance(parsed, dict) and "sessionId" in parsed and "progress" not in parsed:
            return parsed
    return None


def _progress_frame(line: str) -> dict | None:
    """A goal-round progress line as an observer-only frame, or None.

    Observer-only because a chat surface wants the answer, not a running count:
    comms publishes the frame to the session event stream and does not pass it
    to the bot. What it is really for is the socket. A goal-driven dispatch is
    one HTTP response lasting an hour, and comms caps that socket on time since
    the last byte — so without something crossing it per round, every long run
    is read as a mind that stopped answering.
    """
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    progress = parsed.get("progress") if isinstance(parsed, dict) else None
    if not isinstance(progress, dict):
        return None
    return {"type": "goal_progress", "progress": progress, "_observer_only": True}


def _delta_frame(line: str) -> dict | None:
    """An assistant delta line as a streaming frame, or None.

    The runner writes one line per piece of assistant output as the model
    produces it. Translated into the Anthropic-shaped partial event the
    surfaces already read, because one vocabulary across the harnesses is what
    lets a surface render a streamed turn without knowing which harness is
    behind it — claude's own stdout already speaks this shape.

    Not observer-only: the whole point is that it reaches the chat surface.
    """
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    delta = parsed.get("delta") if isinstance(parsed, dict) else None
    if not isinstance(delta, dict):
        return None
    body = delta.get("text")
    if not isinstance(body, str) or not body:
        return None
    kind = delta.get("kind")
    if kind == "reasoning":
        inner = {"type": "thinking_delta", "thinking": body}
    elif kind == "text":
        inner = {"type": "text_delta", "text": body}
    else:
        # A kind this adapter has not grown yet. Relaying it as prose is how a
        # fragment of a tool call ends up spoken in the mind's voice; the
        # runner deliberately writes only the two kinds a reader wants.
        return None
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0, "delta": inner}}


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

    # Cleared per turn: the flag is the *previous* turn's verdict, and a fresh
    # turn nobody has interrupted must not report itself as stopped on purpose
    # the moment its own output is unreadable.
    state["killed"] = False

    # The composed prompt rides in on the conversation's first turn, the way it
    # does for codex: the surface submits the task as a user message, and a
    # system prompt submitted to nothing reaches no transcript.
    conversation_id = state["conversation_id"]
    flags = _conversation_flags(conversation_id)
    task = content if flags[0] == "--resume" else f"{state['system_prompt']}\n\n---\n\n{content}"

    if images:
        # Said out loud rather than logged: a model answering the text as if
        # nothing was attached looks like a model that ignored the picture.
        log.warning("%s session %s: image input not supported, ignoring", NAME, sid)
        yield {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{
                "type": "text",
                "text": f"({len(images)} attached image(s) were not sent —"
                        " this harness has no image input yet.)",
            }]},
        }

    env = os.environ.copy()
    env.update({k: str(v) for k, v in RUNTIME_ENV.items()})
    try:
        env.update(_model_env(state["model"]))
    except RuntimeError as exc:
        # Said out loud, like the spawn failures below. This raises on a mind
        # whose own configuration cannot size a model request, and an unhandled
        # one here ends the response with zero frames — the gateway reports a
        # turn that produced nothing, which is total silence for the operator
        # who is the only person able to fix it.
        log.error("%s session %s: %s", NAME, sid, exc)
        yield {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": f"Error: {exc}"}]},
        }
        yield {"type": "result", "session_id": conversation_id, "stop_reason": "error",
               "is_error": True, "error_code": "MIND_MISCONFIGURED", "error": str(exc)}
        return
    env["DSH_HOME"] = str(DSH_HOME)
    env["DSH_PERMISSION_MODE"] = _permission_mode()
    for key, name in (("client_ref", "CLIENT_REF"), ("owner_type", "OWNER_TYPE"),
                      ("owner_ref", "OWNER_REF")):
        if state.get(key):
            env[name] = state[key]

    # The task travels in a file: MAX_ARG_STRLEN caps one argv entry at 128 KiB
    # regardless of total command-line room, and a composed prompt plus a turn
    # goes past it. The surface deletes nothing, so this process owns the file.
    handle, task_path = tempfile.mkstemp(prefix="dsh-turn-", suffix=".txt")
    objective_path: str | None = None
    returncode: int | None = None
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(task)

        rounds = _goal_rounds()
        goal_flags: list[str] = []
        if rounds > 1:
            # The objective travels separately from the task. A conversation's
            # first turn is the composed system prompt and the message together,
            # and the round driver quotes the objective into every round — so a
            # goal armed with the task would spend the context window on forty
            # copies of a soul. It travels in a file for the same reason the task
            # does: MAX_ARG_STRLEN caps one argv entry at 128 KiB.
            obj_handle, objective_path = tempfile.mkstemp(
                prefix="dsh-goal-", suffix=".txt")
            with os.fdopen(obj_handle, "w", encoding="utf-8") as fh:
                fh.write(content)
            goal_flags = ["--goal-rounds", str(rounds),
                          "--goal-objective-file", objective_path]
        if _stop_on_failed_call():
            goal_flags.append("--stop-on-failed-call")
        cmd = [DSH_BIN, "--profile", dsh_profile(), *flags, *goal_flags,
               "--task-file", task_path]
        log.info("%s session %s: spawning dsh turn (%s %s)",
                 NAME, sid, flags[0], conversation_id)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
                env=env,
                cwd=str(SPAWN_DIR),
                start_new_session=True,
            )
        except OSError as exc:
            # The launcher is not on PATH, or the working directory is gone.
            # A spawn that never started is its own failure and is reported as
            # one: a stream that ends with no frames at all reaches the gateway
            # as a turn that produced nothing, which is the remedy for a quiet
            # model applied to a mind whose harness is not installed.
            log.error("%s session %s: could not spawn %s: %s", NAME, sid, DSH_BIN, exc)
            yield {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{
                    "type": "text",
                    "text": f"The harness could not be started: {exc}",
                }]},
            }
            yield {"type": "result", "session_id": conversation_id,
                   "stop_reason": "not-spawned", "is_error": True,
                   "error_code": "HARNESS_NOT_SPAWNED", "error": str(exc)}
            return
        state["proc"] = proc
        # Captured while the leader is alive: once it is reaped its pgid cannot
        # be looked up, and the group is what has to be signalled.
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            pgid = None

        lines: list[str] = []
        stderr = b""
        timed_out = False
        # Resolved now rather than at import: the console writes this value
        # into the file while this process is serving, and a bound read at
        # boot would mean every edit waited on a container restart.
        deadline = turn_timeout(live_runtime())
        try:
            async with asyncio.timeout(deadline):
                if proc.stdout is not None:
                    # Read with a heartbeat rather than a plain async-for. This
                    # harness writes nothing until a turn or a goal round ends,
                    # and comms caps the response socket on time since the last
                    # byte — so a turn that thinks for longer than that is read
                    # as a mind that stopped answering, and aborting the response
                    # kills this process group with the work still going. The
                    # read task is awaited rather than cancelled on each tick,
                    # because cancelling a readline mid-line loses the line.
                    reading: asyncio.Task[bytes] | None = None
                    while True:
                        if reading is None:
                            reading = asyncio.ensure_future(proc.stdout.readline())
                        done, _ = await asyncio.wait({reading}, timeout=HEARTBEAT_SECONDS)
                        if not done:
                            yield {"type": "turn_heartbeat", "_observer_only": True,
                                   "session_id": conversation_id}
                            continue
                        raw_line = reading.result()
                        reading = None
                        if not raw_line:
                            break
                        decoded = raw_line.decode(errors="replace")
                        lines.append(decoded)
                        delta = _delta_frame(decoded)
                        if delta is not None:
                            yield delta
                            continue
                        frame = _progress_frame(decoded)
                        if frame is not None:
                            yield frame
                if proc.stderr is not None:
                    stderr = await proc.stderr.read()
                returncode = await proc.wait()
        except TimeoutError:
            timed_out = True
            log.error("%s session %s: dsh turn exceeded %ss, killing it",
                      NAME, sid, deadline)
        except ValueError as exc:
            # A single stdout line past the stream limit. Raised after the SSE
            # headers are already out, so an unhandled one ends the response
            # with zero frames and the gateway reports a turn that produced
            # nothing — total silence for the user.
            log.error("%s session %s: unreadable dsh output: %s", NAME, sid, exc)
            stderr = str(exc).encode()
        state["proc"] = None
        # Every turn, not only the abandoned ones. The launcher is a node
        # process that spawns tool and code-runtime children; the leader
        # exiting does not take them with it, and anything still running
        # reparents to PID 1 — which inside the mind's container is this
        # process. codex's adapter kills the group on every turn end for the
        # same reason.
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    finally:
        for path in (task_path, objective_path):
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass

    if timed_out:
        yield {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{
                "type": "text",
                "text": f"The turn was still running after {int(deadline or 0)}"
                        " seconds and was stopped.",
            }]},
        }
        yield {"type": "result", "session_id": conversation_id,
               "stop_reason": "timeout", "is_error": True,
               "error_code": "TURN_TIMEOUT"}
        return

    report = _parse_report(lines)
    if report is None and state.get("killed"):
        # Stopped on purpose — a kill, or a release to another surface. The
        # crash sentence below would read as the harness falling over.
        yield {"type": "result", "session_id": conversation_id,
               "stop_reason": "stopped", "is_error": False}
        return
    if report is None:
        # A process that wrote no report did not complete a turn, whatever its
        # exit status says. Reporting it as an empty success would make a
        # crashed harness indistinguishable from a model with nothing to say.
        detail = stderr.decode(errors="replace").strip()[-2000:]
        log.error("%s session %s: dsh wrote no turn report (rc=%s) %s",
                  NAME, sid, returncode, detail)
        yield {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{
                "type": "text",
                "text": "The harness exited without reporting a turn"
                        f" (exit {returncode})."
                        + (f"\n\n{detail}" if detail else ""),
            }]},
        }
        yield {"type": "result", "session_id": conversation_id,
               "stop_reason": "no-report", "is_error": True}
        return

    yield {"type": "dsh_report", "session_id": sid, "report": report,
           "_observer_only": True}

    text = str(report.get("text") or "")
    # Still written when the turn already streamed every word of it. The chat
    # surfaces drop a buffered copy of prose they have already shown — that
    # mechanism predates this harness and claude relies on it — and the frame is
    # the only thing comms accumulates into the turn ledger. Withholding it put
    # the answer on the screen and left the session's own history holding the
    # question and no reply, which `/history`, the rotation carry-forward and
    # the late-turn merge all read.
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
              tools_unanswered=traffic.get("unanswered"),
              turns=report.get("turns"), goal_phase=report.get("goalPhase"))
    result: dict[str, Any] = {
        "type": "result",
        "session_id": str(report.get("sessionId") or conversation_id),
        "stop_reason": outcome,
        "traffic": traffic,
        "is_error": outcome != "completed",
    }
    # Reported beside the tally rather than folded into it: a build that stopped
    # at round two of forty and one that burned all forty are different
    # failures, and the tally alone cannot tell them apart.
    if report.get("turns") is not None:
        result["turns"] = report["turns"]
    if report.get("goalPhase") is not None:
        result["goal_phase"] = report["goalPhase"]
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
    """Stop the turn in flight and leave the conversation standing.

    A per-turn harness has no interrupt of its own: the only thing to stop is
    the process, and the conversation lives in dsh's session store on disk, so
    killing one turn costs the turn and nothing else. The session row, its
    resume id and every completed turn survive — which is what separates this
    from `DELETE`.

    It used to answer `ok` and do nothing, which is the shape that matters:
    the surface told the operator the work had been interrupted while forty
    goal rounds carried on behind it, and the only remedy left was bouncing
    the container. A refusal reported as a success is worse than a refusal.
    """
    sess = SESSIONS.get(sid)
    if sess is None:
        return JSONResponse({"error": f"Session {sid} not found"}, status_code=404)
    proc = sess.get("proc")
    if proc is None or proc.returncode is not None:
        return {"ok": True, "session_id": sid, "message": "nothing_running"}
    # Read by the turn generator, which reports a process that wrote no report
    # as stopped on purpose rather than as a harness that fell over.
    sess["killed"] = True
    await _reap_proc(proc)
    sess["proc"] = None
    log_event(log, "turn.interrupted", mind_id=MIND_ID, mind_name=NAME, session_id=sid)
    return {"ok": True, "session_id": sid, "message": "interrupted"}


@app.post("/sessions/{sid}/release")
async def release_session(sid: str, surface: str) -> Any:
    """Stop one live surface. dsh's session lives on disk, so nothing is lost.
    """
    if surface == "terminal":
        released = teardown_pty(sid)
        return {"session_id": sid, "surface": surface, "released": released}
    if surface != "stream":
        return JSONResponse({"error": "surface must be stream"}, status_code=400)
    sess = SESSIONS.pop(sid, None)
    if sess is not None:
        sess["killed"] = True
        await _reap_proc(sess.get("proc"))
    return {"session_id": sid, "surface": surface, "released": sess is not None}

@app.delete("/sessions/{sid}")
async def kill_session(sid: str) -> dict:
    sess = SESSIONS.pop(sid, None)
    if sess is not None:
        sess["killed"] = True
        await _reap_proc(sess.get("proc"))
    teardown_pty(sid)
    log.info("Killed %s session %s", NAME, sid)
    log_event(log, "session.closed", mind_id=MIND_ID, mind_name=NAME, session_id=sid)
    return {"session_id": sid, "status": "closed"}


# The console's model picker and the runtime writer are harness-agnostic: both
# read this mind's own runtime.yaml. The skills and files pages are not mounted
# yet — this harness reads its skills from a third directory nobody has taught
# skills_sync about, and a route reporting the wrong one is worse than no route.
# One middleware over every `/sessions` route rather than a decorator per
# route, so a route added later cannot ship open by being forgotten. Without
# it the gateway goes on presenting a token nobody reads, registration keeps
# publishing a credential nobody checks, and every surface stays green while
# anything that can reach the port can open, drive and kill conversations.
install_pty_attach(app, mind_name=NAME, terminals=TERMINALS,
                   spawn=_spawn_pty, rotate=_rotate_pty, mind_dir=MIND_DIR)
runtime_api.install_session_guard(app, mind_dir=MIND_DIR)
runtime_api.install_runtime_routes(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
models_api.install_models_route(app, path=RUNTIME_PATH, mind_id=MIND_ID, log=log)
surface_token_api.install_surface_token_routes(app, mind_id=MIND_ID, log=log)
github_token_api.install_github_token_routes(app, mind_id=MIND_ID, log=log)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("MIND_SERVER_PORT", "8420"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info",
                log_config=None, access_log=False)


if __name__ == "__main__":
    main()
