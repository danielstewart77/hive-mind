"""
Hive Mind — Message broker data layer.

SQLite-backed storage for inter-mind conversations and messages.
Pure data operations — no FastAPI dependency, no session manager dependency.
"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import aiosqlite

log = logging.getLogger("hive-mind.broker")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                    TEXT PRIMARY KEY,
    conversation_id       TEXT NOT NULL REFERENCES conversations(id),
    from_mind             TEXT NOT NULL,
    to_mind               TEXT NOT NULL,
    message_number        INTEGER NOT NULL,
    content               TEXT NOT NULL,
    rolling_summary       TEXT DEFAULT '',
    metadata              TEXT,
    status                TEXT NOT NULL DEFAULT 'pending',
    recipient_session_id  TEXT,
    response_error        TEXT,
    timestamp             REAL NOT NULL,
    UNIQUE(conversation_id, message_number)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
"""

# ---------------------------------------------------------------------------
# Collection backstop — 8x notification threshold per request_type
# ---------------------------------------------------------------------------
BACKSTOP_SECONDS: dict[str, int] = {
    "quick_query": 2400,           # 40 min
    "research": 9600,              # 160 min
    "code_review": 9600,           # 160 min
    "content_generation": 7200,    # 120 min
    "data_analysis": 9600,         # 160 min
    "security_triage": 14400,      # 240 min
    "security_remediation": 43200, # 720 min
}

_DEFAULT_BACKSTOP = 21600  # 6 hours


def get_backstop_seconds(request_type: str | None) -> int:
    """Look up the collection backstop for a request_type."""
    if request_type is None:
        return _DEFAULT_BACKSTOP
    return BACKSTOP_SECONDS.get(request_type, _DEFAULT_BACKSTOP)


# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------
async def init_db(db_path: str) -> aiosqlite.Connection:
    """Connect to (or create) the broker SQLite database."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA)
    await db.commit()
    log.info("broker: db initialized at %s", db_path)
    return db


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
async def create_conversation(db: aiosqlite.Connection, conversation_id: str) -> None:
    """Insert a new conversation row."""
    await db.execute(
        "INSERT INTO conversations (id, created_at) VALUES (?, ?)",
        (conversation_id, time.time()),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
async def insert_message(
    db: aiosqlite.Connection,
    *,
    message_id: str,
    conversation_id: str,
    from_mind: str,
    to_mind: str,
    message_number: int,
    content: str,
    rolling_summary: str,
    metadata: dict | None,
    status: str,
) -> dict:
    """Insert a message. Idempotent on message_id — returns existing row if duplicate.

    Auto-creates the conversation row if it doesn't exist.
    """
    # Check for existing message (idempotency)
    row = await db.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
    existing = await row.fetchone()
    if existing:
        result = dict(existing)
        result["existing"] = True
        return result

    # Auto-create conversation
    await db.execute(
        "INSERT OR IGNORE INTO conversations (id, created_at) VALUES (?, ?)",
        (conversation_id, time.time()),
    )

    metadata_json = json.dumps(metadata) if metadata else None
    await db.execute(
        """INSERT INTO messages
           (id, conversation_id, from_mind, to_mind, message_number, content,
            rolling_summary, metadata, status, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (message_id, conversation_id, from_mind, to_mind, message_number,
         content, rolling_summary, metadata_json, status, time.time()),
    )
    await db.commit()

    row = await db.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
    fetched = await row.fetchone()
    result = dict(fetched) if fetched else {"id": message_id}
    result["existing"] = False
    return result


async def get_messages(db: aiosqlite.Connection, conversation_id: str) -> list[dict]:
    """Get all messages for a conversation, ordered by message_number."""
    rows = await db.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY message_number",
        (conversation_id,),
    )
    return [dict(r) for r in await rows.fetchall()]


async def get_message(db: aiosqlite.Connection, message_id: str) -> dict | None:
    """Get a single message by ID."""
    row = await db.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
    result = await row.fetchone()
    return dict(result) if result else None


