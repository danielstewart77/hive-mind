"""Unit tests for EpilogueDigest formatting."""

from core.epilogue import EpilogueDigest, SessionMetrics, format_digest_for_telegram


def _make_digest(
    summary: str = "Test session summary",
    memories: list | None = None,
    entities: list | None = None,
    turn_count: int = 5,
    duration_minutes: float = 15.0,
    novel_entity_count: int = 1,
) -> EpilogueDigest:
    return EpilogueDigest(
        session_id="test-session-id",
        summary=summary,
        memories=memories or [],
        entities=entities or [],
        metrics=SessionMetrics(
            turn_count=turn_count,
            duration_minutes=duration_minutes,
            novel_entity_count=novel_entity_count,
        ),
    )


class TestFormatDigestForTelegram:
    """Tests for format_digest_for_telegram() function."""

    def test_includes_summary(self) -> None:
        digest = _make_digest(summary="Discussed project architecture")
        result = format_digest_for_telegram(digest)
        assert "Discussed project architecture" in result

    def test_includes_memory_count(self) -> None:
        digest = _make_digest(memories=[
            {"content": "Memory 1", "data_class": "observation"},
            {"content": "Memory 2", "data_class": "observation"},
            {"content": "Memory 3", "data_class": "observation"},
        ])
        result = format_digest_for_telegram(digest)
        assert "3" in result

    def test_includes_entity_count(self) -> None:
        digest = _make_digest(entities=[
            {"entity_type": "Person", "name": "Alice"},
            {"entity_type": "Project", "name": "Hive Mind"},
        ])
        result = format_digest_for_telegram(digest)
        assert "2" in result

    def test_includes_metrics(self) -> None:
        digest = _make_digest(turn_count=12, duration_minutes=45.0)
        result = format_digest_for_telegram(digest)
        assert "12" in result
        assert "45" in result

    def test_truncates_long_content(self) -> None:
        long_summary = "A" * 5000
        digest = _make_digest(summary=long_summary)
        result = format_digest_for_telegram(digest)
        assert len(result) <= 4000

    def test_empty_memories_and_entities(self) -> None:
        digest = _make_digest(memories=[], entities=[])
        result = format_digest_for_telegram(digest)
        assert isinstance(result, str)
        assert len(result) > 0
