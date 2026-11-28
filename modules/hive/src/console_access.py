"""The console's route classification table — the one place a console route
is declared public, the user's own, a mind owner's, or admin-only.

Anything not named here is admin-only (route_access.DEFAULT_RULE), so a page
added later is closed to non-admins until someone decides otherwise here.
Enforcement lives in route_access.AccessMiddleware.
"""

from __future__ import annotations

import os
import time

import auth
import store
from route_access import (
    ADMIN,
    BEARER,
    OWNER,
    PUBLIC,
    SELF,
    USER,
    BearerToken,
    RouteTable,
    Rule,
    owner_of_mind,
    owner_of_session,
)

# ── operations: a request id resolves to whoever started it ────────────


async def record_operation(request_id: str, username: str) -> None:
    """Remember who started an operation, so its status is theirs to read."""
    if not request_id or not username:
        return
    await store.record_operation_requester(
        str(request_id), auth.normalize_username(username), int(time.time()))


async def record_operation_from(result: object, user: dict | None) -> None:
    """Record the operation a hive-tools answer started, if it started one."""
    if not isinstance(result, dict) or not user:
        return
    for key in ("hitl_request_id", "request_id"):
        if result.get(key):
            await record_operation(str(result[key]),
                                   str(user.get("username") or ""))


async def requester_of(request_id: str) -> str | None:
    return await store.operation_requester(request_id)


# ── the bearers a mind presents ────────────────────────────────────────


async def a_minds_key(presented: str) -> str | None:
    """The name of the mind whose own key this is, or None.

    What a mind draws on the canvas with: its own
    key, recognised from the one record of keys on this request. A person's
    key, a replaced one and a cut-off one are nobody's here. No token of this
    site's own stands for a mind."""
    import hashlib

    from hive_data import core

    value = str(presented or "").strip()
    if not value:
        return None
    try:
        row = await core.credential_for_token(
            hashlib.sha256(value.encode("utf-8")).hexdigest())
        if row is None or str(row["role"] or "") != "mind":
            return None
        mind = await core.get_mind_by_id_or_name(str(row["holder"]))
    except Exception:  # noqa: BLE001 - a key that cannot be read admits nobody
        return None
    return str(mind["name"]) if mind else None


async def an_administrators_key(presented: str) -> str | None:
    """The username of the administrator whose own key this is, or None.

    What the operator's key-migration command moves minds with: their own
    key, from a command line with no browser session. A mind's key and a
    non-administrator's are refused."""
    from hive_data import access

    try:
        found = await access.administrator_for(str(presented or "").strip())
    except Exception:  # noqa: BLE001 - a key that cannot be read admits nobody
        return None
    return found.username if found else None


#: The board roles, given on the roles page. A mind writes to the board with
#: its own key, and what it may do is the role it holds in the hive database:
#: the planner reads, creates and updates; the push role does all of that and
#: deletes. Possession of a token is no longer the role.
BOARD_PLANNER_ROLE = "board-planner"
BOARD_PUSH_ROLE = "board-push"
OPERATOR_ROLE = "operator"


async def mind_holding(presented: str, slugs: tuple[str, ...]) -> str | None:
    """The name of the mind whose own key this is, when it holds one of the
    roles named, or None.

    The key is recognised from the one record of keys in the hive database
    (`core.credentials`, the record the gateway, the memory store and the
    tools read), on this request and never remembered. Only a mind's key
    counts: a person's key presented here is nobody's, and a replaced or
    cut-off key is not live in the record. The role is read from the roles
    tables on this request too, so a role taken away stops working on the
    mind's next call.
    """
    import hashlib

    from hive_data import core, roles

    value = str(presented or "").strip()
    if not value:
        return None
    try:
        row = await core.credential_for_token(
            hashlib.sha256(value.encode("utf-8")).hexdigest())
        if row is None or str(row["role"] or "") != "mind":
            return None
        mind = await core.get_mind_by_id_or_name(str(row["holder"]))
        if not mind:
            return None
        held = {r["slug"] for r in await roles.roles_of(mind["mind_id"])}
    except Exception:  # noqa: BLE001 - a key or role that cannot be read admits nobody
        return None
    if not held & set(slugs):
        return None
    return str(mind["name"])


def _holding(*slugs: str):
    async def recognise(presented: str) -> str | None:
        return await mind_holding(presented, slugs)
    return recognise


