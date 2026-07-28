import json
import logging

from core.hive_logging import HiveJsonFormatter, log_event


def test_formatter_emits_json_event_and_redacts_nested_secrets(monkeypatch):
    monkeypatch.setenv("HIVE_SERVICE", "test-service")
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "changed", (), None)
    record.hive_event = "session.created"
    record.hive_fields = {
        "session_id": "s-1",
        "authorization": "Bearer abc",
        "nested": {"api_token": "secret", "safe": True},
    }

    data = json.loads(HiveJsonFormatter().format(record))

    assert data["service"] == "test-service"
    assert data["event"] == "session.created"
    assert data["session_id"] == "s-1"
    assert data["authorization"] == "[REDACTED]"
    assert data["nested"] == {"api_token": "[REDACTED]", "safe": True}


def test_log_event_is_fail_open_for_unprintable_field(monkeypatch):
    class Broken:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    logger = logging.getLogger("test-fail-open")
    monkeypatch.setattr(logger, "log", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError()))

    assert log_event(logger, "test.event", broken=Broken()) is None
