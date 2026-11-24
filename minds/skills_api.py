"""The mind's own view of its skills: what the repo ships, what it runs.

A skill exists three ways. `specs/skills/` is the tracked reference that
travels with a clone; `minds/<name>/reference/skills/` is this mind's own
reference, which is where a skill tuned to its job lives; and each harness
reads a rendered copy from its own directory (`$CLAUDE_CONFIG_DIR/skills`,
`$CODEX_HOME/skills`, `$DSH_HOME/skills`). `skill_reference` owns the
rendering. This module reports how the three stand and moves a skill between
the repo and the mind.

All of it lives on the mind's own filesystem, so the mind is the only thing
that can see it. It reports and applies changes; the console renders and
issues the actions. That is the same shape as `runtime_config` and for the
same reason: a container in this stack, a bare-metal mind on this host, and
a mind on another machine are one code path, and no bind mount can reach the
third.

A row is `same` when the repo's reference matches this mind's and the
harness's copy is exactly what was rendered from it. A freshly rendered copy
is therefore `same` even though its frontmatter differs from the reference:
each harness's copy carries only the frontmatter that harness reads.

A skill can also be *generated*: `install/workflow-skills/` at the top of the
checkout is one folder on the machine, contents ignored by git, where the
operator's mind writes the skills a workflow document asks for. A role that
names one has it delivered exactly as a tracked skill is. When a name is in
both places the generated one wins and the clash is reported, on the pair and
in the log; nothing generated is ever written into `specs/skills/`. The
untouched as-generated copy sits in `.as-generated/` beside them and is not a
skill (a leading dot is never a skill name).

A skill is a *directory*. State compares every file in it, not just the
`SKILL.md`, because a copy moves the whole tree — a `SKILL.md` that matched
while a sibling script differed would report "in sync" and offer no way to
fix it.
"""

from __future__ import annotations

import asyncio
import difflib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import logging
import os
import tempfile

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from core.hive_logging import log_event
from minds import skill_reference
from minds.runtime_api import authorize_admin

PROJECT_DIR = Path(__file__).resolve().parents[1]

log = logging.getLogger("hive-mind.skills")

SKILL_FILE = "SKILL.md"

# A skill directory name. These arrive over HTTP and become filesystem
# paths, so: no separators, no leading dot. A leading dot would admit `..`
# and would also pick up the `.archived` directories a mind keeps.
_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

STATE_SAME = "same"
STATE_DIFFERS = "differs"
STATE_NOT_INSTALLED = "not_installed"
STATE_LOCAL_ONLY = "local_only"
# A directory that is there but cannot be read — wrong owner, bad
# permissions, a dangling symlink. Distinct from absent on purpose: the
# actions offered for "absent" delete what is there.
STATE_UNREADABLE = "unreadable"

# A skill is source, not a build artifact. Anything past this is a
# virtualenv or a node_modules that was never meant to travel.
MAX_SKILL_BYTES = skill_reference.MAX_SKILL_BYTES


class SkillError(ValueError):
    """A request that names something that cannot be a skill, or is absent."""


class SkillUnavailable(SkillError):
    """The skill root itself could not be read. Not the same as empty."""


class SkillConflict(SkillError):
    """An in-place edit that would overwrite a reference changed since."""


class SkillUnstable(SkillUnavailable):
    """A generated skill that is being written as it is read: nothing is delivered."""


class SkillRefused(SkillError):
    """A skill no harness may be handed as written."""


class SkillHarnessUndeclared(SkillError):
    """The mind declares no home for this harness, so nothing is rendered there."""


#: Where the reference a skill is delivered from lives.
SOURCE_TRACKED = "tracked"
SOURCE_GENERATED = "generated"


@dataclass(frozen=True)
class SkillPair:
    """One skill as it exists in the repo and on this mind.

    `repo_text` is the text of the source the skill is delivered from, named
    by `source`: `tracked` (`specs/skills/`) or `generated` (the install's
    own folder). When both hold the name, the generated one is the source and
    `shadowed_text` holds what the tracked one says, so neither is hidden.
    """

    name: str
    state: str
    repo_text: str | None
    installed_text: str | None
    reference_text: str | None = None
    # How the harness's copy stands against what was rendered for it:
    # `rendered`, `edited`, `stale`, `missing` or `unmanaged`.
    copy: str = "missing"
    source: str = SOURCE_TRACKED
    shadowed_text: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "repo": self.repo_text,
            "installed": self.installed_text,
            "reference": self.reference_text,
            "copy": self.copy,
            "source": self.source,
            "shadowed": self.shadowed_text is not None,
            "shadowed_text": self.shadowed_text,
        }


