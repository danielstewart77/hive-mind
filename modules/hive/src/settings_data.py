"""Local per-mind config discovery for the /settings mind registry.

Containerized minds (ada/bob/bilby/nagatha/hex-bot) keep a `runtime.yaml`
under hive_mind's `minds/<name>/`; an edge mind on this host keeps its own
`runtime.yaml` under its hive-edge-minds checkout, mounted at
`/minds_edge/<name>/`. Both are mounted into this container — see
docker-compose.yml. A mind with neither is remote: no local file to show,
only whatever the broker reports.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

HIVE_MIND_MINDS_DIR = Path("/minds_hive_mind")
EDGE_MINDS_DIR = Path("/minds_edge")
HIVE_MIND_MINDS_VOICE_DIR = Path("/minds_hive_mind_voice")
HIVE_TOOLS_REPO_DIR = Path("/hive_tools_repo")
HIVE_INIT_DIR = Path("/hive_init")

_TOOL_CATALOG = {
    "browser": ("Browser", "Web", "Browse and interact with web applications."),
    "calendar": ("Calendar", "Productivity", "Read and manage calendar events."),
    "docker_ops": ("Docker", "Host operations", "Inspect and manage approved services."),
    "gmail": ("Gmail", "Communication", "Search, read, and prepare email."),
    "linkedin": ("LinkedIn", "Communication", "Publish approved professional updates."),
    "ollama": ("Local AI", "Local AI", "Run structured and conversational local models."),
    "schema": ("Knowledge schema", "Knowledge", "Propose new knowledge graph types."),
    "sms": ("SMS", "Communication", "Send approved text messages."),
}

_SECRET_KEY_MARKERS = ("API_KEY", "PASSWORD", "SECRET", "TOKEN", "AUTH")

# hive-init's own allowlist, mirrored here rather than imported since the
# two repos don't share a package — see spark_to_bloom's former
# HIVE_INIT_ALLOWED_FILES for the pattern this was lifted from.
DOWNLOAD_ALLOWED_FILES = {
    "hive-init.py": "text/x-python",
    "hive-init.sh": "text/x-shellscript",
}

_WAV_MAX_BYTES = 20 * 1024 * 1024  # ~10s of clean speech is a few hundred KB; 20MB is generous headroom


def _find_local_config(short_name: str) -> dict[str, Any] | None:
    """Look up a mind's local config file by its short name, either mount."""
    for runtime_path in (
        HIVE_MIND_MINDS_DIR / short_name / "runtime.yaml",
        EDGE_MINDS_DIR / short_name / "runtime.yaml",
    ):
        if not runtime_path.is_file():
            continue
        try:
            config = yaml.safe_load(runtime_path.read_text()) or {}
        except yaml.YAMLError as exc:
            return {"source": "runtime.yaml", "path": str(runtime_path), "error": str(exc)}
        return {"source": "runtime.yaml", "path": str(runtime_path), "config": config, "body": ""}

    return None


