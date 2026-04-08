"""Unit tests for broker wakeup and response collection."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import aiosqlite


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def broker_db(tmp_path):
    from core.broker import init_db

    db_path = str(tmp_path / "broker.db")
    db = _run(init_db(db_path))
    yield db
    _run(db.close())


def _insert_pending(broker_db, conv_id=None, msg_id=None, from_mind="ada",
                    to_mind="nagatha", msg_num=1, content="do the thing",
                    rolling_summary="", metadata=None):
    from core.broker import insert_message

    return _run(insert_message(
        broker_db,
        message_id=msg_id or str(uuid.uuid4()),
        conversation_id=conv_id or str(uuid.uuid4()),
        from_mind=from_mind,
        to_mind=to_mind,
        message_number=msg_num,
        content=content,
        rolling_summary=rolling_summary,
        metadata=metadata,
        status="pending",
    ))


def _mock_session_mgr(response_text="I did the thing"):
    """Create a mock session manager that yields a normal response."""
    mgr = AsyncMock()
    mgr.create_session = AsyncMock(return_value={
        "id": "sess-callee-1",
        "mind_id": "nagatha",
        "status": "running",
    })

    async def fake_send(session_id, content, **kwargs):
        yield {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": response_text}]},
        }
        yield {"type": "result", "result": response_text, "session_id": "claude-sid-1"}

    mgr.send_message = MagicMock(side_effect=fake_send)
    mgr.kill_session = AsyncMock()
    return mgr


class TestBuildWakeupPrompt:
    def test_first_message_no_summary(self):
        from core.broker import build_wakeup_prompt

        prompt = build_wakeup_prompt(
            from_mind="ada",
            to_mind="nagatha",
            conversation_id="conv-1",
            content="Analyse the logs",
            rolling_summary="",
            message_number=1,
        )
        assert "ada" in prompt
        assert "conv-1" in prompt
        assert "Analyse the logs" in prompt
        assert "Summary of conversation" not in prompt

    def test_followup_message_includes_summary(self):
        from core.broker import build_wakeup_prompt

        prompt = build_wakeup_prompt(
            from_mind="ada",
            to_mind="nagatha",
            conversation_id="conv-1",
            content="Follow up question",
            rolling_summary="Turn 1: Ada asked to analyse logs. Nagatha found 3 errors.",
            message_number=2,
        )
        assert "Summary of conversation" in prompt
        assert "Turn 1: Ada asked to analyse logs" in prompt
        assert "Follow up question" in prompt


class TestWakeupAndCollect:
    def test_creates_session_and_sends_message(self, broker_db):
        from core.broker import wakeup_and_collect, get_message

        msg = _insert_pending(broker_db)
        mgr = _mock_session_mgr()

        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=msg["conversation_id"],
            from_mind="ada", to_mind="nagatha",
            content="do the thing", rolling_summary="",
            message_number=1, metadata=None,
        ))

        mgr.create_session.assert_called_once()
        call_kwargs = mgr.create_session.call_args.kwargs
        assert call_kwargs["owner_type"] == "broker"
        assert call_kwargs["mind_id"] == "nagatha"

        mgr.send_message.assert_called_once()

    def test_writes_response_as_new_message(self, broker_db):
        from core.broker import wakeup_and_collect, get_messages

        conv_id = str(uuid.uuid4())
        msg = _insert_pending(broker_db, conv_id=conv_id)
        mgr = _mock_session_mgr(response_text="Analysis complete")

        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            content="do the thing", rolling_summary="",
            message_number=1, metadata=None,
        ))

        messages = _run(get_messages(broker_db, conv_id))
        assert len(messages) == 2
        response = messages[1]
        assert response["from_mind"] == "nagatha"
        assert response["to_mind"] == "ada"
        assert response["content"] == "Analysis complete"
        assert response["status"] == "completed"

    def test_transitions_status_pending_to_dispatched_to_completed(self, broker_db):
        from core.broker import wakeup_and_collect, get_message

        msg = _insert_pending(broker_db)
        statuses = []

        orig_update = None

        async def tracking_update(db, msg_id, status, **kwargs):
            statuses.append(status)
            await orig_update(db, msg_id, status, **kwargs)

        import core.broker as broker_mod
        orig_update = broker_mod.update_message_status

        mgr = _mock_session_mgr()
        with patch.object(broker_mod, "update_message_status", side_effect=tracking_update):
            _run(wakeup_and_collect(
                broker_db, mgr,
                message_id=msg["id"],
                conversation_id=msg["conversation_id"],
                from_mind="ada", to_mind="nagatha",
                content="do the thing", rolling_summary="",
                message_number=1, metadata=None,
            ))

        assert "dispatched" in statuses
        assert "completed" in statuses
        # dispatched must come before completed
        assert statuses.index("dispatched") < statuses.index("completed")

    def test_kills_callee_session_after_collection(self, broker_db):
        from core.broker import wakeup_and_collect

        msg = _insert_pending(broker_db)
        mgr = _mock_session_mgr()

        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=msg["conversation_id"],
            from_mind="ada", to_mind="nagatha",
            content="do the thing", rolling_summary="",
            message_number=1, metadata=None,
        ))

        mgr.kill_session.assert_called_once_with("sess-callee-1")

    def test_handles_session_creation_failure(self, broker_db):
        from core.broker import wakeup_and_collect, get_message

        msg = _insert_pending(broker_db)
        mgr = AsyncMock()
        mgr.create_session = AsyncMock(side_effect=ValueError("mind not found"))
        mgr.kill_session = AsyncMock()

        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=msg["conversation_id"],
            from_mind="ada", to_mind="nagatha",
            content="do the thing", rolling_summary="",
            message_number=1, metadata=None,
        ))

        updated = _run(get_message(broker_db, msg["id"]))
        assert updated["status"] == "failed"
        assert "mind not found" in updated["response_error"]

    def test_handles_send_message_exception(self, broker_db):
        from core.broker import wakeup_and_collect, get_message

        msg = _insert_pending(broker_db)
        mgr = AsyncMock()
        mgr.create_session = AsyncMock(return_value={"id": "sess-1"})
        mgr.kill_session = AsyncMock()

        async def failing_send(session_id, content, **kwargs):
            raise RuntimeError("subprocess crashed")
            yield  # noqa: make it an async generator

        mgr.send_message = MagicMock(side_effect=failing_send)

        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=msg["conversation_id"],
            from_mind="ada", to_mind="nagatha",
            content="do the thing", rolling_summary="",
            message_number=1, metadata=None,
        ))

        updated = _run(get_message(broker_db, msg["id"]))
        assert updated["status"] == "failed"
        assert "subprocess crashed" in updated["response_error"]

    def test_handles_empty_response(self, broker_db):
        from core.broker import wakeup_and_collect, get_messages

        conv_id = str(uuid.uuid4())
        msg = _insert_pending(broker_db, conv_id=conv_id)
        mgr = AsyncMock()
        mgr.create_session = AsyncMock(return_value={"id": "sess-1"})
        mgr.kill_session = AsyncMock()

        async def empty_send(session_id, content, **kwargs):
            yield {"type": "result", "result": ""}

        mgr.send_message = MagicMock(side_effect=empty_send)

        _run(wakeup_and_collect(
            broker_db, mgr,
            message_id=msg["id"],
            conversation_id=conv_id,
            from_mind="ada", to_mind="nagatha",
            content="do the thing", rolling_summary="",
            message_number=1, metadata=None,
        ))

        messages = _run(get_messages(broker_db, conv_id))
        assert len(messages) == 2
        assert messages[1]["status"] == "completed"

    def test_backstop_timeout(self, broker_db):
        from core.broker import wakeup_and_collect, get_message

        msg = _insert_pending(broker_db, metadata={"request_type": "quick_query"})
        mgr = AsyncMock()
        mgr.create_session = AsyncMock(return_value={"id": "sess-1"})
        mgr.kill_session = AsyncMock()

        async def hanging_send(session_id, content, **kwargs):
            await asyncio.sleep(999)
            yield {"type": "result", "result": "never"}  # pragma: no cover

        mgr.send_message = MagicMock(side_effect=hanging_send)

        # Patch backstop to 0.1 seconds for test speed
        with patch("core.broker.get_backstop_seconds", return_value=0.1):
            _run(wakeup_and_collect(
                broker_db, mgr,
                message_id=msg["id"],
                conversation_id=msg["conversation_id"],
                from_mind="ada", to_mind="nagatha",
                content="do the thing", rolling_summary="",
                message_number=1, metadata={"request_type": "quick_query"},
            ))

        updated = _run(get_message(broker_db, msg["id"]))
        assert updated["status"] == "timed_out"
        assert "backstop exceeded" in updated["response_error"]
        mgr.kill_session.assert_called_once_with("sess-1")
