"""Unit tests for EpilogueThresholds configuration."""

from config import EpilogueThresholds, HiveMindConfig


class TestEpilogueThresholdsDefaults:
    """Test that EpilogueThresholds has correct defaults."""

    def test_epilogue_thresholds_defaults(self) -> None:
        t = EpilogueThresholds()
        assert t.max_turns == 20
        assert t.max_duration_minutes == 60
        assert t.max_novel_entities == 5

    def test_epilogue_thresholds_custom_values(self) -> None:
        t = EpilogueThresholds(max_turns=10, max_duration_minutes=30, max_novel_entities=3)
        assert t.max_turns == 10
        assert t.max_duration_minutes == 30
        assert t.max_novel_entities == 3


class TestConfigHasEpilogueThresholds:
    """Test that HiveMindConfig includes epilogue_thresholds."""

    def test_config_has_epilogue_thresholds(self) -> None:
        cfg = HiveMindConfig()
        assert hasattr(cfg, "epilogue_thresholds")
        assert isinstance(cfg.epilogue_thresholds, EpilogueThresholds)
