"""Tests for minds/ada/config.yaml."""

from pathlib import Path

import yaml


class TestAdaConfig:
    """Verify Ada's per-mind config file."""

    def test_ada_config_yaml_exists(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "ada" / "config.yaml"
        assert path.exists(), f"Expected {path} to exist"

    def test_ada_config_yaml_has_required_fields(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "ada" / "config.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert "backend" in data
        assert "model" in data
        assert "soul_node" in data
        assert "roles" in data

    def test_ada_config_backend_is_cli_claude(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "ada" / "config.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["backend"] == "cli_claude"
