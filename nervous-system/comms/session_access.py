"""Every route this gateway serves, and who may reach it.

One table rather than a decorator per route, so a route added later cannot
ship open by somebody forgetting to guard it: `user_access.DEFAULT_RULE` is
`ADMIN`, and `test_comms_route_classification` refuses to go green while any
route is unclassified. Forgetting therefore costs an admin-only route, which
is noticed immediately by whoever needed it, rather than an open one, which is
noticed by nobody.

The classification preserves today's credential for every route it does not
mean to change. Routes that are admin-only now stay `ADMIN` — the mind
machinery which calls `carry-forward`, `pty-text` and `pty-activity` presents
the admin token, so widening them to the service token here would have been a
quiet loosening dressed up as a refactor. Routes the service token reaches now
and which belong to no person are `MACHINE`, and they keep reaching
everything, because a mind is not a person and has no conversations of its own
to be scoped to.

What changes is the person-driven set: a conversation's routes now resolve the
caller and refuse one who does not own the mind the conversation runs on.
"""

from __future__ import annotations

from comms.user_access import (
    ADMIN,
    MACHINE,
    OWNER,
    PUBLIC,
    SCOPED,
    Rule,
    RouteTable,
)

TABLE = RouteTable()

# --- No credential ---------------------------------------------------------
# `/sms/inbound` authenticates the sending device by HMAC over the body and is
# exempt from the bearer gate today; the signature is its credential and the
# bearer would be a second one no gateway sends. `/health` is not here: it took
# the bearer before this table existed, and opening it would be a loosening
# nobody asked for.
TABLE.add(Rule(PUBLIC), "POST /sms/inbound")

# --- A person's own conversation -------------------------------------------
# The session named in the path must run on a mind the caller owns. An admin,
# and the shared token on a hive naming no primary user, reach all of them.
TABLE.add(
    Rule(OWNER, param="session_id"),
    "GET /sessions/{session_id}",
    "DELETE /sessions/{session_id}",
    "POST /sessions/{session_id}/suspend",
    "POST /sessions/{session_id}/resume",
    "POST /sessions/{session_id}/message",
    "GET /sessions/{session_id}/events",
    "GET /sessions/{session_id}/history",
    "POST /sessions/{session_id}/activate",
    "POST /sessions/{session_id}/model",
    "POST /sessions/{session_id}/autopilot",
    "POST /sessions/{session_id}/interrupt",
    "POST /sessions/{session_id}/remote-control",
    "DELETE /sessions/{session_id}/remote-control",
    # Renaming additionally carries `require_rename_bearer`, a capability
    # token that names no user. The two compose: the rename token says the
    # caller may rename something, this says which conversations are theirs.
    "PUT /sessions/{session_id}/name",
    # Both websockets. These are the reason the gate is pure ASGI — the HTTP
    # middleware comms used to rely on does not see them at all.
    "WS /sessions/{session_id}/stream",
    "WS /sessions/{session_id}/attach",
)

# --- A recognised caller, whose handler filters its own answer -------------
TABLE.add(
    Rule(SCOPED),
    "GET /sessions",
    "GET /sessions/names",
    # Creation names the mind it belongs to and the handler checks the caller
    # owns it. Nothing in the path identifies a conversation yet, so there is
    # nothing for an owner rule to resolve.
    "POST /sessions",
    # `/command` carries /kill, /switch, /prune and /status against
    # body-supplied references, and it lives outside the /sessions path
    # entirely — which is exactly why the table enumerates every route rather
    # than everything under one prefix.
    "POST /command",
)

# --- The machinery each mind runs -----------------------------------------
# Addressed by `(client_type, client_ref)` or by mind, not by a conversation
# the caller could own, so there is nothing here for an owner check to scope.
# A *mind's* own credential reaches these and a *person's* does not: every
# rotation hook, context report and turn-ledger write on the hive comes
# through here, and the one shared value they all used to present is gone.
TABLE.add(
    Rule(MACHINE),
    "GET /health",
    "POST /sessions/context",
    "POST /sessions/{session_id}/context",
    "GET /sessions/late-turns",
    "POST /sessions/arm-rotation",
    # The rotation summary. Addressed by the *conversation* id the hook
    # holds — a `claude_sid`, which is not a `sessions.id` and which no
    # lookup here can resolve — so the `OWNER` class refused every mind on
    # the hive the moment it was applied, and the carry-forward each
    # rotation composes was silently dropped. What the write has to be held
    # to is the mind named in its body, which `write_rotation_memory`
    # checks against the caller's own holder.
    "POST /sessions/{session_id}/rotation-memory",
    # A mind registering itself, which is why every mind used to need the
    # admin token. `broker_register_mind` refuses a holder that is not the
    # mind being written, so this class admits the machinery and the handler
    # decides which row it may touch.
    "POST /broker/minds",
    "POST /sessions/fire-rotation",
    "POST /sessions/record-turn",
    "POST /sessions/{session_id}/harness-state",
    # The three calls a mind makes about a conversation of its own: the
    # seed it is owed when it opens a terminal, and the prose and the
    # activity it tails out of that terminal's transcript. They were
    # admin-only, which is why every mind on the hive had to hold a value
    # that opens every other mind — the one thing this story exists to
    # end. `_require_own_session_mind` is stricter than this class on its
    # own: a `service` holder is refused, because a carry-forward is a
    # fully composed prompt and every surface bot holds a service value.
    "POST /sessions/{session_id}/pty-activity",
    "POST /sessions/{session_id}/pty-text",
    "GET /sessions/{session_id}/carry-forward",
    "GET /models",
    "GET /broker/minds",
    # A bot asking who it answers, offering a message for judgement, and
    # carrying its install's existing allow-list entry across on first boot.
    # The service token, because every surface bot holds one and none of
    # these names a conversation: the read returns no phrase and no digest,
    # and the attempt is decided here against this clock rather than the
    # bot's.
    "GET /broker/surface-claims",
    "GET /broker/minds/{mind_id}/surface-claim",
    "POST /broker/minds/{mind_id}/surface-claim/attempt",
    "POST /broker/minds/{mind_id}/surface-claim/adopt",
    "POST /broker/messages",
    "GET /broker/messages",
    "GET /broker/conversations/{conversation_id}",
    "GET /secrets/{key}",
    "GET /secrets/scopes/{mind_name}",
    "GET /sms/inbound/enabled",
    "PUT /sms/inbound/enabled",
    "GET /linkedin/auth",
    "GET /linkedin/callback",
)

# --- Admin only ------------------------------------------------------------
# Each of these takes only the admin token today and continues to.
TABLE.add(
    Rule(ADMIN),
    "GET /sessions/live",
    "GET /sessions/live/text",
    "PUT /broker/minds/{mind_id}/owner",
    # Minting a phrase is the one call that sees it in the clear, and
    # clearing a claim takes a bot off the person holding it. The console
    # presents the admin token for both and scopes the operator to minds they
    # own on its own side, exactly as it does for a surface token.
    "POST /broker/minds/{mind_id}/surface-claim/phrase",
    "DELETE /broker/minds/{mind_id}/surface-claim",
    "PUT /broker/minds/{name}",
    "DELETE /broker/minds/{name}",
    "POST /secrets/scopes",
    "DELETE /secrets/scopes",
    # Minting and revoking a user's credential. Admin, obviously: the route
    # that issues credentials cannot answer to one it issued.
    "PUT /credentials/{holder}",
    "DELETE /credentials/{holder}",
    "GET /credentials",
)