def harness_directory(harness: str) -> str:
    """`claude_cli`, `codex_cli` and `dsh_cli` name their bare harness."""
    try:
        return skill_reference.normalize_harness(harness)
    except skill_reference.RenderError as exc:
        raise SkillError(str(exc)) from exc


def repo_root(harness: str | None = None) -> Path:
    """The tracked references. One directory: a reference is harness-neutral."""
    return PROJECT_DIR / "specs" / "skills"


def install_root() -> Path:
    """The install's one shared folder of generated skills.

    A skill in it is a complete directory with a `SKILL.md`. Its contents are
    not tracked; a mind that mounts the checkout sees it as it is, and a mind
    made from the console has it attached.
    """
    return PROJECT_DIR / "install" / "workflow-skills"


def reference_root() -> Path:
    """This mind's own references."""
    try:
        return skill_reference.reference_root() / "skills"
    except skill_reference.RenderError as exc:
        raise SkillError(str(exc)) from exc


def installed_root(harness: str) -> Path:
    """Where this harness actually loads skills from.

    Read from the environment at call time, not at import: the tests set
    these per case, and a mind's config directory is a deployment fact
    rather than a constant.
    """
    try:
        return skill_reference.harness_home(harness_directory(harness)) / "skills"
    except skill_reference.HarnessUndeclared as exc:
        raise SkillHarnessUndeclared(str(exc)) from exc


def _validate(name: str) -> str:
    if not _SKILL_NAME_RE.fullmatch(name or ""):
        raise SkillError(f"Invalid skill name: {name!r}")
    return name


def _read(root: Path, name: str) -> str | None:
    """The skill's `SKILL.md`, or None when there is no such file.

    Raises rather than returning None when the file is there but cannot be
    read — an unreadable skill must not be reported as an absent one, since
    the remedy offered for absence is to overwrite the directory.
    """
    path = root / name / SKILL_FILE
    if root == install_root():
        _refuse_links(root / name, whole=False)
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillUnavailable(f"{path} cannot be read: {exc}") from exc


def _refuse_links(directory: Path, *, whole: bool) -> None:
    """A generated skill holds no link: one could name a file anywhere on the
    host and have it read into a listing or copied into a mind. Only the
    directory and its `SKILL.md` are looked at unless `whole`, which walks
    every file the skill would carry (what a skill builds does not travel,
    so it is not looked into)."""
    try:
        if directory.is_symlink() or (directory / SKILL_FILE).is_symlink():
            raise SkillUnstable(
                f"generated skill {directory.name} is, or holds as its {SKILL_FILE}, a "
                "link; nothing generated may point outside its own folder, so it was not read")
        if not whole:
            return
        for here, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames[:] = [d for d in dirnames if not skill_reference._is_built(d)]
            for entry in [*dirnames, *(f for f in filenames if not skill_reference._is_built(f))]:
                if (Path(here) / entry).is_symlink():
                    raise SkillUnstable(
                        f"generated skill {directory.name} holds a link "
                        f"({(Path(here) / entry).relative_to(directory)}); nothing generated "
                        "may point outside its own folder, so nothing was delivered")
    except OSError as exc:
        raise SkillUnstable(
            f"generated skill {directory.name} could not be checked ({exc}); "
            "nothing was delivered") from exc


def _fingerprint(root: Path, name: str) -> str | None:
    """A hash over every file in the skill directory, path and content.

    This is what `same` and `differs` are computed from. Comparing only the
    `SKILL.md` would call a skill synced while its scripts had drifted, and
    the console offers no action on a synced row. The same hash the render
    record uses, so built files and symlink targets count the same way.
    """
    directory = root / name
    if not (directory / SKILL_FILE).exists():
        return None
    try:
        return skill_reference.fingerprint(directory)
    except OSError as exc:
        raise SkillUnavailable(f"{directory} cannot be read: {exc}") from exc


