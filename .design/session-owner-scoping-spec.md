# Spec — Scope conversations and sessions to their owner

Story on the hive Kanban board, feature "Multi-user hive".
Work lands in `/home/daniel/Storage/Dev/hive_mind/nervous-system/comms`
(`server.py`, `sessions.py`, `auth.py`, `broker.py`) and in
`/home/daniel/Storage/Dev/hive/src` (`credential_registry.py`,
`credentials_data.py`, user-management screen) for minting.

NO IMPLEMENTATION EXISTS YET. This is a specification to be refuted.

## Established facts about the code as it stands

- `comms/auth.py` has three credentials: `COMMS_BEARER_TOKEN` (service,
  held by every surface bot and every mind including the kids' Windows
  boxes), `COMMS_ADMIN_BEARER_TOKEN`, `COMMS_RENAME_TOKEN`. None names a
  user. Unset service token = bypass mode.
- The live `sessions` table has `owner_type`, `owner_ref` (the *surface*
  owner: a telegram chat id) and `mind_id`. It has no hive-user column.
- The live broker `minds` table has an `owner` column, already populated:
  arnold=sloan, zach=aiden, everything else daniel.
- Session ownership is therefore DERIVED: session -> mind_id -> minds.owner.
  No new column on sessions.
- `create_session` in `sessions.py` already takes `mind_id` keyword-only
  with no default.
- The hardcoded `ada` default is in `/sms/inbound` (server.py ~line 1730).
- `/sessions/names` deliberately answers to the service token today.
- Roughly 30 session routes exist (server.py lines 313-919), a mix of
  person-driven and mind-machinery.
- `hive/src/credential_registry.py` already models two credential kinds:
  shared secret and per-mind credential.

## Requirements (signed off by Daniel)

1. The existing shared `COMMS_BEARER_TOKEN` value becomes Daniel's own user
   credential — unchanged in value, so nothing on this host is re-tokened.
   Every other user gets a distinct credential, minted and revoked from the
   user-management screen, which shows whether each one currently works.
2. A person signed in to the console sees only conversations belonging to
   minds they own, in the session list, the names list, a conversation's
   history and its live view.
3. A person cannot open, adopt, rename, switch, suspend, resume, delete,
   interrupt, message or change the model of a conversation belonging to
   someone else's mind.
4. Asking about someone else's conversation is answered exactly as it is for
   a conversation id that does not exist, so other people's conversation ids
   cannot be found by probing.
5. A request to a person-driven conversation route carrying the old shared
   value is served as Daniel (per requirement 1). A request carrying no
   recognised user credential is refused.
6. An admin credential is served every user's conversations.
7. Each person's Telegram and Discord bot carries that person's credential,
   so their messages reach only their own mind's conversations.
8. Creating a conversation always names the mind it belongs to; nothing is
   defaulted.
9. An inbound text message reaches the mind of the person it was sent to, and
   a number linked to nobody is refused rather than delivered to ada.
10. Revoking a person's credential stops their surfaces and their console
    access on their next request, with no restart.
11. The machinery each mind runs — registering itself, reporting context,
    recording turns, arming and firing rotations, fetching a carry-forward —
    keeps working on the shared service token. Making minds present their own
    credential is a separate committed story and is OUT OF SCOPE here.
12. Arnold's and Zach's boxes have the shared token removed before comms runs
    strict code, so they break deliberately rather than authenticating as
    Daniel. Daniel's value is rotated last so a Zach box powered on later
    presents a dead credential.

## Out of scope

- Per-mind credentials toward comms (separate story).
- Partitioning lucent memory by user (separate story).
- Linking a person's Telegram/Discord identity to their hive account
  (separate story: "Route each person on a surface to their own mind").
- Microsoft / Windows domain sign-in.

## Test list (signed off by Daniel)

1. A minted user credential verifies, and that same value stops verifying
   once revoked, in the same process with no restart.
2. Listing conversations as one user returns only conversations on minds that
   user owns, and never another user's.
