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


def canvas_push_token() -> str:
    """What a mind draws on, and reads, the canvas with.

    Prefers a dedicated CANVAS_PUSH_TOKEN; falls back to the house
    COMMS_BEARER_TOKEN, which both this container and Skippy already hold,
    so drawing works without recreating the container.
    """
    return os.getenv("CANVAS_PUSH_TOKEN") or os.getenv("COMMS_BEARER_TOKEN") or ""


def design_push_token() -> str:
    """The credential a Mind publishes design pages with.

    Deliberately its own, with no fallback to the house tokens. Every Mind
    in the hive already holds `COMMS_ADMIN_BEARER_TOKEN` and
    `COMMS_BEARER_TOKEN`, so either of those as the fallback would let any
    Mind — a kid's Windows box included — rewrite Daniel's requirements and
    press Build. Unset means the publish routes refuse rather than open.
    """
    return os.getenv("DESIGN_PUSH_TOKEN", "")


def cutover_check_token() -> str:
    """The credential the one-database check reads the console with.

    Deliberately its own, with no fallback to the house tokens. The read it
    admits — the whole board, and every row of the memory store — is the
    reason it has to be a value this install issued for this purpose rather
    than one every mind in the hive already holds.

    It exists because the check cannot be made any other way. The board and
    the memory pages answer to a signed-in admin session, so a check running
    outside a browser has no credential to present, and an unchecked console
    is reported as unchecked — which is not a pass. And a read-only
    comparison could not answer the question anyway: the loader made the two
    stores agree, so a console still reading its SQLite file answers
    correctly. The check marks a row in Postgres and requires it back, which
    is why it needs a way in at all.

    Unset means these routes admit no bearer and only a session reaches
    them, which is where every install that is not mid-cutover sits.
    """
    return os.getenv("HIVE_SITE_ADMIN_TOKEN", "")


def board_token() -> str:
    """The credential a Mind reads and writes the Kanban board with.

    The backlog is where the hive's work is decided, and until now the only
    way in was a signed-in admin browser. A mind whose whole job is the
    backlog — the chief of staff — therefore had no door at all, and the
    alternatives it reaches for when it has none are worse than this token:
    raw SQL against `work_items`, which skips the ordering, the hierarchy
    numbering and the write lock the board module holds, or `docker exec`
    into this container, which is host access granted to get at a table.

    Deliberately its own, with no fallback to the house tokens, for the
    reason `design_push_token` gives: every Mind in the hive already holds
    `COMMS_BEARER_TOKEN` and `COMMS_ADMIN_BEARER_TOKEN`, so either as a
    fallback would hand the backlog to every mind on it, a kid's Windows box
    included. Not `HIVE_SITE_ADMIN_TOKEN` either: that one also opens the
    whole memory store, which is far more than the board.

    Unset means the board routes admit no bearer and only a session reaches
    them, which is where an install with no mind on the backlog sits.
    """
    return os.getenv("BOARD_PUSH_TOKEN", "")


CANVAS = "canvas"
DESIGN = "design"
CUTOVER = "cutover"
BOARD = "board"

# ── the table ──────────────────────────────────────────────────────────

TABLE = RouteTable(
    resolvers={
        "name": owner_of_mind,
        "mind_id": owner_of_mind,
        "session_id": owner_of_session,
        "request_id": requester_of,
    },
    bearers={
        CANVAS: BearerToken(
            canvas_push_token, unset_status=503, unset_message="canvas push token not configured"
        ),
        DESIGN: BearerToken(
            design_push_token, unset_status=503,
            unset_message="DESIGN_PUSH_TOKEN is not configured",
        ),
        CUTOVER: BearerToken(
            cutover_check_token, unset_status=401,
            unset_message="Authentication required",
        ),
        BOARD: BearerToken(
            board_token, unset_status=401,
            unset_message="Authentication required",
        ),
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
    "GET /api/roles",
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
    Rule(ADMIN, bearer=CUTOVER),
    "GET /api/memory/tables",
    "GET /api/memory/rows",
)

# The board, reached by a signed-in admin in a browser and by a Mind with
# the board bearer. The whole board is one read (`/api/kanban/data`), which
# the one-database check also makes, so that route names both credentials;
# the rest are the board's alone.
#
# `DELETE /api/kanban/{kind}/{record_id}` is deliberately *not* here and
# stays admin-only. Finished work goes to `done` and stays on the board, so
# a mind working the backlog never needs a delete — and the one operation
# that destroys a row and everything under it is not the one to open to a
# credential sitting in an `.env`.
TABLE.add(
    Rule(ADMIN, bearer=(CUTOVER, BOARD)),
    "GET /api/kanban/data",
)
TABLE.add(
    Rule(ADMIN, bearer=BOARD),
    "GET /api/kanban/numbers",
    "GET /api/kanban/relations/{record_id}",
    "GET /api/kanban/{kind}/{record_id}/descendants",
    "POST /api/kanban/epics",
    "POST /api/kanban/features",
    "POST /api/kanban/stories",
    "POST /api/kanban/tasks",
    "PATCH /api/kanban/{kind}/{record_id}",
    # An item's files. Attaching adds and destroys nothing, so it sits with
    # the board's other writes rather than with its one delete.
    "GET /api/kanban/items/{record_id}/attachments",
    "POST /api/kanban/items/{record_id}/attachments",
    "GET /api/kanban/items/{record_id}/attachments/{attachment_id}",
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
    "POST /api/capability-nodes/hive-tools/install",
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
    # kanban — the pages, and the one write no bearer reaches. The board's
    # API routes are classified above, with the bearer that also admits them.
    "GET /kanban",
    "GET /kanban/board",
    "GET /kanban/backlog",
    "GET /kanban/item/{record_id}",
    # The page's own read of the board and its numbers together. A mind has
    # /api/kanban/data and /api/kanban/numbers; this needs no bearer.
    "GET /api/kanban/board",
    "DELETE /api/kanban/{kind}/{record_id}",
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
    "GET /downloads/{filename}",
    "POST /api/voices/sample",
    # roles: defining one, and the three repo choices
    "GET /api/roles/catalog",
    "POST /api/roles",
    "PUT /api/roles/{role_id}",
    "DELETE /api/roles/{role_id}",
    "POST /api/roles/{role_id}/keep",
    "POST /api/roles/{role_id}/promote",
    "POST /api/roles/repo/{slug}/take",
    # users and groups
    "GET /api/admin/users",
    "POST /api/admin/users",
    "PATCH /api/admin/users/{username}",
    "POST /api/admin/users/{username}/reset",
    "DELETE /api/admin/users/{username}/microsoft",
    "GET /api/admin/groups",
    "POST /api/admin/groups",
    "PUT /api/admin/groups/{group}/members/{username}",
    "DELETE /api/admin/groups/{group}/members/{username}",
)


TABLE.add(Rule(USER), "GET /api/settings/features", "PUT /api/settings/features")
TABLE.add(Rule(ADMIN), "GET /hive-errors", "GET /api/hive-errors")
TABLE.add(Rule(ADMIN), "GET /api/settings/providers", "POST /api/settings/providers",
          "PUT /api/settings/providers/{provider_id}", "DELETE /api/settings/providers/{provider_id}",
          "POST /api/settings/providers/{provider_id}/models")

TABLE.add(Rule(USER), 'GET /api/settings/remote-access', 'PUT /api/settings/remote-access', 'POST /api/remote-access/{action}')
TABLE.add(Rule(ADMIN), 'PUT /api/settings/remote-access/global')
TABLE.add(Rule(PUBLIC), 'POST /api/remote-access/authorize')
