"""Roles on the console: each one against its repo copy (feature 10.18).

A role is a named set of skills, hooks, a model and an allow-list of the
harness's built-in tools; the rules about holding them — the union, the
general role every managed agent holds, that a managed agent stays managed —
are `hive_data.roles`'s, because the gateway reads the same union.

This module is the other half: the role *as the repo ships it*. Each role
the hive ships is a YAML file in `src/roles/`, filed under the role's slug,
and every role on this hive is read against that copy the way the Minds page
reads a skill against its repo's:

* `tracked` — in the repo, and the same as the repo copy.
* `local` — on this hive only.
* `differs` — in both, and not the same; with a diff.
* `repo_only` — the repo has it and this hive does not (a role shipped after
  this hive was seeded, or one deleted here).
* `unreadable` — the repo file is there and cannot be read as a role. Named
  rather than skipped: "this role is local" is a different sentence from
  "its repo copy is broken", and only one of them is true.

**Nothing changes by itself.** Reading the page, a repo update arriving, a
refresh: none of them writes a role on either side. The only writes are the
three choices a person makes — keep yours, take the repo copy, promote
yours — and the one-time seed of a fresh hive. Keep yours is recorded
against the repo copy it was made against (`kept_against`, that copy's
fingerprint): the role stays marked as differing, labelled kept, and the
choice comes back when the repo copy changes.

"The same" is judged on the parsed role, not on the file's bytes: the order a
skill was ticked in, or a comment in the file, is not a difference anybody
can act on. Lists compare as sets, sorted; the built-in tools in their fixed
order.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

import store
from hive_data import roles as role_rules

#: Where the repo copies live, inside this checkout. Read at call time so a
#: test can point it at a directory of its own.
REPO_DIR = Path(__file__).resolve().parent / "roles"

TOOLS = role_rules.TOOLS
GENERAL = role_rules.GENERAL

#: The largest repo file read as a role. A role is a few dozen names.
MAX_ROLE_BYTES = 64 * 1024

STATES = ("tracked", "local", "differs", "repo_only", "unreadable")


def repo_dir() -> Path:
    override = os.environ.get("HIVE_ROLES_DIR", "").strip()
    return Path(override) if override else REPO_DIR


# ------------------------------------------------------------ the role shape

def canonical(role: dict[str, Any]) -> dict[str, Any]:
    """A role as the four grants and a name, in the one order compared."""
    def names(key: str) -> list[str]:
        out = []
        for value in role.get(key) or []:
            text = str(value).strip()
            if text and text not in out:
                out.append(text)
        return sorted(out)

    tools = [t for t in TOOLS if t in {str(v).strip() for v in role.get("tools") or []}]
    model = str(role.get("model") or "").strip() or None
    return {
        "name": " ".join(str(role.get("name") or "").split()),
        "model": model,
        "tools": tools,
        "skills": names("skills"),
        "hooks": names("hooks"),
    }


def render(role: dict[str, Any]) -> str:
    """A role as its repo file says it."""
    return yaml.safe_dump(canonical(role), sort_keys=False, default_flow_style=False)


def fingerprint(role: dict[str, Any]) -> str:
    return hashlib.sha256(render(role).encode()).hexdigest()


def _parse(path: Path) -> dict[str, Any]:
    """One repo file as a role, or `{"error": why}`."""
    try:
        if path.stat().st_size > MAX_ROLE_BYTES:
            return {"error": f"larger than {MAX_ROLE_BYTES} bytes"}
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        return {"error": str(exc).splitlines()[0] if str(exc) else type(exc).__name__}
    if not isinstance(data, dict):
        return {"error": "not a mapping of name, model, tools, skills and hooks"}
    if not str(data.get("name") or "").strip():
        return {"error": "no name"}
    for key in ("tools", "skills", "hooks"):
        if data.get(key) is not None and not isinstance(data.get(key), list):
            return {"error": f"{key} is not a list"}
    unknown = [str(t) for t in data.get("tools") or [] if str(t).strip() not in TOOLS]
    if unknown:
        return {"error": f"not a built-in tool: {', '.join(unknown)}"}
    if data.get("model") is not None and not isinstance(data.get("model"), str):
        return {"error": "model is not a name"}
    return canonical(data)


def repo_roles() -> dict[str, dict[str, Any]]:
    """Every repo copy, by slug. A broken file is `{"error": ...}`, not absent."""
    out: dict[str, dict[str, Any]] = {}
    folder = repo_dir()
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.yaml")):
        out[path.stem] = _parse(path)
    return out


# ------------------------------------------------------------------- states

def _state(hive: dict[str, Any] | None, repo: dict[str, Any] | None) -> str:
    if repo is not None and "error" in repo:
        return "unreadable"
    if hive is None:
        return "repo_only"
    if repo is None:
        return "local"
    return "tracked" if canonical(hive) == repo else "differs"


def _diff(hive: dict[str, Any], repo: dict[str, Any]) -> str:
    return "".join(difflib.unified_diff(
        render(repo).splitlines(keepends=True),
        render(hive).splitlines(keepends=True),
        fromfile="repo", tofile="this hive"))


def describe(hive: dict[str, Any] | None, repo: dict[str, Any] | None,
             slug: str) -> dict[str, Any]:
    """One row of the roles page: the role, its mark, and what may be done."""
    state = _state(hive, repo)
    row: dict[str, Any] = {"slug": slug, "state": state}
    if hive is not None:
        row.update({
            "id": hive["id"], "name": hive["name"], "model": hive["model"],
            "tools": [t for t in TOOLS if t in (hive["tools"] or [])],
            "skills": list(hive["skills"] or []), "hooks": list(hive["hooks"] or []),
            "general": hive["slug"] == GENERAL,
        })
    else:
        row.update({"id": None, "general": slug == GENERAL,
                    **({k: repo[k] for k in ("name", "model", "tools", "skills", "hooks")}
                       if repo and "error" not in repo else {"name": slug})})
    if state == "unreadable":
        row["error"] = repo["error"]
    if state == "differs":
        row["diff"] = _diff(hive, repo)
        row["kept"] = hive.get("kept_against") == fingerprint(repo)
    row["not_allowed"] = [t for t in TOOLS if t not in (row.get("tools") or [])]
    return row


async def listing() -> list[dict[str, Any]]:
    """Every role on this hive and every one in the repo, each marked once."""
    repo = repo_roles()
    rows = []
    seen = set()
    for hive in await store.roles_list():
        seen.add(hive["slug"])
        rows.append(describe(hive, repo.get(hive["slug"]), hive["slug"]))
    for slug, copy in repo.items():
        if slug not in seen:
            rows.append(describe(None, copy, slug))
    rows.sort(key=lambda r: (not r["general"], str(r.get("name") or r["slug"]).lower()))
    return rows


# --------------------------------------------------------------- the seed

async def seed() -> dict[str, Any]:
    """Put the ready-made roles into a hive that has never had them.

    Once per hive, under a lock and with its marker in the same unit, so two
    console processes starting together seed once and a seed cut off half
    way is rolled back whole. A repo file that cannot be read is left out
    and stays on the page as unreadable. The general role exists afterwards
    whatever the repo held: every managed agent holds it.
    """
    added: list[str] = []
    async with store.transaction(hold="roles:seed"):
        if await store.roles_seeded():
            await _ensure_general()
            return {"seeded": False, "added": added}
        held = await store.roles_list()
        existing = {r["slug"] for r in held}
        names = {r["name"].strip().lower() for r in held}
        for slug, copy in repo_roles().items():
            # A local role already called this, under another slug, keeps
            # its name; the repo copy stays offered as "in the repo only".
            if "error" in copy or slug in existing or copy["name"].lower() in names:
                continue
            await store.create_role(copy["name"], slug=slug, skills=copy["skills"],
                                    hooks=copy["hooks"], model=copy["model"],
                                    tools=copy["tools"])
            added.append(slug)
        await _ensure_general()
        await store.mark_roles_seeded(time.time())
    return {"seeded": True, "added": added}


async def _ensure_general() -> None:
    if await store.role_by_slug(GENERAL) is None:
        await store.create_role("General", slug=GENERAL)


# ------------------------------------------------------------- the choices

async def _hive_and_repo(role_id: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
    hive = await store.role(role_id)
    if hive is None:
        raise HTTPException(status_code=404, detail="No such role")
    repo = repo_roles().get(hive["slug"])
    if repo is not None and "error" in repo:
        raise HTTPException(status_code=409,
                            detail=f"The repo copy of this role cannot be read: {repo['error']}")
    return hive, repo


async def keep_yours(role_id: int) -> dict[str, Any]:
    """Keep this hive's version over the repo copy it differs from."""
    hive, repo = await _hive_and_repo(role_id)
    if repo is None or canonical(hive) == repo:
        raise HTTPException(status_code=409, detail="This role does not differ from a repo copy")
    await store.update_role(role_id, kept_against=fingerprint(repo))
    return {"ok": True}