3. Every person-driven conversation route, asked about a conversation
   belonging to another user, answers exactly as it does for a conversation
   id that does not exist.
4. Those same routes, asked by the owner of the conversation, succeed.
5. A request to a person-driven conversation route carrying the old shared
   value is served as Daniel; one carrying an unrecognised value is refused.
6. An admin credential is served every user's conversations.
7. Every session route the app serves is classified as either person-driven
   or mind-machinery, so a session route added later fails the suite until
   somebody classifies it.
8. A text message arriving at a number linked to a user is dispatched to that
   user's own mind.
9. A text message arriving at a number linked to nobody is refused, and
   dispatches nothing.
10. The mind-machinery routes still serve a request carrying the shared
    service token.

## Deliberately untested, named rather than covered by something adjacent

- Each bot carrying its owner's credential: configuration in an environment
  file, verified by running it.
- Arnold's and Zach's boxes: operational, verified on those machines.
- Requirement 8 gets no test: `create_session` already takes its mind id as a
  required argument at HEAD, so a test would pass without this change.

## Open item found while prepping requirement 12

Both kid boxes are unreachable as of 2026-10-17 10:30 CDT — SSH refused on
192.168.5.64 (Arnold) and 192.168.5.62 (Zach). So the token cannot be removed
from either machine before shipping, and each still holds the value that
becomes Daniel's credential.

Two ways to close it, decision pending:

- Rotate Daniel's credential at ship time, distributing the new value to every
  holder on this host through the credentials registry's existing rotate
  control. The kid boxes then hold a dead value and need no visit. Cost: a
  coordinated rotation across every bind-mounted `.env` while this
  conversation is itself running through comms, and `credential_registry.py`
  warns in as many words that rotating for one holder and not the rest takes
  the hive down.
- Ship without rotating and strip the token from each box the next time it is
  powered on. Cost: either box powered on before that visit authenticates as
  Daniel.

## Grill findings — requirements reviewer, verified by hand

Two stated "facts" above were wrong and are corrected here.

**The kids' boxes hold the admin token, not the service token.** Every mind
registers itself through `POST /broker/minds`, which is
`require_admin_bearer` (`server.py:1334`), and `mind_server.py:260` reads
`COMMS_ADMIN_BEARER_TOKEN` to do it. The carry-forward and pty feed routes
are admin-guarded too. The broker shows arnold and zach last registering
35.9 days ago, so both Windows boxes hold that value today. Requirement 6
makes an admin credential a universal conversation reader, so Sloan's and
Aiden's machines can already read, message, rename and delete every one of
Daniel's conversations — before any of this work. Requirement 12 removes the
*service* token from those boxes and closes nothing.

Consequence for sequencing: requirements 2, 3 and 4 cannot be delivered while
minds authenticate with the admin token. "Give each mind its own credential to
comms and lucent" is a prerequisite for this story, not a follow-on.

**`ada` is a schema default, not just an SMS fallback.** `sessions.py:163` is
`mind_id TEXT DEFAULT 'ada'`, line 199 is `moderator_mind_id TEXT NOT NULL
DEFAULT 'ada'`, and line 557 backfilled every pre-existing row the same way.
Since ownership derives from `mind_id`, legacy rows and rows whose `mind_id`
resolves to no registered mind have no owner, and the spec never says who
those answer to.

Other surviving findings to fold in before implementation:

- Requirement 4 ("answered as if it did not exist") has no single answer:
  `GET /sessions/{id}` 404s, `DELETE` raises `ValueError` uncaught and 500s,
  `history` returns an empty 200, `message` returns an empty SSE stream, and
  suspend/resume/activate answer 409/404/410 by row state. On several routes a
  correct refusal and a successful breach are the same response, so Daniel
  cannot check the requirement from outside.
- Requirements 5 and 11 never say whether the two credential sets are a union.
  If they are, a second user's credential can post `/sessions/context`,
  `/sessions/arm-rotation`, `/sessions/record-turn` and
  `/sessions/fire-rotation` against Daniel's conversations — those routes are
  addressed by `client_type`/`client_ref`, not session id, so there is no
  session for requirements 3 and 4 to attach to.