CANVAS = "canvas"
KEY_MIGRATION = "key_migration"
DESIGN = "design"
BOARD = "board"
BOARD_PLANNER = "board_planner"
ROLES = "roles"

# ── the table ──────────────────────────────────────────────────────────

TABLE = RouteTable(
    resolvers={
        "name": owner_of_mind,
        "mind_id": owner_of_mind,
        "session_id": owner_of_session,
        "request_id": requester_of,
    },
    bearers={
        # A mind's own key, recognised from the one record.
        CANVAS: BearerToken(lambda: "", recognise=a_minds_key),
        # Design pages are published only by an operator-role mind, the same
        # role the roles page's operator bearer takes. History does not name
        # which minds held the old design token (the console's compose only
        # read it; each mind's env carried it by hand), so the role that
        # already means "the mind that changes the hive for its owner" is used
        # and no new role has to be created. Any other mind's key is refused.
        DESIGN: BearerToken(
            lambda: "", recognise=_holding(OPERATOR_ROLE),
        ),

        # A mind's own key, recognised from the one record, plus its role.
        BOARD: BearerToken(
            lambda: "", recognise=_holding(BOARD_PUSH_ROLE),
        ),
        BOARD_PLANNER: BearerToken(
            lambda: "", recognise=_holding(BOARD_PLANNER_ROLE, BOARD_PUSH_ROLE),
        ),
        ROLES: BearerToken(
            lambda: "", recognise=_holding(OPERATOR_ROLE),
        ),
        # An administrator's own key, for the operator's command line.
        KEY_MIGRATION: BearerToken(lambda: "", recognise=an_administrators_key),
    },
)

TABLE.add(
    Rule(PUBLIC),
    "MOUNT /static",
    "GET /",
    "GET /login",
    "POST /login",
    "POST /logout",
    # Microsoft and Windows sign-in; each answers 404 unless configured.
    "GET /login/microsoft",
    "GET /login/microsoft/callback",
    "GET /login/windows",
    "GET /health",
)

# A mind draws on the canvas with its bearer; no session admits it.
TABLE.add(Rule(BEARER, bearer=CANVAS), "POST /canvas/push")

TABLE.add(Rule(SELF), "GET /password", "POST /password")

# A person ends every sign-in of their own.
TABLE.add(Rule(USER), "POST /api/sign-ins/end")

# Approving: any signed-in person reaches these; the handler decides who may
# see or decide a request (the owner of the mind that asked, or an
# administrator) and refuses anyone else without showing its content.
TABLE.add(
    Rule(USER),
    "GET /approvals",
    "GET /approvals/{request_id}",
)
# A decision is a form post: signed out, the person is sent to sign in and
# lands back on the request they were deciding.
TABLE.add(
    Rule(USER, lands_on="/approvals/{request_id}"),
    "POST /approvals/{request_id}/decision",
)

# Shells and listings: any signed-in user, each listing filtered to what the
# user owns by the handler (route_access.visible_minds / visible_sessions).
TABLE.add(
    Rule(USER),
    "GET /overview",
    "GET /minds",
    "GET /bots",
    "GET /settings",
    "GET /api/overview",
    "GET /api/settings/minds",
    # Filtered to the Minds the user owns by the handler, like the two above.
    "GET /api/bots",
    "GET /api/settings/hive-comms/sessions",
    # Roles (feature 10.18): anyone signed in may read them, because a
    # person gives their own mind roles from this list; only an admin
    # changes one, since a role changes every mind holding it.
    "GET /roles",
    # A skill submission is a pull request and nothing else, whoever sends
    # it (story 10.18.8), so any signed-in person may make one and read the
    # list of what has been submitted.
    "GET /skills/submit",
    "GET /api/skill-submissions",
    "POST /api/skill-submissions",
    "POST /api/skill-submissions/{submission_id}/retry",
    # Filtered by the handler to the user's own minds (story 2.1.17).
    "GET /usage",
    "GET /api/usage",
)
# The Roles page's listing is any signed-in person's, and the operator's
# bearer reads it too: it cannot change a role it cannot see.
TABLE.add(Rule(USER, bearer=ROLES), "GET /api/roles")
# The settings page is one route; only its sessions section is a user's.
TABLE.add(
    Rule(USER, only_when=(("section", frozenset({"access", "features", "remote-access"})),)),
    "GET /settings/{section}",
)

