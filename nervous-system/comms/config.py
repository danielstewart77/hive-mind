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
            data = _yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_yaml_data = _load_yaml_config()
_DEFAULT_PROVIDERS: dict = _yaml_data.get("providers", {})


@dataclass
class _Config:
    server_port: int = int(os.environ.get("COMMS_PORT", "8424"))
    # Which upstream hosts which model is the inference proxy's business;
    # the gateway carries no model table of its own.
    providers: dict = field(default_factory=lambda: dict(_DEFAULT_PROVIDERS))


config = _Config()


def admin_credential_owner() -> str:
    """The one person whose minds may still be sent this gateway's admin
    credential, or "" for nobody.

    `COMMS_ADMIN_CREDENTIAL_OWNER`, else `admin_credential_owner` in
    comms/config.yaml (read fresh, so setting it needs only a restart). Only
    for a mind of theirs still running code that refuses its own session
    credential on the routes the gateway calls; every other mind is sent its
    own credential and nothing else (DECIDED 2026-11-15, Daniel: a mind not
    owned by him never reaches his credentials).
    """
    if "COMMS_ADMIN_CREDENTIAL_OWNER" in os.environ:
        # Set, even to nothing, it decides: empty is nobody.
        named = os.environ["COMMS_ADMIN_CREDENTIAL_OWNER"]
    else:
        named = str(_load_yaml_config().get("admin_credential_owner") or "")
    return named.strip().lower()