- `POST /sessions` is called both by a person's bot and by
  `bots/scheduler.py:270` holding the shared value, so test 7's
  classification has no correct answer for it. A scoped `GET /sessions`
  returning an empty 200 also makes `_ensure_channel_session` mint a
  duplicate session.
- `PUT /sessions/{id}/name` is in requirement 3's forbidden list but is
  guarded by `require_rename_bearer`, a capability token naming no user.
  Nothing says which guard wins.
- Requirement 9 is unimplementable as written: the SMS webhook payload
  (`sms_inbound.py:52`) carries `sender`, `text`, `message_id`,
  `received_at`, `event`, `device_id` and **no recipient number**, and no
  number-to-user mapping exists in any schema.
- Requirement 1 and requirement 12 contradict each other on whether Daniel's
  value changes. Rotating it is not local: `credentials_data.py:282` writes
  `COMMS_BEARER_TOKEN` into five `.env` files and restarts comms, every
  containerised mind, skippy.service, mordecai.service and hive-tools.
- Requirement 10 cannot hold for Daniel's credential. `require_bearer` reads
  `os.environ` per request, so a registry revocation needs a comms restart;
  and if revocation clears the value, `auth.py:41` returns early, which is
  auth bypass on every route.
- Requirement 7 contradicts 2 and 3 for any mind one person owns and another
  talks to: the conversation is Daniel's by derivation and refused to the
  speaker.

## Grill findings — test reviewer, verified by hand

The test reviewer independently reached the same admin-token conclusion, and
struck or rewrote most of the list.

**Comms has no gate user scoping can hang off.** The only one is
`@app.middleware("http") _bearer_gate` (`server.py:114`), whose own docstring
says websocket connections bypass it entirely — and two session routes are
websockets: `WS /sessions/{id}/stream`, which calls `send_message`, and
`WS /sessions/{id}/attach`, requirement 2's live view. Both do a hand-rolled
inline check reading `COMMS_BEARER_TOKEN` and `COMMS_ADMIN_BEARER_TOKEN` and
close on `4401`. So scoping has to be written as a pure-ASGI middleware, the
way `hive/src/route_access.py` is, or it is absent on exactly the two routes
requirements 2 and 3 name most directly. No requirement says this.

**Test 7 is struck as written.** It is mechanically writable — `comms.server`
imports and enumerates 52 routes, 33 under `/sessions` — but hive's
equivalent only means something because `DEFAULT_RULE = Rule(ADMIN)` refuses
an unclassified route and a companion test adds a route at runtime to prove
it. Comms has no table, no rule kinds and no default. A classification living
in the test file copies rather than imports the thing it protects, and a route
added *and* typed into the machinery set with no scoping passes forever. It
earns its place only if comms gains deny-by-default classification, and then
it needs the runtime-added-route test beside it.

**Green at HEAD, therefore struck or folded:** test 6 (admin sees everything —
so does everyone, delete the feature and it still passes), test 10 (the
complement of a restriction that does not exist yet), and test 5's second
clause (`require_bearer` already 401s an unknown token). Test 5's first clause
must assert the scoping *outcome* of the shared value, not that the request
succeeded — and never assert the username, which would be a constant against
itself if the implementation hardcodes `daniel`. Test 6 survives only as the
admin arm of a two-user fixture whose non-admin arm fails at HEAD.

**Test 10 additionally fails at HEAD for an unrelated reason:**
`/sessions/live`, `/sessions/live/text`, `/sessions/{id}/pty-activity`,
`/sessions/{id}/pty-text` and `/sessions/{id}/carry-forward` are
`require_admin_bearer`, which takes the admin token only, so the shared
service token 401s on all five.

