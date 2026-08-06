"""Tests for comms/config.py YAML loading."""
from __future__ import annotations

from unittest.mock import patch


def test_config_loads_providers_from_yaml(tmp_path):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text("providers:\n  anthropic: {}\n  ollama:\n    api_base: http://x:11434\n")
    import comms.config as cfg_module
    with patch.object(cfg_module, "_CONFIG_YAML", yaml_file):
        data = cfg_module._load_yaml_config()
    assert set(data["providers"]) == {"anthropic", "ollama"}


def test_config_returns_empty_when_file_missing(tmp_path):
    import comms.config as cfg_module
    missing = tmp_path / "nonexistent.yaml"
    with patch.object(cfg_module, "_CONFIG_YAML", missing):
        data = cfg_module._load_yaml_config()
    assert data == {}


def test_config_providers_is_dict():
    from comms.config import config
    assert isinstance(config.providers, dict)
