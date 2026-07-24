"""Comms config — env-driven with YAML model/provider registry.

Loads providers and model→provider mappings from comms/config.yaml when
present. Falls back to empty dicts so existing behaviour is preserved in
environments without the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml as _yaml

PROJECT_DIR = Path(os.environ.get("COMMS_PROJECT_DIR", "/app")).resolve()

_CONFIG_YAML = Path(__file__).parent / "config.yaml"


def _load_yaml_config() -> dict:
    if not _CONFIG_YAML.exists():
        return {}
    try:
        with _CONFIG_YAML.open() as f:
            return _yaml.safe_load(f) or {}
    except Exception:
        return {}


_yaml_data = _load_yaml_config()
_DEFAULT_PROVIDERS: dict = _yaml_data.get("providers", {})
_DEFAULT_MODELS: dict = _yaml_data.get("models", {})


@dataclass
class _Config:
    server_port: int = int(os.environ.get("COMMS_PORT", "8424"))
    providers: dict = field(default_factory=lambda: dict(_DEFAULT_PROVIDERS))
    models: dict = field(default_factory=lambda: dict(_DEFAULT_MODELS))


config = _Config()