**Test 3 has no oracle.** For an id with no row, HEAD answers 404 on
`GET /sessions/{id}`, `/name`, `/activate`, `/interrupt` and `/events`; 409 on
suspend and resume; 400 on `/model`; an unhandled `ValueError` (500) on
`DELETE` and `/autopilot`; a 200 SSE stream on `/message` that raises after
headers are sent; and a 200 `{"messages": []}` on `/history`. Taken literally,
requirement 4 mandates a 500 for a cross-user delete. Error bodies embed the
session id, so a literal body comparison fails on the id alone. And where the
baseline is 200, a status-only comparison is satisfied by an implementation
that leaks — body comparison is load-bearing. The test needs a stated
per-route oracle computed by calling each route with a made-up id in the same
test, and `raise_server_exceptions=False`, and somebody has to decide what
`DELETE` and `/autopilot` should answer.

**Test 1 splits in two.** Comms cannot read hive's `users` table or
auth-service; its stores are `broker.db` and `sessions.db`. A registry-side
mint-revoke test stays green while comms snapshots credentials at boot, which
is what requirement 10 forbids. One test for the registry, one proving comms'
verifier is asked afresh per request.

**Test 8 cannot be written yet** — nothing says where the phone-number-to-user
link lives, and that linking is explicitly out of scope, so the test would
assert against whatever the implementation invents. The link needs its own
numbered requirement first. **Test 9 is green on the obvious fixture:** with
no mind named `ada` registered, `/sms/inbound` already returns
`ack-no-recipient` and dispatches nothing. The fixture must register `ada` and
link no user to the sender, and "refused" has to be defined on the wire — the
route acks 200 deliberately so the SMS gateway does not retry forever. Both
SMS tests need a valid HMAC, `SMS_INBOUND_HMAC_SECRET`, and
`sms_inbound_enabled` on in the broker db; no existing test touches the route,
so that fixture is new work.

**Uncovered by any test in the list:** `GET /sessions/names`
(`sessions.py:1043`) returns every row in the table with no owner filter, and
is deliberately widened to the service token. `_get_row` (`sessions.py:5085`)
resolves a **short id by prefix**, so scoping must be applied after resolution
and the anti-probing requirement is strongest in the prefix form, which no
test probes.

**Already done on the console side.** `hive/src/route_access.py`
`visible_sessions` and `owner_of_session` already scope listings and session
routes to the signed-in user with admin bypass, and
`hive/tests/test_route_access.py` already covers tests 2, 3, 4 and 6 across
twenty-odd routes. Only comms-level versions prove anything here. The two
layers deliberately disagree on refusal shape — the console answers 403 with
`x-hive-refused: owner` so a refusal is never mistaken for a route's own
answer, while requirement 4 asks comms to do the opposite. Defensible across a
trust boundary, but it has to be written down or someone will "fix" one to
match the other.

## Grill findings — edge reviewer, verified by hand

Three findings are not spec defects at all. They are live exposures on the
running gateway, reachable by any holder of the service or admin token —
which includes both of the boys' Windows boxes.

**1. Nothing authorizes the `mind_id` a caller names.** `POST /sessions`
(`server.py:313`) takes `mind_id` off the body and `create_session`
(`sessions.py:780`) does no owner check and no existence check — a missing
mind row is tolerated (`mind_name = (mind_row or {}).get("name") or mind_id`)
and only a missing model raises. So a credential from a kid's box can create
a session on Daniel's skippy, dispatch into it and have the turn written to
lucent under skippy's `mind_id`. Requirement 3 lists verbs on *existing*
conversations; creation is not among them. The same gap is on `POST /command`
and `GET /models?mind_id=`. It also fires in the harmless direction: a session
created naming Arnold's `mind_id` derives to sloan and vanishes from Daniel's
own list the moment it exists, with no error anywhere.

**2. The machinery routes are a cross-user write primitive.** `record_turn`
(`sessions.py:1672`) resolves its target by `(client_type, client_ref)` taken
from the request body, with no mind or owner scoping — `get_active_session` and
then an append. So that credential can post a `user`-role turn into another
person's `session_turns` ledger, where `get_late_turns` folds it into their
next conversation's `<pending-continuation>` as something they typed. That is
prompt injection into another user's mind through a route test 10 required to
keep working. `fire-rotation` on the same pair `respawn-pane -k`s their live
terminal. `/sessions/context` and `late-turns` are addressed the same way.