async def update_message_status(
    db: aiosqlite.Connection,
    message_id: str,
    status: str,
    *,
    recipient_session_id: str | None = None,
    response_error: str | None = None,
) -> None:
    """Update message status and optional fields."""
    fields = ["status = ?"]
    params: list = [status]
    if recipient_session_id is not None:
        fields.append("recipient_session_id = ?")
        params.append(recipient_session_id)
    if response_error is not None:
        fields.append("response_error = ?")
        params.append(response_error)
    params.append(message_id)
    await db.execute(
        f"UPDATE messages SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    await db.commit()


async def get_next_message_number(db: aiosqlite.Connection, conversation_id: str) -> int:
    """Get the next message number for a conversation."""
    row = await db.execute(
        "SELECT COALESCE(MAX(message_number), 0) + 1 AS next_num FROM messages WHERE conversation_id = ?",
        (conversation_id,),
    )
    result = await row.fetchone()
    return result["next_num"] if result else 1


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------
async def get_stranded_messages(db: aiosqlite.Connection) -> dict:
    """Find messages stranded in non-terminal status."""
    pending_rows = await db.execute(
        "SELECT * FROM messages WHERE status = 'pending'"
    )
    dispatched_rows = await db.execute(
        "SELECT * FROM messages WHERE status = 'dispatched'"
    )
    return {
        "pending": [dict(r) for r in await pending_rows.fetchall()],
        "dispatched": [dict(r) for r in await dispatched_rows.fetchall()],
    }


async def recover_stranded_messages(db: aiosqlite.Connection) -> list[dict]:
    """Recover stranded messages on startup.

    - dispatched → failed (callee session is dead after restart)
    - Returns pending messages for re-dispatch by the caller.
    """
    # Fail dispatched messages
    await db.execute(
        "UPDATE messages SET status = 'failed', response_error = 'server_restart_during_delivery' "
        "WHERE status = 'dispatched'"
    )
    await db.commit()

    # Return pending for re-dispatch
    rows = await db.execute("SELECT * FROM messages WHERE status = 'pending'")
    pending = [dict(r) for r in await rows.fetchall()]

    count_row = await (await db.execute(
        "SELECT COUNT(*) as cnt FROM messages WHERE status = 'failed' AND response_error = 'server_restart_during_delivery'"
    )).fetchone()
    dispatched_count = count_row["cnt"] if count_row else 0

    log.info("broker: startup recovery pending=%d dispatched_failed=%d", len(pending), dispatched_count)
    return pending


# ---------------------------------------------------------------------------
# Wakeup prompt
# ---------------------------------------------------------------------------
def build_wakeup_prompt(
    from_mind: str,
    to_mind: str,
    conversation_id: str,
    content: str,
    rolling_summary: str,
    message_number: int,
) -> str:
    """Construct the wakeup prompt sent to the callee's new session."""
    parts = [f"You have a new message from {from_mind}.", "", f"Conversation ID: {conversation_id}", ""]

    if message_number > 1 and rolling_summary:
        parts.extend([
            "Summary of conversation so far:",
            rolling_summary,
            "",
        ])

    parts.extend([
        "New message:",
        content,
        "",
        "Complete the requested work and respond with your findings. Your response will be",
        f"automatically collected and delivered back to {from_mind}.",
    ])

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response collection
# ---------------------------------------------------------------------------
async def _collect_response(send_generator) -> str:
    """Iterate an SSE async generator and extract assistant text.

    Same extraction logic as tools/stateful/inter_mind.py:70-86.
    """
    response_text = ""
    async for event in send_generator:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    response_text += block.get("text", "")
        elif event.get("type") == "result":
            if not response_text:
                response_text = event.get("result", "")
    return response_text


# ---------------------------------------------------------------------------
# Wakeup and collect
# ---------------------------------------------------------------------------
async def wakeup_and_collect(
    db: aiosqlite.Connection,
    session_mgr,
    *,
    message_id: str,
    conversation_id: str,
    from_mind: str,
    to_mind: str,
    content: str,
    rolling_summary: str,
    message_number: int,
    metadata: dict | None,
) -> None:
    """Background task: wake callee, collect response, write to DB.

    1. Update message to dispatched
    2. Create callee session
    3. Send wakeup prompt and collect SSE response (with backstop timeout)
    4. Write response as new message
    5. Update original message to completed
    6. Kill callee session
    """
    request_type = (metadata or {}).get("request_type")
    backstop = get_backstop_seconds(request_type)
    session_id = None

    try:
        # 1. Dispatched
        await update_message_status(db, message_id, "dispatched")

        # 2. Create callee session
        session = await session_mgr.create_session(
            owner_type="broker",
            owner_ref=f"broker-{conversation_id}",
            client_ref=f"broker-{conversation_id}-{to_mind}",
            mind_id=to_mind,
        )
        session_id = session["id"]
        await update_message_status(db, message_id, "dispatched", recipient_session_id=session_id)

        # 3. Send wakeup and collect response
        prompt = build_wakeup_prompt(from_mind, to_mind, conversation_id, content, rolling_summary, message_number)
        response_text = await asyncio.wait_for(
            _collect_response(session_mgr.send_message(session_id, prompt)),
            timeout=backstop,
        )

        # 4. Write response as new message
        next_num = await get_next_message_number(db, conversation_id)
        await insert_message(
            db,
            message_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            from_mind=to_mind,
            to_mind=from_mind,
            message_number=next_num,
            content=response_text,
            rolling_summary="",
            metadata=None,
            status="completed",
        )

        # 5. Mark original as completed
        await update_message_status(db, message_id, "completed")
        log.info("broker: wakeup complete conversation=%s from=%s to=%s", conversation_id, from_mind, to_mind)

    except asyncio.TimeoutError:
        await update_message_status(
            db, message_id, "timed_out",
            response_error=f"backstop exceeded ({backstop}s)",
        )
        log.warning("broker: backstop timeout conversation=%s message=%s backstop=%ds", conversation_id, message_id, backstop)

    except Exception as e:
        await update_message_status(
            db, message_id, "failed",
            response_error=str(e),
        )
        log.error("broker: wakeup failed conversation=%s message=%s error=%s", conversation_id, message_id, e)

    finally:
        # 6. Kill callee session
        if session_id is not None:
            try:
                await session_mgr.kill_session(session_id)
            except Exception:
                log.warning("broker: failed to kill callee session=%s", session_id)
