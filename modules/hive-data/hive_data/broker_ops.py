"""What the gateway's broker half does that `core` could not express.

Each operation here is a real call site in comms' broker, surface-claim and
credential modules, found by porting them rather than by imagining what a
data layer ought to offer. They live beside `core` rather than in it only
because that module is being extended by another port at the same time; they
are over the same `core` schema and follow its rules: every read names its
columns, every write reports how many rows it changed, and column names are
never a caller's.

Three of them are single statements on purpose, because the SQLite module
they replace was one statement and splitting them reopens a race:

* `offer_claim_phrase` and `adopt_claim_if_undecided` are conditional
  upserts. Written as a read and then a write, the bot can be claimed in
  between and the write lands a phrase on a bot somebody already owns, or
  adopts a bot an operator just released.
* `revoke_live_credential` is conditional on the credential still being
  live, so a second revoke reports nothing changed rather than moving the
  revocation time — which is how the console tells "revoked just now" from
  "was already revoked".
"""

from __future__ import annotations

from typing import Any

from . import _sql
from .core import _execute, _fetch, _fetch_one
from .schema import COLUMNS, select_list
from .unit import Unit

Row = dict[str, Any]


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

async def get_message(message_id: str, *,
                      unit: Unit | None = None) -> Row | None:
    """One broker message by its id, or None.

    The insert is idempotent and reports only whether it landed, so the
    caller that has to hand back the stored row — the duplicate's included —
    reads it here.
    """
    return await _fetch_one(
        unit,
        f'SELECT {select_list("messages")} FROM core."messages" '
        'WHERE "id" = $1',
        message_id)


async def messages_with_status(status: str, *,
                               unit: Unit | None = None) -> list[Row]:
    """Every message in one status, oldest first.

    The restart recovery: messages still `pending` when the gateway died are
    re-dispatched in the order they arrived. Ordered by time and then by id,
    because two messages accepted in one instant share a timestamp.
    """
    return await _fetch(
        unit,
        f'SELECT {select_list("messages")} FROM core."messages" '
        'WHERE "status" = $1 ORDER BY "timestamp" ASC, "id" ASC',
        status)


async def move_messages(*, was: str, now: str, response_error: str | None,
                        unit: Unit | None = None) -> Row:
    """Move every message in one status to another, recording why.

    The other half of restart recovery: a message `dispatched` when the
    gateway died has a callee session that died with it, so it is failed with
    the reason rather than left looking in flight for ever. A write keyed to
    one id cannot express it.
    """
    return await _execute(
        unit,
        'UPDATE core."messages" SET "status" = $2, "response_error" = $3 '
        'WHERE "status" = $1',
        was, now, response_error)


# ---------------------------------------------------------------------------
# minds
# ---------------------------------------------------------------------------

#: What a mind's registration may change after the fact. Not `mind_id`, which
#: is the identity every graph node, memory and claim is keyed to, and not
#: `registered_at`, which is when the mind first appeared and is preserved by
#: every re-registration on purpose.
MIND_SETTABLE = frozenset({
    "name", "gateway_url", "model", "harness", "last_seen",
    "session_token", "voice", "voice_engine", "owner",
})


async def update_mind(mind_id: str, /, *, unit: Unit | None = None,
                      **fields: Any) -> Row:
    """Set some of a registered mind's columns, by its identity.

    `core.upsert_mind` is the registration; this is everything after it — a
    changed model or voice from the console, and an owner moved by an admin.
    An upsert cannot express those, because it inserts the mind when the row
    is absent, and a console edit to a mind that was deleted a moment ago
    must report that nothing changed rather than resurrect it.
    """
    sql, args = _sql.update(
        "minds", _sql.passed(fields), {"mind_id": mind_id},
        settable=MIND_SETTABLE)
    return await _execute(unit, sql, *args)


async def count_mind_sessions(mind_id: str, *,
                              unit: Unit | None = None) -> int:
    """How many sessions — conversations — a mind has.

    `core.sessions.mind_id` is ON DELETE RESTRICT, so a mind holding any is a
    mind that cannot be deleted. Counted first so the refusal is a sentence
    about conversations rather than a foreign-key violation.
    """
    row = await _fetch_one(
        unit,
        'SELECT count(*) AS "sessions" FROM core."sessions" '
        'WHERE "mind_id" = $1',
        mind_id)
    return 0 if row is None else int(row["sessions"])


async def delete_mind(mind_id: str, *, unit: Unit | None = None) -> Row:
    """Remove a mind's registration.

    Its surface claims go with it (ON DELETE CASCADE). Anything that is
    RESTRICT — its sessions, its graph edges, its design sessions — refuses
    the delete as `MissingReference`, which is the database keeping a
    conversation from losing the mind it ran on. Secret scopes are keyed by
    the mind's *name*, not its identity, so nothing cascades to them: a
    caller deleting a mind clears those itself, in the same unit.
    """
    return await _execute(
        unit, 'DELETE FROM core."minds" WHERE "mind_id" = $1', mind_id)


async def revoke_all_secret_scopes(mind_name: str, *,
                                   unit: Unit | None = None) -> Row:
    """Revoke every secret a mind name was granted.

    Scopes are keyed by name with no foreign key at all, so a mind deleted
    without this leaves its grants behind — and the next mind registered
    under the same name inherits every secret the old one could read.
    """
    return await _execute(
        unit,
        'DELETE FROM core."secret_scopes" WHERE "mind_name" = $1',
        mind_name)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

