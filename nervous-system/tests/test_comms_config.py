"""Tests for comms/config.py YAML loading and ModelRegistry wiring."""
from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from comms.models import ModelRegistry, Provider


# ---------------------------------------------------------------------------
# ModelRegistry unit tests (no file I/O needed)
# ---------------------------------------------------------------------------

def _make_registry(static_models: dict) -> ModelRegistry:
    providers = {"anthropic": Provider(name="anthropic")}
    return ModelRegistry(providers=providers, static_models=static_models)


def test_registry_resolves_sonnet():
    reg = _make_registry({"sonnet": "anthropic", "opus": "anthropic", "haiku": "anthropic"})
    assert reg.get_provider("sonnet").name == "anthropic"


def test_registry_resolves_opus():
    reg = _make_registry({"sonnet": "anthropic", "opus": "anthropic", "haiku": "anthropic"})
    assert reg.get_provider("opus").name == "anthropic"


def test_registry_resolves_haiku():
    reg = _make_registry({"sonnet": "anthropic", "opus": "anthropic", "haiku": "anthropic"})
    assert reg.get_provider("haiku").name == "anthropic"


def test_registry_raises_on_unknown_model():
    reg = _make_registry({"sonnet": "anthropic"})
    with pytest.raises(ValueError, match="Unknown model: nope"):
        reg.get_provider("nope")


def test_registry_empty_raises_on_any_alias():
    """Reproduces the original bug: empty registry raises on session's current model."""
    reg = _make_registry({})
    with pytest.raises(ValueError, match="Unknown model: sonnet"):
        reg.get_provider("sonnet")


# ---------------------------------------------------------------------------
# Config YAML loading
# ---------------------------------------------------------------------------

def test_config_loads_models_from_yaml(tmp_path):
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(
        "providers:\n  anthropic: {}\nmodels:\n  sonnet: anthropic\n  opus: anthropic\n"
    )
    import comms.config as cfg_module
    with patch.object(cfg_module, "_CONFIG_YAML", yaml_file):
        data = cfg_module._load_yaml_config()
    assert data["models"] == {"sonnet": "anthropic", "opus": "anthropic"}
    assert "anthropic" in data["providers"]


def test_config_returns_empty_when_file_missing(tmp_path):
    import comms.config as cfg_module
    missing = tmp_path / "nonexistent.yaml"
    with patch.object(cfg_module, "_CONFIG_YAML", missing):
        data = cfg_module._load_yaml_config()
    assert data == {}


def test_config_models_is_dict():
    """config.models must be a dict, not a list — ModelRegistry._static requires dict."""
    from comms.config import config
    assert isinstance(config.models, dict)


def test_config_providers_is_dict():
    from comms.config import config
    assert isinstance(config.providers, dict)
