"""
Hive Mind -- Mind registry.

Scans minds/*/MIND.md on startup, parses YAML frontmatter, and builds
an in-memory registry of available minds.
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
    """Parsed representation of a MIND.md file."""

    name: str           # unique identifier
    model: str          # e.g. "claude-sonnet-4-6", "gpt-oss:20b-32k"
    harness: str        # e.g. "claude_cli_claude", "codex_cli_codex"
    gateway_url: str    # e.g. "http://hive_mind:8420"
    prompt_files: list[str] = field(default_factory=list)
    remote: bool = False
    soul_seed: str = ""  # markdown body from MIND.md
    container: ContainerConfig | None = None  # None = runs inside NS container


_REQUIRED_FIELDS = ("name", "model", "harness", "gateway_url")


def parse_mind_file(path: Path) -> MindInfo:
    """Parse a MIND.md file with YAML frontmatter and markdown body.

    Args:
        path: Path to the MIND.md file.

    Returns:
        MindInfo with all fields populated.

    Raises:
        ValueError: If frontmatter is missing or required fields are absent.
    """
    content = path.read_text(encoding="utf-8")

    # Split on --- delimiters (expecting at least 2 occurrences)
    parts = content.split("---")
    if len(parts) < 3:
        raise ValueError(
            f"{path}: missing YAML frontmatter (expected --- delimiters)"
        )

    # YAML is between the first and second ---
    yaml_text = parts[1]
    # Body is everything after the second ---
    body = "---".join(parts[2:]).strip()

    data = yaml.safe_load(yaml_text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: frontmatter is not a YAML mapping")

    # Validate required fields
    for field in _REQUIRED_FIELDS:
        if field not in data or data[field] is None:
            raise ValueError(
                f"{path}: missing required field '{field}' in frontmatter"
            )

    # Parse optional container block
    container_data = data.get("container")
    container: ContainerConfig | None = None
    if container_data is not None:
        if not isinstance(container_data, dict):
            container_data = {}
        container = ContainerConfig(
            image=str(container_data.get("image", "hive_mind:latest")),
            volumes=list(container_data.get("volumes", [])),
            environment=list(container_data.get("environment", [])),
            networks=list(container_data.get("networks", [])),
        )

    prompt_files_data = data.get("prompt_files", [])
    if prompt_files_data is None:
        prompt_files_data = []
    if not isinstance(prompt_files_data, list):
        raise ValueError(f"{path}: prompt_files must be a YAML list")
    prompt_files = [str(item) for item in prompt_files_data]

    return MindInfo(
        name=str(data["name"]),
        model=str(data["model"]),
        harness=str(data["harness"]),
        gateway_url=str(data["gateway_url"]),
        prompt_files=prompt_files,
        remote=bool(data.get("remote", False)),
        soul_seed=body,
        container=container,
    )


class MindRegistry:
    """In-memory registry of minds discovered from the filesystem."""

    def __init__(self, minds_dir: Path) -> None:
        self._minds_dir = minds_dir
        self._minds: dict[str, MindInfo] = {}

    def scan(self) -> None:
        """Scan minds_dir for subdirectories containing MIND.md."""
        if not self._minds_dir.exists():
            log.warning("Minds directory does not exist: %s", self._minds_dir)
            return

        for subdir in sorted(self._minds_dir.iterdir()):
            if not subdir.is_dir():
                continue
            mind_file = subdir / "MIND.md"
            if not mind_file.exists():
                continue
            try:
                info = parse_mind_file(mind_file)
                self._minds[info.name] = info
                log.info(
                    "Registered mind: %s @ %s (harness=%s, model=%s)",
                    info.name,
                    info.gateway_url,
                    info.harness,
                    info.model,
                )
            except Exception:
                log.exception("Failed to parse %s", mind_file)

    def get(self, name: str) -> MindInfo | None:
        """Look up a mind by name."""
        return self._minds.get(name)

    def list_all(self) -> list[MindInfo]:
        """Return all registered minds."""
        return list(self._minds.values())
