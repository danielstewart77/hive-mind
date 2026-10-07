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

import hashlib
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
        # Every field is named, including `container_root`: defaulting it
        # would silently mount a second root over Dev's own path inside every
        # Mind, and a typo in one field must refuse the configuration rather
        # than raise past the routes as a 500 that reads like a Mind which is
        # not a container.
        missing = [
            field
            for field in ("key", "compose_var", "console_path", "container_root")
            if not str(entry.get(field) or "").strip()
        ]
        if missing:
            raise ValueError(
                "MIND_MOUNT_ROOTS is missing " + ", ".join(missing) + " on a root"
            )
        roots.append(
            MountRoot(
                key=str(entry["key"]),
                label=str(entry.get("label") or entry["key"]),
                compose_var=str(entry["compose_var"]),
                console_path=Path(str(entry["console_path"])),
                container_root=str(entry["container_root"]).rstrip("/"),
            )
        )
    if len({root.key for root in roots}) != len(roots):
        raise ValueError("MIND_MOUNT_ROOTS names one key twice")
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
    relative = (relative or "").strip()
    base = root.console_path.resolve()
    if not base.is_dir():
        raise ValueError(f"{root.label} is not readable from the console")
    # Judged before the separators are trimmed. Stripping first turns `/etc`
    # into `etc`, which is then resolved under the root and accepted whenever
    # a folder of that name happens to exist there — an absolute path quietly
    # answered with a different folder.
    if relative.startswith(("/", "\\")) or Path(relative).is_absolute():
        raise ValueError("A folder is named relative to its root")
    relative = relative.strip("/")
    if relative in {"", "."}:
        return ""
    # A name this control cannot write is refused on the way in, not just on
    # the way out of `create_folder`. A folder holding a `$` would be read by
    # the compose parser as a variable and mounted from somewhere else
    # entirely, while the panel went on showing the name that was picked.
    for segment in relative.split("/"):
        if not _SEGMENT_RE.fullmatch(segment):
            raise ValueError(
                f"{segment} is not a name this control can write into a compose file"
            )
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"{relative} is outside {root.label}")
    if not target.is_dir():
        raise ValueError(f"{relative} is not a folder")
    return target.relative_to(base).as_posix()


def folder_exists(root: MountRoot, relative: str) -> bool:
    """Whether a folder a fragment already names is still there.

    A folder removed on the host leaves a mount declared for a path that no
    longer exists; the page says so rather than offering a row whose every
    save is refused for a reason nothing on screen explains.
    """
    try:
        resolve_folder(root, relative)
    except ValueError:
        return False
    return True


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
    try:
        target.mkdir()
    except FileExistsError as exc:
        # Reporting a creation that did not happen would also chown a folder
        # this control never made. It is already in the list to be added.
        raise ValueError(f"{name} already exists here — add it instead") from exc
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


@dataclass(frozen=True)
class ComposeTarget:
    """Where a Mind's mounts are written, and what recreates it from them."""

    fragment: Path
    project_path: str
    service: str
    env_file: Path


def compose_target(name: str) -> ComposeTarget:
    """This Mind's compose file, as this console can reach it.

    A Mind in the hive's own stack keeps a fragment under hive_mind's
    `minds/`, recreated by that project. A containerised edge Mind has its own
    compose project in its own checkout, so it is named in
    `MIND_COMPOSE_TARGETS` rather than assumed — the alternative is a control
    that writes nothing and a recreate aimed at a service the named project
    does not contain.
    """
    if not _MIND_NAME_RE.fullmatch(name or ""):
        raise ValueError("Invalid Mind name")
    raw = os.getenv("MIND_COMPOSE_TARGETS", "").strip()
    if raw:
        try:
            targets = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"MIND_COMPOSE_TARGETS is not valid JSON: {exc}") from exc
        if not isinstance(targets, dict):
            raise ValueError("MIND_COMPOSE_TARGETS must be an object keyed by Mind name")
        entry = targets.get(name)
        if isinstance(entry, dict):
            missing = [
                field
                for field in ("fragment", "project_path")
                if not str(entry.get(field) or "").strip()
            ]
            if missing:
                raise ValueError(
                    f"MIND_COMPOSE_TARGETS is missing {', '.join(missing)} for {name}"
                )
            return ComposeTarget(
                fragment=Path(str(entry["fragment"])),
                project_path=str(entry["project_path"]),
                service=str(entry.get("service") or name),
                env_file=Path(str(entry.get("env_file") or "/env_hive_mind/.env")),
            )
    return ComposeTarget(
        fragment=configuration_data.HIVE_MIND_MINDS_CONFIG_DIR
        / name
        / "container"
        / "compose.yaml",
        project_path=os.getenv(
            "HIVE_MIND_PROJECT_PATH", "/home/daniel/Storage/Dev/hive_mind"
        ),
        service=name,
        env_file=Path("/env_hive_mind/.env"),
    )