**3. `POST /command` is outside the audit band entirely.** It is at
`server.py:1122`, not under `/sessions`, so test 7's enumeration would never
see it. It carries `/kill`, `/switch`, `/prune` — which kills everything
matching a body-supplied `owner_ref` — `/status`, `/model` and `/autopilot`,
and `_resolve_target` accepts a position in a list built from body-supplied
`mind_id` and `owner_ref`, so it hands back other users' session ids.

Further unresolved spec gaps from the same review:

- **Bypass mode.** `require_bearer` returns before reading the header when
  `COMMS_BEARER_TOKEN` is unset (`auth.py:41`), and both websocket routes skip
  their check under `if expected:`. Requirement 5 reads as though it settles
  this but the middleware never gets as far as asking. Refuse-everything makes
  a token-less boot dead; serve-as-Daniel opens every conversation, `attach`
  included, to the LAN behind one `log.warning`.
- **The console sends one token for all its users.** `main.py`
  `_gateway_headers` and `route_access.py` `_comms_headers` both build
  `Authorization` from a single `COMMS_BEARER_TOKEN`. No numbered requirement
  says the console's outbound calls must carry the signed-in user's
  credential, so the likely outcome is every console user served as Daniel,
  requirement 2 satisfied only by hive's in-process filter, and every
  comms-side test green.
- **`visible_sessions` tolerates a `mind_id` that is a name**
  (`normalize_mind_name` fallback) because of the `DEFAULT 'ada'` legacy; the
  live db has rows with `mind_id='example'`. Comms keying on `mind_id` alone
  disagrees, so the console lists a row whose every route refuses it.
- **A re-scaffolded mind breaks registration permanently.** `minds.name` is the
  primary key and `register_mind` selects by `mind_id`, so a fresh `MIND_ID`
  under an existing name INSERTs into a PK collision; `broker_register_mind`
  catches only `OwnerRequired`, so it is a 500 the mind retries forever while
  its sessions derive to no owner. `delete_mind(name)` orphans history the
  same way.
- **Websocket refusal shape.** A pre-accept `ws.close(4401)` reaches the
  browser as HTTP 403, which is what a missing route answers — the mind side
  documents this trap and uses the ASGI denial-response extension to avoid it;
  comms does not. A browser also cannot set an `Authorization` header on a
  handshake, so how a person's credential reaches `attach` is undefined.
  Existing checks leak by close code (4404 versus 4409/4410/4411), so an owner
  check bolted on after them defeats requirement 4. And `attach`, `/events`
  and the `/message` SSE stream are checked once and live for hours, so
  requirement 10's "next request" never ends one.
- **Rename.** `require_rename_bearer` reads the admin token out of the
  `Authorization` header the middleware already consumed, so a naive "resolve
  the user from Authorization" refuses the console's own renames.
- **SMS.** The route is `BEARER_EXEMPT` with only the device's shared HMAC, so
  routing on the unnormalised `sender` field turns a forgeable body value into
  a selector for any user's mind. `conversation_id = f"sms-{sender}"` is one
  global namespace with no user in the key.
- **`group_sessions`** has no owner anywhere and `moderator_mind_id` defaults
  to `'ada'`. Dormant — zero rows live — but requirement 2's "a conversation"
  has no definition for it.
- **Implementation notes:** `require_bearer` compares with `!=` rather than
  `compare_digest` on bytes, which this codebase already learned in
  `require_rename_bearer`. And nothing says where comms reads user credentials
  from, nor what happens when that store is unreachable mid-request — the two
  failure modes being "everyone is everyone" and "the hive stops".

## Decisions after the grill (Daniel, 2026-10-17)