async def holder_of_digest(token_sha: str, *,
                           unit: Unit | None = None) -> str | None:
    """The holder a bearer digest belongs to, revoked or not.

    `core.credential_for_token` answers only live credentials, which is right
    for authenticating and wrong for minting: the digest is unique across
    every row, revoked ones included, so a value pasted into a second
    holder's field collides with a revoked row too — and the operator needs
    to be told whose it was rather than handed a uniqueness violation.
    """
    row = await _fetch_one(
        unit,
        'SELECT "holder" FROM core."credentials" WHERE "token_sha" = $1',
        token_sha)
    return None if row is None else row["holder"]


async def revoke_live_credential(holder: str, revoked_at: float, *,
                                 unit: Unit | None = None) -> Row:
    """Revoke a holder's credential only if it is still live.

    A second revoke changes nothing and says so. `core.revoke_credential`
    would move the revocation time forward and report a row changed, which
    reads as "revoked just now" for a credential that died last month.
    """
    return await _execute(
        unit,
        'UPDATE core."credentials" SET "revoked_at" = $2 '
        'WHERE "holder" = $1 AND "revoked_at" IS NULL',
        holder, revoked_at)


async def list_credentials(*, unit: Unit | None = None) -> list[Row]:
    """Every credential, revoked ones included, never the digest.

    The console's listing. `core.list_live_credentials` omits the revoked,
    and the screen has to say "revoked" where it would otherwise have to say
    "never had one" — different facts with different remedies.
    """
    return await _fetch(
        unit,
        f'SELECT {select_list("credentials")} FROM core."credentials" '
        'ORDER BY "username", "holder"')


# ---------------------------------------------------------------------------
# surface claims
# ---------------------------------------------------------------------------

#: Everything on a claim row but the key it is stored under.
CLAIM_SETTABLE = frozenset(
    c for c in COLUMNS["surface_claims"] if c not in ("mind_id", "surface"))


async def update_claim(mind_id: str, surface: str, /, *,
                       unit: Unit | None = None, **fields: Any) -> Row:
    """Set some of an existing claim's columns; change nothing if absent.

    Not an upsert. A release from the console on a bot that was never
    registered must not write a row: a row marked released is a bot that
    will never be adopted, and a bot nobody has set up has not been released
    by anybody.
    """
    sql, args = _sql.update(
        "surface_claims", _sql.passed(fields),
        {"mind_id": mind_id, "surface": surface},
        settable=CLAIM_SETTABLE)
    return await _execute(unit, sql, *args)


async def offer_claim_phrase(
    *, mind_id: str, surface: str, phrase_hash: str, issued_at: float,
    expires_at: float, unit: Unit | None = None,
) -> Row:
    """Put a phrase on offer for a bot nobody has claimed.

    One statement, conditional on the bot still being unclaimed. A fresh row
    is written with nobody holding it; an existing one has its phrase
    replaced and its guess counter reset — the previous phrase stops working
    the moment a new one is issued. A claimed bot changes nothing, and the
    row count says so.
    """
    return await _execute(
        unit,
        'INSERT INTO core."surface_claims" AS c '
        '("mind_id", "surface", "claimed_ref", "claimed_at", "phrase_hash", '
        '"phrase_issued_at", "phrase_expires_at") '
        'VALUES ($1, $2, NULL, NULL, $3, $4, $5) '
        'ON CONFLICT ("mind_id", "surface") DO UPDATE SET '
        '"phrase_hash" = EXCLUDED."phrase_hash", '
        '"phrase_issued_at" = EXCLUDED."phrase_issued_at", '
        '"phrase_expires_at" = EXCLUDED."phrase_expires_at", '
        '"phrase_attempts" = 0 '
        'WHERE c."claimed_ref" IS NULL',
        mind_id, surface, phrase_hash, issued_at, expires_at)


async def adopt_claim_if_undecided(
    *, mind_id: str, surface: str, claimed_ref: str, claimed_at: float,
    now: float, unit: Unit | None = None,
) -> Row:
    """Give a bot to one person, only while nothing has been decided about it.

    Undecided means: nobody holds it, nobody released it on purpose, and no
    phrase is on offer that has not yet expired. A row that merely exists is
    not a decision — saving a bot's token writes one, which is how every bot
    is set up. One statement, so two containers booting at once produce one
    claim.
    """
    return await _execute(
        unit,
        'INSERT INTO core."surface_claims" AS c '
        '("mind_id", "surface", "claimed_ref", "claimed_at", "phrase_hash", '
        '"phrase_expires_at") '
        'VALUES ($1, $2, $3, $4, NULL, NULL) '
        'ON CONFLICT ("mind_id", "surface") DO UPDATE SET '
        '"claimed_ref" = EXCLUDED."claimed_ref", '
        '"claimed_at" = EXCLUDED."claimed_at", '
        '"phrase_hash" = NULL, "phrase_issued_at" = NULL, '
        '"phrase_expires_at" = NULL '
        'WHERE c."claimed_ref" IS NULL '
        'AND c."released_at" IS NULL '
        'AND (c."phrase_hash" IS NULL '
        '     OR (c."phrase_expires_at" IS NOT NULL '
        '         AND c."phrase_expires_at" <= $5))',
        mind_id, surface, claimed_ref, claimed_at, now)
