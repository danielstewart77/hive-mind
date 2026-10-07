"""The project folders a containerised Mind can reach.

A Mind's bind mounts live in its own compose fragment
(`minds/<name>/container/compose.yaml`), which is the durable truth: a
`docker restart` replays the config the container was created with and
genuinely cannot add a mount, so a change here is written to the fragment and
the container is then recreated from it.

Two kinds of mount appear in that fragment and only one is editable from
here. A *project* mount is one this control wrote: its source is a declared
root's compose variable and its destination is under that root's container
path. Everything else is infrastructure — the checkout the Mind boots from,
its keyring, its harness — shown so the operator can see it and never
rewritten, because a Mind that lost one of those does not start.

Nothing outside the declared roots can be mounted however the request is
spelled. Containment is judged on the *resolved* path: a `..` and a symlink
aimed out of the root both land outside it, and this is a bind mount on the
host, so what the kernel would mount is what decides.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

import configuration_data

#: A folder name the operator may create. Deliberately narrower than what the
#: filesystem allows: this name becomes a path inside a compose file, and a
#: colon or a `$` in it would be read by the compose parser rather than by us.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")

_MIND_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: `${VAR}` or `${VAR:-default}`, optionally followed by a path under it.
_SOURCE_RE = re.compile(
    r"^\$\{(?P<var>[A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}(?P<rel>/.+)?$"
)

#: The roots a Mind's mounts may come from, seeded with Daniel's Dev tree.
#: Overridable as JSON in `MIND_MOUNT_ROOTS` so a second root can be added
#: without a code change — each root names the compose variable its spec is
#: written with, the path *this console* can see it at, and the directory
#: inside the Mind its folders appear under.
_DEFAULT_ROOTS: list[dict[str, str]] = [
    {
        "key": "dev",
        "label": "Dev",
        "compose_var": "HOST_DEV_DIR",
        "console_path": "/dev_roots",
        "container_root": "/mnt/dev",
    }
]


@dataclass(frozen=True)
class MountRoot:
    key: str
    label: str
    compose_var: str
    console_path: Path
    container_root: str

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "container_root": self.container_root,
            "readable": self.console_path.is_dir(),
        }


def declared_roots() -> list[MountRoot]:
    raw = os.getenv("MIND_MOUNT_ROOTS", "").strip()
    entries = _DEFAULT_ROOTS
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"MIND_MOUNT_ROOTS is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list) or not all(isinstance(e, dict) for e in parsed):
            raise ValueError("MIND_MOUNT_ROOTS must be a list of objects")
        entries = parsed
    roots: list[MountRoot] = []
    for entry in entries:
        roots.append(
            MountRoot(
                key=str(entry["key"]),
                label=str(entry.get("label") or entry["key"]),
                compose_var=str(entry["compose_var"]),
                console_path=Path(str(entry["console_path"])),
                container_root=str(entry.get("container_root") or "/mnt/dev").rstrip("/"),
            )
        )
    return roots


def _root(key: str) -> MountRoot:
    for root in declared_roots():
        if root.key == key:
            return root
    raise ValueError(f"{key} is not a declared mount root")


def _root_for_var(var: str) -> MountRoot | None:
    for root in declared_roots():
        if root.compose_var == var:
            return root
    return None


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def resolve_folder(root: MountRoot, relative: str) -> str:
    """The folder a request names, as a path relative to its root.

    Refuses anything that does not resolve *inside* the root — a `..`, an
    absolute path, a symlink pointing out of it. The check is on the resolved
    path because that is what the kernel would bind.
    """
    relative = (relative or "").strip().strip("/")
    base = root.console_path.resolve()
    if not base.is_dir():
        raise ValueError(f"{root.label} is not readable from the console")
    if relative in {"", "."}:
        return ""
    if Path(relative).is_absolute():
        raise ValueError("A folder is named relative to its root")
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"{relative} is outside {root.label}")
    if not target.is_dir():
        raise ValueError(f"{relative} is not a folder")
    return target.relative_to(base).as_posix()


def list_folders(root_key: str, relative: str = "") -> dict[str, Any]:
    """The folders directly under one place in a declared root."""
    root = _root(root_key)
    resolved = resolve_folder(root, relative)
    base = root.console_path.resolve()
    here = base / resolved if resolved else base
    folders = []
    for child in sorted(here.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        try:
            path = resolve_folder(root, f"{resolved}/{child.name}" if resolved else child.name)
        except ValueError:
            # A symlink out of the root: named rather than dropped, so the
            # page can say why it is not offered.
            folders.append({"name": child.name, "path": "", "mountable": False})
            continue
        folders.append({"name": child.name, "path": path, "mountable": True})
    return {"root": root.key, "path": resolved, "folders": folders}


def create_folder(root_key: str, parent: str, name: str) -> dict[str, Any]:
    """Create one folder under a declared root, and nowhere else."""
    root = _root(root_key)
    name = (name or "").strip()
    if not _SEGMENT_RE.fullmatch(name):
        raise ValueError(
            "A folder name may hold letters, digits, spaces, dots, dashes and underscores"
        )
    resolved_parent = resolve_folder(root, parent)
    base = root.console_path.resolve()
    target = (base / resolved_parent / name) if resolved_parent else (base / name)
    target.mkdir(exist_ok=True)
    # Judged after the fact as well: the parent resolving inside the root is
    # not on its own a promise about the child.
    path = resolve_folder(root, f"{resolved_parent}/{name}" if resolved_parent else name)
    # This console runs as root. A folder it creates and the Mind then writes
    # into would otherwise be root-owned on the operator's own machine.
    try:
        os.chown(
            target,
            int(os.getenv("HOST_UID", "1000")),
            int(os.getenv("HOST_GID", "1000")),
        )
    except (OSError, ValueError):
        pass
    return {"root": root.key, "path": path, "name": name}


# ---------------------------------------------------------------------------
# The fragment
# ---------------------------------------------------------------------------


def fragment_path(name: str) -> Path:
    if not _MIND_NAME_RE.fullmatch(name or ""):
        raise ValueError("Invalid Mind name")
    return configuration_data.HIVE_MIND_MINDS_CONFIG_DIR / name / "container" / "compose.yaml"


def _split_spec(spec: str) -> tuple[str, str, str] | None:
    """A bind spec as (source, target, mode).

    Split on the colons that separate the three fields, which is not every
    colon in the string: `${VAR:-default}` carries one of its own, and
    splitting naively there reports the tail of the source as the Mind's
    container path.
    """
    fields: list[str] = []
    current: list[str] = []
    depth = 0
    for char in str(spec):
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if char == ":" and depth == 0:
            fields.append("".join(current))
            current = []
            continue
        current.append(char)
    fields.append("".join(current))
    if len(fields) < 2:
        return None
    return fields[0], fields[1], fields[2] if len(fields) > 2 else "rw"


def _project_row(spec: str) -> dict[str, Any] | None:
    """The row this control would show for a spec, or None if it owns none."""
    parts = _split_spec(spec)
    if not parts:
        return None
    source, target, mode = parts
    match = _SOURCE_RE.fullmatch(source)
    if not match:
        return None
    root = _root_for_var(match.group("var"))
    if root is None:
        return None
    relative = (match.group("rel") or "").strip("/")
    expected = f"{root.container_root}/{Path(relative).name}" if relative else root.container_root
    if target.rstrip("/") != expected:
        return None
    return {
        "root": root.key,
        "root_label": root.label,
        "path": relative,
        "name": Path(relative).name if relative else root.label,
        "container_path": expected,
        "writable": mode != "ro",
        "spec": spec,
    }


def mount_spec(root: MountRoot, relative: str, writable: bool) -> str:
    mode = "rw" if writable else "ro"
    if not relative:
        return f"${{{root.compose_var}}}:{root.container_root}:{mode}"
    return (
        f"${{{root.compose_var}}}/{relative}:"
        f"{root.container_root}/{Path(relative).name}:{mode}"
    )


def _service_volumes(document: Any, name: str) -> list[str]:
    services = document.get("services") if isinstance(document, dict) else None
    service = services.get(name) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise ValueError(f"This fragment declares no service named {name}")
    volumes = service.get("volumes")
    if volumes is None:
        return []
    if not isinstance(volumes, list) or not all(isinstance(v, str) for v in volumes):
        raise ValueError("This Mind's volumes are not a list of bind specs")
    return list(volumes)


def read_mounts(name: str) -> dict[str, Any]:
    """The project folders this Mind's fragment declares, and what it may not edit."""
    source = fragment_path(name)
    if not source.is_file():
        raise ValueError("This Mind has no compose fragment on this host")
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    specs = _service_volumes(document, name)
    mounts, infrastructure = [], []
    for spec in specs:
        row = _project_row(spec)
        if row is not None:
            mounts.append(row)
            continue
        parts = _split_spec(spec)
        infrastructure.append(
            {
                "spec": spec,
                "container_path": parts[1] if parts else spec,
                "writable": bool(parts) and parts[2] != "ro",
            }
        )
    return {
        "mounts": mounts,
        "infrastructure": infrastructure,
        "roots": [root.as_json() for root in declared_roots()],
    }