**Requirement 12 is struck.** Daniel's credential is never rotated during this
work. The current `COMMS_BEARER_TOKEN` value becomes his user credential at
its existing value, every one of his bots including Skippy keeps presenting
it, and nothing on this host is re-tokened. Arnold's and Zach's boxes keep the
value until those machines are next powered on and physically reachable, when
they are issued their own. Accepted risk, explicitly: for the hours or days
until then, either box can act as Daniel. Daniel's judgement is that this is
not a serious security matter and that losing Skippy's own gateway access
mid-change is the far worse outcome.

**Requirement 11 corrected.** Minds authenticate their machinery calls with
`COMMS_ADMIN_BEARER_TOKEN`, not the service token. That continues to be
accepted unchanged. The mind-machinery routes are untouched by this story.

**The invariant.** No change may alter the credential Skippy's own surfaces
present, nor the set of credentials comms accepts on the routes those
surfaces use, in a way that is not reversible without keyboard access. Daniel
is away from the machine.

## Test list after the grill

Struck:

- Old test 7 as worded, and reinstated conditionally — see the decision below.
- Old test 6 and old test 10 as standing tests; they are green at HEAD. They
  return as the admin arm and the machinery arm of a two-user fixture whose
  other arm fails at HEAD.
- Old test 5's second clause (an unrecognised value is refused): green at HEAD.
- Old test 8: no phone-number-to-user link exists and defining one is out of
  scope, so the test would assert against whatever the implementation
  invented.

Decided: **comms gains a route classification with deny-by-default**,
implemented as pure-ASGI middleware rather than an HTTP middleware, because
`@app.middleware("http")` is bypassed by exactly the two websocket routes
requirements 2 and 3 name most directly. That makes old test 7 falsifiable and
it keeps its place, paired with a companion that adds a route at runtime and
proves it is refused — the shape `hive/tests/test_route_access.py` already
uses. Enumeration covers every route, not a `/sessions` prefix, so
`POST /command` is inside the band.

The list as it now stands:

1. The registry mints a user credential that verifies, and that same value
   stops verifying once revoked.
2. Comms' verifier is asked afresh on every request, so a revocation lands
   without a restart.
3. Listing conversations as one user returns only conversations on minds that
   user owns, and never the other user's. The admin arm of the same fixture
   returns both.
4. `GET /sessions/names` is filtered the same way, since it returns every row
   in the table today.
5. Every person-driven route, asked about the other user's conversation,
   answers exactly what it answers for a made-up id — the oracle computed by
   calling that route with a made-up id inside the same test, compared on
   status and on error shape with the id normalised out.
6. Those same routes, asked by the owner, succeed.
7. A short id prefix resolves through `_get_row` before scoping is applied, so
   probing by prefix is refused the same way as probing by full id.
8. A request carrying the existing shared value is served Daniel's
   conversations and not the second user's. Asserted on which conversations
   came back, never on the username.
9. Every route the app serves is classified, so a route added later fails the
   suite until somebody classifies it.
10. A route added at runtime with no classification is refused to a non-admin.
11. Creating a conversation on a mind the caller does not own is refused.
12. `record_turn`, addressed by client reference, refuses a reference bound to
    a mind the caller does not own.
13. `POST /command` is scoped: `/kill`, `/switch`, `/prune` and `/status`
    reach only the caller's own conversations.
14. The mind-machinery routes still serve a request carrying the admin token.
15. `/sms/inbound` no longer resolves a hardcoded `ada`, and an inbound
    message it cannot route dispatches nothing.

Behaviour changes this requires, stated rather than discovered: `DELETE
/sessions/{id}` and `POST /sessions/{id}/autopilot` currently raise an
uncaught `ValueError` and answer 500 for an unknown id; they answer 404. A
cross-user refusal must not be a 500.

Deliberately untested, with the reason: each bot carrying its owner's
credential is configuration in an environment file, verified by running it.
Arnold's and Zach's boxes are operational, verified on those machines when
they are reachable. Requirement 8's "nothing is defaulted" needs no test —
`create_session` already takes its mind id as a required argument at HEAD.
Websocket credential delivery from a browser, which cannot set an
`Authorization` header on a handshake, is a design gap recorded above and not
closed by this story; the proxy continues to present Daniel's credential.
