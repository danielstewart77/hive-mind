"""Tests for group_chat config in config.py and config.yaml."""

from pathlib import Path

import yaml


class TestGroupChatConfig:
    """Verify group_chat configuration."""

    def test_config_has_group_chat_attribute(self):
        from config import HiveMindConfig
        cfg = HiveMindConfig()
        assert hasattr(cfg, "group_chat")

    def test_config_yaml_has_group_chat_block(self):
        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert "group_chat" in data

    def test_group_chat_config_has_default_moderator(self):
        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert data["group_chat"]["default_moderator"] == "ada"

    def test_group_chat_config_has_available_minds(self):
        config_path = Path(__file__).resolve().parents[2] / "config.yaml"
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert "ada" in data["group_chat"]["available_minds"]
        assert "nagatha" in data["group_chat"]["available_minds"]
