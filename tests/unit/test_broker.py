"""Unit tests for core/broker.py — broker data layer."""

import asyncio
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest
import aiosqlite


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def broker_db(tmp_path):
    """Create a temporary broker DB and return the connection."""
    from core.broker import init_db

    db_path = str(tmp_path / "broker.db")
    db = _run(init_db(db_path))
    yield db
    _run(db.close())


class TestInitDb:
    def test_init_db_creates_tables(self, tmp_path):
        from core.broker import init_db

        db_path = str(tmp_path / "broker.db")
        db = _run(init_db(db_path))
        try:
            # Check tables exist via raw sqlite3
            conn = sqlite3.connect(db_path)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            conn.close()
            assert "conversations" in tables
            assert "messages" in tables
        finally:
            _run(db.close())

    def test_init_db_creates_indexes(self, tmp_path):
        from core.broker import init_db

        db_path = str(tmp_path / "broker.db")
        db = _run(init_db(db_path))
        try:
            conn = sqlite3.connect(db_path)
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            conn.close()
            assert "idx_messages_conversation_id" in indexes
            assert "idx_messages_status" in indexes
        finally:
            _run(db.close())


class TestCreateConversation:
    def test_create_conversation_inserts_row(self, broker_db):
        from core.broker import create_conversation

        conv_id = str(uuid.uuid4())
        _run(create_conversation(broker_db, conv_id))

        row = _run(broker_db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)))
        result = _run(row.fetchone())
        assert result is not None
        assert result["id"] == conv_id
        assert result["created_at"] > 0


class TestInsertMessage:
    def _make_msg_kwargs(self, **overrides):
        defaults = {
            "message_id": str(uuid.uuid4()),
            "conversation_id": str(uuid.uuid4()),
            "from_mind": "ada",
            "to_mind": "nagatha",
            "message_number": 1,
            "content": "test message",
            "rolling_summary": "",
            "metadata": None,
            "status": "pending",
        }
        defaults.update(overrides)
        return defaults

    def test_insert_message_inserts_row(self, broker_db):
        from core.broker import insert_message

        kwargs = self._make_msg_kwargs()
        _run(insert_message(broker_db, **kwargs))

        row = _run(broker_db.execute("SELECT * FROM messages WHERE id = ?", (kwargs["message_id"],)))
        result = _run(row.fetchone())
        assert result is not None
        assert result["from_mind"] == "ada"
        assert result["to_mind"] == "nagatha"
        assert result["content"] == "test message"
        assert result["status"] == "pending"
        assert result["recipient_session_id"] is None
        assert result["response_error"] is None
        assert result["timestamp"] > 0

    def test_insert_message_auto_creates_conversation(self, broker_db):
        from core.broker import insert_message

        conv_id = str(uuid.uuid4())
        kwargs = self._make_msg_kwargs(conversation_id=conv_id)
        _run(insert_message(broker_db, **kwargs))

        row = _run(broker_db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)))
        result = _run(row.fetchone())
        assert result is not None

    def test_insert_message_duplicate_id_is_idempotent(self, broker_db):
        from core.broker import insert_message

        msg_id = str(uuid.uuid4())
        kwargs = self._make_msg_kwargs(message_id=msg_id)
        result1 = _run(insert_message(broker_db, **kwargs))

        # Insert again with same id
        result2 = _run(insert_message(broker_db, **kwargs))

        # Should return existing row, not create a duplicate
        assert result2["id"] == msg_id
        assert result2.get("existing") is True

        # Verify only one row
        rows = _run(broker_db.execute("SELECT COUNT(*) as cnt FROM messages WHERE id = ?", (msg_id,)))
        count = _run(rows.fetchone())
        assert count["cnt"] == 1

    def test_insert_message_duplicate_message_number_rejected(self, broker_db):
        from core.broker import insert_message

        conv_id = str(uuid.uuid4())
        kwargs1 = self._make_msg_kwargs(conversation_id=conv_id, message_number=1)
        _run(insert_message(broker_db, **kwargs1))

        kwargs2 = self._make_msg_kwargs(conversation_id=conv_id, message_number=1)
        with pytest.raises(Exception):
            _run(insert_message(broker_db, **kwargs2))