async def take_repo(slug: str) -> dict[str, Any]:
    """Replace this hive's role with the repo copy, or add it if absent."""
    repo = repo_roles().get(slug)
    if repo is None:
        raise HTTPException(status_code=404, detail="The repo has no copy of this role")
    if "error" in repo:
        raise HTTPException(status_code=409,
                            detail=f"The repo copy of this role cannot be read: {repo['error']}")
    hive = await store.role_by_slug(slug)
    grants = {k: repo[k] for k in ("skills", "hooks", "model", "tools")}
    try:
        if hive is None:
            role_id = await store.create_role(repo["name"], slug=slug, **grants)
        else:
            role_id = hive["id"]
            await store.update_role(role_id, name=repo["name"], kept_against=None, **grants)
    except store_conflict() as exc:
        raise HTTPException(status_code=409,
                            detail=f"Another role is already called {repo['name']!r}") from exc
    return {"ok": True, "id": role_id}


async def promote(role_id: int) -> dict[str, Any]:
    """Write this hive's version into the repo checkout as the hive copy.

    Into the working tree, uncommitted, the same as a skill's write-back on
    the Minds page: what reaches the hive's repository is a commit somebody
    reviews. Written beside the target and renamed over it, so a reader
    never sees half a file.
    """
    hive = await store.role(role_id)
    if hive is None:
        raise HTTPException(status_code=404, detail="No such role")
    folder = repo_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{hive['slug']}.yaml"
    fd, staging = tempfile.mkstemp(prefix=".role-", suffix=".yaml", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render(hive))
        os.replace(staging, target)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise
    await store.update_role(role_id, kept_against=None)
    return {"ok": True, "path": str(target)}