def _names(root: Path) -> set[str]:
    """Skill names under `root`.

    An unreadable root raises. Returning an empty set would make "this mind
    has no skills" indistinguishable from "this directory could not be
    opened", and the console states the former as fact.
    """
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise SkillUnavailable(f"{root} cannot be listed: {exc}") from exc
    names = set()
    for entry in entries:
        if not _SKILL_NAME_RE.fullmatch(entry.name):
            continue
        try:
            if entry.is_dir() and (entry / SKILL_FILE).is_file():
                names.add(entry.name)
        except OSError:
            # There, but it cannot be stat'ed — a mode-000 directory or a
            # wrong owner. Name it anyway: dropping it silently is how a
            # skill that exists disappears from the inventory, and the pair
            # will report it as unreadable.
            names.add(entry.name)
    return names


_WARNED: set[tuple[str, str]] = set()


def _warn_once(key: tuple[str, str], event: str, **fields) -> None:
    """Say a standing condition once per process: a listing asks for every
    skill each time the Minds page opens."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    log_event(log, event, level=logging.WARNING, **fields)


def generated_problem() -> str | None:
    """Why the install folder cannot be read, or None.

    An absent folder is not a problem: it is every install that has no
    generated skill. One that is there and cannot be listed is reported and
    treated as holding nothing, so the tracked skills are still delivered.
    """
    try:
        _names(install_root())
    except SkillUnavailable as exc:
        return str(exc)
    return None


def _generated_names() -> set[str]:
    try:
        return _names(install_root())
    except SkillUnavailable as exc:
        _warn_once(("unreadable", str(install_root())), "mind.skill.install_folder_unreadable",
                   error=str(exc))
        return set()


def source_root(name: str) -> Path:
    """The folder a skill is delivered from: the install's own when it holds
    the name, the tracked one otherwise."""
    return install_root() if name in _generated_names() else repo_root()


def _in_install_folder(name: str) -> bool:
    """Whether the install's folder has, or may have, an entry of this name.

    Any entry counts, complete or not, a link or a directory, and a folder
    that cannot be looked into is taken to hold it: this decides what may
    never be written into the tracked skills, so doubt answers yes.
    """
    try:
        os.lstat(install_root() / name)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def _source_of(root: Path) -> str:
    return SOURCE_GENERATED if root == install_root() else SOURCE_TRACKED


def list_skills(harness: str) -> list[SkillPair]:
    """Every skill this mind knows about, from any side, sorted by name."""
    h = harness_directory(harness)
    names = (_names(repo_root()) | _generated_names() | _names(reference_root())
             | _names(installed_root(h)))
    return [_pair(h, name) for name in sorted(names)]


def _pair(harness: str, name: str) -> SkillPair:
    repo, reference, installed = source_root(name), reference_root(), installed_root(harness)
    source = _source_of(repo)
    try:
        repo_text, reference_text = _read(repo, name), _read(reference, name)
        installed_text = _read(installed, name)
        repo_hash, reference_hash = _fingerprint(repo, name), _fingerprint(reference, name)
        copy = skill_reference.copy_status(skill_reference.KIND_SKILL, name, harness)
    except (SkillUnavailable, OSError, skill_reference.RecordsCorrupt):
        return SkillPair(name, STATE_UNREADABLE, None, None, source=source)
    shadowed = None
    if source == SOURCE_GENERATED:
        try:
            shadowed = _read(repo_root(), name)
        except SkillUnavailable:
            shadowed = None
        if shadowed is not None:
            _warn_once(("clash", name), "mind.skill.generated_shadows_tracked", skill=name)
    return SkillPair(
        name, _state(repo_hash, reference_hash, copy),
        repo_text, installed_text, reference_text, copy, source, shadowed,
    )


def _state(repo_hash: str | None, reference_hash: str | None, copy: str) -> str:
    if repo_hash is None:
        return STATE_LOCAL_ONLY
    if reference_hash is None:
        return STATE_NOT_INSTALLED
    if repo_hash == reference_hash and copy == "rendered":
        return STATE_SAME
    return STATE_DIFFERS


def diff_skill(harness: str, name: str) -> str:
    """Unified diff of the repo's reference against this mind's.

    A mind with no reference yet is diffed against the copy its harness
    runs, which is what a write-back would adopt.
    """
    _validate(name)
    repo_text = _read(source_root(name), name)
    mind_text = _read(reference_root(), name)
    if mind_text is None:
        mind_text = _read(installed_root(harness), name)
    if repo_text is None and mind_text is None:
        raise SkillError(f"No such skill: {name}")
    return "".join(
        difflib.unified_diff(
            (repo_text or "").splitlines(keepends=True),
            (mind_text or "").splitlines(keepends=True),
            fromfile=f"repo/{name}/{SKILL_FILE}",
            tofile=f"mind/{name}/{SKILL_FILE}",
        )
    )


def _copy_skill(source: Path, target: Path, name: str) -> None:
    """Replace one skill directory wholesale.

    A skill is a directory, not a file — references, scripts and templates
    ride along with the SKILL.md and a copy that moved only the markdown
    would leave the rest stale. What it built (a venv, node_modules,
    bytecode) does not travel, and the swap leaves no moment with no skill.
    """
    origin = source / name
    if not (origin / SKILL_FILE).is_file():
        raise SkillError(f"No such skill: {name}")
    _guard_size(origin, name)
    destination = target / name
    staged = skill_reference._aside(destination, "incoming")
    try:
        skill_reference._copytree(origin, staged)
        skill_reference._swap_in(staged, destination)
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def _replace(path: Path) -> None:
    """Clear `path` so a rename can land on it, symlink or directory alike.

    No `ignore_errors`: a partial delete followed by a failed rename leaves
    a half-destroyed skill behind a 500, and the harness would still try to
    load it.
    """
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _transports(catalog, notify):
    return (
        catalog or skill_reference.proxy_catalog(),
        notify or skill_reference.telegram_notifier(),
    )


def _rendering(call, *args, **kwargs):
    """Run a `skill_reference` call, in this module's error vocabulary."""
    try:
        return call(*args, **kwargs)
    except skill_reference.RenderConflict as exc:
        raise SkillConflict(str(exc)) from exc
    except skill_reference.RenderRefused as exc:
        raise SkillRefused(str(exc)) from exc
    except skill_reference.HarnessUndeclared as exc:
        raise SkillHarnessUndeclared(str(exc)) from exc
    except skill_reference.RecordsCorrupt as exc:
        raise SkillUnavailable(str(exc)) from exc
    except skill_reference.RenderError as exc:
        raise SkillError(str(exc)) from exc