TABLE.add(
    Rule(OWNER, param="mind_id"),
    "GET /minds/{mind_id}",
    # A mind's picture, read and set. Keyed by id rather than by name, like
    # the page above it and unlike the per-name routes below, because the
    # file on disk has to survive a rename — see `mind_images`.
    "GET /api/minds/{mind_id}/image",
    "POST /api/minds/{mind_id}/image",
)
TABLE.add(
    Rule(OWNER, param="name"),
    "GET /api/minds/{name}/configuration",
    "PATCH /api/minds/{name}/configuration",
    "GET /api/minds/{name}/proxy-key",
    "PUT /api/minds/{name}/proxy-key",
    "GET /api/minds/{name}/settings",
    "PATCH /api/minds/{name}/settings",
    "GET /api/minds/{name}/voice",
    "PUT /api/minds/{name}/voice",
    "POST /api/minds/{name}/voice/clips",
    "GET /api/minds/{name}/soul",
    "PUT /api/minds/{name}/soul",
    "GET /api/minds/{name}/skills",
    "GET /api/minds/{name}/skills/{skill}/diff",
    "POST /api/minds/{name}/skills/{skill}/install",
    "POST /api/minds/{name}/skills/{skill}/write-back",
    "DELETE /api/minds/{name}/skills/{skill}",
    "GET /api/minds/{name}/files/{tree}",
    "GET /api/minds/{name}/files/{tree}/content",
    "PUT /api/minds/{name}/files/{tree}/content",
    "GET /api/minds/{name}/surface-token",
    "PUT /api/minds/{name}/surface-token",
    # Registering a bot and deciding who it answers, both scoped to the Mind
    # named in the path — so a person can set up their own mind's bot and
    # reaches nobody else's by naming it.
    "GET /api/minds/{name}/bot-claim",
    "POST /api/minds/{name}/bot-claim/phrase",
    "DELETE /api/minds/{name}/bot-claim",
    "PUT /api/minds/{name}/github-token",
    "POST /api/minds/{name}/restart",
    # The roles a mind holds: its owner decides them, within the roles an
    # admin defined.
    "GET /api/minds/{name}/roles",
    "PUT /api/minds/{name}/roles",
)
TABLE.add(
    Rule(OWNER, param="session_id"),
    "DELETE /api/settings/hive-comms/sessions/{session_id}",
)
TABLE.add(Rule(OWNER, param="request_id"), "GET /api/operations/{request_id}")
TABLE.add(Rule(OWNER, param='mind_id'),
          'POST /api/minds/{mind_id}/rename',
          'GET /api/minds/{mind_id}/rename',
          'POST /api/minds/{mind_id}/rename/recover',
          'POST /api/minds/{mind_id}/rename/cancel')

# The canvas snapshot and design sessions are also reached by a mind with a
# bearer of its own, which the middleware verifies.
TABLE.add(Rule(ADMIN, bearer=CANVAS), "GET /canvas/render.svg")
TABLE.add(
    Rule(ADMIN, bearer=DESIGN),
    "GET /api/design/sessions",
    "POST /api/design/sessions",
    "GET /api/design/sessions/{design_id}",
    "DELETE /api/design/sessions/{design_id}",
    "POST /api/design/sessions/{design_id}/attach",
    "POST /api/design/sessions/{design_id}/pages/{page}/publish",
    "POST /api/design/sessions/{design_id}/pages/{page}/submit",
    "POST /api/design/sessions/{design_id}/pages/{page}/accept",
    "POST /api/design/trim-graph",
    "GET /api/design/symbols",
    "GET /api/design/symbol-roots",
)

# The three reads the one-database check makes, which a signed-in admin
# reaches as before and the check reaches with its own bearer. They are here
# rather than among the admin-only routes below because the bearer is part of
# the rule: without it the console is the one app the cutover cannot verify,
# and an unchecked app is reported as unchecked rather than counted as
# working.
TABLE.add(
    Rule(ADMIN),
    "GET /api/memory/tables",
    "GET /api/memory/rows",
)

