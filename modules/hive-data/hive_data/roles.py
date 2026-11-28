"""The named operations over roles: what an agent may use (feature 10.18).

A role is a named set of three grants — skills, hooks and the harness's
built-in tools (`TOOLS`). An agent holds roles and may use exactly the union
of what they grant. A role also carries a suggested model tier (`TIERS`),
which is advice shown where a mind's model is chosen and grants nothing: no
role decides which model a mind runs (feature 893). Roles do not nest: a role holds grants, never
another role, so the union is one flat read rather than a walk.

**Management is switched on per mind, and stays on.** `board.managed_minds`
records that a mind is under role management. A mind that was never put
under it runs as it always has, and the reading for it says so
(`managed: False`) rather than handing back an empty union, which would
read as "denied everything". Once managed, a mind holds the general role
implicitly and whatever `board.mind_roles` adds; deleting a role it held
removes that role's grants and never takes the mind out of management —
the last role going must not hand a mind back everything.

**The union is read, never stored.** `grants_for` computes it from the roles
as they stand at the moment of the read, so a changed role is a changed
answer for every mind holding it on the very next read. There is no copy to
refresh, which is the whole of "changing a role changes every agent holding
it immediately".

The console and the gateway both read through here, so the two cannot hold
different opinions about what a mind may do.

Every operation takes an optional `unit` and behaves as the `core` ones do.
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

from . import _sql
from .core import _execute, _execute_returning, _fetch, _fetch_one
from .errors import ValueRefused
from .rows import UNSET
from .schema import qualified, select_list
from .unit import Unit

Row = dict[str, Any]

ROLES = "board.roles"
MANAGED = "board.managed_minds"
MIND_ROLES = "board.mind_roles"
SUBMISSIONS = "board.skill_submissions"

#: The harness built-in tools a role's allow-list may name. Shell, files,
#: sub-minds and web; nothing else is a built-in tool in the sense a role
#: grants. The schema checks the same list.
TOOLS = ("shell", "files", "sub-minds", "web")

#: The model tiers a role may suggest, smallest first. The words the Chief of
#: Staff workflow uses; the schema checks the same list. No model carries a
#: tier: a person reads the suggestion and picks.
TIERS = ("low", "medium", "high", "extra-high")

#: The role every managed mind holds. Its slug, which never changes.
GENERAL = "general"

#: What `update_role` may set. The slug is fixed at creation: it is what the
#: role's repo copy is filed under, and a rename must not orphan that file.
_ROLE_SETTABLE = frozenset({"name", "skills", "hooks", "suggested_tier", "tools",
                            "kept_against", "updated_at"})

_SUBMISSION_SETTABLE = frozenset({"branch", "new_skill", "pr_number", "pr_url", "merged_at",
                                  "closed_at", "checked_at"})

_SLUG_RUN = re.compile(r"[^a-z0-9]+")


def slug_of(name: str) -> str:
    """A role's name as its key: lower case, every other run one hyphen.

    "Chief of Staff" is `chief-of-staff`. Refused when nothing is left.
    """
    slug = _SLUG_RUN.sub("-", str(name or "").strip().lower()).strip("-")
    if not slug:
        raise ValueRefused(f"a role needs a name with a letter or digit in it: {name!r}")
    return slug


def _names(values: Iterable[str] | None) -> list[str]:
    """A grant list: trimmed, blanks dropped, each name once, in order."""
    out: list[str] = []
    for value in values or ():
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def _tools(values: Iterable[str] | None) -> list[str]:
    chosen = _names(values)
    unknown = [t for t in chosen if t not in TOOLS]
    if unknown:
        raise ValueRefused(
            f"not a built-in tool a role can allow: {', '.join(unknown)}. "
            f"The built-in tools are {', '.join(TOOLS)}.")
    return [t for t in TOOLS if t in chosen]


def _tier(value: str | None) -> str | None:
    """A suggested tier as stored, or None for no suggestion.

    "Extra high" and "extra_high" are the stored `extra-high`.
    """
    text = "-".join(str(value or "").strip().lower().replace("_", " ").split())
    if not text:
        return None
    if text not in TIERS:
        raise ValueRefused(
            f"not a tier a role can suggest: {value!r}. "
            f"The tiers are {', '.join(TIERS)}.")
    return text


def _name(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueRefused("a role needs a name")
    return text


# ------------------------------------------------------------------- roles

async def list_roles(*, unit: Unit | None = None) -> list[Row]:
    """Every role, by name."""
    return await _fetch(
        unit, f"SELECT {select_list(ROLES)} FROM {qualified(ROLES)} "
        'ORDER BY lower("name"), "id"')


async def get_role(role_id: int, *, unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit, f"SELECT {select_list(ROLES)} FROM {qualified(ROLES)} "
        'WHERE "id" = $1', int(role_id))


async def role_by_slug(slug: str, *, unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit, f"SELECT {select_list(ROLES)} FROM {qualified(ROLES)} "
        'WHERE "slug" = $1', slug)


async def create_role(name: str, *, slug: str | None = None,
                      skills: Iterable[str] | None = None,
                      hooks: Iterable[str] | None = None,
                      suggested_tier: str | None = None,
                      tools: Iterable[str] | None = None,
                      at: float | None = None,
                      unit: Unit | None = None) -> Row:
    """Make a role. A name or slug another role has is a `Conflict`."""
    now = time.time() if at is None else at
    clean = _name(name)
    sql, args = _sql.insert(ROLES, {
        "slug": slug_of(slug or clean), "name": clean,
        "skills": _names(skills), "hooks": _names(hooks),
        "suggested_tier": _tier(suggested_tier), "tools": _tools(tools),
        "created_at": now, "updated_at": now,
    }, returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def update_role(role_id: int, *, name: Any = UNSET, skills: Any = UNSET,
                      hooks: Any = UNSET, suggested_tier: Any = UNSET,
                      tools: Any = UNSET, kept_against: Any = UNSET,
                      at: float | None = None,
                      unit: Unit | None = None) -> Row:
    """Change a role's own fields; the slug never moves."""
    fields: dict[str, Any] = {}
    if name is not UNSET:
        fields["name"] = _name(name)
    if skills is not UNSET:
        fields["skills"] = _names(skills)
    if hooks is not UNSET:
        fields["hooks"] = _names(hooks)
    if suggested_tier is not UNSET:
        fields["suggested_tier"] = _tier(suggested_tier)
    if tools is not UNSET:
        fields["tools"] = _tools(tools)
    if kept_against is not UNSET:
        fields["kept_against"] = kept_against
    fields["updated_at"] = time.time() if at is None else at
    sql, args = _sql.update(ROLES, fields, {"id": int(role_id)},
                            settable=_ROLE_SETTABLE)
    return await _execute(unit, sql, *args)