def fragment_path(name: str) -> Path:
    return compose_target(name).fragment


def compose_variables(env_file: Path) -> set[str] | None:
    """The variable names that compose file's project defines.

    None when the file cannot be read, which is not the same as a file that
    defines nothing: an undefined variable in a bind source expands to the
    empty string, so `${HOST_NOTES_DIR}/journal` becomes `/journal` — created
    at the host filesystem root, root-owned and empty, while the panel goes on
    showing the folder that was picked.
    """
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if value.strip():
            names.add(key.strip())
    return names


def _spec_fields(spec: str) -> list[str]:
    """A bind spec's colon-separated fields.

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
    return fields


def _split_spec(spec: str) -> tuple[str, str, str] | None:
    fields = _spec_fields(spec)
    if len(fields) < 2:
        return None
    return fields[0], fields[1], fields[2] if len(fields) > 2 else "rw"


def _project_row(spec: str) -> dict[str, Any] | None:
    """The row this control would show for a spec, or None if it owns none."""
    parts = _split_spec(spec)
    if not parts:
        return None
    source, target, mode = parts
    # Only the three-field spec with a plain mode is ours. A spec carrying a
    # SELinux label or a propagation flag (`ro,z`, `rw,rslave`) reads as
    # writable under a naive check and is flattened by a save that rewrites
    # three fields from a boolean — so it is left alone instead.
    if mode not in {"ro", "rw"} or len(_spec_fields(spec)) != 3:
        return None
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


class StaleFragment(Exception):
    """The fragment changed since the page that is saving read it."""


class NoComposeFile(Exception):
    """This Mind's container is not defined by a file this console can reach.

    Distinct from a file that cannot be read: nothing is wrong, the folders
    are simply not this console's to set — Mordecai holds the whole host
    filesystem through one mount declared in his own checkout's compose
    project. Reported as a control that does not apply rather than as an
    error to go and fix.
    """


def _service_volumes(document: Any, name: str) -> list[str]:
    services = document.get("services") if isinstance(document, dict) else None
    service = services.get(name) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise ValueError(f"This fragment declares no service named {name}")
    volumes = service.get("volumes")
    if volumes is None:
        return []
    if not isinstance(volumes, list) or not all(isinstance(v, str) for v in volumes):
        raise ValueError(
            "This Mind's volumes are written in a form this control cannot "
            "rewrite (a long-syntax or merged list) — edit the fragment by hand"
        )
    return list(volumes)


def _entry_spec(line: str) -> str | None:
    """The bind spec a volumes-block line declares, or None if it is not one.

    A quoted spec is the same spec; an anchored or aliased entry is not one
    this control will rewrite, because the text on the line is not what the
    parsed document holds.
    """
    stripped = line.lstrip()
    if not stripped.startswith("-"):
        return None
    spec = stripped[1:].strip()
    if spec.startswith(("&", "*", "<<", "{", "[")):
        return None
    if len(spec) >= 2 and spec[0] == spec[-1] and spec[0] in {'"', "'"}:
        spec = spec[1:-1]
    return spec


def revision_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_mounts(name: str) -> dict[str, Any]:
    """The project folders this Mind's fragment declares, and what it may not edit."""
    target = compose_target(name)
    source = target.fragment
    if not source.is_file():
        raise NoComposeFile(
            "This Mind's container is not defined by a compose file this console "
            "can reach, so its folders are set where that file lives"
        )
    original = source.read_text(encoding="utf-8")
    document = yaml.safe_load(original)
    specs = _service_volumes(document, target.service)
    roots = declared_roots()
    by_key = {root.key: root for root in roots}
    mounts, infrastructure = [], []
    for spec in specs:
        row = _project_row(spec)
        if row is not None:
            root = by_key.get(row["root"])
            # A folder removed on the host is still declared here. Saying so is
            # the difference between a row the operator knows to drop and a
            # save refused for a reason nothing on screen explains.
            row["missing"] = not (root and folder_exists(root, row["path"]))
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
    rewritable, detail = True, ""
    try:
        _volume_block(original.splitlines(keepends=True), target.service)
    except ValueError as exc:
        # Found out now rather than when the operator presses Save: the panel
        # can list what is mounted and still say it cannot write this file.
        rewritable, detail = False, str(exc)
    return {
        "mounts": mounts,
        "infrastructure": infrastructure,
        "roots": [root.as_json() for root in roots],
        "revision": revision_of(original),
        "rewritable": rewritable,
        "detail": detail,
    }


