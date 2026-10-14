"""The terminal's route classification table — the one place a terminal route
is declared public, the user's own, a mind owner's, or admin-only.

Anything not named here is admin-only (route_access.DEFAULT_RULE), so a
route added later is closed to non-admins until someone decides otherwise
here. Enforcement lives in route_access.AccessMiddleware, which is the
console's module verbatim; this table is the terminal's half.

Every session-keyed route resolves session → mind → owner through comms, the
attach websocket included: a pane runs its harness with permissions skipped,
so the attach is the most consequential route on the site.
"""

from __future__ import annotations

import os

from route_access import (
    OWNER,
    PUBLIC,
    SELF,
    USER,
    BearerToken,
    RouteTable,
    Rule,
    owner_of_session,
)


def labels_token() -> str:
    """The label writer's bearer: the mind renaming a conversation when a
    rename arrives over Telegram. Unset means no bearer is accepted at all.

    **Deliberately not ``COMMS_BEARER_TOKEN``.** That secret opens every
    route on the mind, the one that types into a live pane included, and a
    pane runs the harness with permissions skipped. This token renames a
    conversation and does nothing else, so a copy that escapes — into a log,
    a screenshot, a transcript — is worth exactly one renamed conversation.
    """
    return os.getenv("TERMINAL_LABELS_TOKEN", "")


LABELS = "labels"

TABLE = RouteTable(
    resolvers={"session_id": owner_of_session},
    bearers={LABELS: BearerToken(labels_token)},
)

TABLE.add(
    Rule(PUBLIC),
    "MOUNT /static",
    "GET /",
    "GET /login",
    "POST /login",
    "POST /logout",
    "GET /health",
)

TABLE.add(Rule(SELF), "GET /password", "POST /password")

# Any signed-in user. The listings are filtered to the user's own minds by
# the handler (route_access.visible_minds / visible_sessions), and opening a
# session checks the mind named in the body, which no path parameter carries.
TABLE.add(
    Rule(USER),
    "GET /terminal",
    "GET /api/minds",
    "GET /api/terminal/sessions",
    "POST /api/terminal/sessions",
    "POST /api/terminal/tts",
    "POST /api/terminal/stt",
)
# Also reached by the mind with the label token, which the middleware
# verifies; a wrong one is refused, never answered from the cookie.
TABLE.add(Rule(USER, bearer=LABELS), "GET /api/terminal/labels")

TABLE.add(
    Rule(OWNER, param="session_id"),
    "WS /api/terminal/attach/{session_id}",
    "GET /api/console/{session_id}/stream",
    "POST /api/console/{session_id}/message",
    "POST /api/terminal/sessions/{session_id}/suspend",
    "POST /api/terminal/sessions/{session_id}/resume",
    "DELETE /api/terminal/sessions/{session_id}",
)
TABLE.add(
    Rule(OWNER, param="session_id", bearer=LABELS), "PUT /api/terminal/labels/{session_id}"
)
