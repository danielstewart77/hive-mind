"""Integration tests for broker round-trip: POST → wakeup → collect → GET."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def broker_db(tmp_path):
    from core.broker import init_db

    db_path = str(tmp_path / "broker.db")
    db = _run(init_db(db_path))
    yield db
    _run(db.close())


def _mock_session_mgr(response_text="task done"):
    mgr = AsyncMock()
    mgr.create_session = AsyncMock(return_value={
        "id": f"sess-{uuid.uuid4().hex[:8]}",
        "mind_id": "nagatha", "status": "running",
    })

    async def fake_send(session_id, content, **kwargs):
        yield {"type": "assistant", "message": {"content": [{"type": "text", "text": response_text}]}}
        yield {"type": "result", "result": response_text}

    mgr.send_message = MagicMock(side_effect=fake_send)
    mgr.kill_session = AsyncMock()
    return mgr


class TestBrokerRoundTrip:
    def test_dispatches_and_collects_response(self, broker_db):
        from core.broker import insert_message, get_messages, wakeup_and_collect

        conv_id = str(uuid.uuid4())
        msg = _run(insert_message(
            broker_db,
            message_id=str(uuid.uuid4()),
            conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            message_number=1,
            content="analyse the logs",
            rolling_summary="",
            metadata=json.dumps({"request_type": "quick_query"}),
            status="pending",
        ))

        mgr = _mock_session_mgr("Found 3 errors in the logs")
        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            content="analyse the logs",
            rolling_summary="",
            message_number=1,
            metadata={"request_type": "quick_query"},
        ))

        messages = _run(get_messages(broker_db, conv_id))
        assert len(messages) == 2

        request = messages[0]
        assert request["from_mind"] == "ada"
        assert request["status"] == "completed"

        response = messages[1]
        assert response["from_mind"] == "nagatha"
        assert response["to_mind"] == "ada"
        assert response["content"] == "Found 3 errors in the logs"
        assert response["status"] == "completed"

    def test_handles_wakeup_failure(self, broker_db):
        from core.broker import insert_message, get_message, wakeup_and_collect

        msg = _run(insert_message(
            broker_db,
            message_id=str(uuid.uuid4()),
            conversation_id=str(uuid.uuid4()),
            from_mind="ada", to_mind="nagatha",
            message_number=1, content="test",
            rolling_summary="", metadata=None, status="pending",
        ))

        mgr = AsyncMock()
        mgr.create_session = AsyncMock(side_effect=RuntimeError("connection refused"))
        mgr.kill_session = AsyncMock()

        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=msg["conversation_id"],
            from_mind="ada", to_mind="nagatha",
            content="test", rolling_summary="",
            message_number=1, metadata=None,
        ))

        updated = _run(get_message(broker_db, msg["id"]))
        assert updated["status"] == "failed"
        assert "connection refused" in updated["response_error"]

    def test_handles_callee_exception(self, broker_db):
        from core.broker import insert_message, get_message, wakeup_and_collect

        msg = _run(insert_message(
            broker_db,
            message_id=str(uuid.uuid4()),
            conversation_id=str(uuid.uuid4()),
            from_mind="ada", to_mind="nagatha",
            message_number=1, content="test",
            rolling_summary="", metadata=None, status="pending",
        ))

        mgr = AsyncMock()
        mgr.create_session = AsyncMock(return_value={"id": "sess-x"})
        mgr.kill_session = AsyncMock()

        async def crashing_send(session_id, content, **kwargs):
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}
            raise RuntimeError("callee crashed mid-response")

        mgr.send_message = MagicMock(side_effect=crashing_send)

        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=msg["conversation_id"],
            from_mind="ada", to_mind="nagatha",
            content="test", rolling_summary="",
            message_number=1, metadata=None,
        ))

        updated = _run(get_message(broker_db, msg["id"]))
        assert updated["status"] == "failed"
        assert "callee crashed" in updated["response_error"]

    def test_multi_turn_preserves_conversation(self, broker_db):
        from core.broker import insert_message, get_messages, wakeup_and_collect

        conv_id = str(uuid.uuid4())

        # Turn 1
        msg1 = _run(insert_message(
            broker_db,
            message_id=str(uuid.uuid4()),
            conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            message_number=1, content="first question",
            rolling_summary="", metadata=None, status="pending",
        ))
        mgr = _mock_session_mgr("first answer")
        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg1["id"], conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            content="first question", rolling_summary="",
            message_number=1, metadata=None,
        ))

        # Turn 3 (ada follow-up)
        msg3 = _run(insert_message(
            broker_db,
            message_id=str(uuid.uuid4()),
            conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            message_number=3, content="follow-up question",
            rolling_summary="Turn 1: ada asked first question. Nagatha answered: first answer.",
            metadata=None, status="pending",
        ))
        mgr2 = _mock_session_mgr("follow-up answer")
        _run(wakeup_and_collect(
            broker_db, mgr2,
            message_id=msg3["id"], conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            content="follow-up question",
            rolling_summary="Turn 1: ada asked first question. Nagatha answered: first answer.",
            message_number=3, metadata=None,
        ))

        messages = _run(get_messages(broker_db, conv_id))
        assert len(messages) == 4
        assert [m["message_number"] for m in messages] == [1, 2, 3, 4]

    def test_idempotent_post_no_duplicate(self, broker_db):
        from core.broker import insert_message, get_messages

        conv_id = str(uuid.uuid4())
        msg_id = str(uuid.uuid4())

        r1 = _run(insert_message(
            broker_db,
            message_id=msg_id, conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            message_number=1, content="hello",
            rolling_summary="", metadata=None, status="pending",
        ))
        assert r1["existing"] is False

        r2 = _run(insert_message(
            broker_db,
            message_id=msg_id, conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            message_number=1, content="hello",
            rolling_summary="", metadata=None, status="pending",
        ))
        assert r2["existing"] is True

        messages = _run(get_messages(broker_db, conv_id))
        assert len(messages) == 1


class TestStartupRecovery:
    def test_redispatches_pending(self, broker_db):
        from core.broker import insert_message, get_message, wakeup_and_collect, recover_stranded_messages

        msg = _run(insert_message(
            broker_db,
            message_id=str(uuid.uuid4()),
            conversation_id=str(uuid.uuid4()),
            from_mind="ada", to_mind="nagatha",
            message_number=1, content="stranded task",
            rolling_summary="", metadata=None, status="pending",
        ))

        # Simulate startup recovery
        pending = _run(recover_stranded_messages(broker_db))
        assert any(m["id"] == msg["id"] for m in pending)

        # Re-dispatch
        mgr = _mock_session_mgr("recovered response")
        for m in pending:
            _run(wakeup_and_collect(
                broker_db, mgr,
                message_id=m["id"],
                conversation_id=m["conversation_id"],
                from_mind=m["from_mind"], to_mind=m["to_mind"],
                content=m["content"], rolling_summary=m.get("rolling_summary", ""),
                message_number=m["message_number"], metadata=None,
            ))

        updated = _run(get_message(broker_db, msg["id"]))
        assert updated["status"] == "completed"

    def test_fails_dispatched(self, broker_db):
        from core.broker import insert_message, get_message, recover_stranded_messages, update_message_status

        msg = _run(insert_message(
            broker_db,
            message_id=str(uuid.uuid4()),
            conversation_id=str(uuid.uuid4()),
            from_mind="ada", to_mind="nagatha",
            message_number=1, content="in-flight task",
            rolling_summary="", metadata=None, status="pending",
        ))
        # Simulate it was dispatched before crash
        _run(update_message_status(broker_db, msg["id"], "dispatched"))

        _run(recover_stranded_messages(broker_db))

        updated = _run(get_message(broker_db, msg["id"]))
        assert updated["status"] == "failed"
        assert "server_restart" in updated["response_error"]
