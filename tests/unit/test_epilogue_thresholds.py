"""Unit tests for session metrics and threshold checking."""

from config import EpilogueThresholds
from core.epilogue import SessionMetrics, exceeds_threshold


class TestExceedsThreshold:
    """Tests for exceeds_threshold() function."""

    def test_below_all_thresholds_returns_false(self) -> None:
        metrics = SessionMetrics(turn_count=5, duration_minutes=15.0, novel_entity_count=1)
        assert exceeds_threshold(metrics, EpilogueThresholds()) is False

    def test_exceeds_turn_count_returns_true(self) -> None:
        metrics = SessionMetrics(turn_count=25, duration_minutes=15.0, novel_entity_count=1)
        assert exceeds_threshold(metrics, EpilogueThresholds()) is True

    def test_exceeds_duration_returns_true(self) -> None:
        metrics = SessionMetrics(turn_count=5, duration_minutes=90.0, novel_entity_count=1)
        assert exceeds_threshold(metrics, EpilogueThresholds()) is True

    def test_exceeds_entity_count_returns_true(self) -> None:
        metrics = SessionMetrics(turn_count=5, duration_minutes=15.0, novel_entity_count=8)
        assert exceeds_threshold(metrics, EpilogueThresholds()) is True

    def test_at_exact_threshold_returns_false(self) -> None:
        metrics = SessionMetrics(turn_count=20, duration_minutes=60.0, novel_entity_count=5)
        assert exceeds_threshold(metrics, EpilogueThresholds()) is False

    def test_custom_thresholds(self) -> None:
        metrics = SessionMetrics(turn_count=11, duration_minutes=15.0, novel_entity_count=1)
        thresholds = EpilogueThresholds(max_turns=10)
        assert exceeds_threshold(metrics, thresholds) is True
