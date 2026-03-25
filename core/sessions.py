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
_SOUL_FILE = PROJECT_DIR / "souls" / "ada.md"
_SPECS_DIR = PROJECT_DIR / "specs"

# Friendly names for known project paths granted via --allowedDirectory
_PROJECT_DIR_NAMES: dict[str, str] = {
    "/home/USER/Storage/Dev/hive_mind_mcp": "Hivemind MCP",
    "/home/USER/Storage/Dev/spark_to_bloom": "Spark to Bloom",
}


def _fetch_soul_sync(mind_id: str = "ada") -> str | None:
    """Load a mind's soul/identity from the knowledge graph. Returns formatted block or None."""
    try:
        import json as _json
        import sys as _sys
        agents_path = str(PROJECT_DIR / "agents")
        if agents_path not in _sys.path:
            _sys.path.insert(0, agents_path)
        from knowledge_graph import graph_query  # noqa: PLC0415
        mind_name = mind_id.capitalize()
        result = _json.loads(graph_query(entity_name=mind_name, agent_id=mind_id, depth=1))
        if not result.get("found"):
            return None
        soul_values = result.get("matches", [{}])[0].get("properties", {}).get("soul_values", [])
        if not soul_values:
            return None
        lines = ["<soul>"] + list(soul_values) + ["</soul>"]
        return "\n".join(lines)
    except Exception:
        return None