def _redact_config(value: Any, key: str = "") -> Any:
    """Return browser-safe config while preserving useful non-secret settings."""
    normalized_key = key.upper()
    if any(marker in normalized_key for marker in _SECRET_KEY_MARKERS):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: _redact_config(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _browser_safe_local_config(local: dict[str, Any] | None) -> dict[str, Any] | None:
    if local is None:
        return None
    return {
        **local,
        "config": _redact_config(local.get("config") or {}),
    }


def build_mind_registry(broker_minds: list[dict]) -> list[dict]:
    """Merge broker-reported minds with whatever local config file exists.

    Each row carries the broker's live identity (id, gateway_url, model,
    harness, last_seen) plus, when found, the full local config — which is
    the more authoritative source for harness/model/voice/backstory since
    the broker only knows what was last registered, not the file's current
    contents.
    """
    rows = []
    for m in broker_minds:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or ""
        local = _browser_safe_local_config(_find_local_config(name)) if name else None
        rows.append({
            "name": name,
            "id": m.get("id", ""),
            "gateway_url": m.get("gateway_url", ""),
            "model": m.get("model", ""),
            "harness": m.get("harness", ""),
            "registered_at": m.get("registered_at"),
            "last_seen": m.get("last_seen"),
            "local_config": local,
        })
    rows.sort(key=lambda r: r["name"])
    return rows


def hive_tools_status() -> dict[str, Any]:
    """Whether the hive-tools repo is present on this host.

    A missing host path is auto-created empty by compose's bind mount, so
    "directory exists but has no entries" is treated the same as "not
    cloned" — there is currently no git remote configured for hive-tools
    to clone *from*, so that case is surfaced but not actionable yet.
    """
    if not HIVE_TOOLS_REPO_DIR.is_dir():
        return {"present": False, "path": str(HIVE_TOOLS_REPO_DIR), "capabilities": []}
    entries = list(HIVE_TOOLS_REPO_DIR.iterdir())
    tools_dir = HIVE_TOOLS_REPO_DIR / "tools"
    capabilities = []
    if tools_dir.is_dir():
        for path in sorted(tools_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue
            tool_id = path.stem
            name, category, description = _TOOL_CATALOG.get(
                tool_id,
                (tool_id.replace("_", " ").title(), "Other", "Hive capability."),
            )
            capabilities.append(
                {
                    "id": tool_id,
                    "name": name,
                    "category": category,
                    "description": description,
                }
            )
    return {
        "present": bool(entries),
        "path": str(HIVE_TOOLS_REPO_DIR),
        "entry_count": len(entries),
        "capabilities": capabilities,
    }


def voice_ref_status(short_name: str) -> dict[str, Any] | None:
    """Whether a mind has a chatterbox voice_ref.wav, and its stats."""
    path = HIVE_MIND_MINDS_DIR / short_name / "voice_ref.wav"
    if not path.is_file():
        return None
    stat = path.stat()
    return {"path": str(path), "size_bytes": stat.st_size, "modified_at": stat.st_mtime}


def save_voice_ref(short_name: str, data: bytes) -> Path:
    """Write a mind's voice_ref.wav to the read-write mind mount.

    Validated only at the RIFF/WAVE header level — chatterbox is picky
    about format but does its own real validation on synthesis; this is
    just enough to reject an obviously-wrong upload (a PNG, an mp3) before
    it lands on disk.
    """
    if not short_name or not re.match(r"^[A-Za-z0-9_-]+$", short_name):
        raise ValueError(f"invalid mind name: {short_name!r}")
    if len(data) > _WAV_MAX_BYTES:
        raise ValueError(f"file too large: {len(data)} bytes (max {_WAV_MAX_BYTES})")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file (missing RIFF/WAVE header)")

    mind_dir = HIVE_MIND_MINDS_VOICE_DIR / short_name
    mind_dir.mkdir(parents=True, exist_ok=True)
    target = mind_dir / "voice_ref.wav"
    target.write_bytes(data)
    return target


def list_downloads() -> list[dict[str, Any]]:
    """Installer assets available from hive-init, for the downloads tab."""
    downloads = []
    for filename, content_type in DOWNLOAD_ALLOWED_FILES.items():
        path = HIVE_INIT_DIR / filename
        if not path.is_file():
            continue
        stat = path.stat()
        downloads.append({
            "filename": filename,
            "content_type": content_type,
            "size_bytes": stat.st_size,
            "modified_at": stat.st_mtime,
        })
    return downloads


def resolve_download_path(filename: str) -> Path | None:
    """Resolve a requested download filename against the allowlist, safely."""
    if filename not in DOWNLOAD_ALLOWED_FILES:
        return None
    path = (HIVE_INIT_DIR / filename).resolve()
    if path.parent != HIVE_INIT_DIR.resolve() or not path.is_file():
        return None
    return path
