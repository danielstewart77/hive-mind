"""Tests for minds/nagatha/config.yaml."""

from pathlib import Path

import yaml


class TestNagathaConfig:
    """Verify Nagatha's per-mind config file."""

    def test_nagatha_config_yaml_exists(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "nagatha" / "config.yaml"
        assert path.exists(), f"Expected {path} to exist"

    def test_nagatha_config_yaml_has_required_fields(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "nagatha" / "config.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert "backend" in data
        assert "model" in data
        assert "soul_node" in data
        assert "roles" in data

    def test_nagatha_config_backend_is_sdk_claude(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "nagatha" / "config.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["backend"] == "sdk_claude"
