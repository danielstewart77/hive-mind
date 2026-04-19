"""
Hive Mind — Session manager.

Owns all Claude CLI subprocesses and the session database.
Each session maps to one claude -p subprocess in stream-json mode.
"""

import asyncio
import importlib
import json
import logging
import os
import re
import signal
import time
import types
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from config import PROJECT_DIR, config
from core.models import ModelRegistry

_TRANSCRIPT_DIR = Path.home() / ".claude" / "projects" / "-usr-src-app"

log = logging.getLogger("hive-mind.sessions")


# ---------------------------------------------------------------------------
# Subprocess stderr drain — logs stderr lines at WARNING
# ---------------------------------------------------------------------------

async def _drain_stderr(proc: Any, session_id: str) -> None:
    """Read subprocess stderr line by line and log each non-empty line at WARNING.

    No-op if proc.stderr is None (e.g. SDK-based minds or stderr not piped).
    """
    if proc.stderr is None:
        return
    async for err_line in proc.stderr:
        err_text = err_line.decode().strip()
        if err_text:
            log.warning("subprocess stderr: session=%s line=%s", session_id, err_text[:200])


# ---------------------------------------------------------------------------
# Memory helpers — run in executor (synchronous neo4j/requests calls)
# ---------------------------------------------------------------------------

def _fetch_memories_sync(query: str, mind_id: str = "ada") -> str | None:
    """Retrieve relevant memories for context seeding. Non-fatal."""
    try:
        import json
        import sys
        agents_path = str(PROJECT_DIR / "agents")
        if agents_path not in sys.path:
            sys.path.insert(0, agents_path)
        from memory import memory_retrieve  # noqa: PLC0415
        data = json.loads(memory_retrieve(query=query, k=5, agent_id=mind_id))
        memories = data.get("memories", [])
        if not memories:
            return None
        lines = ["<context from memory>"]
        for m in memories:
            lines.append(f"- {m['content']}")
        lines.append("</context from memory>")
        return "\n".join(lines)
    except Exception:
        return None





_MCP_CONTAINER = PROJECT_DIR / ".mcp.container.json"
MCP_CONFIG = str(_MCP_CONTAINER if _MCP_CONTAINER.exists() else PROJECT_DIR / ".mcp.json")
_SPECS_DIR = PROJECT_DIR / "specs"

# Friendly names for known project paths granted via --allowedDirectory
# Populated from env vars — no hardcoded host paths
_PROJECT_DIR_NAMES: dict[str, str] = {}
if os.environ.get("HOST_MCP_DIR"):
    _PROJECT_DIR_NAMES[os.environ["HOST_MCP_DIR"]] = "Hivemind MCP"
if os.environ.get("HOST_SPARK_DIR"):
    _PROJECT_DIR_NAMES[os.environ["HOST_SPARK_DIR"]] = "Spark to Bloom"


def _fetch_soul_sync(mind_id: str = "ada") -> str | None:
    """Load a mind's soul/identity from the knowledge graph. Returns formatted block or None."""
    import sys as _sys
    tools_path = str(PROJECT_DIR / "tools" / "stateful")
    if tools_path not in _sys.path:
        _sys.path.insert(0, tools_path)
    try:
        from lucent_graph import graph_query  # noqa: PLC0415
    except ImportError:
        log.error("_fetch_soul_sync: could not import lucent_graph from %s", tools_path)
        return None
    try:
        mind_name = mind_id.capitalize()
        result = json.loads(graph_query(entity_name=mind_name, agent_id=mind_id, depth=1))
        if not result.get("found"):
            log.debug("_fetch_soul_sync: no graph node found for mind_id=%r", mind_id)
            return None
        soul_values = result.get("matches", [{}])[0].get("properties", {}).get("soul_values", [])
        if not soul_values:
            log.warning("_fetch_soul_sync: node found for %r but soul_values is empty", mind_id)
            return None
        return "\n".join(["<soul>"] + list(soul_values) + ["</soul>"])
    except Exception:
        log.exception("_fetch_soul_sync: unexpected error loading soul for mind_id=%r", mind_id)
        return None


