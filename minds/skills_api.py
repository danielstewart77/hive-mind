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

A skill is a *directory*. State compares every file in it, not just the
`SKILL.md`, because a copy moves the whole tree — a `SKILL.md` that matched
while a sibling script differed would report "in sync" and offer no way to
fix it.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from core.hive_logging import log_event
from minds import skill_reference
from minds.runtime_api import authorize_admin

PROJECT_DIR = Path(__file__).resolve().parents[1]

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
MAX_SKILL_BYTES = 8 * 1024 * 1024


class SkillError(ValueError):
    """A request that names something that cannot be a skill, or is absent."""


class SkillUnavailable(SkillError):
    """The skill root itself could not be read. Not the same as empty."""


class SkillConflict(SkillError):
    """An in-place edit that would overwrite a reference changed since."""


class SkillRefused(SkillError):
    """A skill no harness may be handed as written."""


@dataclass(frozen=True)
class SkillPair:
    """One skill as it exists in the repo and on this mind."""

    name: str
    state: str
    repo_text: str | None
    installed_text: str | None
    reference_text: str | None = None
    # How the harness's copy stands against what was rendered for it:
    # `rendered`, `edited`, `stale`, `missing` or `unmanaged`.
    copy: str = "missing"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "repo": self.repo_text,
            "installed": self.installed_text,
            "reference": self.reference_text,
            "copy": self.copy,
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
    return skill_reference.harness_home(harness_directory(harness)) / "skills"


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
    try:
        return path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillUnavailable(f"{path} cannot be read: {exc}") from exc


def _fingerprint(root: Path, name: str) -> str | None:
    """A hash over every file in the skill directory, path and content.

    This is what `same` and `differs` are computed from. Comparing only the
    `SKILL.md` would call a skill synced while its scripts had drifted, and
    the console offers no action on a synced row.
    """
    directory = root / name
    if not (directory / SKILL_FILE).exists():
        return None
    digest = hashlib.sha256()
    try:
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            digest.update(str(path.relative_to(directory)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError as exc:
        raise SkillUnavailable(f"{directory} cannot be read: {exc}") from exc
    return digest.hexdigest()


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


def list_skills(harness: str) -> list[SkillPair]:
    """Every skill this mind knows about, from any side, sorted by name."""
    h = harness_directory(harness)
    names = _names(repo_root()) | _names(reference_root()) | _names(installed_root(h))
    return [_pair(h, name) for name in sorted(names)]


def _pair(harness: str, name: str) -> SkillPair:
    repo, reference, installed = repo_root(), reference_root(), installed_root(harness)
    try:
        repo_text, reference_text = _read(repo, name), _read(reference, name)
        installed_text = _read(installed, name)
        repo_hash, reference_hash = _fingerprint(repo, name), _fingerprint(reference, name)
        copy = skill_reference.copy_status(skill_reference.KIND_SKILL, name, harness)
    except (SkillUnavailable, OSError):
        return SkillPair(name, STATE_UNREADABLE, None, None)
    return SkillPair(
        name, _state(repo_hash, reference_hash, copy),
        repo_text, installed_text, reference_text, copy,
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
    repo_text = _read(repo_root(), name)
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


def _tree_bytes(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def _copy_skill(source: Path, target: Path, name: str) -> None:
    """Replace one skill directory wholesale.

    A skill is a directory, not a file — references, scripts and templates
    ride along with the SKILL.md and a copy that moved only the markdown
    would leave the rest stale.

    Symlinks are copied as symlinks. Following them turns a skill carrying a
    virtualenv into hundreds of megabytes of materialised interpreter, which
    a `venv/` gitignore then hides from `git status` entirely.
    """
    origin = source / name
    if not (origin / SKILL_FILE).is_file():
        raise SkillError(f"No such skill: {name}")
    size = _tree_bytes(origin)
    if size > MAX_SKILL_BYTES:
        raise SkillError(
            f"{name} is {size // (1024 * 1024)} MB — larger than a skill should be. "
            "Something built (a virtualenv, node_modules) is inside it."
        )

    target.mkdir(parents=True, exist_ok=True)
    # A unique staging directory, not a name derived from the skill: two
    # writers sharing this directory would otherwise delete each other's
    # staging mid-copy and each report success over a truncated tree.
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.incoming.", dir=target))
    incoming = staging / name
    destination = target / name
    try:
        shutil.copytree(origin, incoming, symlinks=True)
        _replace(destination)
        incoming.rename(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
    except skill_reference.RenderError as exc:
        raise SkillError(str(exc)) from exc


def _guard_size(directory: Path, name: str) -> None:
    size = _tree_bytes(directory)
    if size > MAX_SKILL_BYTES:
        raise SkillError(
            f"{name} is {size // (1024 * 1024)} MB — larger than a skill should be. "
            "Something built (a virtualenv, node_modules) is inside it."
        )


def install_skill(harness: str, name: str, *, catalog=None, notify=None) -> SkillPair:
    """Take the repo's reference onto this mind and render it everywhere.

    Apply and revert are one act, so whatever the copies held is replaced:
    the render record is dropped first, which makes an in-place edit a copy
    to overwrite rather than one to merge.
    """
    _validate(name)
    h = harness_directory(harness)
    catalog, notify = _transports(catalog, notify)
    _copy_skill(repo_root(), reference_root(), name)
    _rendering(skill_reference.forget_record, skill_reference.KIND_SKILL, name)
    outcome = _rendering(
        skill_reference.check, catalog=catalog, notify=notify,
        kinds=[skill_reference.KIND_SKILL], names=[name],
    )
    if outcome.refused:
        raise SkillRefused(outcome.refused[0]["reason"])
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
