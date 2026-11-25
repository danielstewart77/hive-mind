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
A fragment with no `volumes` or `environment` list, or an empty one, is given
one; a list written any other way than a block of plain lines is refused.
A fragment that is a link is not one this console wrote (see
`configuration_data._own_service`), so it is left alone rather than followed.
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
    if listed is None:
        return []
    if not isinstance(listed, list) or not all(isinstance(item, str) for item in listed):
        raise AttachmentRefused(
            f"this mind's {key} are not written as a block list of plain lines, which is "
            "the only shape this can edit — edit the fragment by hand")
    return list(listed)


def _service(lines: list[str], name: str) -> tuple[int, int, int, int]:
    """The service's key line, the end of its lines, its keys' indent and its
    own indent, all in line numbers and columns."""
    services_at = next((i for i, line in enumerate(lines)
                        if re.fullmatch(r"services:\s*", line.rstrip("\r\n"))), None)
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
        if indent == child_indent and re.fullmatch(rf"\s+{re.escape(name)}:\s*", line.rstrip("\r\n")):
            service_at = index
            break
    if service_at is None:
        raise AttachmentRefused(f"this fragment declares no service named {name}")
    end, key_indent = service_at + 1, None
    for index in range(service_at + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= child_indent:
            break
        if key_indent is None:
            key_indent = indent
        end = index + 1
    if key_indent is None:
        raise AttachmentRefused(f"this fragment's service {name} holds nothing to edit")
    return service_at, end, key_indent, child_indent


def _block(lines: list[str], name: str, key: str):
    """Where one service's `key:` entries are: `(start, end, indent, header)`,
    the half-open line range of the entries and the indent they take. A key
    that is absent, or written `key: []`, has no entries yet: the range is
    empty and `header` says what to do about the key itself, `("add", pad)`
    or `("replace", line, pad)`; otherwise it is None."""
    service_at, service_end, key_indent, _child = _service(lines, name)
    key_at = None
    for index in range(service_at + 1, service_end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == key_indent and re.fullmatch(rf"{key}:(\s*(\[\s*\])?)?(\s+#.*)?", line.strip()):
            key_at = index
            break
        if indent == key_indent and re.match(rf"{key}:", line.strip()):
            raise AttachmentRefused(
                f"this mind's {key} are not written as a block list of plain lines, which "
                "is the only shape this can edit — edit the fragment by hand")
    pad = " " * key_indent
    if key_at is None:
        return service_end, service_end, pad + "  ", ("add", pad)
    flow_empty = re.search(r"\[\s*\]", lines[key_at]) is not None
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
    if flow_empty and end > key_at + 1:
        raise AttachmentRefused(
            f"this mind's {key} are written both as an empty list and as lines; "
            "edit the fragment by hand")
    return (key_at + 1, end, entry_indent or pad + "  ",
            ("replace", key_at, pad) if flow_empty else None)


def _plain(line: str) -> str | None:
    """The value a list line declares, or None when it is not a plain one
    (anchored, aliased, a flow collection or a comment): such a line is kept
    as it is, and never matched. A comment after the value is not part of it."""
    stripped = line.strip()
    if not stripped.startswith("-"):
        return None
    value = stripped[1:].strip()
    if value.startswith(("&", "*", "<<", "{", "[", "#")):
        return None
    if value[:1] in {'"', "'"}:
        closing = value.find(value[0], 1)
        if closing < 0:
            return None
        return value[1:closing]
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def _comment(line: str) -> str:
    """The `# ...` a list line ends with, with the space before it, or ''."""
    value = line.strip()[1:].strip()
    if value[:1] in {'"', "'"}:
        rest = value[value.find(value[0], 1) + 1:]
    else:
        match = re.search(r"\s+#", value)
        rest = value[match.start():] if match else ""
    comment = re.fullmatch(r"\s*(#.*)", rest)
    return f"  {comment.group(1)}" if comment else ""


def _skills_line(spec: str) -> bool:
    fields = mind_mounts._spec_fields(spec)
    return len(fields) >= 2 and fields[1].rstrip("/") == configuration_data.WORKFLOW_SKILLS_CONTAINER_PATH


def _edit_block(lines: list[str], name: str, key: str, change, append: list[str],
                newline: str) -> list[str]:
    """`lines` with one list's plain entries passed through `change(value)`
    (the new value, or None to drop the line), and `append` added after the
    last entry, the list being made first when the fragment has none. Every
    other line, comments included, is kept as it is, and what is added ends
    as the file's own lines do."""
    start, end, indent, header = _block(lines, name, key)
    kept: list[str] = []
    for line in lines[start:end]:
        value = _plain(line)
        if value is None:
            kept.append(line)
            continue
        replaced = change(value)
        if replaced is None:
            continue
        kept.append(line if replaced == value
                    else f"{indent}- {replaced}{_comment(line)}{newline}")
    at = len(kept)
    while at > 0 and not kept[at - 1].strip():
        at -= 1
    kept[at:at] = [f"{indent}- {value}{newline}" for value in append]
    head = lines[:start]
    if start > 0 and not any(line.lstrip().startswith("-") for line in kept) and not (
            header and header[0] == "add"):
        # Nothing is left in the list: say so as `key: []`, not as a bare key
        # (which reads as null), and keep any comment that was in it.
        pad = " " * (len(head[start - 1]) - len(head[start - 1].lstrip()))
        head[start - 1] = f"{pad}{key}: []{newline}"
        return head + kept + lines[end:]
    if header and header[0] == "add":
        kept.insert(0, f"{header[1]}{key}:{newline}")
    elif header:
        head[header[1]] = f"{header[2]}{key}:{newline}"
    return head + kept + lines[end:]


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
    # What is added ends as this file's own lines do.
    newline = "\r\n" if original.count("\r\n") * 2 > original.count("\n") else "\n"
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline

    if want_volumes != volumes:
        line = next(spec for spec in want_volumes if _skills_line(spec))
        lines = _edit_block(lines, name, "volumes",
                            lambda value: line if _skills_line(value) else value,
                            [] if any(_skills_line(spec) for spec in volumes) else [line],
                            newline)
    if want_environment != environment:
        owned = (CREDENTIAL_VARIABLE, CONSOLE_VARIABLE)
        lines = _edit_block(
            lines, name, "environment",
            lambda value: (None if value.split("=", 1)[0] in owned
                           and value not in want_environment else value),
            [item for item in want_environment if item not in environment], newline)
    candidate = "".join(lines)

    try:
        parsed = yaml.safe_load(candidate)
        after_volumes = _entries(parsed, name, "volumes")
        after_environment = _entries(parsed, name, "environment")
    except (yaml.YAMLError, AttachmentRefused) as exc:
        raise AttachmentRefused(f"that edit would leave the fragment unreadable: {exc}") from exc
    expected = yaml.safe_load(original)
    # A list the edit emptied is left as a bare key, which reads as null.
    for key, wanted in (("volumes", want_volumes), ("environment", want_environment)):
        expected["services"][name][key] = wanted
        if not wanted and parsed["services"][name].get(key) is None:
            parsed["services"][name][key] = wanted
    if (after_volumes != want_volumes or after_environment != want_environment
            or parsed != expected):
        raise AttachmentRefused(
            "that edit did not come out as what was asked for, so nothing was written — "
            "this fragment is in a shape this cannot edit")
    return candidate


def _write(path: Path, expected: str, candidate: str) -> None:
    """Replace the fragment atomically, keeping its owner and mode, and only
    if it is still what was read. A link never gets here: a fragment that is
    one is not a file this console wrote."""
    state = path.stat()
    with open(path, encoding="utf-8", newline="") as stream:
        current = stream.read()
    if current != expected:
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
        # `newline=""`: a CRLF file is read, compared and written as it is.
        with open(path, encoding="utf-8", newline="") as stream:
            original = stream.read()
        candidate = _rewrite(original, name, operator)
        if candidate != original:
            _write(path, original, candidate)
            answer["changed"] = True
    except (OSError, yaml.YAMLError) as exc:
        raise AttachmentRefused(f"this mind's compose file could not be edited: {exc}") from exc
    return answer