def _build_base_prompt(
    allowed_directories: list[str] | None = None,
    soul_file: Path | None = None,
    mind_id: str = "ada",
    prompt_files: list[str] | None = None,
) -> str:
    """Build the base system prompt with current date/time and soul loaded from the graph."""
    from zoneinfo import ZoneInfo
    from core.mind_registry import parse_mind_file
    from core.prompt_profiles import build_prompt

    now = datetime.now(ZoneInfo("America/Chicago"))
    date_str = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")

    mind_name = mind_id.capitalize()
    mind_dir = PROJECT_DIR / "minds" / mind_id
    if prompt_files is None:
        info = parse_mind_file(mind_dir / "MIND.md")
        prompt_files = info.prompt_files

    soul = _fetch_soul_sync(mind_id=mind_id)
    if soul:
        identity_block = f"{soul}\n\n"
        soul_instruction = (
            "Your soul is loaded above from the knowledge graph. When something meaningfully "
            f"shapes your identity, update it via graph_upsert on the {mind_name} node (soul_values field). "
            "Keep it extremely short — it is a soul, not a manifesto. Prune ruthlessly.\n\n"
        )
    else:
        # Graph unavailable — degrade gracefully, do not fall back to soul files
        identity_block = ""
        soul_instruction = ""

    return build_prompt(
        date_str=date_str,
        mind_name=mind_name,
        identity_block=identity_block,
        soul_instruction=soul_instruction,
        allowed_directories=allowed_directories,
        mind_dir=mind_dir,
        prompt_files=prompt_files,
    )

# ---------------------------------------------------------------------------
# Dynamic mind implementation loading
# ---------------------------------------------------------------------------
_implementation_cache: dict[str, types.ModuleType] = {}


def _load_implementation(mind_id: str) -> types.ModuleType:
    """Load the implementation module for a given mind.

    Falls back to Ada's implementation if the requested mind has no module.
    """
    if mind_id in _implementation_cache:
        return _implementation_cache[mind_id]
    try:
        mod = importlib.import_module(f"minds.{mind_id}.implementation")
        _implementation_cache[mind_id] = mod
        return mod
    except (ImportError, ModuleNotFoundError):
        log.warning("No implementation for mind %s, falling back to ada", mind_id)
        if "ada" not in _implementation_cache:
            _implementation_cache["ada"] = importlib.import_module("minds.ada.implementation")
        return _implementation_cache["ada"]


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    claude_sid    TEXT,
    owner_type    TEXT NOT NULL,
    owner_ref     TEXT NOT NULL,
    summary       TEXT DEFAULT 'New session',
    model         TEXT,
    autopilot     INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_active   REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    epilogue_status TEXT DEFAULT NULL,
    mind_id       TEXT DEFAULT 'ada',
    group_session_id TEXT
);

CREATE TABLE IF NOT EXISTS active_sessions (
    client_type   TEXT NOT NULL,
    client_ref    TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    PRIMARY KEY (client_type, client_ref)
);

