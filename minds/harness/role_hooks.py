"""Install the hooks a role names into one mind's harness, from this repository.

A role names hooks; this is how a mind comes to have them without a file
copied by hand. The scripts are kept in ``minds/harness/role_hook_scripts/``
and are the same for every mind and every harness: each reads the mind's own
environment or resolves from where it sits.

What an install does, for one harness home (``.claude``, ``.codex`` or
``.dsh``):

* copies each wanted hook's files into ``<home>/role-hooks/`` — a directory
  of this installer's own, so nothing a mind keeps by hand in ``<home>/hooks/``
  is ever written over;
* registers each hook in that harness's own hooks file, in its own format:
  ``settings.json`` for Claude, ``config.toml`` for Codex, and for dsh the
  file the mind's ``DSH_HOOKS_CONFIG`` names, else ``<home>/hooks.json``.

Run again, it rewrites the scripts and brings its own registrations up to
this repository's command and timeout. It never registers a hook twice.

A registration is this installer's when it runs exactly the script it put in
``<home>/role-hooks/``. A registration of the same hook anywhere else is a
hand copy, and a hand copy is left alone and reported with how it differs
from the tracked hook. Only ``replace`` rewrites one, and only after the
difference has been handed to the caller.

The four baseline hooks (memory capture, retrieval and the two rotation
hooks) are not installed from here: each harness has its own installer for
those, and a granted baseline name is reported as such.

Every hooks file is written whole — staged beside the real file, read back,
then renamed over it — so a spawn reading it never sees half. A file that
cannot be read as its format is left untouched and the install reports it.

::

    python3 -m minds.harness.role_hooks --harness dsh --home DIR \\
        [--hooks a,b] [--hooks-file FILE] [--replace] [--python PATH]

Without ``--hooks`` the names are the mind's grants from the gateway. It
prints one JSON object naming what it did.
"""

from __future__ import annotations

import argparse
import difflib
import fcntl
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from minds.harness import role_guard

#: Where the tracked scripts are kept in this repository.
SCRIPTS_DIR = Path(__file__).resolve().parent / "role_hook_scripts"

#: dsh's own transcript reader, which the Python hooks need under dsh.
DSH_TRANSCRIPT = Path(__file__).resolve().parent / "dsh_hook_scripts" / "dsh_transcript.py"

#: The directory inside a harness home that belongs to this installer.
INSTALL_DIR = "role-hooks"

HARNESSES = ("claude", "codex", "dsh")

#: Installed by each harness's own installer, never from here.
BASELINE = ("auto_remember", "contextual_retrieval", "rotation_check", "rotation_fire")


@dataclass(frozen=True)
class Hook:
    """One tracked hook: what it is called, what runs it and when."""

    name: str
    script: str
    events: tuple[str, ...]
    #: Seconds the harness waits; a prompt hook holds the prompt, so it has one.
    timeout: int | None = None
    #: Other files the script needs beside it.
    helpers: tuple[str, ...] = ()
    #: What follows the script on the command line: nothing, the harness and
    #: repository ("harness"), or the interpreter and repository ("render").
    arguments: str = ""
    #: Other names a hand copy of this hook is registered under.
    aliases: tuple[str, ...] = ()


CATALOGUE: dict[str, Hook] = {hook.name: hook for hook in (
    Hook("time_inject", "time_inject.sh", ("UserPromptSubmit",), timeout=10),
    Hook("surface_inject", "surface_inject.sh", ("UserPromptSubmit",), timeout=10),
    Hook("prose_reminder", "prose_reminder.sh", ("UserPromptSubmit",), timeout=10,
         helpers=("operator_name.sh",)),
    Hook("soul_proposal_inject", "soul_proposal_inject.sh", ("UserPromptSubmit",),
         timeout=10, helpers=("operator_name.sh",)),
    Hook("skill_render_check", "skill_render_check.sh", ("Stop",), arguments="render"),
    Hook("bg_snapshot", "bg_snapshot.py", ("Stop", "SubagentStop"),
         helpers=("rotation_state.py",)),
    Hook("skill_telemetry", "skill_telemetry.py", ("Stop",), arguments="harness",
         helpers=("role_hook_env.py",), aliases=("skill_telemetry_capture",)),
    Hook("training_capture", "training_capture.py", ("Stop",), arguments="harness",
         helpers=("role_hook_env.py",)),
)}


class HooksFileError(ValueError):
    """The mind's hooks file cannot be read as its format, or would not be
    after the edit. Nothing has been written when this is raised."""


