"""Local per-mind config discovery for the /settings mind registry.

Containerized minds (ada/bob/bilby/nagatha/hex-bot) keep a `runtime.yaml`
under hive_mind's `minds/<name>/`; an edge mind on this host keeps its own
`runtime.yaml` under its hive-edge-minds checkout, mounted at
`/minds_edge/<name>/`. Both are mounted into this container — see
docker-compose.yml. A mind with neither is remote: no local file to show,
only whatever the broker reports.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from capability_install import has_checkout

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
            "owner": m.get("owner") or None,
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

    Presence means a git checkout, not merely a non-empty directory: compose
    auto-creates both the mount root and a `.env` placeholder inside it on a
    host that never cloned, so counting entries would report a bare mount as
    installed and hide the Capability Nodes clone action that fixes it.
    """
    if not HIVE_TOOLS_REPO_DIR.is_dir():
        return {"present": False, "path": str(HIVE_TOOLS_REPO_DIR), "capabilities": []}
    # One predicate for "installed" across the console: a bare .git left by an
    # interrupted clone is not a checkout. See capability_install.has_checkout.
    present = has_checkout(HIVE_TOOLS_REPO_DIR)
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
        "present": present,
        "path": str(HIVE_TOOLS_REPO_DIR),
        "capabilities": capabilities,
    }


#: The clip a chatterbox mind is spoken from when it names none. The original
#: single-clip convention, which every mind installed before clips became
#: selectable is still on.
DEFAULT_VOICE_CLIP = "voice_ref.wav"

#: A clip filename, matching what a mind will accept into its own
#: `runtime.yaml`. A leading underscore is allowed because real clips have
#: one; a separator is not. Deliberately the same shape as the mind-side
#: pattern — a name this accepted and the mind refused would be a clip that
#: uploads, lists, and cannot be selected.
_CLIP_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


def _mind_clip_dir(short_name: str, root: Path) -> Path:
    """The directory holding one mind's reference recordings."""
    if not short_name or not re.fullmatch(r"[A-Za-z0-9_-]+", short_name):
        raise ValueError(f"invalid mind name: {short_name!r}")
    return root / short_name


def list_voice_clips(short_name: str) -> list[dict[str, Any]]:
    """Every reference recording sitting in this mind's own directory.

    A mind's directory accumulates candidates — a clip recorded twice, a
    stereo take kept beside a mono one — and only the one the mind names is
    spoken. Listing them is what makes the others selectable instead of
    needing a re-upload of a file already on the disk.
    """
    try:
        directory = _mind_clip_dir(short_name, HIVE_MIND_MINDS_DIR)
    except ValueError:
        return []
    if not directory.is_dir():
        return []
    clips = []
    # Case-insensitively on the extension, because the upload accepts `.WAV` —
    # which is what Windows Sound Recorder and several converters emit. A
    # listing that globbed `*.wav` would take such a file, report success
    # naming it, and then never show it again.
    for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if path.suffix.lower() != ".wav" or not path.is_file():
            continue
        if not _CLIP_NAME_RE.fullmatch(path.name):
            # Named rather than dropped. A recording the picker cannot offer
            # is still a recording the operator put there, and a listing that
            # quietly omits things teaches its reader to distrust it about
            # absence.
            clips.append({"name": path.name, "unselectable": True})
            continue
        stat = path.stat()
        clips.append(
            {
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        )
    return clips


def clip_filename(filename: str) -> str:
    """The name an uploaded clip is stored under.

    The basename only: an upload arrives named by whatever the browser sent,
    and a name carrying a path would otherwise be joined onto a directory.
    `.wav` is required rather than appended, because a clip that is not a WAV
    is one chatterbox will refuse at synthesis — long after the upload said
    it worked.
    """
    name = Path(str(filename or "").replace("\\", "/")).name.strip()
    if not name.lower().endswith(".wav"):
        raise ValueError("a reference clip must be a .wav file")
    if not _CLIP_NAME_RE.fullmatch(name):
        raise ValueError(f"not a usable clip filename: {filename!r}")
    return name


class ClipExists(ValueError):
    """A clip of that name is already in this Mind's directory."""


def save_voice_clip(short_name: str, filename: str, data: bytes) -> Path:
    """Write an uploaded reference clip into the mind's own directory.

    Under its own name, beside whatever is already there: a mind's clips are
    candidates and an upload adds one rather than replacing the active one.
    What makes a clip active is the mind naming it in its `runtime.yaml`,
    which is a separate write the console makes next.

    A name already taken is refused rather than overwritten. The recording a
    mind is currently spoken from is usually the one called `voice_ref.wav`,
    which is also what a second take gets called by whoever recorded it — so
    an overwrite changes a mind's voice with no save, nothing on screen to
    show it, and the previous recording gone.

    Validated only at the RIFF/WAVE header level — chatterbox is picky about
    format but does its own real validation on synthesis; this is just enough
    to reject an obviously-wrong upload (a PNG, an mp3) before it lands.
    """
    name = clip_filename(filename)
    if len(data) > _WAV_MAX_BYTES:
        raise ValueError(f"file too large: {len(data)} bytes (max {_WAV_MAX_BYTES})")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file (missing RIFF/WAVE header)")

    mind_dir = _mind_clip_dir(short_name, HIVE_MIND_MINDS_VOICE_DIR)
    mind_dir.mkdir(parents=True, exist_ok=True)
    target = mind_dir / name
    if target.exists():
        raise ClipExists(
            f"{name} is already one of this Mind's clips. Rename the file and"
            " upload it again, or remove the existing one first."
        )

    # Staged and renamed rather than written in place. The voice server opens
    # these files on every spoken sentence, so a clip written in place is one
    # chatterbox can read half of — a truncated WAV it refuses, on a path that
    # was fine a moment earlier. The staging name is dotted so the listing,
    # which matches on the extension, cannot offer a half-written upload.
    staging = mind_dir / f".{name}.incoming"
    try:
        staging.write_bytes(data)
        os.replace(staging, target)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
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