def store_conflict() -> type[BaseException]:
    import hive_data
    return hive_data.Conflict


# ------------------------------------------------------- what can be ticked

#: A hook is a script directly in a Mind's hooks directory, named by its file
#: name without this suffix — the name the guard on the Mind matches.
HOOK_SUFFIXES = (".sh", ".py")


def hook_names(tree: dict[str, Any]) -> list[str]:
    """The hooks in one Mind's hooks listing, each name once.

    Only scripts at the top of the directory: what sits in a folder under it
    is a cache or the hooks' own state, never something a harness runs.
    """
    names: list[str] = []
    for entry in tree.get("files") or []:
        path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
        if "/" in path or path.startswith("."):
            continue
        for suffix in HOOK_SUFFIXES:
            stem = path[: -len(suffix)]
            if path.endswith(suffix) and stem and stem not in names:
                names.append(stem)
    return names


def catalog(reports: list[dict[str, Any]], community: set[str]) -> dict[str, Any]:
    """Every skill and hook the answering Minds report, for the role editor.

    A skill or a hook exists nowhere but on a Mind, so the list is what the
    Minds say: one row per name, with whether any Mind's repo carries the
    skill and which Minds have it. A Mind that did not answer is named, per
    list, so a short list is never read as a complete one.
    """
    skills: dict[str, dict[str, Any]] = {}
    hooks: dict[str, list[str]] = {}
    silent: dict[str, list[str]] = {"skills": [], "hooks": []}
    for report in reports:
        mind = str(report.get("mind") or "")
        listing = report.get("skills")
        if not isinstance(listing, dict):
            silent["skills"].append(mind)
        else:
            for item in listing.get("skills") or []:
                name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
                if not name:
                    continue
                row = skills.setdefault(name, {"name": name, "repo": False, "minds": [],
                                               "community": name in community})
                if item.get("repo") is not None:
                    row["repo"] = True
                if item.get("installed") is not None and mind not in row["minds"]:
                    row["minds"].append(mind)
        tree = report.get("hooks")
        if not isinstance(tree, dict):
            silent["hooks"].append(mind)
        else:
            for name in hook_names(tree):
                holders = hooks.setdefault(name, [])
                if mind not in holders:
                    holders.append(mind)
    return {
        "skills": sorted(skills.values(), key=lambda r: r["name"].lower()),
        "hooks": [{"name": n, "minds": hooks[n]} for n in sorted(hooks, key=str.lower)],
        "skills_unanswered": sorted(silent["skills"]),
        "hooks_unanswered": sorted(silent["hooks"]),
    }


# ------------------------------------------------------------- editing

def grants_from(body: dict[str, Any]) -> dict[str, Any]:
    """The four grants out of a request body, refused when malformed."""
    out: dict[str, Any] = {}
    for key in ("skills", "hooks", "tools"):
        if key in body:
            value = body[key]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise HTTPException(status_code=400, detail=f"{key} must be a list of names")
            out[key] = value
    if "tools" in out:
        unknown = [t for t in out["tools"] if t.strip() not in TOOLS]
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f"Not a built-in tool: {', '.join(unknown)}")
    if "model" in body:
        value = body["model"]
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=400, detail="model must be a name")
        out["model"] = value
    return out
