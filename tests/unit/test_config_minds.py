"""Tests for the minds block in HiveMindConfig."""

import yaml
from unittest.mock import patch

from config import PROJECT_DIR


class TestConfigMindsAttribute:
    """Step 1: HiveMindConfig has a minds field that parses from YAML."""

    def test_config_has_minds_attribute(self):
        from config import HiveMindConfig

        instance = HiveMindConfig()
        assert hasattr(instance, "minds")
        assert instance.minds == {}

    def test_config_minds_parsed_from_yaml(self):
        from config import HiveMindConfig

        fake_yaml = {
            "minds": {
                "ada": {"backend": "cli_claude", "model": "sonnet", "soul": "souls/ada.md", "db": "data/ada.db"},
                "nagatha": {"backend": "sdk_claude", "model": "sonnet", "soul": "souls/nagatha.md", "db": "data/nagatha.db"},
                "skippy": {"backend": "ollama", "model": "llama3", "soul": "souls/skippy.md", "db": "data/skippy.db"},
            }
        }
        with patch("config._yaml_config", fake_yaml):
            cfg = HiveMindConfig.from_yaml()
        assert "ada" in cfg.minds
        assert "nagatha" in cfg.minds
        assert "skippy" in cfg.minds
        assert cfg.minds["ada"]["backend"] == "cli_claude"
        assert cfg.minds["nagatha"]["model"] == "sonnet"
        assert cfg.minds["skippy"]["soul"] == "souls/skippy.md"

    def test_config_minds_defaults_to_empty_dict(self):
        from config import HiveMindConfig

        with patch("config._yaml_config", {}):
            cfg = HiveMindConfig.from_yaml()
        assert cfg.minds == {}


class TestConfigYamlMindsBlock:
    """Step 2: config.yaml contains a valid minds block."""

    def _load_config_yaml(self) -> dict:
        config_path = PROJECT_DIR / "config.yaml"
        with open(config_path) as f:
            return yaml.safe_load(f)

    def test_config_yaml_has_valid_minds_block(self):
        data = self._load_config_yaml()
        assert "minds" in data
        assert "ada" in data["minds"]
        assert "nagatha" in data["minds"]
        assert "skippy" in data["minds"]

    def test_each_mind_has_required_fields(self):
        data = self._load_config_yaml()
        required = {"backend", "model", "soul", "db"}
        for name, mind in data["minds"].items():
            assert required.issubset(mind.keys()), f"Mind '{name}' missing fields: {required - set(mind.keys())}"

    def test_ada_mind_config_values(self):
        data = self._load_config_yaml()
        ada = data["minds"]["ada"]
        assert ada["backend"] == "cli_claude"
        assert ada["model"] == "sonnet"
        assert ada["soul"] == "souls/ada.md"
        assert ada["db"] == "data/ada.db"

    def test_nagatha_mind_config_values(self):
        data = self._load_config_yaml()
        nagatha = data["minds"]["nagatha"]
        assert nagatha["backend"] == "sdk_claude"
        assert nagatha["model"] == "sonnet"
        assert nagatha["soul"] == "souls/nagatha.md"
        assert nagatha["db"] == "data/nagatha.db"

    def test_skippy_mind_config_values(self):
        data = self._load_config_yaml()
        skippy = data["minds"]["skippy"]
        assert skippy["backend"] == "ollama"
        assert skippy["model"] == "llama3"
        assert skippy["soul"] == "souls/skippy.md"
        assert skippy["db"] == "data/skippy.db"

    def test_bob_mind_config_values(self):
        data = self._load_config_yaml()
        assert "bob" in data["minds"]
        bob = data["minds"]["bob"]
        assert bob["backend"] == "cli_ollama"
        assert bob["model"] == "gpt-oss:20b-32k"
        assert bob["soul"] == "souls/bob.md"
        assert bob["db"] == "data/bob.db"
