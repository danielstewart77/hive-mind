"""Local per-mind config discovery for the /settings mind registry.

Containerized minds (ada/bob/bilby/nagatha/hex-bot) keep a `runtime.yaml`
under hive_mind's `minds/<name>/`; bare-metal Skippy keeps a `MIND.md`
(YAML frontmatter + prose body) under his own repo's `minds/<name>/`.
Both directories are mounted read-only into this container — see
docker-compose.yml. A mind with neither is remote: no local file to show,
only whatever the broker reports.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

HIVE_MIND_MINDS_DIR = Path("/minds_hive_mind")
SKIPPY_MINDS_DIR = Path("/minds_skippy")
HIVE_MIND_MINDS_VOICE_DIR = Path("/minds_hive_mind_voice")
HIVE_TOOLS_REPO_DIR = Path("/hive_tools_repo")
HIVE_INIT_DIR = Path("/hive_init")

# hive-init's own allowlist, mirrored here rather than imported since the
# two repos don't share a package — see spark_to_bloom's former
# HIVE_INIT_ALLOWED_FILES for the pattern this was lifted from.
DOWNLOAD_ALLOWED_FILES = {
    "hive-init.py": "text/x-python",
    "hive-init.sh": "text/x-shellscript",
}

_WAV_MAX_BYTES = 20 * 1024 * 1024  # ~10s of clean speech is a few hundred KB; 20MB is generous headroom

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _parse_mind_md(text: str) -> dict[str, Any]:
    """Split a MIND.md's YAML frontmatter from its prose body."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {"config": {}, "body": text.strip()}
    front, body = match.groups()
    try:
        config = yaml.safe_load(front) or {}
    except yaml.YAMLError:
        config = {}
    return {"config": config, "body": body.strip()}


def _find_local_config(short_name: str) -> dict[str, Any] | None:
    """Look up a mind's local config file by its short name, either mount."""
    runtime_path = HIVE_MIND_MINDS_DIR / short_name / "runtime.yaml"
    if runtime_path.is_file():
        try:
            config = yaml.safe_load(runtime_path.read_text()) or {}
        except yaml.YAMLError as exc:
            return {"source": "runtime.yaml", "path": str(runtime_path), "error": str(exc)}
        return {"source": "runtime.yaml", "path": str(runtime_path), "config": config, "body": ""}

    mind_md_path = SKIPPY_MINDS_DIR / short_name / "MIND.md"
    if mind_md_path.is_file():
        parsed = _parse_mind_md(mind_md_path.read_text())
        return {"source": "MIND.md", "path": str(mind_md_path), **parsed}

    return None


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
        local = _find_local_config(name) if name else None
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
        return {"present": False, "path": str(HIVE_TOOLS_REPO_DIR)}
    entries = list(HIVE_TOOLS_REPO_DIR.iterdir())
    return {"present": bool(entries), "path": str(HIVE_TOOLS_REPO_DIR), "entry_count": len(entries)}


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