class TestGetMessages:
    def _make_msg_kwargs(self, **overrides):
        defaults = {
            "message_id": str(uuid.uuid4()),
            "conversation_id": str(uuid.uuid4()),
            "from_mind": "ada",
            "to_mind": "nagatha",
            "message_number": 1,
            "content": "test message",
            "rolling_summary": "",
            "metadata": None,
            "status": "pending",
        }
        defaults.update(overrides)
        return defaults

    def test_get_messages_returns_ordered_by_message_number(self, broker_db):
        from core.broker import insert_message, get_messages

        conv_id = str(uuid.uuid4())
        # Insert out of order
        for num in [3, 1, 2]:
            _run(insert_message(broker_db, **self._make_msg_kwargs(
                conversation_id=conv_id,
                message_number=num,
                content=f"msg-{num}",
            )))

        messages = _run(get_messages(broker_db, conv_id))
        assert len(messages) == 3
        assert [m["message_number"] for m in messages] == [1, 2, 3]

    def test_get_messages_filters_by_conversation_id(self, broker_db):
        from core.broker import insert_message, get_messages

        conv_a = str(uuid.uuid4())
        conv_b = str(uuid.uuid4())
        _run(insert_message(broker_db, **self._make_msg_kwargs(conversation_id=conv_a, content="a")))
        _run(insert_message(broker_db, **self._make_msg_kwargs(conversation_id=conv_b, content="b")))

        messages = _run(get_messages(broker_db, conv_a))
        assert len(messages) == 1
        assert messages[0]["content"] == "a"


class TestUpdateMessageStatus:
    def test_update_message_status(self, broker_db):
        from core.broker import insert_message, update_message_status, get_message

        msg_id = str(uuid.uuid4())
        _run(insert_message(broker_db, message_id=msg_id, conversation_id=str(uuid.uuid4()),
                            from_mind="ada", to_mind="nagatha", message_number=1,
                            content="test", rolling_summary="", metadata=None, status="pending"))

        _run(update_message_status(broker_db, msg_id, "dispatched"))
        msg = _run(get_message(broker_db, msg_id))
        assert msg["status"] == "dispatched"

    def test_update_message_status_with_error(self, broker_db):
        from core.broker import insert_message, update_message_status, get_message

        msg_id = str(uuid.uuid4())
        _run(insert_message(broker_db, message_id=msg_id, conversation_id=str(uuid.uuid4()),
                            from_mind="ada", to_mind="nagatha", message_number=1,
                            content="test", rolling_summary="", metadata=None, status="pending"))

        _run(update_message_status(broker_db, msg_id, "failed", response_error="session creation failed"))
        msg = _run(get_message(broker_db, msg_id))
        assert msg["status"] == "failed"
        assert msg["response_error"] == "session creation failed"


class TestGetNextMessageNumber:
    def test_get_next_message_number_starts_at_1(self, broker_db):
        from core.broker import get_next_message_number

        conv_id = str(uuid.uuid4())
        num = _run(get_next_message_number(broker_db, conv_id))
        assert num == 1

    def test_get_next_message_number_increments(self, broker_db):
        from core.broker import insert_message, get_next_message_number

        conv_id = str(uuid.uuid4())
        for n in [1, 2]:
            _run(insert_message(broker_db, message_id=str(uuid.uuid4()),
                                conversation_id=conv_id, from_mind="ada", to_mind="nagatha",
                                message_number=n, content=f"msg-{n}", rolling_summary="",
                                metadata=None, status="completed"))

        num = _run(get_next_message_number(broker_db, conv_id))
        assert num == 3


class TestRecoverStrandedMessages:
    def _insert_msg(self, broker_db, status, conv_id=None):
        from core.broker import insert_message

        msg_id = str(uuid.uuid4())
        _run(insert_message(broker_db, message_id=msg_id,
                            conversation_id=conv_id or str(uuid.uuid4()),
                            from_mind="ada", to_mind="nagatha", message_number=1,
                            content="test", rolling_summary="", metadata=None,
                            status=status))
        return msg_id

    def test_recover_stranded_pending_returns_messages(self, broker_db):
        from core.broker import get_stranded_messages

        msg_id = self._insert_msg(broker_db, "pending")
        stranded = _run(get_stranded_messages(broker_db))
        pending_ids = [m["id"] for m in stranded["pending"]]
        assert msg_id in pending_ids

    def test_recover_stranded_dispatched_marks_failed(self, broker_db):
        from core.broker import recover_stranded_messages, get_message

        msg_id = self._insert_msg(broker_db, "dispatched")
        _run(recover_stranded_messages(broker_db))

        msg = _run(get_message(broker_db, msg_id))
        assert msg["status"] == "failed"
        assert "server_restart" in msg["response_error"]