def _guard_size(directory: Path, name: str) -> None:
    try:
        skill_reference._guard_size(directory, name)
    except skill_reference.RenderError as exc:
        raise SkillError(str(exc)) from exc


def _stable_copy(name: str, staging: Path) -> tuple[Path, str]:
    """A generated skill as it is at one moment, copied to `staging`.

    The operator writes into the install folder while minds start, so a copy
    taken as it stands can hold a half-written skill. The copy is taken only
    when the folder's fingerprint is the same before and after it and the
    `SKILL.md` in the copy is a whole one (see `_not_a_whole_skill`) and the
    skill holds no link; otherwise nothing is delivered and the reason is
    said. A file written after `SKILL.md` cannot be told from one that was
    never meant to be there, so a sibling still to come is not detected.
    """
    origin = install_root() / name
    _refuse_links(origin, whole=True)
    try:
        before = _fingerprint(install_root(), name)
        _guard_size(origin, name)
        staged = staging / name
        skill_reference._copytree(origin, staged)
        after = _fingerprint(install_root(), name)
        text = (staged / SKILL_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillUnstable(
            f"generated skill {name} could not be read whole ({exc}); it may be being "
            "written, so nothing was delivered") from exc
    if before is None or before != after:
        raise SkillUnstable(
            f"generated skill {name} changed while it was being read; it may be being "
            "written, so nothing was delivered")
    why = _not_a_whole_skill(name, text)
    if why:
        raise SkillUnstable(f"generated skill {name} has a {SKILL_FILE} that {why}; it may "
                            "be being written, so nothing was delivered")
    return staged, text


def _not_a_whole_skill(name: str, text: str) -> str | None:
    """Why this `SKILL.md` is not yet a whole one, or None. A write cut short
    is not an empty file but one whose frontmatter never closes, so a stable
    folder is not enough: it must open and close its frontmatter, name the
    skill it sits in, and describe it."""
    if not text.strip():
        return "is empty"
    if not text.startswith("---\n"):
        return "has no frontmatter"
    if "\n---\n" not in text[3:] and not text.endswith("\n---"):
        return "has frontmatter that never closes"
    try:
        frontmatter, _body = skill_reference.split_frontmatter(text)
    except skill_reference.RenderError as exc:
        return f"has frontmatter that cannot be read ({exc})"
    if frontmatter.get("name") != name:
        return f"names {frontmatter.get('name')!r}, not {name!r}"
    if not str(frontmatter.get("description") or "").strip():
        return "has no description"
    return None


def install_skill(harness: str, name: str, *, catalog=None, notify=None,
                  only_if_unchanged: bool = False) -> SkillPair:
    """Take the source's reference onto this mind and render it.

    The source is the install's own folder when it holds the name, the
    tracked one otherwise; a generated skill is delivered from a stable copy
    of it, never from the folder as the operator may be writing it, and what
    it was delivered from is recorded with the mind's render record so a later
    change to the source can be told from a change the mind made.

    Apply and revert are one act for the harness named: its copy is
    replaced whatever it held. Every other harness goes through the ordinary
    pass, so a copy tuned there is reported as a conflict rather than
    silently discarded. A refused skill changes nothing at all.

    `only_if_unchanged` makes it an update (see `update_skill`): the copy is
    replaced only when a delivery is recorded and the mind has changed
    nothing since, judged under the lock the write takes; otherwise it raises
    `SkillConflict` and nothing is touched.
    """
    _validate(name)
    h = harness_directory(harness)
    catalog, notify = _transports(catalog, notify)
    root = source_root(name)
    with tempfile.TemporaryDirectory(prefix="skill-stage-") as scratch:
        delivered_from = None
        if root == install_root():
            origin, text = _stable_copy(name, Path(scratch))
            delivered_from = skill_reference.fingerprint(origin)
        else:
            origin, text = root / name, _read(root, name)
        if text is None:
            raise SkillError(f"No such skill: {name}")
        _rendering(
            skill_reference.install, skill_reference.KIND_SKILL, name, h, text, origin,
            catalog=catalog, notify=notify, delivered_from=delivered_from,
            only_if_unchanged=only_if_unchanged,
        )
    return _pair(h, name)


#: What `update_skill` found.
UPDATE_OWN = "own"          # nothing was delivered from the install's folder: the mind's own
UPDATE_CURRENT = "current"  # the source is what the mind has
UPDATE_DONE = "updated"     # the source moved and the mind had changed nothing: it has the new text
UPDATE_KEPT = "changed"     # the source moved and the mind had changed its copy: left as it is
UPDATE_MISSING = "missing"  # the source moved and a copy the mind was given is gone: left as it is
UPDATE_NO_HOME = "no_home"  # the source moved and a harness it was given to has no home now


def update_skill(harness: str, name: str, *, catalog=None, notify=None) -> str:
    """Hand this mind the install folder's newer copy of a generated skill it
    was given, unless it has changed its own.

    Only a copy delivered from the install's folder, and recorded as such, is
    ever touched: one the mind has always had, and every tracked skill, is the
    mind's own and is left exactly as it is, and so is a copy that was given
    from the install's folder when that folder no longer holds the name. The
    delivered-from record is the fingerprint of the source the copy was given
    from; when the source's is different, the copy is replaced only if the
    mind's reference and every harness's copy are exactly what that delivery
    left. An edit waiting in another harness's copy is the mind's change too,
    and so is a harness with no home declared, where the update could not
    land. The update is all or nothing for the mind (see `skill_reference.install`).
    A mind that has made its copy the new source itself is current.
    Returns one of the `UPDATE_*` words; raises what reading or writing raises,
    including `SkillError` when the update could not be completed and was undone.
    """
    _validate(name)
    h = harness_directory(harness)
    if source_root(name) != install_root():
        return UPDATE_OWN
    records = _rendering(skill_reference.load_records)
    recorded, stopped = skill_reference.delivery(records, skill_reference.KIND_SKILL, name, h)
    if recorded is None:
        return UPDATE_OWN
    source = _fingerprint(install_root(), name)
    if source == recorded:
        return UPDATE_CURRENT
    if stopped:
        if (stopped[0] == skill_reference.CHANGED_EDITED
                and _pair(h, name).state == STATE_SAME):
            # The mind has made its copy exactly the new source: nothing
            # differs, so nothing is reported and the next change is judged
            # from here.
            _rendering(skill_reference.refresh_delivery, skill_reference.KIND_SKILL, name,
                       source)
            return UPDATE_CURRENT
        return {skill_reference.CHANGED_MISSING: UPDATE_MISSING,
                skill_reference.CHANGED_UNAVAILABLE: UPDATE_NO_HOME}.get(stopped[0], UPDATE_KEPT)
    try:
        install_skill(h, name, catalog=catalog, notify=notify, only_if_unchanged=True)
    except SkillConflict:
        return UPDATE_KEPT
    return UPDATE_DONE


def resolve_skill(harness: str, name: str, *, catalog=None, notify=None) -> SkillPair:
    """Settle a conflict: this harness's copy becomes the reference."""
    _validate(name)
    h = harness_directory(harness)
    catalog, notify = _transports(catalog, notify)
    _rendering(skill_reference.resolve, skill_reference.KIND_SKILL, name, h,
               catalog=catalog, notify=notify)
    return _pair(h, name)


def write_back_skill(harness: str, name: str, *, catalog=None, notify=None) -> SkillPair:
    """Copy this mind's reference into the repo checkout.

    An in-place edit is merged into the reference first, and a copy with no
    reference is adopted as one, so what lands is what the mind runs.
    Nothing is committed. The write lands in a working tree whose owner
    still has to review and push it, which is the whole safety story.
    """
    _validate(name)
    h = harness_directory(harness)
    if _in_install_folder(name):
        raise SkillRefused(
            f"skill {name} is generated: it lives in this install's workflow-skills folder "
            "and is never written into the tracked skills")
    catalog, notify = _transports(catalog, notify)
    if _read(reference_root(), name) is None:
        copy = installed_root(h) / name
        if not (copy / SKILL_FILE).is_file():
            raise SkillError(f"No such skill: {name}")
        _guard_size(copy, name)
        _rendering(skill_reference.adopt, skill_reference.KIND_SKILL, name, h,
                   catalog=catalog, notify=notify)
    else:
        _rendering(skill_reference.merge_copy, skill_reference.KIND_SKILL, name,
                   catalog=catalog, notify=notify)
    _copy_skill(reference_root(), repo_root(), name)
    return _pair(h, name)


def remove_skill(harness: str, name: str) -> None:
    """Delete this mind's reference and every copy rendered from it.

    The repo is never touched. This harness's copy goes too when nothing
    rendered it, which is what removing a skill from a harness always meant.
    """
    _validate(name)
    h = harness_directory(harness)
    had_reference = _read(reference_root(), name) is not None
    if had_reference:
        _rendering(skill_reference.remove, skill_reference.KIND_SKILL, name)
    target = installed_root(h) / name
    if (target / SKILL_FILE).is_file():
        _replace(target)
    elif not had_reference:
        raise SkillError(f"No such skill: {name}")


def check_all(*, catalog=None, notify=None) -> dict:
    """The full render pass over this mind's skills and agents.

    Called with no arguments by mind start and by the gateway's pre-switch
    route; the proxy listing and the Telegram notifier are its transports.
    """
    catalog, notify = _transports(catalog, notify)
    return _rendering(skill_reference.check, catalog=catalog, notify=notify).as_dict()


def _failure(exc: Exception) -> JSONResponse:
    """One mapping for every skills failure, so the console reads one shape."""
    if isinstance(exc, SkillUnavailable):
        return JSONResponse({"error": str(exc)}, status_code=503)
    # An edit that would overwrite a newer reference, and a skill no harness
    # may be handed, are refusals of a real skill — never "no such skill".
    if isinstance(exc, SkillConflict):
        return JSONResponse({"error": str(exc)}, status_code=409)
    if isinstance(exc, SkillRefused):
        return JSONResponse({"error": str(exc)}, status_code=422)
    # A harness this mind declares no home for: a fact about the mind, not
    # a missing skill and not a missing route.
    if isinstance(exc, SkillHarnessUndeclared):
        return JSONResponse({"error": str(exc)}, status_code=409)
    if isinstance(exc, SkillError):
        return JSONResponse({"error": str(exc)}, status_code=404)
    # A malformed runtime value reaches here as a bare ValueError, and a
    # bare 500 with FastAPI's own body tells the console nothing.
    return JSONResponse({"error": str(exc)}, status_code=500)


def install_skills_routes(app: FastAPI, *, harness: str, mind_id: str, log) -> None:
    """Mount the skills routes on a mind's FastAPI app.

    Both harness servers call this, the same way both call
    `install_runtime_routes`. `harness` is the mind's default; every route
    takes the harness it acts on, since every skill is rendered into all
    three and the console says which copy it is looking at.
    """

    @app.get("/skills")
    async def get_skills(
        req: Request, harness_name: str | None = Query(None, alias="harness")
    ):
        """Every skill this mind knows: the repo's, its own, and the harness copy.

        Guarded like the writes: this returns the full text of every skill
        the mind runs, on a port reachable across the LAN.
        """
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            return {
                "harness": harness_directory(harness_name or harness),
                "skills": [
                    pair.as_dict() for pair in list_skills(harness_name or harness)
                ],
            }
        except (ValueError, OSError) as exc:
            return _failure(exc)

    @app.get("/skills/{name}/diff")
    async def get_skill_diff(
        req: Request, name: str, harness_name: str | None = Query(None, alias="harness")
    ):
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            return {"name": name, "diff": diff_skill(harness_name or harness, name)}
        except (ValueError, OSError) as exc:
            return _failure(exc)

    @app.post("/skills/{name}/install")
    async def post_skill_install(
        req: Request, name: str, harness_name: str | None = Query(None, alias="harness")
    ):
        """Take the repo's reference onto this mind — apply and revert alike."""
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            # Off the event loop: a directory copy is not instant, and this
            # loop also serves every session this mind is holding.
            pair = await asyncio.to_thread(install_skill, harness_name or harness, name)
        except (ValueError, OSError) as exc:
            return _failure(exc)
        log_event(log, "mind.skill.installed", mind_id=mind_id, skill=name)
        return {"saved": True, "skill": pair.as_dict()}

    @app.post("/skills/{name}/write-back")
    async def post_skill_write_back(
        req: Request, name: str, harness_name: str | None = Query(None, alias="harness")
    ):
        """Copy this mind's reference into its repo checkout, uncommitted."""
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            pair = await asyncio.to_thread(write_back_skill, harness_name or harness, name)
        except (ValueError, OSError) as exc:
            return _failure(exc)
        log_event(log, "mind.skill.written_back", mind_id=mind_id, skill=name)
        return {"saved": True, "skill": pair.as_dict()}

    @app.post("/skills/{name}/resolve")
    async def post_skill_resolve(
        req: Request, name: str, from_: str = Query(..., alias="from")
    ):
        """Settle a conflict: the named harness's copy becomes the reference.

        Every other copy is regenerated from it. This is what a conflict
        notification asks the operator, or the mind, to decide.
        """
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            pair = await asyncio.to_thread(resolve_skill, from_, name)
        except (ValueError, OSError) as exc:
            return _failure(exc)
        log_event(log, "mind.skill.resolved", mind_id=mind_id, skill=name, harness=from_)
        return {"saved": True, "skill": pair.as_dict()}

    @app.delete("/skills/{name}")
    async def delete_skill(
        req: Request, name: str, harness_name: str | None = Query(None, alias="harness")
    ):
        """Remove this mind's reference and copies; the repo keeps its own."""
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            await asyncio.to_thread(remove_skill, harness_name or harness, name)
        except (ValueError, OSError) as exc:
            return _failure(exc)
        log_event(log, "mind.skill.removed", mind_id=mind_id, skill=name)
        return {"removed": True, "name": name}


async def check_at_start(log, mind_id: str) -> None:
    """Render this mind's references once at start, off the event loop.

    Never fatal: a mind that cannot render its skills still answers, and
    the pass reports and notifies its own failures.
    """
    try:
        await asyncio.to_thread(check_all)
    except Exception as exc:  # noqa: BLE001
        log_event(log, "mind.skill.check_failed", level=logging.WARNING,
                  mind_id=mind_id, error=str(exc))