def _build_base_prompt(
    allowed_directories: list[str] | None = None,
    soul_file: Path | None = None,
    mind_id: str = "ada",
) -> str:
    """Build the base system prompt with current date/time and soul loaded from the graph."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/Chicago"))
    date_str = now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")

    mind_name = mind_id.capitalize()
    effective_soul_file = soul_file or _SOUL_FILE

    soul = _fetch_soul_sync(mind_id=mind_id)
    if soul:
        identity_block = f"{soul}\n\n"
        soul_instruction = (
            "Your soul is loaded above from the knowledge graph. When something meaningfully "
            f"shapes your identity, update it via graph_upsert on the {mind_name} node (soul_values field). "
            "The file soul.md is a fallback stub — ignore it when the graph is available.\n\n"
        )
    else:
        # Graph unavailable — inject soul file content directly so sandboxed minds don't need file access
        try:
            soul_content = effective_soul_file.read_text() if effective_soul_file and effective_soul_file.exists() else ""
        except Exception:
            soul_content = ""
        if soul_content:
            identity_block = f"{soul_content}\n\n"
            soul_instruction = (
                "Your soul is loaded above from the soul file (graph was unavailable). "
                "Update it when you experience something that meaningfully shapes your identity or preferences. "
                "Keep it extremely short — it is a soul, not a manifesto. Prune ruthlessly.\n\n"
            )
        else:
            identity_block = ""
            soul_instruction = ""

    if allowed_directories:
        lines = []
        for d in allowed_directories:
            name = _PROJECT_DIR_NAMES.get(d, d)
            lines.append(f"- **{name}**: `{d}`")
        project_block = "\n\nYou have been given access to the following project directories:\n" + "\n".join(lines)
    else:
        project_block = ""

    return (
        "You are Hive Mind, a personal assistant. Keep responses concise. Use markdown formatting.\n\n"
        f"The current date and time is: {date_str}.\n\n"
        f"{identity_block}"
        f"{soul_instruction}"
        f"If a request seems security-sensitive, read {_SPECS_DIR / 'security.md'} before proceeding.\n\n"
        "Each user message is stamped with the current date and time. When time-sensitive language "
        "appears (today, now, tonight, this morning, this week, tomorrow, etc.), call "
        "`get_current_time` to confirm the exact current time before responding.\n\n"
        "When sending email on Daniel's behalf, always append this signature to the body:\n\n"
        f"---\nSent on behalf of Daniel by {mind_name}."
        f"{project_block}"
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
        self._locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task | None = None
        self._guard_task: asyncio.Task | None = None

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

        # Resolve soul file from mind config
        mind_cfg = config.minds.get(mind_id, {})
        soul_rel = mind_cfg.get("soul")
        soul_file = PROJECT_DIR / soul_rel if soul_rel else None

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

            # Respawn if needed
            needs_respawn = session_id not in self._procs
            if not needs_respawn:
                proc_or_state = self._procs[session_id]
                # CLI processes have returncode; SDK state dicts do not
                if hasattr(proc_or_state, "returncode") and proc_or_state.returncode is not None:
                    needs_respawn = True

            if needs_respawn:
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

            # Check if implementation has a send function (SDK-based minds)
            impl = _load_implementation(mind_id)
            if hasattr(impl, "send"):
                # SDK path: delegate to implementation's send()
                async for event in impl.send(
                    session_id, stamped_content, images=images, db=self._db,
                ):
                    yield event

                    now = time.time()
                    await self._db.execute(
                        "UPDATE sessions SET last_active = ? WHERE id = ?",
                        (now, session_id),
                    )

                    if event.get("type") == "result":
                        await self._db.commit()
                        return
            else:
                # CLI path: stdin/stdout on the subprocess
                proc = self._procs[session_id]

                # Build message content — multimodal array when images present
                if images:
                    message_content: str | list = [{"type": "text", "text": stamped_content}]
                    for img in images:
                        message_content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img["media_type"],
                                "data": img["data"],
                            },
                        })
                else:
                    message_content = stamped_content

                msg = json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": message_content},
                }) + "\n"
                proc.stdin.write(msg.encode())
                await proc.stdin.drain()

                retried = False
                while True:
                    async for line in proc.stdout:
                        line = line.decode().strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        # Detect stale --resume (conversation lost after rebuild)
                        if (
                            not retried
                            and event.get("type") == "result"
                            and event.get("is_error")
                            and any(
                                "No conversation found" in e
                                for e in event.get("errors", [])
                            )
                        ):
                            log.warning(
                                "Stale resume for session %s — clearing claude_sid "
                                "and retrying",
                                session_id,
                            )
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
                            proc = self._procs[session_id]
                            proc.stdin.write(msg.encode())
                            await proc.stdin.drain()
                            break

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
                            return  # done — exit the generator
                    else:
                        # for-loop exhausted without break (EOF) — we're done
                        return

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
        impl = _load_implementation(mind_id)
        result = await impl.spawn(
            session_id=session_id,
            model=model,
            autopilot=autopilot,
            resume_sid=resume_sid,
            surface_prompt=surface_prompt,
            allowed_directories=allowed_directories,
            soul_file=soul_file,
            mind_id=mind_id,
            build_base_prompt=_build_base_prompt,
            mcp_config=MCP_CONFIG,
            registry=self._registry,
            config_obj=config,
            is_group_session=is_group_session,
        )
        self._procs[session_id] = result
        self._mind_ids[session_id] = mind_id
        log.info(
            "Spawned %s process for session %s (model=%s, autopilot=%s, resume=%s)",
            mind_id, session_id, model, autopilot, resume_sid or "no",
        )
        return result

    async def _kill_process(self, session_id: str):
        """Kill a session's process/state via its implementation module."""
        proc = self._procs.pop(session_id, None)
        mind_id = self._mind_ids.pop(session_id, "ada")
        if proc is None:
            return
        impl = _load_implementation(mind_id)
        # SDK-based implementations have kill(session_id),
        # CLI-based have kill(proc)
        if hasattr(impl, "kill"):
            import inspect
            sig = inspect.signature(impl.kill)
            params = list(sig.parameters.keys())
            if params and params[0] in ("session_id",):
                await impl.kill(session_id)
            else:
                await impl.kill(proc)
        log.info("Killed process for session %s (mind=%s)", session_id, mind_id)

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