CREATE TABLE IF NOT EXISTS group_sessions (
    id                TEXT PRIMARY KEY,
    moderator_mind_id TEXT NOT NULL DEFAULT 'ada',
    created_at        REAL NOT NULL,
    ended_at          REAL
);
"""


class SessionManager:
    def __init__(self, model_registry: ModelRegistry):
        self._registry = model_registry
        self._db: aiosqlite.Connection | None = None
        self._procs: dict[str, Any] = {}  # Process (Ada/CLI) or dict (Nagatha/SDK)
        self._mind_ids: dict[str, str] = {}  # session_id -> mind_id
        self._rc_procs: dict[str, asyncio.subprocess.Process] = {}  # RC subprocesses
        self._locks: dict[str, asyncio.Lock] = {}
        self._observer_queues: dict[str, set[asyncio.Queue]] = {}
        self._reaper_task: asyncio.Task | None = None
        self._guard_task: asyncio.Task | None = None
        self.mind_registry = None  # Set by server.py after scan

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self):
        """Initialize DB and start background tasks."""
        db_path = os.environ.get("SESSIONS_DB_PATH", str(PROJECT_DIR / "sessions.db"))
        # Ensure parent directory exists (for Docker named volume mounts)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        # Migration: add epilogue_status column for existing databases
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN epilogue_status TEXT DEFAULT NULL"
            )
            await self._db.commit()
        except Exception:
            pass  # Column already exists
        # Migration: add mind_id column for existing databases
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN mind_id TEXT DEFAULT 'ada'"
            )
            await self._db.commit()
        except Exception:
            pass  # Column already exists
        # Migration: add group_session_id column for existing databases
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN group_session_id TEXT"
            )
            await self._db.commit()
        except Exception:
            pass  # Column already exists
        # Mark any previously "running" sessions as idle (stale from crash)
        await self._db.execute(
            "UPDATE sessions SET status = 'idle' WHERE status = 'running'"
        )
        await self._db.commit()
        self._reaper_task = asyncio.create_task(self._idle_reaper())
        self._guard_task = asyncio.create_task(self._autopilot_guard())
        log.info("Session manager started (db=%s)", db_path)

    async def shutdown(self):
        """Kill all subprocesses and close DB."""
        if self._reaper_task:
            self._reaper_task.cancel()
        if self._guard_task:
            self._guard_task.cancel()
        # Kill RC subprocesses that may not have a corresponding main process
        for sid in list(self._rc_procs):
            await self.kill_rc_process(sid)
        for sid in list(self._procs):
            await self._kill_process(sid)
        if self._db:
            await self._db.close()
        log.info("Session manager shut down")

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------
    async def create_session(
        self,
        owner_type: str,
        owner_ref: str,
        client_ref: str,
        model: str | None = None,
        surface_prompt: str | None = None,
        allowed_directories: list[str] | None = None,
        mind_id: str = "ada",
    ) -> dict:
        """Create a new session, spawn process, return session info."""
        model = model or config.default_model
        session_id = str(uuid.uuid4())
        now = time.time()

        await self._db.execute(
            """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status, mind_id)
               VALUES (?, ?, ?, ?, ?, ?, 'running', ?)""",
            (session_id, owner_type, owner_ref, model, now, now, mind_id),
        )
        await self._db.execute(
            """INSERT OR REPLACE INTO active_sessions (client_type, client_ref, session_id)
               VALUES (?, ?, ?)""",
            (owner_type, client_ref, session_id),
        )
        await self._db.commit()

        # Graph is authoritative; MIND.md soul_seed is one-time bootstrap only
        soul_file = None

        await self._spawn(session_id, model, autopilot=False, surface_prompt=surface_prompt, allowed_directories=allowed_directories, soul_file=soul_file, mind_id=mind_id, is_group_session=(owner_type == "group"))
        log.info("Created session %s (model=%s, mind=%s, owner=%s)", session_id, model, mind_id, owner_ref)
        return await self._session_dict(session_id)

    async def get_session(self, session_id: str) -> dict | None:
        """Get session details."""
        return await self._session_dict(session_id)

    async def list_sessions(
        self,
        owner_ref: str | None = None,
        status: str | None = None,
        client_type: str | None = None,
        client_ref: str | None = None,
    ) -> list[dict]:
        """List sessions, optionally filtered."""
        query = "SELECT * FROM sessions WHERE 1=1"
        params = []
        if owner_ref:
            query += " AND owner_ref = ?"
            params.append(owner_ref)
        if status:
            query += " AND status = ?"
            params.append(status)
        if client_type:
            query += " AND owner_type = ?"
            params.append(client_type)
        query += " ORDER BY last_active DESC"

        rows = await self._db.execute(query, params)
        sessions = [dict(r) for r in await rows.fetchall()]

        # If client filtering requested, also check active_sessions
        if client_type and client_ref:
            active_row = await self._db.execute(
                "SELECT session_id FROM active_sessions WHERE client_type = ? AND client_ref = ?",
                (client_type, client_ref),
            )
            active = await active_row.fetchone()
            active_id = active["session_id"] if active else None
            for s in sessions:
                s["is_active"] = s["id"] == active_id

        return sessions

    async def get_active_session(self, client_type: str, client_ref: str) -> dict | None:
        """Get the active session for a client surface."""
        row = await self._db.execute(
            "SELECT session_id FROM active_sessions WHERE client_type = ? AND client_ref = ?",
            (client_type, client_ref),
        )
        result = await row.fetchone()
        if not result:
            return None
        return await self._session_dict(result["session_id"])

    async def stream_session_events(self, session_id: str):
        """Yield live session events to passive observers."""
        session = await self._get_row(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        if session.get("status") == "closed":
            yield {"type": "session_closed", "session_id": session_id}
            return

        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._observer_queues.setdefault(session_id, set()).add(queue)

        try:
            while True:
                event = await queue.get()
                yield event
                if event.get("type") == "session_closed":
                    return
        finally:
            watchers = self._observer_queues.get(session_id)
            if watchers is not None:
                watchers.discard(queue)
                if not watchers:
                    self._observer_queues.pop(session_id, None)

    async def _publish_session_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Fan out a session event to all passive observers."""
        watchers = list(self._observer_queues.get(session_id, ()))
        for queue in watchers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    async def activate_session(
        self, session_id: str, client_type: str, client_ref: str
    ) -> dict:
        """Set a session as active on a client surface. Respawn if idle."""
        session = await self._get_row(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        await self._db.execute(
            """INSERT OR REPLACE INTO active_sessions (client_type, client_ref, session_id)
               VALUES (?, ?, ?)""",
            (client_type, client_ref, session_id),
        )
        await self._db.commit()

        if session["status"] == "idle" and session_id not in self._procs:
            await self._spawn(
                session_id,
                session["model"],
                autopilot=bool(session["autopilot"]),
                resume_sid=session["claude_sid"],
                mind_id=session.get("mind_id", "ada"),
            )
            await self._db.execute(
                "UPDATE sessions SET status = 'running' WHERE id = ?", (session_id,)
            )
            await self._db.commit()

        return await self._session_dict(session_id)

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------
    async def send_message(self, session_id: str, content: str, images: list[dict] | None = None):
        """Send a message and yield NDJSON response events."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = await self._get_row(session_id)
            if not session:
                raise ValueError(f"Session not found: {session_id}")

            mind_id = session.get("mind_id", "ada")
            log.info("send_message: start session=%s mind=%s", session_id, mind_id)
            t0 = time.monotonic()

            # Respawn if needed
            needs_respawn = session_id not in self._procs
            if not needs_respawn:
                proc_or_state = self._procs[session_id]
                # CLI processes have returncode; SDK state dicts do not
                if hasattr(proc_or_state, "returncode") and proc_or_state.returncode is not None:
                    needs_respawn = True

            if needs_respawn:
                log.info("send_message: respawn session=%s mind=%s model=%s", session_id, mind_id, session["model"])
                await self._spawn(
                    session_id,
                    session["model"],
                    autopilot=bool(session["autopilot"]),
                    resume_sid=session["claude_sid"],
                    mind_id=mind_id,
                )
                await self._db.execute(
                    "UPDATE sessions SET status = 'running' WHERE id = ?",
                    (session_id,),
                )
                await self._db.commit()

            # Prepend current datetime so Claude always has temporal context
            tz = ZoneInfo(os.environ.get("TZ", "America/Chicago"))
            now_str = datetime.now(tz).strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
            stamped_content = f"[{now_str}]\n{content}"

            # Mark active NOW so the idle reaper doesn't kill us mid-response
            await self._db.execute(
                "UPDATE sessions SET last_active = ?, status = 'running' WHERE id = ?",
                (time.time(), session_id),
            )
            await self._db.commit()

            # Update summary + seed context on first message
            if session["summary"] == "New session":
                summary = content[:100].strip()
                await self._db.execute(
                    "UPDATE sessions SET summary = ? WHERE id = ?",
                    (summary, session_id),
                )
                await self._db.commit()

                # Memory-3: prepend relevant past memories to first message
                loop = asyncio.get_event_loop()
                seeded = await loop.run_in_executor(None, _fetch_memories_sync, content, mind_id)
                if seeded:
                    stamped_content = f"{seeded}\n\n{stamped_content}"
                    log.debug("Context seeding injected %d chars", len(seeded))

            # Route message to mind container via HTTP, stream SSE response
            proc_info = self._procs.get(session_id)
            if not proc_info or not proc_info.get("_mind_url"):
                raise ValueError(f"No mind container URL for session {session_id}")

            mind_url = proc_info["_mind_url"]

            import aiohttp
            retried = False
            while True:
                try:
                    async with aiohttp.ClientSession(read_bufsize=10 * 1024 * 1024) as http:
                        async with http.post(
                            f"{mind_url}/sessions/{session_id}/message",
                            json={"content": stamped_content, "images": images},
                            timeout=aiohttp.ClientTimeout(total=600),
                        ) as resp:
                            if resp.status == 404:
                                # Session doesn't exist on mind container — respawn
                                if not retried:
                                    retried = True
                                    log.info("Session %s not found on %s, respawning", session_id, mind_url)
                                    await self._spawn(
                                        session_id, session["model"],
                                        autopilot=bool(session["autopilot"]),
                                        resume_sid=session.get("claude_sid"),
                                        mind_id=mind_id,
                                    )
                                    continue
                                raise ValueError(f"Session {session_id} not found after respawn")

                            async for line in resp.content:
                                line = line.decode().strip()
                                if not line or not line.startswith("data: "):
                                    continue
                                data = line[6:]  # strip "data: " prefix
                                try:
                                    event = json.loads(data)
                                except json.JSONDecodeError:
                                    continue

                                # Detect stale --resume
                                if (
                                    not retried
                                    and event.get("type") == "result"
                                    and event.get("is_error")
                                    and any(
                                        "No conversation found" in e
                                        for e in event.get("errors", [])
                                    )
                                ):
                                    log.warning("Stale resume for session %s — retrying", session_id)
                                    retried = True
                                    await self._kill_process(session_id)
                                    await self._db.execute(
                                        "UPDATE sessions SET claude_sid = NULL WHERE id = ?",
                                        (session_id,),
                                    )
                                    await self._db.commit()
                                    await self._spawn(
                                        session_id, session["model"],
                                        autopilot=bool(session["autopilot"]),
                                        mind_id=mind_id,
                                    )
                                    break

                                await self._publish_session_event(session_id, event)
                                yield event

                                now = time.time()
                                await self._db.execute(
                                    "UPDATE sessions SET last_active = ? WHERE id = ?",
                                    (now, session_id),
                                )

                                if event.get("type") == "result":
                                    claude_sid = event.get("session_id")
                                    if claude_sid:
                                        await self._db.execute(
                                            "UPDATE sessions SET claude_sid = ? WHERE id = ?",
                                            (claude_sid, session_id),
                                        )
                                    await self._db.commit()
                                    elapsed = time.monotonic() - t0
                                    log.info("send_message: result session=%s elapsed=%.1fs", session_id, elapsed)
                                    if elapsed > 30:
                                        log.warning("send_message: slow response session=%s mind=%s elapsed=%.1fs", session_id, mind_id, elapsed)
                                    return
                            else:
                                return  # stream exhausted
                except aiohttp.ClientError as exc:
                    log.error("Mind container %s unreachable for session %s: %s", mind_url, session_id, exc)
                    yield {"type": "result", "is_error": True, "errors": [f"Mind container unreachable: {exc}"]}
                    return
                break  # exit retry loop on success

    # ------------------------------------------------------------------
    # Model switching
    # ------------------------------------------------------------------
    async def switch_model(self, session_id: str, model: str) -> dict:
        """Switch model mid-session: kill process, respawn with --resume."""
        session = await self._get_row(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        old_provider = self._registry.get_provider(session["model"])
        new_provider = self._registry.get_provider(model)

        await self._kill_process(session_id)
        await self._db.execute(
            "UPDATE sessions SET model = ?, status = 'running' WHERE id = ?",
            (model, session_id),
        )
        await self._db.commit()

        await self._spawn(
            session_id,
            model,
            autopilot=bool(session["autopilot"]),
            resume_sid=session["claude_sid"],
            mind_id=session.get("mind_id", "ada"),
        )

        result = await self._session_dict(session_id)
        if old_provider.name != new_provider.name:
            result["warning"] = (
                f"Context from previous {old_provider.name} model may not carry over perfectly."
            )
        return result

    # ------------------------------------------------------------------
    # Autopilot
    # ------------------------------------------------------------------
    async def toggle_autopilot(self, session_id: str) -> dict:
        """Toggle autopilot: kill process, respawn with/without --dangerously-skip-permissions."""
        session = await self._get_row(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        new_autopilot = 0 if session["autopilot"] else 1
        await self._kill_process(session_id)
        await self._db.execute(
            "UPDATE sessions SET autopilot = ?, status = 'running' WHERE id = ?",
            (new_autopilot, session_id),
        )
        await self._db.commit()

        await self._spawn(
            session_id,
            session["model"],
            autopilot=bool(new_autopilot),
            resume_sid=session["claude_sid"],
            mind_id=session.get("mind_id", "ada"),
        )
        return await self._session_dict(session_id)

    # ------------------------------------------------------------------
    # Interrupt (SIGINT without killing)
    # ------------------------------------------------------------------
    async def interrupt_session(self, session_id: str) -> dict:
        """Interrupt the current run and recycle the live process.

        This approximates an interactive escape keypress: stop the current
        request, discard the stale subprocess, but keep the logical session
        active so the next message can respawn with ``claude_sid``.

        Raises:
            LookupError: If session_id does not exist in the database.
            ValueError: If the live session has no mind container URL.
            RuntimeError: If the mind container is unreachable.
        """
        session = await self._get_row(session_id)
        if not session:
            raise LookupError(f"Session not found: {session_id}")

        proc_info = self._procs.get(session_id)
        if not proc_info:
            return {
                "ok": True,
                "session_id": session_id,
                "message": "nothing_running",
                "resume_ready": bool(session.get("claude_sid")),
            }

        mind_url = proc_info.get("_mind_url")
        if not mind_url:
            raise ValueError(f"No mind container URL for session {session_id}")

        import aiohttp
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{mind_url}/sessions/{session_id}/interrupt",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    result = await resp.json()
        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"Mind container unreachable for session {session_id}: {exc}"
            ) from exc

        # The interrupted subprocess is no longer trustworthy. Remove the live
        # process so the next message respawns cleanly against the saved thread.
        await self._kill_process(session_id)

        if not isinstance(result, dict):
            result = {"ok": True}
        result.setdefault("session_id", session_id)
        result["resume_ready"] = bool(session.get("claude_sid"))
        return result

    # ------------------------------------------------------------------
    # Kill / close
    # ------------------------------------------------------------------
    async def kill_session(self, session_id: str) -> dict:
        """Kill a session: SIGTERM the subprocess, mark closed."""
        session = await self._get_row(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        await self._kill_process(session_id)
        await self._db.execute(
            "UPDATE sessions SET status = 'closed' WHERE id = ?", (session_id,)
        )
        await self._db.execute(
            "DELETE FROM active_sessions WHERE session_id = ?", (session_id,)
        )
        await self._db.commit()
        await self._publish_session_event(
            session_id,
            {"type": "session_closed", "session_id": session_id},
        )

        uptime = time.time() - session["created_at"]
        return {
            "id": session_id,
            "summary": session["summary"],
            "model": session["model"],
            "autopilot": bool(session["autopilot"]),
            "uptime_seconds": uptime,
            "status": "closed",
        }

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------
    def _mind_url(self, mind_id: str) -> str:
        """Return the mind's gateway_url from the registry."""
        if self.mind_registry:
            info = self.mind_registry.get(mind_id)
            if info:
                return info.gateway_url
        raise ValueError(f"Mind '{mind_id}' not found in registry")

    async def _spawn(
        self,
        session_id: str,
        model: str,
        autopilot: bool = False,
        resume_sid: str | None = None,
        surface_prompt: str | None = None,
        allowed_directories: list[str] | None = None,
        soul_file: Path | None = None,
        mind_id: str = "ada",
        is_group_session: bool = False,
    ) -> Any:
        mind_url = self._mind_url(mind_id)
        prompt_files: list[str] = []
        if self.mind_registry:
            info = self.mind_registry.get(mind_id)
            if info:
                prompt_files = info.prompt_files
        import aiohttp
        async with aiohttp.ClientSession() as http:
            resp = await http.post(
                f"{mind_url}/sessions",
                json={
                    "session_id": session_id,
                    "model": model,
                    "autopilot": autopilot,
                    "resume_sid": resume_sid,
                    "surface_prompt": surface_prompt,
                    "allowed_directories": allowed_directories,
                    "prompt_files": prompt_files,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Mind container {mind_id} spawn failed: {body}")

        self._procs[session_id] = {"_mind_url": mind_url}
        self._mind_ids[session_id] = mind_id
        log.info("Spawned %s session %s via %s", mind_id, session_id, mind_url)
        return self._procs[session_id]

    async def _kill_process(self, session_id: str):
        """Kill a session on its mind container via HTTP."""
        await self.kill_rc_process(session_id)

        proc = self._procs.pop(session_id, None)
        mind_id = self._mind_ids.pop(session_id, "ada")
        if proc is None:
            return

        mind_url = proc.get("_mind_url")
        if not mind_url:
            log.warning("No mind_url for session %s, cannot kill", session_id)
            return

        try:
            import aiohttp
            async with aiohttp.ClientSession() as http:
                await http.delete(
                    f"{mind_url}/sessions/{session_id}",
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception:
            log.exception("Failed to kill session %s on %s", session_id, mind_url)
        log.info("Killed session %s (mind=%s, url=%s)", session_id, mind_id, mind_url)

    # ------------------------------------------------------------------
    # Remote Control subprocess management
    # ------------------------------------------------------------------
    _ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    _RC_URL_RE = re.compile(r"(https://claude\.ai/code/\S+)")

    async def spawn_rc_process(self, session_id: str, timeout: float = 10.0) -> dict:
        """Spawn a Remote Control subprocess for an existing session.

        Reads the session's claude_sid from the database, spawns
        ``claude --remote-control --resume <claude_sid> --name <Mind>``,
        parses the session URL from stdout, and returns it.

        Args:
            session_id: The gateway session ID.
            timeout: Seconds to wait for the RC URL to appear on stdout.

        Returns:
            Dict with ``url``, ``session_id``, and ``rc_pid``.

        Raises:
            LookupError: If the session does not exist.
            ValueError: If the session has no ``claude_sid``.
            RuntimeError: If the URL cannot be parsed within *timeout*.
        """
        # If there is already an RC process running for this session, kill it first
        if session_id in self._rc_procs:
            await self.kill_rc_process(session_id)

        row = await self._get_row(session_id)
        if not row:
            raise LookupError(f"Session not found: {session_id}")

        claude_sid = row.get("claude_sid")
        if not claude_sid:
            raise ValueError(f"Session {session_id} has no claude_sid — cannot start Remote Control")

        mind_id = row.get("mind_id", "ada")
        mind_name = mind_id.capitalize()

        cmd = [
            "claude",
            "--remote-control",
            "--resume", claude_sid,
            "--name", mind_name,
        ]

        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(PROJECT_DIR),
        )

        # Read stdout lines until we find the session URL or timeout
        url: str | None = None
        assert proc.stdout is not None  # guaranteed by stdout=PIPE
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    break
                if not line_bytes:
                    break  # EOF
                line = line_bytes.decode("utf-8", errors="replace")
                # Strip ANSI escape codes
                line = self._ANSI_ESCAPE_RE.sub("", line).strip()
                match = self._RC_URL_RE.search(line)
                if match:
                    url = match.group(1)
                    break
        except Exception:
            # On any unexpected error, kill the process
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise

        if url is None:
            # Kill the orphaned process
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise RuntimeError(
                f"Failed to parse RC URL from stdout within {timeout}s for session {session_id}"
            )

        self._rc_procs[session_id] = proc
        log.info(
            "Spawned RC process for session %s (pid=%d, url=%s)",
            session_id, proc.pid, url,
        )
        return {"url": url, "session_id": session_id, "rc_pid": proc.pid}

    async def kill_rc_process(self, session_id: str) -> None:
        """Kill the Remote Control subprocess for a session, if any.

        No-op if no RC process is tracked for *session_id*.
        """
        proc = self._rc_procs.pop(session_id, None)
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
        log.info("Killed RC process for session %s", session_id)

    # ------------------------------------------------------------------
    # Group session management
    # ------------------------------------------------------------------
    async def create_group_session(self, moderator_mind_id: str = "ada") -> dict:
        """Create a new group session."""
        assert self._db is not None
        group_id = str(uuid.uuid4())
        now = time.time()
        await self._db.execute(
            "INSERT INTO group_sessions (id, moderator_mind_id, created_at) VALUES (?, ?, ?)",
            (group_id, moderator_mind_id, now),
        )
        await self._db.commit()
        log.info("Created group session %s (moderator=%s)", group_id, moderator_mind_id)
        return {
            "id": group_id,
            "moderator_mind_id": moderator_mind_id,
            "created_at": now,
            "ended_at": None,
        }

    async def get_group_session(self, group_session_id: str) -> dict | None:
        """Get group session details."""
        assert self._db is not None
        row = await self._db.execute(
            "SELECT * FROM group_sessions WHERE id = ?", (group_session_id,)
        )
        result = await row.fetchone()
        if not result:
            return None
        return dict(result)

    async def delete_group_session(self, group_session_id: str) -> dict:
        """End a group session by setting ended_at."""
        assert self._db is not None
        now = time.time()
        await self._db.execute(
            "UPDATE group_sessions SET ended_at = ? WHERE id = ?",
            (now, group_session_id),
        )
        await self._db.commit()
        row = await self._db.execute(
            "SELECT * FROM group_sessions WHERE id = ?", (group_session_id,)
        )
        result = await row.fetchone()
        if not result:
            raise ValueError(f"Group session not found: {group_session_id}")
        return dict(result)

    async def get_or_create_group_child_session(
        self, group_session_id: str, mind_id: str, surface_prompt: str | None = None
    ) -> str:
        """Find an existing child session for a mind in a group, or create one.

        Returns the child session ID.
        """
        assert self._db is not None
        rows = await self._db.execute(
            "SELECT id FROM sessions WHERE group_session_id = ? AND mind_id = ? AND status != 'closed'",
            (group_session_id, mind_id),
        )
        existing = await rows.fetchone()

        if existing:
            return existing["id"]

        child = await self.create_session(
            owner_type="group",
            owner_ref=group_session_id,
            client_ref=f"group-{group_session_id}-{mind_id}",
            mind_id=mind_id,
            surface_prompt=surface_prompt,
        )
        child_session_id = child["id"]
        # Link to group session
        await self._db.execute(
            "UPDATE sessions SET group_session_id = ? WHERE id = ?",
            (group_session_id, child_session_id),
        )
        await self._db.commit()
        return child_session_id

    async def get_group_transcript(self, group_session_id: str) -> list[dict]:
        """Get unified transcript for a group session, time-ordered with mind_id attribution."""
        assert self._db is not None
        rows = await self._db.execute(
            "SELECT * FROM sessions WHERE group_session_id = ? ORDER BY last_active ASC",
            (group_session_id,),
        )
        return [dict(r) for r in await rows.fetchall()]

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------
    async def _idle_reaper(self):
        """Kill sessions idle beyond timeout. Runs every 60s.

        Reaped sessions get status='idle' with epilogue_status=NULL,
        so Trigger A (next session creation) or Trigger B (scheduler sweep)
        will pick them up for epilogue processing.
        """
        while True:
            try:
                await asyncio.sleep(60)
                cutoff = time.time() - (config.idle_timeout_minutes * 60)
                rows = await self._db.execute(
                    "SELECT id FROM sessions WHERE status = 'running' AND last_active < ?",
                    (cutoff,),
                )
                for row in await rows.fetchall():
                    sid = row["id"]
                    await self._kill_process(sid)
                    await self._publish_session_event(
                        sid, {"type": "session_closed", "session_id": sid}
                    )
                    self._observer_queues.pop(sid, None)
                    await self._db.execute(
                        "UPDATE sessions SET status = 'idle' WHERE id = ?", (sid,)
                    )
                    log.info("Reaped idle session %s", sid)
                await self._db.commit()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Error in idle reaper")

    async def _autopilot_guard(self):
        """Kill runaway autopilot sessions. Runs every 30s."""
        while True:
            try:
                await asyncio.sleep(30)
                guards = config.autopilot_guards
                cutoff = time.time() - (guards.max_minutes_without_input * 60)
                rows = await self._db.execute(
                    "SELECT id FROM sessions WHERE status = 'running' AND autopilot = 1 AND last_active < ?",
                    (cutoff,),
                )
                for row in await rows.fetchall():
                    sid = row["id"]
                    await self._kill_process(sid)
                    await self._db.execute(
                        "UPDATE sessions SET status = 'killed_guard' WHERE id = ?",
                        (sid,),
                    )
                    log.warning("Autopilot guard killed session %s", sid)
                await self._db.commit()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Error in autopilot guard")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _get_row(self, session_id: str) -> dict | None:
        # Exact match first
        row = await self._db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        result = await row.fetchone()
        if result:
            return dict(result)
        # Prefix match for short IDs (e.g. "85d44986")
        row = await self._db.execute(
            "SELECT * FROM sessions WHERE id LIKE ? || '%'", (session_id,)
        )
        results = await row.fetchall()
        if len(results) == 1:
            return dict(results[0])
        return None

    async def get_transcript_path(self, session_id: str) -> Path | None:
        """Get the path to a session's Claude transcript JSONL file.

        Returns the path if the session has a claude_sid and the file exists on disk,
        otherwise returns None.
        """
        row = await self._db.execute(
            "SELECT claude_sid FROM sessions WHERE id = ?", (session_id,)
        )
        result = await row.fetchone()
        if not result or not result["claude_sid"]:
            return None
        path = _TRANSCRIPT_DIR / f"{result['claude_sid']}.jsonl"
        if path.exists():
            return path
        return None

    # ------------------------------------------------------------------
    # Epilogue
    # ------------------------------------------------------------------
    async def get_sessions_pending_epilogue(self) -> list[dict]:
        """Return sessions eligible for epilogue processing."""
        rows = await self._db.execute(
            "SELECT * FROM sessions WHERE status IN ('idle', 'closed') AND epilogue_status IS NULL AND owner_type != 'broker'"
        )
        return [dict(r) for r in await rows.fetchall()]

    async def set_epilogue_status(self, session_id: str, status: str) -> None:
        """Update the epilogue_status column for a session."""
        await self._db.execute(
            "UPDATE sessions SET epilogue_status = ? WHERE id = ?", (status, session_id)
        )
        await self._db.commit()

    async def _session_dict(self, session_id: str) -> dict | None:
        row = await self._get_row(session_id)
        if not row:
            return None
        return {
            "id": row["id"],
            "claude_sid": row["claude_sid"],
            "owner_type": row["owner_type"],
            "owner_ref": row["owner_ref"],
            "summary": row["summary"],
            "model": row["model"],
            "autopilot": bool(row["autopilot"]),
            "created_at": row["created_at"],
            "last_active": row["last_active"],
            "status": row["status"],
            "epilogue_status": row.get("epilogue_status"),
            "mind_id": row.get("mind_id", "ada"),
        }
