"""Tests for group_chat config in config.py and config.yaml.example.

The structural contract is pinned against the tracked example config; a
deployment's actual roster lives in its gitignored config.yaml and is not a
repo concern.
"""

from pathlib import Path

import yaml

EXAMPLE = Path(__file__).resolve().parents[2] / "config.yaml.example"


class TestGroupChatConfig:
    """Verify group_chat configuration."""

    def test_config_has_group_chat_attribute(self):
        from config import HiveMindConfig
        cfg = HiveMindConfig()
        assert hasattr(cfg, "group_chat")

    def test_example_config_has_group_chat_block(self):
        data = yaml.safe_load(EXAMPLE.read_text())
        assert "group_chat" in data

    def test_example_group_chat_declares_a_moderator(self):
        data = yaml.safe_load(EXAMPLE.read_text())
        assert data["group_chat"]["default_moderator"]

    def test_example_group_chat_declares_available_minds(self):
        data = yaml.safe_load(EXAMPLE.read_text())
        minds = data["group_chat"]["available_minds"]
        assert isinstance(minds, list) and minds