def _volume_block(lines: list[str], name: str) -> tuple[int, int, str]:
    """The half-open line range of the service's volumes entries, and their indent."""
    services_at = None
    for index, line in enumerate(lines):
        if re.fullmatch(r"services:\s*", line.rstrip("\n")):
            services_at = index
            break
    service_at = None
    # Matched as a service key — a direct child of `services:`, at the indent
    # its siblings sit at. The same text appears as a top-level named volume
    # and as a `depends_on` entry inside another service, and taking the first
    # line anywhere that matches picks one of those instead.
    child_indent = None
    for index in range(0 if services_at is None else services_at + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if services_at is not None:
            if child_indent is None:
                child_indent = indent
            if indent == 0:
                break
            if indent != child_indent:
                continue
        if re.fullmatch(rf"\s+{re.escape(name)}:\s*", line.rstrip("\n")):
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
        if re.fullmatch(r"\s*volumes:\s*", line.rstrip("\n")):
            volumes_at = index
            break
    if volumes_at is None:
        raise ValueError(
            "This Mind's volumes are not written as a block list under the "
            "service, which is the only shape this control can rewrite — edit "
            "the fragment by hand"
        )
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


def save_mounts(
    name: str, rows: list[dict[str, Any]], revision: str
) -> dict[str, Any]:
    """Write the chosen folders into this Mind's fragment.

    Every other line and every comment is left exactly as it was: only the
    entries this control owns are rewritten. The result is parsed and compared
    against what was asked for before it replaces the file, so a save that
    would leave the fragment unreadable, or drop a mount the Mind needs to
    boot, refuses whole rather than landing.

    The save carries the revision the page read. Two tabs editing one Mind
    otherwise overwrite each other with no sign of it: the second save's
    response lists exactly what that tab intended, and the mount the first one
    added is gone from the file and from the recreated container.
    """
    target = compose_target(name)
    source = target.fragment
    if not source.is_file():
        raise NoComposeFile(
            "This Mind's container is not defined by a compose file this console "
            "can reach, so its folders are set where that file lives"
        )
    if not revision:
        raise ValueError("A save must carry the revision the page read")

    defined = compose_variables(target.env_file)
    wanted: list[str] = []
    seen_targets: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each mount is an object")
        root = _root(str(row.get("root") or ""))
        if "writable" not in row:
            raise ValueError("Each mount says whether it is writable")
        if defined is not None and root.compose_var not in defined:
            # An undefined variable expands to nothing, so the bind source
            # becomes an absolute path at the filesystem root: a folder Docker
            # creates empty and root-owned, while the panel shows the one that
            # was picked.
            raise ValueError(
                f"{root.label} is mounted through ${{{root.compose_var}}}, which "
                f"{target.env_file} does not define — set it there first"
            )
        relative = resolve_folder(root, str(row.get("path") or ""))
        spec = mount_spec(root, relative, bool(row["writable"]))
        bind_target = _split_spec(spec)[1]
        if bind_target in seen_targets:
            raise ValueError(
                f"Two folders would both mount at {bind_target} — rename one or drop it"
            )
        seen_targets.add(bind_target)
        wanted.append(spec)

    original = source.read_text(encoding="utf-8")
    if revision_of(original) != revision:
        raise StaleFragment(
            "This Mind's compose file changed since this page read it. Reload "
            "and make the change again — saving now would drop whatever else "
            "was written."
        )
    document = yaml.safe_load(original)
    before = _service_volumes(document, target.service)
    infrastructure = [spec for spec in before if _project_row(spec) is None]

    lines = original.splitlines(keepends=True)
    # A file whose last line has no newline would otherwise have an appended
    # entry glued onto it, which parses as something else entirely and is
    # then reported as a save that would drop an infrastructure mount.
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = lines[-1] + "\n"
    start, end, indent = _volume_block(lines, target.service)
    newline = "\n"
    rewritten: list[str] = []
    remaining = list(wanted)
    for line in lines[start:end]:
        spec = _entry_spec(line)
        if spec is None or _project_row(spec) is None:
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
        after = _service_volumes(parsed, target.service)
    except (yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"That save would leave the fragment unreadable: {exc}") from exc
    dropped = [spec for spec in infrastructure if spec not in after]
    if dropped:
        raise ValueError(
            "That save would drop a mount this Mind needs to boot: " + ", ".join(dropped)
        )
    if [spec for spec in after if _project_row(spec) is not None] != wanted:
        raise ValueError(
            "That save did not write the folders that were chosen, so nothing "
            "was written at all — this fragment's volumes are in a shape this "
            "control cannot rewrite"
        )

    # Written through a symlink rather than over it: replacing the link leaves
    # whatever else reads the real path on the old content with nothing to say
    # it diverged.
    source = source.resolve()
    stat = source.stat()
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=str(source.parent),
        prefix=".compose-", suffix=".tmp", delete=False,
    )
    try:
        with handle as stream:
            stream.write(candidate)
        os.chmod(handle.name, stat.st_mode & 0o7777)
        # This console runs as root inside its container and the fragment is
        # in Daniel's own checkout. A new inode owned by root is a file he can
        # no longer edit, and a `git checkout` touching that path fails.
        try:
            os.chown(handle.name, stat.st_uid, stat.st_gid)
        except (OSError, AttributeError):
            pass
        os.replace(handle.name, source)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return read_mounts(name)