async def delete_role(role_id: int, *, unit: Unit | None = None) -> Row:
    """Delete a role, and with it every mind's holding of it.

    The general role is refused here as well as on the page: every managed
    mind holds it, so it is the one role whose absence changes what the
    union means.
    """
    sql = (f"DELETE FROM {qualified(ROLES)} "
           'WHERE "id" = $1 AND "slug" <> $2')
    return await _execute(unit, sql, int(role_id), GENERAL)


# --------------------------------------------------------- holding roles

async def is_managed(mind_id: str, *, unit: Unit | None = None) -> bool:
    row = await _fetch_one(
        unit, f'SELECT "mind_id" FROM {qualified(MANAGED)} WHERE "mind_id" = $1',
        mind_id)
    return row is not None


async def managed_minds(*, unit: Unit | None = None) -> list[Row]:
    return await _fetch(
        unit, f"SELECT {select_list(MANAGED)} FROM {qualified(MANAGED)} "
        'ORDER BY "mind_id"')


async def roles_of(mind_id: str, *, unit: Unit | None = None) -> list[Row]:
    """The roles a mind holds, general first, then by name.

    Empty for a mind that is not under role management: it holds nothing,
    not even general, because nothing about it is decided by roles.
    """
    sql = (f"SELECT {_role_columns('r')} "
           f"FROM {qualified(ROLES)} AS r "
           f"JOIN {qualified(MANAGED)} AS m ON m.\"mind_id\" = $1 "
           f"WHERE r.\"slug\" = $2 OR r.\"id\" IN ("
           f"SELECT \"role_id\" FROM {qualified(MIND_ROLES)} WHERE \"mind_id\" = $1) "
           'ORDER BY (r."slug" <> $2), lower(r."name"), r."id"')
    return await _fetch(unit, sql, mind_id, GENERAL)


def _role_columns(alias: str) -> str:
    from .schema import COLUMNS
    return ", ".join(f'{alias}."{c}"' for c in COLUMNS[ROLES])


async def holders(role_id: int, *, unit: Unit | None = None) -> list[str]:
    """Every mind holding a role, by id. Every managed mind holds general."""
    role = await get_role(role_id, unit=unit)
    if role is None:
        return []
    if role["slug"] == GENERAL:
        rows = await managed_minds(unit=unit)
    else:
        rows = await _fetch(
            unit, f'SELECT "mind_id" FROM {qualified(MIND_ROLES)} '
            'WHERE "role_id" = $1 ORDER BY "mind_id"', int(role_id))
    return [r["mind_id"] for r in rows]