# The board, reached by a signed-in admin in a browser and by a Mind with
# one of the two board bearers. The whole board is one read
# (`/api/kanban/data`), which the one-database check also makes, so that
# route names its credential too; the rest are the board's alone.
#
# The two board bearers differ by exactly one route. The chief of staff has
# full create, read, update and delete on the board (Daniel, 2026-11-19); a
# planning mind has everything but the delete (Daniel, 2026-11-20). So
# `DELETE /api/kanban/{kind}/{record_id}` names the full bearer alone. It
# takes the row and everything under it; `?only_if_empty=true` makes it
# refuse a row with anything under it, which the board tool sends unless
# the caller asked for the cascade.
TABLE.add(
    Rule(ADMIN, bearer=(BOARD, BOARD_PLANNER)),
    "GET /api/kanban/data",
)
TABLE.add(
    Rule(ADMIN, bearer=(BOARD, BOARD_PLANNER)),
    "GET /api/kanban/numbers",
    "GET /api/kanban/relations/{record_id}",
    "GET /api/kanban/{kind}/{record_id}/descendants",
    "POST /api/kanban/epics",
    "POST /api/kanban/features",
    "POST /api/kanban/stories",
    "POST /api/kanban/tasks",
    "PATCH /api/kanban/{kind}/{record_id}",
    # An item's files.
    "GET /api/kanban/items/{record_id}/attachments",
    "POST /api/kanban/items/{record_id}/attachments",
    "GET /api/kanban/items/{record_id}/attachments/{attachment_id}",
)
TABLE.add(
    Rule(ADMIN, bearer=BOARD),
    "DELETE /api/kanban/{kind}/{record_id}",
)

# Moving existing minds onto one key, and retiring the old keys: the
# operator's, by a signed-in admin or by the operator's own bearer, never a
# mind's (feature 834, story 884).
TABLE.add(
    Rule(ADMIN, bearer=KEY_MIGRATION),
    "GET /api/settings/key-migration",
    "POST /api/settings/key-migration/retire",
    "POST /api/settings/key-migration/tools-credential/{holder}",
    "POST /api/settings/key-migration/{name}/move",
    "POST /api/settings/key-migration/{name}/undo",
)

# Roles: defining one, changing any part of one, and the three repo choices.
# A signed-in admin reaches them as before and the operator with its own
# bearer. Nothing here is narrower for the bearer than for the admin: the
# operator may do anything to a role. The single-skill add and remove are the
# operator's way to change one skill without replacing the whole list.
TABLE.add(
    Rule(ADMIN, bearer=ROLES),
    "GET /api/roles/catalog",
    "POST /api/roles",
    "PUT /api/roles/{role_id}",
    "DELETE /api/roles/{role_id}",
    "POST /api/roles/{role_id}/skills",
    "DELETE /api/roles/{role_id}/skills/{skill}",
    "POST /api/roles/{role_id}/keep",
    "POST /api/roles/{role_id}/promote",
    "POST /api/roles/repo/{slug}/take",
)

