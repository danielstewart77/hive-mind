"""A mind's own view of its skills: what the repo ships, what it runs.

A skill exists twice. `specs/skills/<harness>/` is the tracked source that
travels with a clone; the harness reads a different directory entirely
(`$CLAUDE_CONFIG_DIR/skills` or `$CODEX_HOME/skills`), and that copy is the
one that runs. Nothing keeps them in step, so a mind drifts from the repo
the moment anyone edits a skill in place — which is a feature, since that is
how a mind gets a skill tuned to its own job.

Both directories live on the mind's own filesystem, so the mind is the only
thing that can see both. It reports the pair and applies changes to either
side; the console renders and issues the actions. That is the same shape as
`runtime_api` and for the same reason: a container in this stack, a
bare-metal mind on this host, and a mind on another machine are one code
path, and no bind mount can reach the third.

There are two harness directories rather than one because Codex honours a
subset of Claude's frontmatter. The files could be shared, but writing them
separately keeps each one honest about what its harness actually reads.

A skill is a *directory*. State compares every file in it, not just the
`SKILL.md`, because a copy moves the whole tree — a `SKILL.md` that matched
while a sibling script differed would report "in sync" and offer no way to
fix it.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.hive_logging import log_event
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


@dataclass(frozen=True)
class SkillPair:
    """One skill as it exists in the repo and on this mind."""

    name: str
    state: str
    repo_text: str | None
    installed_text: str | None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "repo": self.repo_text,
            "installed": self.installed_text,
        }


def harness_directory(harness: str) -> str:
    """`claude_cli` and `codex_cli` name their own source directory."""
    key = (harness or "").strip().lower()
    if key.startswith("codex"):
        return "codex"
    if key.startswith("claude"):
        return "claude"
    raise SkillError(f"Unknown harness: {harness!r}")


def repo_root(harness: str) -> Path:
    return PROJECT_DIR / "specs" / "skills" / harness_directory(harness)


def installed_root(harness: str) -> Path:
    """Where this harness actually loads skills from.

    Read from the environment at call time, not at import: the tests set
    these per case, and a mind's config directory is a deployment fact
    rather than a constant.
    """
    if harness_directory(harness) == "codex":
        home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    else:
        home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(home) / "skills"


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
    """Every skill this mind knows about, from either side, sorted by name."""
    repo, installed = repo_root(harness), installed_root(harness)
    pairs = []
    for name in sorted(_names(repo) | _names(installed)):
        pairs.append(_pair_in(repo, installed, name))
    return pairs


def _pair_in(repo: Path, installed: Path, name: str) -> SkillPair:
    try:
        repo_text, installed_text = _read(repo, name), _read(installed, name)
        state = _state(_fingerprint(repo, name), _fingerprint(installed, name))
    except SkillUnavailable:
        return SkillPair(name, STATE_UNREADABLE, None, None)
    return SkillPair(name, state, repo_text, installed_text)


def _state(repo_hash: str | None, installed_hash: str | None) -> str:
    if repo_hash is None:
        return STATE_LOCAL_ONLY
    if installed_hash is None:
        return STATE_NOT_INSTALLED
    return STATE_SAME if repo_hash == installed_hash else STATE_DIFFERS


def diff_skill(harness: str, name: str) -> str:
    """Unified diff of the repo version against the installed one."""
    _validate(name)
    repo_text = _read(repo_root(harness), name)
    installed_text = _read(installed_root(harness), name)
    if repo_text is None and installed_text is None:
        raise SkillError(f"No such skill: {name}")
    return "".join(
        difflib.unified_diff(
            (repo_text or "").splitlines(keepends=True),
            (installed_text or "").splitlines(keepends=True),
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


def install_skill(harness: str, name: str) -> SkillPair:
    """Copy the repo's version onto this mind. Apply and revert are one act."""
    _validate(name)
    repo, installed = repo_root(harness), installed_root(harness)
    _copy_skill(repo, installed, name)
    return _pair_in(repo, installed, name)


def write_back_skill(harness: str, name: str) -> SkillPair:
    """Copy this mind's version into the repo checkout.

    Nothing is committed. The write lands in a working tree whose owner
    still has to review and push it, which is the whole safety story.
    """
    _validate(name)
    repo, installed = repo_root(harness), installed_root(harness)
    _copy_skill(installed, repo, name)
    return _pair_in(repo, installed, name)


def remove_skill(harness: str, name: str) -> None:
    """Delete this mind's copy. The repo is never touched."""
    _validate(name)
    target = installed_root(harness) / name
    if not (target / SKILL_FILE).is_file():
        raise SkillError(f"No such skill: {name}")
    _replace(target)



def _failure(exc: Exception) -> JSONResponse:
    """One mapping for every skills failure, so the console reads one shape."""
    if isinstance(exc, SkillUnavailable):
        return JSONResponse({"error": str(exc)}, status_code=503)
    if isinstance(exc, SkillError):
        return JSONResponse({"error": str(exc)}, status_code=404)
    # A malformed runtime value reaches here as a bare ValueError, and a
    # bare 500 with FastAPI's own body tells the console nothing.
    return JSONResponse({"error": str(exc)}, status_code=500)


def install_skills_routes(app: FastAPI, *, harness: str, mind_id: str, log) -> None:
    """Mount the skills routes on a mind's FastAPI app.

    Both harness servers call this, the same way both call
    `install_runtime_routes`. The harness is fixed for the life of the
    process — a mind does not change which CLI it runs — so it is passed in
    rather than re-read per request.
    """

    @app.get("/skills")
    async def get_skills(req: Request):
        """Every skill this mind knows, repo side and installed side.

        Guarded like the writes: this returns the full text of every skill
        the mind runs, on a port reachable across the LAN.
        """
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            return {
                "harness": harness_directory(harness),
                "skills": [pair.as_dict() for pair in list_skills(harness)],
            }
        except (ValueError, OSError) as exc:
            return _failure(exc)

    @app.get("/skills/{name}/diff")
    async def get_skill_diff(req: Request, name: str):
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            return {"name": name, "diff": diff_skill(harness, name)}
        except (ValueError, OSError) as exc:
            return _failure(exc)

    @app.post("/skills/{name}/install")
    async def post_skill_install(req: Request, name: str):
        """Copy the repo's version onto this mind — apply and revert alike."""
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            # Off the event loop: a directory copy is not instant, and this
            # loop also serves every session this mind is holding.
            pair = await asyncio.to_thread(install_skill, harness, name)
        except (ValueError, OSError) as exc:
            return _failure(exc)
        log_event(log, "mind.skill.installed", mind_id=mind_id, skill=name)
        return {"saved": True, "skill": pair.as_dict()}

    @app.post("/skills/{name}/write-back")
    async def post_skill_write_back(req: Request, name: str):
        """Copy this mind's version into its repo checkout, uncommitted."""
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            pair = await asyncio.to_thread(write_back_skill, harness, name)
        except (ValueError, OSError) as exc:
            return _failure(exc)
        log_event(log, "mind.skill.written_back", mind_id=mind_id, skill=name)
        return {"saved": True, "skill": pair.as_dict()}

    @app.delete("/skills/{name}")
    async def delete_skill(req: Request, name: str):
        """Remove this mind's copy. The repo keeps whatever it had."""
        denied = authorize_admin(req)
        if denied is not None:
            return denied
        try:
            await asyncio.to_thread(remove_skill, harness, name)
        except (ValueError, OSError) as exc:
            return _failure(exc)
        log_event(log, "mind.skill.removed", mind_id=mind_id, skill=name)
        return {"removed": True, "name": name}
