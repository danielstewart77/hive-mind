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

from comms.config import PROJECT_DIR, config
from comms.models import ModelRegistry

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

def _fetch_memories_sync(query: str, mind_id: str) -> str | None:
    """Retrieve relevant memories for context seeding. Non-fatal."""
    try:
        import json
        import sys
        agents_path = str(PROJECT_DIR / "agents")
        if agents_path not in sys.path:
            sys.path.insert(0, agents_path)
        from memory import memory_retrieve  # noqa: PLC0415
        data = json.loads(memory_retrieve(query=query, k=5, mind_id=mind_id))
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
    mind_id       TEXT DEFAULT 'ada',
    group_session_id TEXT,
    rotation_armed INTEGER NOT NULL DEFAULT 0,
    rotated_from  TEXT
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

CREATE TABLE IF NOT EXISTS session_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mind_id     TEXT NOT NULL,
    mind_name   TEXT NOT NULL,
    client_ref  TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_session_memory_lookup
    ON session_memory (mind_id, client_ref, created_at DESC);

CREATE TABLE IF NOT EXISTS session_turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_session_turns_lookup
    ON session_turns (session_id, created_at ASC);
"""


class SessionManager:
    # Idle interval after which the observer event stream emits a ping.
    # Must stay under intermediary idle timeouts (Cloudflare ~100s).
    EVENT_HEARTBEAT_SECONDS = 20.0
    # Sessions idle longer than this are reaped to 'closed'. Nothing else
    # ever closes an abandoned session — rotation and explicit kills close
    # their own, but a session whose surface simply walked away stays
    # 'idle' forever and reads as active everywhere downstream.
    REAP_IDLE_AFTER_SECONDS = 7 * 86400
    REAP_INTERVAL_SECONDS = 3600.0

    def __init__(self, model_registry: ModelRegistry):
        self._registry = model_registry
        self._db: aiosqlite.Connection | None = None
        self._procs: dict[str, Any] = {}  # Process (Ada/CLI) or dict (Nagatha/SDK)
        self._mind_ids: dict[str, str] = {}  # session_id -> mind_id
        self._rc_procs: dict[str, asyncio.subprocess.Process] = {}  # RC subprocesses
        self._locks: dict[str, asyncio.Lock] = {}
        self._observer_queues: dict[str, set[asyncio.Queue]] = {}
        self._reaper_task: asyncio.Task | None = None
        self.broker_db = None  # Set by server.py lifespan; broker.minds IS the mind registry

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
        # FK enforcement is per-connection in SQLite and OFF by default.
        # Without this, orphan active_sessions rows survive their parent
        # sessions row's deletion — exactly the dangling-pointer class of
        # bug that caused "two active rows for the same chat" mysteries.
        await self._db.execute("PRAGMA foreign_keys = ON")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        # Migration: drop the legacy epilogue_status column if it survives
        # from earlier deployments. The epilogue subsystem was removed.
        try:
            await self._db.execute(
                "ALTER TABLE sessions DROP COLUMN epilogue_status"
            )
            await self._db.commit()
        except Exception:
            pass  # Column already gone (or sqlite < 3.35 — column is harmless)
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
        # Migration: add rotation_armed column for existing databases. When
        # set, the next user turn finalizes a pending session rotation (swap
        # to a fresh session) instead of the rotation Stop hook clearing the
        # session on the assistant's turn. See arm_rotation / send_message.
        try:
            await self._db.execute(
                "ALTER TABLE sessions ADD COLUMN rotation_armed INTEGER NOT NULL DEFAULT 0"
            )
            await self._db.commit()
        except Exception:
            pass  # Column already exists
        # Migration: add rotated_from, the id of the session this one replaced.
        # Surfaces that reattach across a rotation need to identify the
        # successor exactly; without lineage they guess by mind, and with two
        # terminals open on one mind the guess lands on the *other* live
        # session and the two conversations cross.
        try:
            await self._db.execute("ALTER TABLE sessions ADD COLUMN rotated_from TEXT")
            await self._db.commit()
        except Exception:
            pass  # Column already exists
        # Backfill: every session owns a conversation id. Rows created before
        # the id was minted at session creation may still be blank — those are
        # sessions that never finished a turn, so there is no conversation on
        # disk to lose. Give them one now rather than leaving a hole any
        # attach would have to invent its way around.
        await self._db.execute(
            "UPDATE sessions SET claude_sid = lower(hex(randomblob(4))) || '-' "
            "|| lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))),2) "
            "|| '-a' || substr(lower(hex(randomblob(2))),2) || '-' "
            "|| lower(hex(randomblob(6))) "
            "WHERE claude_sid IS NULL OR claude_sid = ''"
        )
        # Mark any previously "running" sessions as idle (stale from crash)
        await self._db.execute(
            "UPDATE sessions SET status = 'idle' WHERE status = 'running'"
        )
        await self._db.commit()
        self._reaper_task = asyncio.create_task(self._reap_loop())
        log.info("Session manager started (db=%s)", db_path)

    async def shutdown(self):
        """Kill all subprocesses and close DB."""
        if self._reaper_task:
            self._reaper_task.cancel()
            self._reaper_task = None
        # Kill RC subprocesses that may not have a corresponding main process
        for sid in list(self._rc_procs):
            await self.kill_rc_process(sid)
        for sid in list(self._procs):
            await self._kill_process(sid)
        if self._db:
            await self._db.close()
        log.info("Session manager shut down")

    async def _reap_loop(self):
        """Periodically sweep abandoned sessions to 'closed'.

        First sweep runs immediately so a restart clears any backlog of
        ghosts without waiting an interval.
        """
        while True:
            try:
                await self.reap_stale_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Session reaper sweep failed")
            await asyncio.sleep(self.REAP_INTERVAL_SECONDS)

    async def reap_stale_sessions(self) -> list[str]:
        """Close idle sessions untouched for REAP_IDLE_AFTER_SECONDS.

        A session with a live tracked subprocess is skipped no matter how
        old its last_active is — liveness beats the timestamp. Rows are
        closed, not deleted: 'closed' is what Archived means downstream.
        """
        cutoff = time.time() - self.REAP_IDLE_AFTER_SECONDS
        rows = await self._db.execute_fetchall(
            "SELECT id FROM sessions WHERE status = 'idle' AND last_active < ?",
            (cutoff,),
        )
        stale = [r["id"] for r in rows if r["id"] not in self._procs]
        for session_id in stale:
            await self._db.execute(
                "UPDATE sessions SET status = 'closed' WHERE id = ?", (session_id,)
            )
            await self._db.execute(
                "DELETE FROM active_sessions WHERE session_id = ?", (session_id,)
            )
        if stale:
            await self._db.commit()
            log.info("Reaped %d stale idle session(s): %s", len(stale),
                     ", ".join(s[:8] for s in stale))
        return stale

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
        *,
        mind_id: str,
        rotated_from: str | None = None,
    ) -> dict:
        """Create a new session, spawn process, return session info."""
        # The mind's preferred model lives in broker.minds (set at registration
        # from each mind's own config). The caller can override per-session,
        # but absent that, the mind picks. No comms-wide silent fallback —
        # if the mind isn't registered, broker.get_mind_by_id 404s downstream.
        from comms import bootstrap_loader  # noqa: PLC0415
        from comms import broker  # noqa: PLC0415
        mind_row = await broker.get_mind_by_id(self.broker_db, mind_id)
        mind_name = (mind_row or {}).get("name") or mind_id
        if not model:
            model = (mind_row or {}).get("model")
            if not model:
                raise ValueError(
                    f"no model: caller did not specify and mind_id={mind_id} "
                    "is not in broker.minds (or has no model column set)"
                )

        session_id = str(uuid.uuid4())
        # The conversation id is minted HERE and nowhere else. A session owns
        # one conversation from birth, before any surface has spoken to it, so
        # "which conversation is this?" always has an answer — for Telegram,
        # for the web terminal, for a session that has never taken a turn.
        # Minds are handed this id and pin the harness to it; they never mint
        # one of their own, and nothing downstream ever overwrites it.
        claude_sid = str(uuid.uuid4())
        now = time.time()

        await self._db.execute(
            """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid, created_at, last_active, status, mind_id, rotated_from)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
            (session_id, owner_type, owner_ref, model, claude_sid, now, now, mind_id, rotated_from),
        )
        await self._db.execute(
            """INSERT OR REPLACE INTO active_sessions (client_type, client_ref, session_id)
               VALUES (?, ?, ?)""",
            (owner_type, client_ref, session_id),
        )
        await self._db.commit()

        # Graph is authoritative; MIND.md soul_seed is one-time bootstrap only
        soul_file = None

        system_prompt_blocks = await bootstrap_loader.compose_prompt_blocks(
            mind_id=mind_id,
            mind_name=mind_name,
            client_ref=client_ref,
            db=self._db,
        )

        await self._spawn(
            session_id,
            model,
            autopilot=False,
            resume_sid=claude_sid,
            surface_prompt=surface_prompt,
            allowed_directories=allowed_directories,
            soul_file=soul_file,
            mind_id=mind_id,
            is_group_session=(owner_type == "group"),
            client_ref=client_ref,
            owner_type=owner_type,
            owner_ref=owner_ref,
            system_prompt_blocks=system_prompt_blocks,
        )
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

        # If client filtering requested, also check active_sessions.
        # A session that is the current active binding but has status='closed'
        # is treated as INACTIVE — without this guard the bot will keep
        # picking it (e.g. after a rotation that didn't successfully retire
        # the binding) and the conversation can't transition to a new
        # session. Caller should fall through to ensure_session → create.
        if client_type and client_ref:
            active_row = await self._db.execute(
                "SELECT session_id FROM active_sessions WHERE client_type = ? AND client_ref = ?",
                (client_type, client_ref),
            )
            active = await active_row.fetchone()
            active_id = active["session_id"] if active else None
            for s in sessions:
                s["is_active"] = (
                    s["id"] == active_id
                    and s.get("status") != "closed"
                )

        return sessions

    # Surfaces whose sessions a person can pick up somewhere else. A broker
    # or scheduler session is machine-driven — nobody is holding it, and
    # offering it in a chat picker is offering to hijack an errand.
    _ADOPTABLE_OWNER_TYPES = ("web", "terminal")

    @staticmethod
    def _surface_label(owner_type: str) -> str:
        """The human name of a surface, without its mind-uuid suffix."""
        base = (owner_type or "").split(":", 1)[0]
        return "terminal" if base in SessionManager._ADOPTABLE_OWNER_TYPES else base

    async def list_selectable_sessions(
        self,
        owner_ref: str,
        client_type: str | None = None,
        client_ref: str | None = None,
        mind_id: str | None = None,
    ) -> list[dict]:
        """Sessions this surface can switch to — its own, plus live ones
        another surface is holding on the same mind.

        A conversation started in the browser terminal used to be invisible
        from Telegram: the surfaces share the conversation id and the
        transcript, but the session row is scoped by ``owner_ref``, so a
        chat could only ever see what the chat had started. That is the
        whole gap — you had to be back at a browser to continue what you
        started at the desk. Adoptable rows carry ``adoptable: True`` so a
        picker can say where a session is currently living.

        Closed sessions are left out of both halves. A closed session has
        already had its memory harvested and has nothing to switch to, and
        a surface accumulates them without bound — this chat had 138 of
        them, which rendered a picker several times longer than Telegram
        will accept, so the reply was rejected and the command looked dead.
        """
        sessions = [
            s
            for s in await self.list_sessions(
                owner_ref=owner_ref, client_type=client_type, client_ref=client_ref
            )
            if s.get("status") != "closed"
        ]
        for session in sessions:
            session["surface"] = self._surface_label(session.get("owner_type", ""))
            session["adoptable"] = False
        seen = {s["id"] for s in sessions}

        placeholders = ",".join("?" for _ in self._ADOPTABLE_OWNER_TYPES)
        query = (
            f"SELECT * FROM sessions WHERE owner_type IN ({placeholders}) "
            "AND status != 'closed' AND owner_ref != ?"
        )
        params: list = [*self._ADOPTABLE_OWNER_TYPES, owner_ref]
        if mind_id:
            query += " AND mind_id = ?"
            params.append(mind_id)
        query += " ORDER BY last_active DESC"

        rows = await self._db.execute(query, params)
        for row in await rows.fetchall():
            session = dict(row)
            if session["id"] in seen:
                continue
            session["surface"] = self._surface_label(session.get("owner_type", ""))
            session["adoptable"] = True
            session["is_active"] = False
            sessions.append(session)

        return sessions

    async def release_on_mind(self, session_id: str, surface: str) -> bool:
        """Ask the mind to end one surface's process, keeping the session.

        The conversation lives in the transcript, so ending a process is
        not ending the conversation — the next surface resumes it. Minds
        that predate the route simply answer 404, which is not an error
        here: nothing to release is the same outcome as released.
        """
        mind_url = (self._procs.get(session_id) or {}).get("_mind_url")
        if not mind_url:
            mind_url = await self._mind_url_for_session(
                session_id, self._mind_ids.get(session_id)
            )
        if not mind_url:
            log.warning("No mind_url for session %s, cannot release %s", session_id, surface)
            return False

        try:
            import aiohttp
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{mind_url}/sessions/{session_id}/release",
                    params={"surface": surface},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 404:
                        log.info("Mind at %s has no release route — nothing to hand over", mind_url)
                        return False
                    body = await resp.json()
                    return bool(body.get("released"))
        except Exception:
            log.exception("Failed to release %s for session %s", surface, session_id)
            return False

    async def get_active_session(self, client_type: str, client_ref: str) -> dict | None:
        """Get the active session for a client surface, only if it's still
        actually running. A closed session that the active_sessions table
        still points at is treated as no active session — caller must create
        a fresh one. Prevents zombie sessions from being resurrected."""
        row = await self._db.execute(
            """
            SELECT a.session_id
            FROM active_sessions a
            JOIN sessions s ON s.id = a.session_id
            WHERE a.client_type = ? AND a.client_ref = ?
              AND s.status != 'closed'
            """,
            (client_type, client_ref),
        )
        result = await row.fetchone()
        if not result:
            return None
        return await self._session_dict(result["session_id"])

    # ------------------------------------------------------------------
    # Rotation arming (finalize-on-user-turn)
    # ------------------------------------------------------------------
    async def arm_rotation(self, client_type: str, client_ref: str) -> dict:
        """Mark the active session for (client_type, client_ref) as pending
        rotation.

        Called by the per-mind ``rotation_check`` Stop hook once it has
        written the carry-forward, INSTEAD of clearing the session inline.
        The Stop hook fires on an assistant turn, so clearing there can kill
        the old session mid-reply to a message that arrived during the
        rotation window. Arming defers the actual swap to ``send_message``,
        which finalizes it on the next user turn — the rollover always lands
        on the user's turn and never destroys an assistant turn.
        """
        active = await self.get_active_session(client_type, client_ref)
        if not active:
            return {"ok": False, "error": "no active session"}
        await self._db.execute(
            "UPDATE sessions SET rotation_armed = 1 WHERE id = ?", (active["id"],)
        )
        await self._db.commit()
        log.info(
            "armed rotation: session=%s client=%s/%s", active["id"], client_type, client_ref
        )
        return {"ok": True, "session_id": active["id"]}

    async def _finalize_armed_rotation(self, session: dict) -> str | None:
        """Consume a session's armed flag by swapping to a fresh session.

        Kills the armed session (quiescent — its assistant turn already
        Stopped, which is what armed it) and creates its replacement, which
        boots with the carry-forward via ``create_session`` →
        ``bootstrap_loader``. Returns the new session id, or ``None`` when the
        swap can't proceed (no ``client_ref`` to rebind the surface) — in
        which case the caller falls through and delivers on the old session.
        """
        routing = await self._routing_for(session)
        client_ref = routing["client_ref"]
        old_id = session["id"]
        if not client_ref:
            # Without client_ref we can't rebind a new active session to the
            # surface. Disarm and let the caller deliver normally rather than
            # strand the conversation.
            await self._db.execute(
                "UPDATE sessions SET rotation_armed = 0 WHERE id = ?", (old_id,)
            )
            await self._db.commit()
            log.warning(
                "armed rotation: no client_ref for session=%s; disarming, delivering on old session",
                old_id,
            )
            return None
        log.info("armed rotation: finalizing on user turn, retiring session=%s", old_id)
        await self.kill_session(old_id)
        new = await self.create_session(
            owner_type=routing["owner_type"],
            owner_ref=routing["owner_ref"],
            client_ref=client_ref,
            mind_id=session["mind_id"],
            rotated_from=old_id,
        )
        return new["id"]

    async def _forward_to_session(self, session_id: str, content: str, images: list[dict] | None):
        """Re-dispatch a turn to another session, yielding its events.

        A thin seam over ``send_message`` so the armed-rotation redirect in
        ``send_message`` is a single call (and independently testable).
        """
        async for event in self.send_message(session_id, content, images=images):
            yield event

    async def stream_session_events(self, session_id: str):
        """Yield live session events to passive observers.

        Emits a ping event whenever the stream has been idle for
        EVENT_HEARTBEAT_SECONDS — sessions can sit silent for minutes
        between turns, and proxies in front of web surfaces (Cloudflare
        idles out around 100s) sever quiet connections, dropping every
        event published before the observer reconnects.
        """
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
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=self.EVENT_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield {"type": "ping", "session_id": session_id}
                    continue
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
        self, session_id: str, client_type: str, client_ref: str,
        owner_type: str | None = None, owner_ref: str | None = None,
    ) -> dict:
        """Set a session as active on a client surface. Respawn if idle.

        Passing ``owner_type``/``owner_ref`` makes this an *adoption*: a
        session another surface holds moves here. That means ending the
        outgoing process — two harness processes on one conversation each
        hold it in memory, neither sees the other's turns, and both append
        turns the other doesn't know happened — and retargeting the row so
        replies and unsolicited turns follow the session to its new home.
        The conversation itself is untouched: it lives in the transcript,
        and the respawn below resumes it with everything the other surface
        said still in it.
        """
        session = await self._get_row(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        adopting = bool(owner_ref) and (
            session["owner_ref"] != owner_ref or session["owner_type"] != owner_type
        )
        if adopting:
            await self.release_on_mind(session_id, "terminal")
            await self._db.execute(
                "UPDATE sessions SET owner_type = ?, owner_ref = ?, status = 'idle' WHERE id = ?",
                (owner_type, owner_ref, session_id),
            )
            await self._db.execute(
                "DELETE FROM active_sessions WHERE session_id = ?", (session_id,)
            )
            await self._db.commit()
            log.info("Session %s adopted by %s/%s (was %s/%s)", session_id,
                     owner_type, owner_ref, session["owner_type"], session["owner_ref"])
            session = await self._get_row(session_id)

        await self._db.execute(
            """INSERT OR REPLACE INTO active_sessions (client_type, client_ref, session_id)
               VALUES (?, ?, ?)""",
            (client_type, client_ref, session_id),
        )
        await self._db.commit()

        if session["status"] == "idle" and session_id not in self._procs:
            routing = await self._routing_for(session)
            await self._spawn(
                session_id,
                session["model"],
                autopilot=bool(session["autopilot"]),
                resume_sid=session["claude_sid"],
                mind_id=session["mind_id"],
                **routing,
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

            # Refuse to send to deliberately-closed sessions. Without this
            # guard, the respawn path below would resurrect the session
            # (with its old claude_sid as --resume) and the rotation cycle
            # never actually transitions the conversation to a fresh one.
            if session.get("status") == "closed":
                log.info("send_message: refusing closed session=%s — caller must pick up new active session", session_id)
                raise ValueError(f"Session {session_id} is closed")

            # Pending rotation finalizes HERE, on the user's turn. The Stop
            # hook armed this session after writing the carry-forward; swap to
            # a fresh session now and dispatch this turn to it, so the user's
            # message is the new session's first turn. Never fires for
            # un-armed sessions, so normal delivery is unchanged.
            if session.get("rotation_armed"):
                new_id = await self._finalize_armed_rotation(session)
                if new_id and new_id != session_id:
                    async for event in self._forward_to_session(new_id, content, images):
                        yield event
                    return
                # new_id is None → disarmed (no client_ref); fall through and
                # deliver on this session as usual.

            mind_id = session["mind_id"]
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
                routing = await self._routing_for(session)
                await self._spawn(
                    session_id,
                    session["model"],
                    autopilot=bool(session["autopilot"]),
                    resume_sid=session["claude_sid"],
                    mind_id=mind_id,
                    **routing,
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

            await self._publish_session_event(
                session_id,
                {
                    "type": "user",
                    "session_id": session_id,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": content}],
                    },
                },
            )

            await self._db.execute(
                "INSERT INTO session_turns (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                (session_id, content, time.time()),
            )
            await self._db.commit()

            # Route message to mind container via HTTP, stream SSE response
            proc_info = self._procs.get(session_id)
            if not proc_info or not proc_info.get("_mind_url"):
                raise ValueError(f"No mind container URL for session {session_id}")

            mind_url = proc_info["_mind_url"]

            import aiohttp
            retried = False
            _assistant_buf: list[str] = []
            _pinned_sid = session.get("claude_sid") or None
            _sid_warned = False
            while True:
                try:
                    async with aiohttp.ClientSession(read_bufsize=10 * 1024 * 1024) as http:
                        async with http.post(
                            f"{mind_url}/sessions/{session_id}/message",
                            json={"content": stamped_content, "images": images},
                            # Long Claude turns (heavy thinking + tool use) can exceed 10 min.
                            # Cap on no-data-received instead of total elapsed so we don't
                            # truncate legitimate long turns (which the bot then sees as
                            # ClientPayloadError / TransferEncodingError).
                            timeout=aiohttp.ClientTimeout(total=None, sock_read=600),
                        ) as resp:
                            if resp.status == 404:
                                # Session doesn't exist on mind container.
                                # If the session was deliberately closed
                                # (e.g. by rotation), don't resurrect it —
                                # raising lets the caller fall through to
                                # ensure_session and pick up the new one.
                                # Otherwise (idle eviction etc.) respawn once.
                                fresh = await self._get_row(session_id)
                                if fresh and fresh.get("status") == "closed":
                                    log.info(
                                        "Session %s is closed; not respawning (rotation killed it)",
                                        session_id,
                                    )
                                    raise ValueError(
                                        f"Session {session_id} closed — caller should pick up the new active session"
                                    )
                                if not retried:
                                    retried = True
                                    log.info("Session %s not found on %s, respawning", session_id, mind_url)
                                    routing = await self._routing_for(session)
                                    await self._spawn(
                                        session_id, session["model"],
                                        autopilot=bool(session["autopilot"]),
                                        resume_sid=session.get("claude_sid"),
                                        mind_id=mind_id,
                                        **routing,
                                    )
                                    continue
                                raise ValueError(f"Session {session_id} not found after respawn")

                            if resp.status != 200:
                                # Any other error from the mind: surface the
                                # real failure instead of silently ending the
                                # stream (the SSE line-reader below would find
                                # no `data:` lines in a JSON error body and
                                # yield nothing, so the surface shows a
                                # useless generic error).
                                body_text = await resp.text()
                                try:
                                    detail = json.loads(body_text).get("error", body_text)
                                except (json.JSONDecodeError, AttributeError):
                                    detail = body_text
                                detail = detail or f"HTTP {resp.status}"
                                log.error(
                                    "Mind %s returned HTTP %s for session %s: %s",
                                    mind_id, resp.status, session_id, detail,
                                )
                                err_event = {
                                    "type": "result",
                                    "subtype": "error",
                                    "is_error": True,
                                    "result": (
                                        f"ERROR from mind '{mind_id}' "
                                        f"(HTTP {resp.status}): {detail}"
                                    ),
                                }
                                await self._publish_session_event(session_id, err_event)
                                yield err_event
                                return

                            async for line in resp.content:
                                line = line.decode().strip()
                                if not line or not line.startswith("data: "):
                                    continue
                                data = line[6:]  # strip "data: " prefix
                                try:
                                    event = json.loads(data)
                                except json.JSONDecodeError:
                                    continue

                                observer_only = bool(event.pop("_observer_only", False))

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
                                    # Mint the replacement here rather than
                                    # blanking the column and letting the mind
                                    # invent one: a session is never without a
                                    # conversation id, not even for the width
                                    # of a respawn.
                                    fresh_sid = str(uuid.uuid4())
                                    await self._db.execute(
                                        "UPDATE sessions SET claude_sid = ? WHERE id = ?",
                                        (fresh_sid, session_id),
                                    )
                                    await self._db.commit()
                                    _pinned_sid = fresh_sid
                                    _sid_warned = False
                                    routing = await self._routing_for(session)
                                    await self._spawn(
                                        session_id, session["model"],
                                        autopilot=bool(session["autopilot"]),
                                        resume_sid=fresh_sid,
                                        mind_id=mind_id,
                                        **routing,
                                    )
                                    break

                                await self._publish_session_event(session_id, event)
                                if observer_only:
                                    continue
                                yield event

                                # Accumulate assistant text for turn storage
                                if event.get("type") == "assistant":
                                    msg = event.get("message") or {}
                                    _content = msg.get("content") or event.get("content") or event.get("delta") or event.get("text") or ""
                                    if isinstance(_content, list):
                                        _content = "".join(c.get("text", "") for c in _content if isinstance(c, dict) and c.get("type") == "text")
                                    if isinstance(_content, str) and _content:
                                        _assistant_buf.append(_content)

                                now = time.time()
                                await self._db.execute(
                                    "UPDATE sessions SET last_active = ? WHERE id = ?",
                                    (now, session_id),
                                )

                                # The harness echoes the conversation id we
                                # pinned it to. It is not news, and it is not
                                # authority: the row was written at session
                                # creation. Log a divergence, never follow it.
                                event_sid = event.get("session_id")
                                if event_sid and event_sid != _pinned_sid and not _sid_warned:
                                    _sid_warned = True
                                    log.warning(
                                        "Mind %s reported conversation %s for session %s, "
                                        "which is pinned to %s — ignoring",
                                        mind_id, event_sid, session_id, _pinned_sid,
                                    )

                                if event.get("type") == "result":
                                    if _assistant_buf:
                                        await self._db.execute(
                                            "INSERT INTO session_turns (session_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                                            (session_id, "".join(_assistant_buf), now),
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

        routing = await self._routing_for(session)
        await self._spawn(
            session_id,
            model,
            autopilot=bool(session["autopilot"]),
            resume_sid=session["claude_sid"],
            mind_id=session["mind_id"],
            **routing,
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

        routing = await self._routing_for(session)
        await self._spawn(
            session_id,
            session["model"],
            autopilot=bool(new_autopilot),
            resume_sid=session["claude_sid"],
            mind_id=session["mind_id"],
            **routing,
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
        if session.get("owner_type") == "scheduler":
            await self._db.execute("DELETE FROM session_turns WHERE session_id = ?", (session_id,))
            await self._db.execute("DELETE FROM active_sessions WHERE session_id = ?", (session_id,))
            await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        else:
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
    async def _get_mind_row(self, mind_id: str) -> dict:
        """Resolve a mind_id to its broker.minds row, or raise."""
        from comms import broker  # noqa: PLC0415
        if self.broker_db is None:
            raise ValueError("SessionManager.broker_db not wired; call lifespan startup first")
        row = await broker.get_mind_by_id(self.broker_db, mind_id)
        if not row:
            raise ValueError(f"Mind '{mind_id}' not found in broker.minds")
        return row

    async def _spawn(
        self,
        session_id: str,
        model: str,
        autopilot: bool = False,
        resume_sid: str | None = None,
        surface_prompt: str | None = None,
        allowed_directories: list[str] | None = None,
        soul_file: Path | None = None,
        *,
        mind_id: str,
        is_group_session: bool = False,
        client_ref: str | None = None,
        owner_type: str | None = None,
        owner_ref: str | None = None,
        system_prompt_blocks: str = "",
    ) -> Any:
        # Every spawn carries the session's conversation id. A mind handed
        # none would have to invent one, and an invented id is a conversation
        # only that process knows about — the blank-terminal bug in one line.
        if not resume_sid:
            raise ValueError(
                f"refusing to spawn session {session_id} without a conversation id"
            )
        row = await self._get_mind_row(mind_id)
        mind_url = row["gateway_url"]
        mind_name = row["name"]
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
                    "mind_name": mind_name,
                    "client_ref": client_ref,
                    "owner_type": owner_type,
                    "owner_ref": owner_ref,
                    "system_prompt_blocks": system_prompt_blocks,
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

    async def _mind_url_for_session(self, session_id: str, mind_id: str | None = None) -> str | None:
        """Resolve a session's mind base URL from the database.

        The fallback path for anything the in-memory process cache doesn't
        know about.
        """
        if not mind_id:
            row = await self._get_row(session_id)
            mind_id = (row or {}).get("mind_id")
        if not mind_id:
            return None
        try:
            mind_row = await self._get_mind_row(mind_id)
        except Exception:
            log.warning("Cannot resolve mind %s for session %s", mind_id, session_id)
            return None
        return (mind_row or {}).get("gateway_url")

    async def _kill_process(self, session_id: str):
        """Kill a session on its mind container via HTTP."""
        await self.kill_rc_process(session_id)

        proc = self._procs.pop(session_id, None)
        mind_id = self._mind_ids.pop(session_id, None)

        mind_url = (proc or {}).get("_mind_url")
        if not mind_url:
            # Nothing in memory doesn't mean nothing is running. A session
            # born in the browser terminal never went through _spawn, and
            # after a hive-comms restart the cache is empty for every live
            # session. Either way the mind still holds a process (the web
            # terminal's pty outlives its socket by design), so resolve the
            # mind from the DB instead of leaking it.
            mind_url = await self._mind_url_for_session(session_id, mind_id)
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
    async def create_group_session(self, moderator_mind_id: str) -> dict:
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

    async def _routing_for(self, session: dict) -> dict:
        """Resolve (owner_type, owner_ref, client_ref) for an existing session
        so respawn paths inject the same env into the subprocess that the
        original create_session call did. The rotation Stop hook reads
        CLIENT_REF/OWNER_TYPE/OWNER_REF from env; if respawn loses any of
        them, the hook bails on every Stop with "missing client_ref" and
        rotation silently breaks.

        owner_type / owner_ref live on the sessions row. client_ref lives
        on active_sessions, keyed by session_id.
        """
        owner_type = session.get("owner_type") or ""
        owner_ref = session.get("owner_ref") or ""
        client_ref = ""
        try:
            cursor = await self._db.execute(
                "SELECT client_ref FROM active_sessions WHERE session_id = ? LIMIT 1",
                (session["id"],),
            )
            row = await cursor.fetchone()
            if row and row["client_ref"]:
                client_ref = row["client_ref"]
        except Exception:
            log.exception("client_ref lookup failed for session=%s", session.get("id"))
        return {"owner_type": owner_type, "owner_ref": owner_ref, "client_ref": client_ref}

    async def get_session_turns(self, session_id: str) -> list[dict]:
        """Return stored turn history for a session from the session_turns table."""
        cursor = await self._db.execute(
            "SELECT role, content FROM session_turns WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    async def get_late_turns(
        self, client_type: str, client_ref: str, since: float
    ) -> dict:
        """Return the active session's turns committed after a watermark.

        The session-rotation Stop hook records a ``rotation_started_at``
        watermark when it begins its multi-minute Ollama work. Any user
        turn accepted during that window is written to ``session_turns``
        immediately (in ``send_message``) but is invisible to the
        transcript-tail reread if the assistant reply never completed
        before ``/clear``. This is the durable merge source of truth the
        hook queries before clearing: every turn with
        ``created_at > since`` for the surface's currently-active session.

        ``client_type`` is the session's ``owner_type`` (the
        ``active_sessions`` table keys on it). Returns
        ``{"session_id": <id|None>, "turns": [{role, content, created_at}]}``;
        an empty turn list when there is no active session.
        """
        active = await self.get_active_session(client_type, client_ref)
        if not active:
            return {"session_id": None, "turns": []}
        session_id = active["id"]
        cursor = await self._db.execute(
            """SELECT role, content, created_at
                 FROM session_turns
                WHERE session_id = ? AND created_at > ?
                ORDER BY created_at ASC""",
            (session_id, since),
        )
        rows = await cursor.fetchall()
        return {
            "session_id": session_id,
            "turns": [
                {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
                for r in rows
            ],
        }

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
            "mind_id": row.get("mind_id", "ada"),
            "rotated_from": row.get("rotated_from"),
        }