def _volume_block(lines: list[str], name: str) -> tuple[int, int, str]:
    """The half-open line range of the service's volumes entries, and their indent."""
    service_at = None
    for index, line in enumerate(lines):
        if re.fullmatch(rf"\s+{re.escape(name)}:\s*", line):
            service_at = index
            break
    if service_at is None:
        raise ValueError(f"This fragment declares no service named {name}")
    service_indent = len(lines[service_at]) - len(lines[service_at].lstrip())

    volumes_at = None
    for index in range(service_at + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= service_indent:
            break
        if re.fullmatch(r"\s*volumes:\s*", line):
            volumes_at = index
            break
    if volumes_at is None:
        raise ValueError("This Mind's compose fragment declares no volumes block")
    volumes_indent = len(lines[volumes_at]) - len(lines[volumes_at].lstrip())

    end = volumes_at + 1
    entry_indent = ""
    for index in range(volumes_at + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            end = index
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= volumes_indent and not line.lstrip().startswith("#"):
            break
        if line.lstrip().startswith("-") and not entry_indent:
            entry_indent = line[: len(line) - len(line.lstrip())]
        end = index + 1
    return volumes_at + 1, end, entry_indent or " " * (volumes_indent + 2)


def save_mounts(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Write the chosen folders into this Mind's fragment.

    Every other line and every comment is left exactly as it was: only the
    entries this control owns are rewritten. The result is parsed and compared
    against what was asked for before it replaces the file, so a save that
    would leave the fragment unreadable, or drop a mount the Mind needs to
    boot, refuses whole rather than landing.
    """
    source = fragment_path(name)
    if not source.is_file():
        raise ValueError("This Mind has no compose fragment on this host")

    wanted: list[str] = []
    seen_targets: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each mount is an object")
        root = _root(str(row.get("root") or ""))
        relative = resolve_folder(root, str(row.get("path") or ""))
        spec = mount_spec(root, relative, bool(row.get("writable", True)))
        target = _split_spec(spec)[1]
        if target in seen_targets:
            raise ValueError(
                f"Two folders would both mount at {target} — rename one or drop it"
            )
        seen_targets.add(target)
        wanted.append(spec)

    original = source.read_text(encoding="utf-8")
    document = yaml.safe_load(original)
    before = _service_volumes(document, name)
    infrastructure = [spec for spec in before if _project_row(spec) is None]

    lines = original.splitlines(keepends=True)
    start, end, indent = _volume_block(lines, name)
    newline = "\n"
    rewritten: list[str] = []
    remaining = list(wanted)
    for line in lines[start:end]:
        stripped = line.lstrip()
        if not stripped.startswith("-"):
            rewritten.append(line)
            continue
        spec = stripped[1:].strip()
        if _project_row(spec) is None:
            rewritten.append(line)
            continue
        if remaining:
            rewritten.append(f"{indent}- {remaining.pop(0)}{newline}")
        # else: this folder was removed, so its entry goes with it.
    if remaining:
        tail = [f"{indent}- {spec}{newline}" for spec in remaining]
        at = len(rewritten)
        while at > 0 and not rewritten[at - 1].strip():
            at -= 1
        rewritten[at:at] = tail

    candidate = "".join(lines[:start] + rewritten + lines[end:])

    try:
        parsed = yaml.safe_load(candidate)
        after = _service_volumes(parsed, name)
    except (yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"That save would leave the fragment unreadable: {exc}") from exc
    missing = [spec for spec in infrastructure if spec not in after]
    if missing:
        raise ValueError(
            "That save would drop a mount this Mind needs to boot: " + ", ".join(missing)
        )
    if [spec for spec in after if _project_row(spec) is not None] != wanted:
        raise ValueError("That save did not write the folders that were chosen")

    mode = source.stat().st_mode
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=str(source.parent),
        prefix=".compose-", suffix=".tmp", delete=False,
    )
    try:
        with handle as stream:
            stream.write(candidate)
        os.chmod(handle.name, mode & 0o7777)
        os.replace(handle.name, source)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return read_mounts(name)
