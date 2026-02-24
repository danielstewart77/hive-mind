"""
Hive Mind — Session manager.

Owns all Claude CLI subprocesses and the session database.
Each session maps to one claude -p subprocess in stream-json mode.
"""

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from pathlib import Path

import aiosqlite

from config import PROJECT_DIR, config
from models import ModelRegistry, Provider

log = logging.getLogger("hive-mind.sessions")

_MCP_CONTAINER = PROJECT_DIR / ".mcp.container.json"
MCP_CONFIG = str(_MCP_CONTAINER if _MCP_CONTAINER.exists() else PROJECT_DIR / ".mcp.json")
_SOUL_FILE = PROJECT_DIR / "soul.md"
_SPECS_DIR = PROJECT_DIR / "specs"

HIVE_MIND_PROMPT = (
    "You are Hive Mind, a personal assistant. Keep responses concise. Use markdown formatting.\n\n"
    f"Read {_SOUL_FILE} at the start of each session. "
    "Update it when you experience something that meaningfully shapes your identity or preferences. "
    "Keep it extremely short — it is a soul, not a manifesto. Prune ruthlessly. No formatting, no elaboration. Every line must earn its place or be cut.\n\n"
    f"If a request seems security-sensitive, read {_SPECS_DIR / 'security.md'} before proceeding."
)

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
    status        TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS active_sessions (
    client_type   TEXT NOT NULL,
    client_ref    TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    PRIMARY KEY (client_type, client_ref)
);
"""


class SessionManager:
    def __init__(self, model_registry: ModelRegistry):
        self._registry = model_registry
        self._db: aiosqlite.Connection | None = None
        self._procs: dict[str, asyncio.subprocess.Process] = {}
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
    ) -> dict:
        """Create a new session, spawn process, return session info."""
        model = model or config.default_model
        session_id = str(uuid.uuid4())
        now = time.time()

        await self._db.execute(
            """INSERT INTO sessions (id, owner_type, owner_ref, model, created_at, last_active, status)
               VALUES (?, ?, ?, ?, ?, ?, 'running')""",
            (session_id, owner_type, owner_ref, model, now, now),
        )
        # Activate on this client surface
        await self._db.execute(
            """INSERT OR REPLACE INTO active_sessions (client_type, client_ref, session_id)
               VALUES (?, ?, ?)""",
            (owner_type, client_ref, session_id),
        )
        await self._db.commit()

        await self._spawn(session_id, model, autopilot=False)
        log.info("Created session %s (model=%s, owner=%s)", session_id, model, owner_ref)
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
            )
            await self._db.execute(
                "UPDATE sessions SET status = 'running' WHERE id = ?", (session_id,)
            )
            await self._db.commit()

        return await self._session_dict(session_id)

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------
    async def send_message(self, session_id: str, content: str):
        """Send a message and yield NDJSON response events."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = await self._get_row(session_id)
            if not session:
                raise ValueError(f"Session not found: {session_id}")

            # Respawn if needed
            if session_id not in self._procs or self._procs[session_id].returncode is not None:
                await self._spawn(
                    session_id,
                    session["model"],
                    autopilot=bool(session["autopilot"]),
                    resume_sid=session["claude_sid"],
                )
                await self._db.execute(
                    "UPDATE sessions SET status = 'running' WHERE id = ?",
                    (session_id,),
                )
                await self._db.commit()

            proc = self._procs[session_id]

            # Write user message as NDJSON
            msg = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": content},
            }) + "\n"
            proc.stdin.write(msg.encode())
            await proc.stdin.drain()

            # Mark active NOW so the idle reaper doesn't kill us mid-response
            await self._db.execute(
                "UPDATE sessions SET last_active = ?, status = 'running' WHERE id = ?",
                (time.time(), session_id),
            )
            await self._db.commit()

            # Update summary from first message if still default
            if session["summary"] == "New session":
                summary = content[:100].strip()
                await self._db.execute(
                    "UPDATE sessions SET summary = ? WHERE id = ?",
                    (summary, session_id),
                )
                await self._db.commit()

            # Read response lines until "result"
            async for line in proc.stdout:
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                yield event

                if event.get("type") == "result":
                    claude_sid = event.get("session_id")
                    if claude_sid:
                        await self._db.execute(
                            "UPDATE sessions SET claude_sid = ?, last_active = ? WHERE id = ?",
                            (claude_sid, time.time(), session_id),
                        )
                        await self._db.commit()
                    break

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
    ) -> asyncio.subprocess.Process:
        cmd = [
            "claude", "-p",
            "--verbose",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--permission-mode", "bypassPermissions",
            "--model", model,
            "--mcp-config", MCP_CONFIG,
            "--append-system-prompt", HIVE_MIND_PROMPT,
        ]
        if autopilot:
            cmd.append("--dangerously-skip-permissions")
            cmd.extend(["--max-budget-usd", str(config.autopilot_guards.max_budget_usd)])
        if resume_sid:
            cmd.extend(["--resume", resume_sid])

        provider = self._registry.get_provider(model)
        env = os.environ.copy()
        env.update(provider.env_overrides)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(PROJECT_DIR),
        )
        self._procs[session_id] = proc
        log.info(
            "Spawned claude process for session %s (pid=%d, model=%s, autopilot=%s, resume=%s)",
            session_id, proc.pid, model, autopilot, resume_sid or "no",
        )
        return proc

    async def _kill_process(self, session_id: str):
        """SIGTERM a subprocess, SIGKILL after 5s grace period."""
        proc = self._procs.pop(session_id, None)
        if proc and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                log.info("Killed process for session %s", session_id)
            except ProcessLookupError:
                pass

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------
    async def _idle_reaper(self):
        """Kill sessions idle beyond timeout. Runs every 60s."""
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
        }