class TestBackstopSeconds:
    def test_quick_query_backstop(self):
        from core.broker import get_backstop_seconds
        assert get_backstop_seconds("quick_query") == 2400  # 40 min

    def test_security_remediation_backstop(self):
        from core.broker import get_backstop_seconds
        assert get_backstop_seconds("security_remediation") == 43200  # 720 min

    def test_unknown_type_returns_default(self):
        from core.broker import get_backstop_seconds
        assert get_backstop_seconds("unknown_type") == 21600  # 6 hr

    def test_none_type_returns_default(self):
        from core.broker import get_backstop_seconds
        assert get_backstop_seconds(None) == 21600


class TestMindsTable:
    """Tests for the broker minds table."""

    def test_init_db_creates_minds_table(self, tmp_path):
        from core.broker import init_db

        db_path = str(tmp_path / "broker.db")
        db = _run(init_db(db_path))
        try:
            conn = sqlite3.connect(db_path)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            conn.close()
            assert "minds" in tables
        finally:
            _run(db.close())

    def test_register_mind_inserts_row(self, broker_db):
        from core.broker import register_mind

        _run(register_mind(
            broker_db,
            mind_id="ada",
            gateway_url="http://hive_mind:8420",
            model="sonnet",
            harness="claude_cli_claude",
        ))

        row = _run(broker_db.execute("SELECT * FROM minds WHERE mind_id = ?", ("ada",)))
        result = _run(row.fetchone())
        assert result is not None
        assert result["mind_id"] == "ada"
        assert result["gateway_url"] == "http://hive_mind:8420"
        assert result["model"] == "sonnet"
        assert result["harness"] == "claude_cli_claude"
        assert result["registered_at"] > 0
        assert result["last_seen"] > 0

    def test_register_mind_upsert_updates_last_seen(self, broker_db):
        from core.broker import register_mind

        _run(register_mind(
            broker_db,
            mind_id="ada",
            gateway_url="http://hive_mind:8420",
            model="sonnet",
            harness="claude_cli_claude",
        ))

        # Read original timestamps
        row = _run(broker_db.execute("SELECT registered_at, last_seen FROM minds WHERE mind_id = ?", ("ada",)))
        first = _run(row.fetchone())
        original_registered_at = first["registered_at"]

        # Register again (upsert)
        import time
        time.sleep(0.01)  # ensure time difference
        _run(register_mind(
            broker_db,
            mind_id="ada",
            gateway_url="http://hive_mind:8420",
            model="sonnet",
            harness="claude_cli_claude",
        ))

        row = _run(broker_db.execute("SELECT registered_at, last_seen FROM minds WHERE mind_id = ?", ("ada",)))
        second = _run(row.fetchone())
        assert second["registered_at"] == original_registered_at  # unchanged
        assert second["last_seen"] >= first["last_seen"]  # updated

    def test_get_registered_minds_returns_all(self, broker_db):
        from core.broker import register_mind, get_registered_minds

        _run(register_mind(broker_db, mind_id="ada", gateway_url="http://hive_mind:8420",
                           model="sonnet", harness="claude_cli_claude"))
        _run(register_mind(broker_db, mind_id="bob", gateway_url="http://hive_mind:8420",
                           model="gpt-oss:20b-32k", harness="claude_cli_ollama"))

        minds = _run(get_registered_minds(broker_db))
        assert len(minds) == 2
        ids = [m["mind_id"] for m in minds]
        assert "ada" in ids
        assert "bob" in ids

    def test_get_registered_minds_empty_table(self, broker_db):
        from core.broker import get_registered_minds

        minds = _run(get_registered_minds(broker_db))
        assert minds == []
