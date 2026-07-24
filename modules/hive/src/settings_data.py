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