def hooks_file_for(harness: str, home: Path, hooks_file: Path | None = None) -> Path:
    """The file this harness reads its hook registrations from."""
    if hooks_file is not None:
        return Path(hooks_file)
    return Path(home) / {"claude": "settings.json", "codex": "config.toml",
                         "dsh": "hooks.json"}[harness]


def command_for(hook: Hook, harness: str, home: Path, *, python: str,
                app_root: Path) -> str:
    """The command line that runs this hook from this harness home."""
    script = str(Path(home) / INSTALL_DIR / hook.script)
    if hook.script.endswith(".py"):
        words = [python, script]
    else:
        words = ["bash", script]
    if hook.arguments == "harness":
        words += ["--harness", harness, "--app-root", str(app_root)]
    elif hook.arguments == "render":
        words += [python, str(app_root)]
    return " ".join(shlex.quote(word) for word in words)


def _script_path(command: str) -> Path | None:
    """The script a hook command runs: its first argument with a script suffix."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for word in words:
        base = word.rsplit("/", 1)[-1]
        if any(base.endswith(suffix) and len(base) > len(suffix)
               for suffix in role_guard.SCRIPT_SUFFIXES):
            return Path(word)
    return None


def _is_ours(command: str, hook: Hook, home: Path) -> bool:
    script = _script_path(command)
    if script is None:
        return False
    try:
        return script.resolve() == (Path(home) / INSTALL_DIR / hook.script).resolve()
    except (OSError, ValueError):
        return False


def _names(hook: Hook) -> set[str]:
    return {hook.name, *hook.aliases}


# --------------------------------------------------------------------------
# The two shapes a hooks file takes. Each holds the file's text and answers
# the same four questions; neither writes anything.
# --------------------------------------------------------------------------

class _JsonHooks:
    """Claude's ``settings.json`` and dsh's ``hooks.json``: one shape."""

    def __init__(self, text: str, harness: str):
        self.harness = harness
        try:
            self.data = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            raise HooksFileError(f"not JSON: {exc}") from exc
        if not isinstance(self.data, dict) or not isinstance(
                self.data.get("hooks", {}), dict):
            raise HooksFileError("the hooks file is not a JSON object with a hooks object")

    def _items(self):
        for event, groups in (self.data.get("hooks") or {}).items():
            for group in groups if isinstance(groups, list) else []:
                items = group.get("hooks") if isinstance(group, dict) else None
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict) and isinstance(item.get("command"), str):
                        yield event, item

    def entries(self) -> list[tuple[str, str, int | None]]:
        return [(event, item["command"], item.get("timeout"))
                for event, item in self._items()]

    def rewrite(self, event: str, old: str, new: str, timeout: int | None) -> None:
        for found, item in self._items():
            if found == event and item["command"] == old:
                item["command"] = new
                item.pop("timeout", None)
                if timeout:
                    item["timeout"] = timeout
                return

    def add(self, event: str, command: str, timeout: int | None) -> None:
        item = {"type": "command", "command": command,
                **({"timeout": timeout} if timeout else {})}
        group = {"matcher": "", "hooks": [item]} if self.harness == "claude" else {
            "hooks": [item]}
        groups = self.data.setdefault("hooks", {}).setdefault(event, [])
        if not isinstance(groups, list):
            raise HooksFileError(f"the {event} hooks are not a list")
        groups.append(group)

    def text(self) -> str:
        return json.dumps(self.data, indent=2) + "\n"


_COMMAND_LINE = re.compile(r"^(\s*)command\s*=\s*(.+?)\s*$")
_TIMEOUT_LINE = re.compile(r"^\s*timeout\s*=")


def _toml_value(source: str):
    try:
        return tomllib.loads("x = " + source)["x"]
    except tomllib.TOMLDecodeError:
        return None


class _CodexHooks:
    """Codex's ``config.toml``. The file is the operator's and carries their
    comments and layout, so it is edited as text, a line at a time, and every
    edit is judged by reading the result back."""

    def __init__(self, text: str, harness: str = "codex"):
        self.source = text
        self._parse(text)

    @staticmethod
    def _parse(text: str) -> dict:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise HooksFileError(f"not TOML: {exc}") from exc
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            raise HooksFileError("the hooks table is not a table")
        return hooks

    def entries(self) -> list[tuple[str, str, int | None]]:
        found = []
        for event, groups in self._parse(self.source).items():
            for group in groups if isinstance(groups, list) else []:
                items = group.get("hooks") if isinstance(group, dict) else None
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict) and isinstance(item.get("command"), str):
                        found.append((event, item["command"], item.get("timeout")))
        return found

    @staticmethod
    def _event_of(lines: list[str], index: int) -> str | None:
        """The event whose table the line at ``index`` sits in."""
        for line in reversed(lines[:index]):
            if match := re.match(r"\s*\[\[hooks\.([A-Za-z]+)(?:\.hooks)?\]\]", line):
                return match.group(1)
            if line.lstrip().startswith("["):
                return None
        return None

    def rewrite(self, event: str, old: str, new: str, timeout: int | None) -> None:
        lines = self.source.splitlines(keepends=True)
        at = [index for index, line in enumerate(lines)
              if (match := _COMMAND_LINE.match(line.rstrip("\r\n")))
              and _toml_value(match.group(2)) == old]
        if len(at) > 1:
            # One hook on two events is the same command line twice; the
            # event's own table says which of them this is.
            at = [index for index in at if self._event_of(lines, index) == event]
        if len(at) != 1:
            raise HooksFileError(
                f"{len(at)} lines of the Codex configuration run {old!r} on "
                f"{event}; one is needed to rewrite it in place")
        index = at[0]
        indent = _COMMAND_LINE.match(lines[index].rstrip("\r\n")).group(1)
        end = "\r\n" if lines[index].endswith("\r\n") else "\n"
        replacement = [f"{indent}command = {json.dumps(new)}{end}"]
        if timeout:
            replacement.append(f"{indent}timeout = {timeout}{end}")
        # The entry's own timeout, wherever it sits between this entry's
        # header and the next table.
        first = index
        while first > 0 and not lines[first - 1].lstrip().startswith("["):
            first -= 1
        last = index + 1
        while last < len(lines) and not lines[last].lstrip().startswith("["):
            last += 1
        body = [line for line in lines[first:index] if not _TIMEOUT_LINE.match(line)]
        tail = [line for line in lines[index + 1:last] if not _TIMEOUT_LINE.match(line)]
        candidate = "".join(lines[:first] + body + replacement + tail + lines[last:])
        before = sorted(self.entries(), key=repr)
        after = sorted(_CodexHooks(candidate).entries(), key=repr)
        expected = sorted([entry for entry in before if entry[:2] != (event, old)]
                          + [(event, new, timeout)], key=repr)
        if after != expected or len(before) != len(after):
            raise HooksFileError(
                f"rewriting {old!r} in place would change more than that entry")
        self.source = candidate

    def add(self, event: str, command: str, timeout: int | None) -> None:
        block = (f"[[hooks.{event}]]\n[[hooks.{event}.hooks]]\n"
                 f'type = "command"\ncommand = {json.dumps(command)}\n')
        if timeout:
            block += f"timeout = {timeout}\n"
        text = self.source
        if text and not text.endswith("\n"):
            text += "\n"
        candidate = text + ("\n" if text else "") + block
        before = self.entries()
        try:
            after = _CodexHooks(candidate).entries()
        except HooksFileError as exc:
            raise HooksFileError(
                f"a {event} hook cannot be appended to this Codex "
                f"configuration: {exc}") from exc
        if sorted(after, key=repr) != sorted(before + [(event, command, timeout)], key=repr):
            raise HooksFileError(
                f"appending a {event} hook would change more than that entry")
        self.source = candidate

    def text(self) -> str:
        return self.source


def _open(harness: str, text: str):
    return (_CodexHooks if harness == "codex" else _JsonHooks)(text, harness)


# --------------------------------------------------------------------------
# What differs between a hand copy and the tracked hook
# --------------------------------------------------------------------------

class HandCopyUnreadable(OSError):
    """A hand copy's script could not be read, so its difference from the
    tracked hook cannot be named — and what cannot be named is not replaced."""


def differences(hook: Hook, hand_command: str, hand_timeout: int | None,
                tracked_command: str) -> list[dict]:
    """How a hand copy differs from the tracked hook, one entry per thing.

    The registration itself, then every file: the script the hand command
    runs, and each file beside it that has the name of a tracked one. A
    tracked file with no hand counterpart is new; a hand wrapper the tracked
    hook has no use for is shown whole, because it is being dropped.
    """
    found: list[dict] = []
    if hand_command != tracked_command:
        found.append({"what": "command", "hand": hand_command, "tracked": tracked_command})
    if hand_timeout != hook.timeout:
        found.append({"what": "timeout", "hand": hand_timeout, "tracked": hook.timeout})
    script = _script_path(hand_command)
    if script is None:
        raise HandCopyUnreadable(f"no script in the hand command {hand_command!r}")
    try:
        hand_text = script.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise HandCopyUnreadable(f"{script}: {exc}") from exc
    tracked_files = (hook.script, *hook.helpers)
    if script.name not in tracked_files:
        found.append({"what": "file", "file": script.name, "only": "hand",
                      "hand_path": str(script), "content": hand_text.splitlines()})
    for name in tracked_files:
        tracked_text = (SCRIPTS_DIR / name).read_text()
        hand_path = script.parent / name
        try:
            hand_text = hand_path.read_text()
        except FileNotFoundError:
            found.append({"what": "file", "file": name, "only": "tracked"})
            continue
        except (OSError, UnicodeDecodeError) as exc:
            raise HandCopyUnreadable(f"{hand_path}: {exc}") from exc
        if hand_text == tracked_text:
            continue
        found.append({"what": "file", "file": name, "hand_path": str(hand_path),
                      "diff": list(difflib.unified_diff(
                          hand_text.splitlines(), tracked_text.splitlines(),
                          fromfile=f"hand/{name}", tofile=f"tracked/{name}",
                          lineterm=""))})
    return found


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _write_whole(target: Path, content: str | bytes, *, mode: int | None = None,
                 check: Callable[[Path], None] | None = None) -> None:
    """Put *content* at *target* whole or not at all.

    Staged beside the real file — the one a symlink leads to, so the link
    survives — given the old file's mode, read back by *check*, then renamed.
    """
    target = Path(os.path.realpath(target))
    try:
        old = target.stat()
    except FileNotFoundError:
        old = None
    if mode is None:
        mode = old.st_mode & 0o7777 if old is not None else 0o644
    data = content.encode() if isinstance(content, str) else content
    fd, staging = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(staging, mode)
        # Run as root over a file the mind owns, the file stays the mind's;
        # a new file takes its directory's owner.
        keep = old if old is not None else target.parent.stat()
        if os.geteuid() == 0 and (keep.st_uid, keep.st_gid) != (0, 0):
            os.chown(staging, keep.st_uid, keep.st_gid)
        if check is not None:
            check(Path(staging))
        os.replace(staging, target)
    except BaseException:
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass
        raise


def _files(hooks: list[Hook], harness: str) -> dict[str, Path]:
    """Every file these hooks need in the install directory, by name."""
    wanted: dict[str, Path] = {}
    for hook in hooks:
        for name in (hook.script, *hook.helpers):
            wanted[name] = SCRIPTS_DIR / name
        if hook.arguments == "harness" and harness == "dsh":
            wanted[DSH_TRANSCRIPT.name] = DSH_TRANSCRIPT
    return wanted


def install(harness: str, home: Path, names: list[str], *,
            hooks_file: Path | None = None, python: str = sys.executable,
            app_root: Path = APP_ROOT, replace: bool = False,
            on_difference: Callable[[str, list[dict]], None] | None = None) -> dict:
    """Install the named hooks into this harness home; what was done, per hook.

    Each name ends as one of: ``installed`` (registered now), ``current``
    (already registered as tracked), ``updated`` (its registration brought up
    to this repository's), ``replaced`` (a hand copy rewritten to the tracked
    hook), ``hand_copy_kept``, ``baseline`` or ``unknown``.

    *on_difference* is called with a hook's name and its differences before
    anything of that hook's is written.
    """
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}")
    if not os.path.isabs(python):
        raise ValueError(f"the interpreter must be an absolute path: {python!r}")
    # Absolute, because the registered commands are run from wherever the
    # harness happens to be.
    home = Path(os.path.abspath(home))
    path = Path(os.path.abspath(hooks_file_for(harness, home, hooks_file)))
    report: dict = {"harness": harness, "home": str(home), "hooks_file": str(path),
                    # Where the installed hooks will take the mind's folder to
                    # be unless HIVE_MIND_DIR says otherwise.
                    "mind_dir": str(home.resolve().parent),
                    "hooks": {}, "hooks_file_written": False}
    wanted: list[Hook] = []
    for name in dict.fromkeys(names):
        if name in CATALOGUE:
            wanted.append(CATALOGUE[name])
        elif name in BASELINE:
            report["hooks"][name] = {"status": "baseline"}
        else:
            report["hooks"][str(name)] = {"status": "unknown"}
    if not wanted:
        return report

    home.mkdir(parents=True, exist_ok=True)
    # Beside the hooks file itself, so two homes that share one exclude each
    # other.
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    with open(real.parent / f".{real.name}.role-hooks.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            text = path.read_text() if path.exists() else ""
            hooks = _open(harness, text)
        except (HooksFileError, OSError, UnicodeDecodeError) as exc:
            report["error"] = f"{path}: {exc}"
            return report

        expected: list[tuple[str, str, int | None]] = []
        to_copy: list[Hook] = []
        for hook in wanted:
            tracked = command_for(hook, harness, home, python=python, app_root=app_root)
            entries = hooks.entries()
            ours = [entry for entry in entries if _is_ours(entry[1], hook, home)]
            hand = [entry for entry in entries
                    if role_guard.hook_name(entry[1]) in _names(hook)
                    and not _is_ours(entry[1], hook, home)]
            outcome: dict = {}
            if hand:
                try:
                    found: list[dict] = []
                    for _event, command, timeout in dict.fromkeys(
                            (None, entry[1], entry[2]) for entry in hand):
                        found += differences(hook, command, timeout, tracked)
                    outcome["differences"] = found
                except HandCopyUnreadable as exc:
                    outcome.update(status="hand_copy_kept",
                                   reason=f"its difference cannot be named: {exc}")
                else:
                    events = [entry[0] for entry in hand]
                    if not replace:
                        outcome["status"] = "hand_copy_kept"
                    elif ours or len(set(events)) != len(events):
                        outcome.update(
                            status="hand_copy_kept",
                            reason="registered more than once for one event")
                    elif not set(events) <= set(hook.events):
                        stray = sorted(set(events) - set(hook.events))
                        outcome.update(
                            status="hand_copy_kept",
                            reason="registered for an event the tracked hook "
                                   f"does not run on: {', '.join(stray)}")
                    else:
                        if on_difference is not None:
                            on_difference(hook.name, found)
                        before = hooks.text()
                        try:
                            for event, command, _timeout in hand:
                                hooks.rewrite(event, command, tracked, hook.timeout)
                            for event in hook.events:
                                if event not in events:
                                    hooks.add(event, tracked, hook.timeout)
                        except HooksFileError as exc:
                            hooks = _open(harness, before)
                            outcome.update(status="hand_copy_kept", reason=str(exc))
                        else:
                            outcome["status"] = "replaced"
                            expected += [(event, tracked, hook.timeout)
                                         for event in {*events, *hook.events}]
                            to_copy.append(hook)
                report["hooks"][hook.name] = outcome
                continue

            own_events = [entry[0] for entry in ours]
            if len(set(own_events)) != len(own_events):
                # Not this installer's doing, and not its to guess which one
                # the operator meant to keep.
                report["hooks"][hook.name] = {
                    "status": "registered_twice",
                    "reason": "the tracked hook is already registered more than "
                              "once for one event; nothing of it was changed"}
                continue
            changed = added = False
            before = hooks.text()
            try:
                for event, command, timeout in ours:
                    if (command, timeout) != (tracked, hook.timeout):
                        hooks.rewrite(event, command, tracked, hook.timeout)
                        changed = True
                for event in hook.events:
                    if event not in own_events:
                        hooks.add(event, tracked, hook.timeout)
                        added = True
            except HooksFileError as exc:
                hooks = _open(harness, before)
                report["hooks"][hook.name] = {"status": "failed", "reason": str(exc)}
                continue
            expected += [(event, tracked, hook.timeout)
                         for event in {*hook.events, *(entry[0] for entry in ours)}]
            to_copy.append(hook)
            report["hooks"][hook.name] = {
                "status": "installed" if added and not ours else
                          "updated" if changed or added else "current"}

        if not to_copy:
            return report

        # Scripts first: a registration never names a file that is not there.
        directory = home / INSTALL_DIR

        def refused(why: str) -> dict:
            report["error"] = why
            for hook in to_copy:
                report["hooks"][hook.name] = {"status": "failed", "reason": why}
            return report

        if directory.is_symlink():
            # The whole folder is a link the mind made: every write would
            # land in the folder at its far end.
            return refused(f"{directory} is a link the mind made to another folder; "
                           "nothing was installed through it")
        directory.mkdir(parents=True, exist_ok=True)
        for name, source in _files(to_copy, harness).items():
            mode = 0o755 if name.endswith((".py", ".sh")) else 0o644
            data = source.read_bytes()
            target = directory / name
            if target.is_symlink():
                # A link is the mind's doing, wherever it leads: writing
                # would go through it and replace the file at its far end.
                # Said unless it already leads to the repository's bytes.
                # One hook's link never costs the other hooks theirs.
                try:
                    same = target.read_bytes() == data
                except OSError:
                    same = False
                if not same:
                    report.setdefault("links_kept", []).append(name)
                continue
            try:
                # Run at every spawn of a managed mind: a file that is
                # already the repository's is not written again.
                if target.read_bytes() == data and target.stat().st_mode & 0o777 == mode:
                    continue
            except OSError:
                pass
            _write_whole(target, data, mode=mode)
        report["scripts"] = sorted(_files(to_copy, harness))

        encoded = hooks.text()
        if encoded != text:
            def check(staged: Path) -> None:
                found = _open(harness, staged.read_text()).entries()
                for entry in expected:
                    if found.count(entry) != 1:
                        raise HooksFileError(
                            f"{entry[1]!r} would be registered {found.count(entry)} "
                            f"times for {entry[0]}")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_whole(path, encoded, check=check)
            except (HooksFileError, OSError) as exc:
                report["error"] = f"{path}: {exc}"
                for hook in to_copy:
                    report["hooks"][hook.name] = {"status": "failed"}
                return report
            report["hooks_file_written"] = True
    return report


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

_HOME_VARIABLE = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "dsh": "DSH_HOME"}


def granted_hooks(environ: dict | None = None) -> list[str] | None:
    """The hook names this mind's roles grant; None when nobody could say.

    None covers a gateway that did not answer and a mind that is not under
    role management alike: in both, no role has named a hook to install.
    """
    environ = os.environ if environ is None else environ
    mind = environ.get("MIND_ID") or environ.get("MIND_NAME") or ""
    grants = role_guard.fetch(mind, environ.get("COMMS_URL", ""),
                              environ.get("COMMS_BEARER_TOKEN", ""))
    if not grants or not grants.get("managed"):
        return None
    hooks = grants.get("hooks")
    return [str(name) for name in hooks] if isinstance(hooks, list) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m minds.harness.role_hooks",
        description="Install the hooks a role names into one mind's harness home.")
    parser.add_argument("--harness", required=True, choices=HARNESSES)
    parser.add_argument("--home", type=Path,
                        help="the harness home; by default the one the environment names")
    parser.add_argument("--hooks-file", type=Path,
                        help="the hooks file, when it is not the home's own "
                             "(dsh: DSH_HOOKS_CONFIG by default)")
    parser.add_argument("--hooks", help="comma-separated hook names; by default "
                                        "the mind's grants from the gateway")
    parser.add_argument("--replace", action="store_true",
                        help="rewrite a hand copy to the tracked hook, after "
                             "its difference has been printed")
    parser.add_argument("--python", default=sys.executable,
                        help="the interpreter the Python hooks are registered with")
    args = parser.parse_args(argv)

    home = args.home or (Path(os.environ[_HOME_VARIABLE[args.harness]])
                         if os.environ.get(_HOME_VARIABLE[args.harness]) else None)
    if home is None:
        print(json.dumps({"error": f"no --home and no {_HOME_VARIABLE[args.harness]}"}))
        return 2
    hooks_file = args.hooks_file
    if hooks_file is None and args.harness == "dsh" and os.environ.get("DSH_HOOKS_CONFIG"):
        hooks_file = Path(os.environ["DSH_HOOKS_CONFIG"])
    if hooks_file is not None and hooks_file.name == "hooks.effective.json":
        print(json.dumps({"error": f"{hooks_file} is generated at every spawn; "
                                   "name the hooks file it is generated from"}))
        return 2
    if args.hooks is not None:
        names = [name.strip() for name in args.hooks.split(",") if name.strip()]
    else:
        granted = granted_hooks()
        if granted is None:
            print(json.dumps({"error": "the gateway named no hooks for this mind "
                                       "and --hooks was not given; nothing installed"}))
            return 3
        names = granted

    def announce(name: str, found: list[dict]) -> None:
        print(json.dumps({"replacing": name, "differences": found}), flush=True)

    try:
        report = install(args.harness, home, names, hooks_file=hooks_file,
                         python=args.python, replace=args.replace,
                         on_difference=announce)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(report))
    return 2 if report.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
