"""Unit tests for epilogue exception detection and notification."""

from unittest.mock import patch

from core.epilogue import (
    EpilogueDigest,
    EpilogueException,
    SessionMetrics,
    check_exceptions,
    format_exception_notification,
)


def _make_digest(novel_entity_count: int = 0) -> EpilogueDigest:
    return EpilogueDigest(
        session_id="test-session-id",
        summary="Test summary",
        memories=[],
        entities=[],
        metrics=SessionMetrics(
            turn_count=5,
            duration_minutes=15.0,
            novel_entity_count=novel_entity_count,
        ),
    )


class TestCheckExceptions:
    """Tests for check_exceptions() function."""

    def test_no_exceptions_returns_empty_list(self) -> None:
        digest = _make_digest(novel_entity_count=2)
        result = check_exceptions(digest, write_errors=0, total_writes=5)
        assert result == []

    def test_high_novel_entity_count_triggers_exception(self) -> None:
        digest = _make_digest(novel_entity_count=11)
        result = check_exceptions(digest)
        assert len(result) == 1
        assert result[0].trigger == "high_novel_entities"

    def test_novel_entity_count_at_boundary_no_exception(self) -> None:
        digest = _make_digest(novel_entity_count=10)
        result = check_exceptions(digest)
        assert result == []

    def test_high_error_rate_triggers_exception(self) -> None:
        digest = _make_digest()
        result = check_exceptions(digest, write_errors=3, total_writes=4)
        assert len(result) == 1
        assert result[0].trigger == "high_error_rate"

    def test_low_error_rate_no_exception(self) -> None:
        digest = _make_digest()
        result = check_exceptions(digest, write_errors=1, total_writes=10)
        assert result == []

    def test_zero_writes_no_error_rate_exception(self) -> None:
        digest = _make_digest()
        result = check_exceptions(digest, write_errors=0, total_writes=0)
        assert result == []

    def test_multiple_exceptions_returned(self) -> None:
        digest = _make_digest(novel_entity_count=11)
        result = check_exceptions(digest, write_errors=3, total_writes=4)
        assert len(result) == 2
        triggers = {e.trigger for e in result}
        assert triggers == {"high_novel_entities", "high_error_rate"}


class TestFormatExceptionNotification:
    """Tests for format_exception_notification() function."""

    def test_format_exception_notification(self) -> None:
        exceptions = [
            EpilogueException(trigger="high_novel_entities", detail="11 novel entities found"),
            EpilogueException(trigger="high_error_rate", detail="3/4 writes failed"),
        ]
        message = format_exception_notification("test-session-id-12345", exceptions)
        assert "test-ses" in message  # truncated session id
        assert "high_novel_entities" in message
        assert "high_error_rate" in message
        assert "11 novel entities found" in message
        assert "3/4 writes failed" in message


class TestNotifyException:
    """Tests for _notify_exception() function."""

    @patch("core.epilogue._hitl_request")
    def test_notify_exception_calls_hitl_request(self, mock_hitl) -> None:
        from core.epilogue import _notify_exception

        mock_hitl.return_value = False  # doesn't matter, fire-and-forget
        exceptions = [
            EpilogueException(trigger="high_novel_entities", detail="11 novel entities"),
        ]
        _notify_exception("test-session-id", exceptions)
        mock_hitl.assert_called_once()
        call_args = mock_hitl.call_args[0][0]
        assert "high_novel_entities" in call_args

    @patch("core.epilogue._hitl_request", side_effect=RuntimeError("connection refused"))
    def test_notify_exception_failure_does_not_raise(self, mock_hitl) -> None:
        from core.epilogue import _notify_exception

        exceptions = [
            EpilogueException(trigger="high_error_rate", detail="3/4 writes failed"),
        ]
        # Should not raise
        _notify_exception("test-session-id", exceptions)
