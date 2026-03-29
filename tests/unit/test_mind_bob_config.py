"""Tests for minds/bob/config.yaml."""

from pathlib import Path

import yaml


class TestBobConfig:
    """Verify Bob's per-mind config file."""

    def test_bob_config_yaml_exists(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "bob" / "config.yaml"
        assert path.exists(), f"Expected {path} to exist"

    def test_bob_config_yaml_has_required_fields(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "bob" / "config.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert "backend" in data
        assert "model" in data
        assert "soul_node" in data
        assert "roles" in data

    def test_bob_config_backend_is_cli_ollama(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "bob" / "config.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["backend"] == "cli_ollama"

    def test_bob_config_model_is_gpt_oss(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "bob" / "config.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["model"] == "gpt-oss:20b-32k"

    def test_bob_config_soul_node_is_bob(self):
        path = Path(__file__).resolve().parents[2] / "minds" / "bob" / "config.yaml"
        with open(path) as f:
            data = yaml.safe_load(f)
        assert data["soul_node"] == "Bob"