async def set_mind_roles(mind_id: str, role_ids: Iterable[int], *,
                         at: float | None = None,
                         unit: Unit | None = None) -> None:
    """Make these the roles a mind holds, beside general, and manage it.

    Puts the mind under role management if it was not already, and never
    takes it out: an empty list leaves it holding general alone. The general
    role is not stored as an assignment, so naming it here changes nothing.
    """
    mind = str(mind_id or "").strip()
    if not mind:
        raise ValueRefused("a mind id is required")
    now = time.time() if at is None else at
    wanted = sorted({int(r) for r in role_ids})

    async def _write(u: Unit) -> None:
        await u.hold(f"roles:mind:{mind}")
        await u.execute(
            f'INSERT INTO {qualified(MANAGED)} ("mind_id", "managed_at") '
            'VALUES ($1, $2) ON CONFLICT ("mind_id") DO NOTHING', mind, now)
        await u.execute(
            f'DELETE FROM {qualified(MIND_ROLES)} WHERE "mind_id" = $1 '
            'AND NOT ("role_id" = ANY($2::bigint[]))', mind, wanted)
        await u.execute(
            f'INSERT INTO {qualified(MIND_ROLES)} ("mind_id", "role_id", "assigned_at") '
            f'SELECT $1, r."id", $3 FROM {qualified(ROLES)} AS r '
            'WHERE r."id" = ANY($2::bigint[]) AND r."slug" <> $4 '
            'ON CONFLICT ("mind_id", "role_id") DO NOTHING',
            mind, wanted, now, GENERAL)

    if unit is not None:
        await _write(unit)
        return
    from .unit import transaction
    async with transaction() as u:
        await _write(u)


async def grants_for(mind_id: str, *, unit: Unit | None = None) -> Row:
    """What a mind may use: the union of every role it holds, read now.

    `managed` False means roles decide nothing about this mind and every
    other field is empty — not "denied everything". For a managed mind each
    list is the union, each name once: a grant held through two roles is
    one grant, and losing one of the two leaves it held by the other. A
    role's suggested tier is not a grant and is not here.
    """
    held = await roles_of(mind_id, unit=unit)
    managed = bool(held) or await is_managed(mind_id, unit=unit)
    skills: list[str] = []
    hooks: list[str] = []
    tools: list[str] = []
    for role in held:
        for name in role["skills"] or []:
            if name not in skills:
                skills.append(name)
        for name in role["hooks"] or []:
            if name not in hooks:
                hooks.append(name)
        for name in role["tools"] or []:
            if name not in tools:
                tools.append(name)
    return {
        "mind_id": mind_id,
        "managed": managed,
        "roles": [r["slug"] for r in held],
        "skills": sorted(skills),
        "hooks": sorted(hooks),
        "tools": [t for t in TOOLS if t in tools],
    }


# ------------------------------------------------------- skill submissions

async def add_submission(*, skill: str, submitted_by: str, by_owner: bool,
                         new_skill: bool, repository: str, branch: str,
                         at: float | None = None,
                         unit: Unit | None = None) -> Row:
    """Record a submission before anything is sent to GitHub."""
    sql, args = _sql.insert(SUBMISSIONS, {
        "skill": skill, "submitted_by": submitted_by.strip().lower(),
        "by_owner": by_owner, "new_skill": new_skill,
        "repository": repository, "branch": branch,
        "created_at": time.time() if at is None else at,
    }, returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def update_submission(submission_id: int, *,
                            unit: Unit | None = None, **fields: Any) -> Row:
    sql, args = _sql.update(SUBMISSIONS, _sql.passed(fields),
                            {"id": int(submission_id)},
                            settable=_SUBMISSION_SETTABLE)
    return await _execute(unit, sql, *args)


async def get_submission(submission_id: int, *,
                         unit: Unit | None = None) -> Row | None:
    return await _fetch_one(
        unit, f"SELECT {select_list(SUBMISSIONS)} FROM {qualified(SUBMISSIONS)} "
        'WHERE "id" = $1', int(submission_id))


async def list_submissions(*, unit: Unit | None = None) -> list[Row]:
    """Every submission, newest first."""
    return await _fetch(
        unit, f"SELECT {select_list(SUBMISSIONS)} FROM {qualified(SUBMISSIONS)} "
        'ORDER BY "created_at" DESC, "id" DESC')