# House-wide: named so the table is complete, and so nobody mistakes a
# missing line for an oversight. Each moves when its own story scopes it.
TABLE.add(
    Rule(ADMIN),
    # setup, topology, mind creation and registration
    "GET /setup",
    "GET /minds/new",
    "GET /edge-minds",
    "GET /edge-minds/new",
    "GET /outposts",
    "GET /outposts/new",
    "GET /tools",
    "GET /api/minds/install-options",
    "POST /api/minds",
    "POST /api/minds/{name}/register",
    "POST /api/minds/{name}/start",
    "PUT /api/minds/{name}/owner",
    "GET /api/settings/new-mind/check-name",
    "GET /api/outposts",
    "GET /api/edge-minds",
    "POST /api/outposts/bootstrap",
    "POST /api/edge-minds/bootstrap",
    "POST /api/edge-minds/verify",
    # host paths a mind mounts
    "GET /api/minds/{name}/mounts",
    "GET /api/minds/{name}/mounts/folders",
    "POST /api/minds/{name}/mounts/folders",
    "PUT /api/minds/{name}/mounts",
    # capability nodes, Ollama, installs
    "GET /capability-nodes",
    "GET /api/tools",
    "GET /api/capability-nodes",
    "PUT /api/capability-nodes/{node_id}/configuration",
    "DELETE /api/capability-nodes/{node_id}/configuration",
    "POST /api/capability-nodes/inference-proxy/install",
    "GET /api/capability-nodes/ollama/models",
    "POST /api/capability-nodes/ollama/models/pull",
    "DELETE /api/capability-nodes/ollama/models",
    # activity, dashboard and host metrics
    "GET /activity",
    "GET /dashboard",
    "GET /api/dashboard",
    "GET /api/dashboard/text",
    "GET /harness-runs",
    "GET /api/harness-runs",
    # kanban — the pages. The board's API routes are classified above, with
    # the bearer that also admits them.
    "GET /kanban",
    "GET /kanban/board",
    "GET /kanban/backlog",
    "GET /kanban/item/{record_id}",
    # The page's own read of the board and its numbers together. A mind has
    # /api/kanban/data and /api/kanban/numbers; this needs no bearer.
    "GET /api/kanban/board",
    # design pages
    "GET /design",
    "GET /design/{design_id}",
    # The chief of staff's live tree. Admin rather than merely
    # authenticated: it names every task Daniel has handed out, the questions
    # he has not answered and which mind is on what, and Sloan and Aiden hold
    # console accounts.
    "GET /chief-of-staff",
    "GET /api/chief-of-staff",
    # knowledge: graph, memory, rules
    "GET /graph",
    "GET /graph/data",
    "GET /graph/public-data",
    "GET /memory",
    "GET /rules",
    "GET /api/rules",
    "POST /api/rules",
    "PUT /api/rules/{rule_id}",
    "DELETE /api/rules/{rule_id}",
    # sentinel
    "GET /sentinel",
    "GET /api/sentinel/review",
    "GET /api/sentinel/queue",
    "POST /api/sentinel/review/mark",
    "POST /api/sentinel/review/overturn",
    "POST /api/sentinel/queue/apply",
    # The catalogue is admin-only rather than merely authenticated: Sloan and
    # Aiden hold console accounts, and these rows decide what escalates to
    # Daniel at three in the morning.
    "GET /api/sentinel/buckets",
    "POST /api/sentinel/buckets/save",
    # training
    "GET /training",
    "GET /api/training/status",
    "GET /api/training/base-models",
    "GET /api/training/in-flight",
    "POST /api/training/kill",
    "GET /api/training/logs",
    "GET /api/training/runs",
    "POST /api/training/curate",
    "POST /api/training/export",
    "POST /api/training/train",
    "POST /api/training/deploy",
    # canvas
    "GET /canvas",
    "POST /canvas/submit",
    "WS /ws/canvas",
    # house settings, credentials, voices, downloads
    "GET /api/settings/hive-tools",
    "GET /api/settings/voice",
    "GET /api/settings/downloads",
    "GET /api/settings/credentials",
    "POST /api/settings/credentials/{key}/check",
    "POST /api/settings/credentials/{key}/rotate",
    "POST /api/settings/credentials/github_pat/set",
    # Per-holder credentials. Admin for the same reason the services'
    # own routes are: a route that issues credentials must not answer to
    # one it issued, and the listing names every holder on the hive.
    "POST /api/settings/holders/{service}/{holder}/check",
    "POST /api/settings/holders/{service}/{holder}/rotate",
    "POST /api/settings/holders/{service}/{holder}/revoke",
    # Cutting a mind off from the hive: the same power as revoking each of
    # its holders by hand, in one action.
    "GET /api/minds/{name}/cut-off",
    "POST /api/minds/{name}/cut-off",
    "GET /downloads/{filename}",
    "POST /api/voices/sample",
    # users and groups
    "GET /api/admin/users",
    "POST /api/admin/users",
    "PATCH /api/admin/users/{username}",
    "POST /api/admin/users/{username}/reset",
    "POST /api/admin/users/{username}/sign-ins/end",
    "POST /api/admin/users/{username}/key/replace",
    "DELETE /api/admin/users/{username}/microsoft",
    "GET /api/admin/groups",
    "POST /api/admin/groups",
    "PUT /api/admin/groups/{group}/members/{username}",
    "DELETE /api/admin/groups/{group}/members/{username}",
)


TABLE.add(Rule(USER), "GET /api/settings/features", "PUT /api/settings/features")
# A person replaces their own key, the one this console presents to the minds
# they own (feature 834, requirement 19). Only their own: the name is the
# signed-in user's, never taken from the request.
TABLE.add(Rule(USER), "GET /api/account/key", "POST /api/account/key/replace")
TABLE.add(Rule(ADMIN), "GET /hive-errors", "GET /api/hive-errors")
TABLE.add(Rule(ADMIN), "GET /api/settings/providers", "POST /api/settings/providers",
          "PUT /api/settings/providers/{provider_id}", "DELETE /api/settings/providers/{provider_id}",
          "POST /api/settings/providers/{provider_id}/models")

TABLE.add(Rule(USER), 'GET /api/settings/remote-access', 'PUT /api/settings/remote-access', 'POST /api/remote-access/{action}')
TABLE.add(Rule(ADMIN), 'PUT /api/settings/remote-access/global')
TABLE.add(Rule(PUBLIC), 'POST /api/remote-access/authorize')
