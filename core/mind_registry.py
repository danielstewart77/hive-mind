"""
Hive Mind -- Mind registry.

Scans minds/*/runtime.yaml on startup and builds an in-memory registry of
available minds. runtime.yaml is the only file the registry reads from each mind folder.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("hive-mind.mind_registry")


@dataclass
class ContainerConfig:
    """Optional container isolation configuration for a mind."""

    image: str = "hive_mind:latest"
    volumes: list[str] = field(default_factory=list)
    environment: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)


@dataclass
class MindInfo:
    """Parsed representation of a mind's runtime.yaml."""

    name: str           # operational handle (folder slug, env var, registry key)
    mind_id: str        # canonical archival UUID (sessions DB / KG / broker)
    model: str          # e.g. "sonnet", "gpt-oss:20b-32k", "codex"
    harness: str        # e.g. "claude_cli", "codex_cli", "claude_sdk"
    gateway_url: str    # e.g. "http://ada:8420"
    prompt_files: list[str] = field(default_factory=list)
    remote: bool = False
    soul_seed: str = ""  # kept for interface compatibility; always "" now
    container: ContainerConfig | None = None  # None = runs inside NS container


_REQUIRED_FIELDS = ("name", "mind_id", "default_model", "harness", "gateway_url")


def parse_mind_file(path: Path) -> MindInfo:
    """Parse a mind's runtime.yaml. Path must point at a runtime.yaml file.

    Args:
        path: Path to runtime.yaml.

    Returns:
        MindInfo with all fields populated.

    Raises:
        ValueError: If the file is not a runtime.yaml or required fields are absent.
    """
    if path.name != "runtime.yaml":
        raise ValueError(
            f"{path}: parse_mind_file only accepts runtime.yaml — "
            f"runtime.yaml is the only mind config file (Phase 1 of runtime-config refactor)"
        )

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: runtime.yaml is not a YAML mapping")

    for f in _REQUIRED_FIELDS:
        if f not in data or data[f] is None:
            raise ValueError(f"{path}: missing required field '{f}'")

    prompt_files_data = data.get("prompt_files") or []
    if not isinstance(prompt_files_data, list):
        raise ValueError(f"{path}: prompt_files must be a YAML list")

    return MindInfo(
        name=str(data["name"]),
        mind_id=str(data["mind_id"]),
        model=str(data["default_model"]),
        harness=str(data["harness"]),
        gateway_url=str(data["gateway_url"]),
        prompt_files=[str(p) for p in prompt_files_data],
        remote=bool(data.get("remote", False)),
        soul_seed="",
        container=None,
    )


class MindRegistry:
    """In-memory registry of minds discovered from the filesystem."""

    def __init__(self, minds_dir: Path, single_mind: str | None = None) -> None:
        self._minds_dir = minds_dir
        self._single_mind = single_mind
        self._minds: dict[str, MindInfo] = {}

    def scan(self) -> None:
        """Scan minds_dir for subdirectories containing runtime.yaml."""
        if not self._minds_dir.exists():
            log.warning("Minds directory does not exist: %s", self._minds_dir)
            return

        for subdir in sorted(self._minds_dir.iterdir()):
            if not subdir.is_dir():
                continue
            if self._single_mind and subdir.name != self._single_mind:
                continue

            runtime_file = subdir / "runtime.yaml"
            if not runtime_file.exists():
                log.error(
                    "%s: missing runtime.yaml — mind not registered. "
                    "All minds must have runtime.yaml after Phase 1 of the "
                    "runtime-config refactor.",
                    subdir.name,
                )
                continue

            try:
                info = parse_mind_file(runtime_file)
                self._minds[info.name] = info
                log.info(
                    "Registered mind: %s @ %s (harness=%s, model=%s)",
                    info.name,
                    info.gateway_url,
                    info.harness,
                    info.model,
                )
            except Exception:
                log.exception("Failed to parse %s", runtime_file)

    def get(self, name: str) -> MindInfo | None:
        """Look up a mind by short name (operational handle)."""
        return self._minds.get(name)

    def lookup_by_id(self, mind_id: str) -> MindInfo | None:
        """Look up a mind by its canonical UUID."""
        for info in self._minds.values():
            if info.mind_id == mind_id:
                return info
        return None

    def list_all(self) -> list[MindInfo]:
        """Return all registered minds."""
        return list(self._minds.values())
