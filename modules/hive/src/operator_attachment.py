"""What a console-made mind is attached to, as its roles change.

Every mind this console makes has the install's folder of generated skills
attached read-only (`configuration_data.workflow_skills_volume`), because it
otherwise sees only its own folder and a copy of the published code. The one
that holds the operator role is the one that writes those skills, so for it
the folder is attached writable, and it is handed the two values it reaches
the console's Roles routes with: the operator's credential, as a reference to
a variable of the stack's `.env` and never as a value, and the console's
address. Taking the role away puts it all back.

A container's attachments are fixed when it is made, so this changes the
mind's compose fragment and the caller recreates the container from it, the
way the mounts page does. Only the fragment is this module's: it says whether
it changed and the caller does the rest.

The fragment is edited a line at a time, the way `mind_mounts.save_mounts`
does it. Every other line and comment stays exactly as it was; the result is
parsed and compared with what was asked for before it replaces the file, and
anything this module is not sure of refuses the whole edit and writes nothing.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

import configuration_data
import mind_mounts

#: The slug of the role whose holder writes the generated skills.
OPERATOR_ROLE_SLUG = "operator"

#: The credential the operator changes roles with, and where the console is,
#: as the fragment spells them. The first is a reference, never a literal.
CREDENTIAL_VARIABLE = "ROLES_PUSH_TOKEN"
CREDENTIAL_LINE = f"{CREDENTIAL_VARIABLE}=${{{CREDENTIAL_VARIABLE}}}"
CONSOLE_VARIABLE = "HIVE_SITE_URL"
CONSOLE_LINE = f"{CONSOLE_VARIABLE}=http://hive-site:8710"


class AttachmentRefused(ValueError):
    """The fragment is in a shape this module cannot edit with certainty."""


def _fragment(name: str) -> Path:
    return configuration_data.HIVE_MIND_MINDS_CONFIG_DIR / name / "container" / "compose.yaml"


def _entries(document: Any, name: str, key: str) -> list[str]:
    services = document.get("services") if isinstance(document, dict) else None
    service = services.get(name) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise AttachmentRefused(f"this fragment declares no service named {name}")
    listed = service.get(key)
    if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
        raise AttachmentRefused(
            f"this mind's {key} are not written as a block list of plain lines, which is "
            "the only shape this can edit — edit the fragment by hand")
    return list(listed)


def _block(lines: list[str], name: str, key: str) -> tuple[int, int, str]:
    """The half-open line range of one service's `key:` entries, and their indent."""
    services_at = next((i for i, line in enumerate(lines)
                        if re.fullmatch(r"services:\s*", line.rstrip("\n"))), None)
    if services_at is None:
        raise AttachmentRefused("this fragment has no services block")
    service_at, child_indent = None, None
    for index in range(services_at + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if child_indent is None:
            child_indent = indent
        if indent == child_indent and re.fullmatch(rf"\s+{re.escape(name)}:\s*", line.rstrip("\n")):
            service_at = index
            break
    if service_at is None:
        raise AttachmentRefused(f"this fragment declares no service named {name}")
    key_at, key_indent = None, None
    for index in range(service_at + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= child_indent:
            break
        if key_indent is None:
            key_indent = indent
        if indent == key_indent and re.fullmatch(rf"{key}:\s*", line.strip()):
            key_at = index
            break
    if key_at is None:
        raise AttachmentRefused(
            f"this mind's {key} are not written as a block list under the service, which "
            "is the only shape this can edit — edit the fragment by hand")
    end, entry_indent = key_at + 1, ""
    for index in range(key_at + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        # PyYAML writes a list at the indent of its key, so an entry may sit
        # level with `volumes:`; anything else at that level ends the block.
        if line.lstrip().startswith("#"):
            continue
        if indent < key_indent or (indent == key_indent and not line.lstrip().startswith("-")):
            break
        if line.lstrip().startswith("-") and not entry_indent:
            entry_indent = line[: len(line) - len(line.lstrip())]
        end = index + 1
    return key_at + 1, end, entry_indent or " " * (key_indent + 2)


def _plain(line: str) -> str | None:
    """The value a list line declares, or None when it is not a plain one
    (anchored, aliased, a flow collection or a comment): such a line is kept
    as it is, and never matched."""
    stripped = line.lstrip()
    if not stripped.startswith("-"):
        return None
    value = stripped[1:].strip()
    if value.startswith(("&", "*", "<<", "{", "[", "#")):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def _skills_line(spec: str) -> bool:
    fields = mind_mounts._spec_fields(spec)
    return len(fields) >= 2 and fields[1].rstrip("/") == configuration_data.WORKFLOW_SKILLS_CONTAINER_PATH


def _edit_block(lines: list[str], name: str, key: str, change, append: list[str]) -> list[str]:
    """`lines` with one list's plain entries passed through `change(value)`
    (the new value, or None to drop the line), and `append` added after the
    last entry. Every other line, comments included, is kept as it is."""
    start, end, indent = _block(lines, name, key)
    kept: list[str] = []
    for line in lines[start:end]:
        value = _plain(line)
        if value is None:
            kept.append(line)
            continue
        replaced = change(value)
        if replaced is None:
            continue
        kept.append(line if replaced == value else f"{indent}- {replaced}\n")
    at = len(kept)
    while at > 0 and not kept[at - 1].strip():
        at -= 1
    kept[at:at] = [f"{indent}- {value}\n" for value in append]
    return lines[:start] + kept + lines[end:]


def _wanted(volumes: list[str], environment: list[str], operator: bool) -> tuple[list[str], list[str]]:
    """The two lists as they must read, from what the fragment holds now."""
    host = configuration_data.WORKFLOW_SKILLS_HOST_PATH
    mode = "rw" if operator else "ro"
    line = f"{host}:{configuration_data.WORKFLOW_SKILLS_CONTAINER_PATH}:{mode}"
    new_volumes, seen = [], False
    for spec in volumes:
        if not _skills_line(spec):
            new_volumes.append(spec)
            continue
        fields = mind_mounts._spec_fields(spec)
        if fields[0] != host or len(fields) > 3 or seen:
            raise AttachmentRefused(
                f"this mind already mounts {configuration_data.WORKFLOW_SKILLS_CONTAINER_PATH} "
                f"as {spec!r}, which is not the install's skills folder as this console "
                "attaches it — set it by hand")
        seen = True
        new_volumes.append(line)
    if not seen:
        new_volumes.append(line)
    new_environment = []
    for item in environment:
        key = item.split("=", 1)[0]
        if key == CREDENTIAL_VARIABLE:
            if operator and item != CREDENTIAL_LINE:
                raise AttachmentRefused(
                    f"this mind already sets {CREDENTIAL_VARIABLE} to something other than a "
                    "reference to the stack's variable — set it by hand")
            if operator:
                new_environment.append(item)
            continue  # taken with the role, whatever it held
        if key == CONSOLE_VARIABLE and item == CONSOLE_LINE and not operator:
            continue
        if key == CONSOLE_VARIABLE and operator and item != CONSOLE_LINE:
            raise AttachmentRefused(
                f"this mind already sets {CONSOLE_VARIABLE} to something other than the "
                "console's address — set it by hand")
        new_environment.append(item)
    if operator:
        for line_ in (CREDENTIAL_LINE, CONSOLE_LINE):
            if line_ not in new_environment:
                new_environment.append(line_)
    return new_volumes, new_environment


def _rewrite(original: str, name: str, operator: bool) -> str:
    document = yaml.safe_load(original)
    volumes = _entries(document, name, "volumes")
    environment = _entries(document, name, "environment")
    want_volumes, want_environment = _wanted(volumes, environment, operator)
    if want_volumes == volumes and want_environment == environment:
        return original

    lines = original.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"

    if want_volumes != volumes:
        line = next(spec for spec in want_volumes if _skills_line(spec))
        lines = _edit_block(lines, name, "volumes",
                            lambda value: line if _skills_line(value) else value,
                            [] if any(_skills_line(spec) for spec in volumes) else [line])
    if want_environment != environment:
        owned = (CREDENTIAL_VARIABLE, CONSOLE_VARIABLE)
        lines = _edit_block(
            lines, name, "environment",
            lambda value: (None if value.split("=", 1)[0] in owned
                           and value not in want_environment else value),
            [item for item in want_environment if item not in environment])
    candidate = "".join(lines)

    try:
        parsed = yaml.safe_load(candidate)
        after_volumes = _entries(parsed, name, "volumes")
        after_environment = _entries(parsed, name, "environment")
    except (yaml.YAMLError, AttachmentRefused) as exc:
        raise AttachmentRefused(f"that edit would leave the fragment unreadable: {exc}") from exc
    expected = yaml.safe_load(original)
    expected["services"][name]["volumes"] = want_volumes
    expected["services"][name]["environment"] = want_environment
    if (after_volumes != want_volumes or after_environment != want_environment
            or parsed != expected):
        raise AttachmentRefused(
            "that edit did not come out as what was asked for, so nothing was written — "
            "this fragment is in a shape this cannot edit")
    return candidate


def _write(path: Path, expected: str, candidate: str) -> None:
    """Replace the fragment, through a link rather than over it, keeping its
    owner and mode, and only if it is still what was read."""
    path = path.resolve()
    state = path.stat()
    if path.read_text(encoding="utf-8") != expected:
        raise AttachmentRefused("this mind's compose file changed while it was being edited; "
                                "nothing was written")
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=str(path.parent),
        prefix=".compose-", suffix=".tmp", delete=False)
    try:
        with handle as stream:
            stream.write(candidate)
        os.chmod(handle.name, state.st_mode & 0o7777)
        try:
            os.chown(handle.name, state.st_uid, state.st_gid)
        except (OSError, AttributeError):
            pass
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def reconcile(name: str, operator: bool) -> dict[str, Any]:
    """Make this mind's compose fragment say what its roles call for.

    Always the install's skills folder, `rw` and with the two environment
    lines iff `operator`, `ro` and without them otherwise. Idempotent: a
    fragment that already says it is not written. A mind this console did not
    make, or whose container is not defined by the fragment this console
    wrote, is left alone and the answer says so. Raises `AttachmentRefused`,
    having written nothing, when the fragment is in a shape it cannot edit
    with certainty.
    """
    answer: dict[str, Any] = {"applicable": False, "changed": False, "operator": operator}
    if configuration_data._own_service(name) is None:
        answer["detail"] = ("this mind was not made by this console, so its attachments "
                            "are set where its container is defined")
        return answer
    path = _fragment(name)
    try:
        target = mind_mounts.compose_target(name).fragment
    except ValueError as exc:
        raise AttachmentRefused(str(exc)) from exc
    if target != path:
        answer["detail"] = "this mind's container is defined by a compose file this console did not write"
        return answer
    answer["applicable"] = True
    try:
        original = path.read_text(encoding="utf-8")
        candidate = _rewrite(original, name, operator)
        if candidate != original:
            _write(path, original, candidate)
            answer["changed"] = True
    except (OSError, yaml.YAMLError) as exc:
        raise AttachmentRefused(f"this mind's compose file could not be edited: {exc}") from exc
    return answer
