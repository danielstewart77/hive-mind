"""Which one person a bot answers, and how it learned.

A bot is never shared: one Telegram or Discord bot answers exactly one
person. That fact used to live in each install's `config.yaml` as
`telegram_allowed_users`, which is the wrong home for it twice over — the
list admits several people where the design admits one, and the surfaces
read it once at boot, so changing it means restarting the mind.

So the claim travels the path the voice and the model already travel: the
broker row is what every caller asks. `hive_surfaces` reads it on a short
cache exactly as `voice_routing` reads the engine, which is what lets a
person claim a bot and be answered by the process already running.

Nobody types their own numeric id. The console mints a pass phrase, shows it
once, and keeps none of it: what is stored here is a digest and a deadline.
The bot hands the text it received to this module and is told yes or no, so
the comparison happens on the gateway's clock rather than on a Windows box
that has been powered off for a fortnight, and two people racing the same
phrase resolve against one conditional update instead of two winners.

A phrase is never readable back, not even by an admin. One that could be
read is one an unguarded listing route hands to the LAN, and the claim that
follows is indistinguishable from the real person's.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import unicodedata
from dataclasses import dataclass

import aiosqlite

log = logging.getLogger(__name__)

#: The surfaces a bot can be. One row per mind per surface: a mind may hold a
#: Telegram bot and a Discord bot, and each is claimed on its own.
SURFACES: tuple[str, ...] = ("telegram", "discord")

#: How long a minted phrase stays claimable. Long enough to read it off the
#: console and type it into a phone, short enough that a phrase left on a
#: screen overnight is not a standing invitation.
PHRASE_TTL_SECONDS = 30 * 60

#: Words a phrase is built from — short, unambiguous when spoken aloud, and
#: free of the characters a phone keyboard rewrites. No pair differs only by
#: a letter a mobile autocorrect swaps.
_WORDS: tuple[str, ...] = (
    "anchor", "basalt", "cinder", "driftwood", "ember", "fathom", "granite",
    "harbour", "indigo", "juniper", "kestrel", "lantern", "meridian", "nimbus",
    "obsidian", "pewter", "quarry", "rivet", "saffron", "tundra", "umber",
    "verdigris", "willow", "zephyr",
)

#: How many words one phrase carries. Five from twenty-four is twenty-three
#: bits — three was fourteen, which is thirteen thousand candidates against
#: a thirty-minute window, and the comment claiming fifty was simply wrong.
#: The attempt counter below is what actually closes it; this is for the
#: case where somebody is guessing faster than the counter is read.
_PHRASE_WORDS = 5

#: How many wrong guesses retire a phrase. The route is reachable by anyone
#: who can direct-message an unclaimed bot, so a window with no counter is a
#: window somebody can simply walk through.
MAX_PHRASE_ATTEMPTS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS surface_claims (
    mind_id          TEXT NOT NULL,
    surface          TEXT NOT NULL,
    claimed_ref      TEXT,
    claimed_at       REAL,
    phrase_hash      TEXT,
    phrase_issued_at REAL,
    phrase_expires_at REAL,
    bot_ref          TEXT,
    phrase_attempts  INTEGER NOT NULL DEFAULT 0,
    released_at      REAL,
    PRIMARY KEY (mind_id, surface)
);
"""


class UnknownSurface(ValueError):
    """A surface this build has no notion of."""


@dataclass(frozen=True)
class ClaimState:
    """What the console and the surfaces are allowed to know.

    `claimed_ref` is the surface's own id for the one person, held as text
    because Discord's snowflakes outgrow a signed 64-bit column in the
    places this travels through as JSON.

    `phrase_pending` says a claim is on offer without saying what it is.
    """

    mind_id: str
    surface: str
    claimed_ref: str = ""
    claimed_at: float = 0.0
    phrase_pending: bool = False
    phrase_expires_at: float = 0.0

    @property
    def claimed(self) -> bool:
        return bool(self.claimed_ref)


@dataclass(frozen=True)
class ClaimOutcome:
    """The answer to one inbound message on an unclaimed bot.

    `reason` is reported rather than folded into a bare false, because the
    remedies differ: a phrase nobody minted needs an admin on the console, an
    expired one needs a fresh mint, and a mismatch needs the person to look
    at what they typed.
    """

    claimed: bool
    reason: str
    claimed_ref: str = ""


def _check_surface(surface: str) -> str:
    key = (surface or "").strip().lower()
    if key not in SURFACES:
        raise UnknownSurface(f"no such surface: {surface!r}")
    return key


def normalize_phrase(text: str) -> str:
    """One spelling of a phrase, whatever keyboard produced it.

    A phone capitalises the first word, adds a trailing full stop, and on iOS
    turns a hyphen pair into an en dash. None of those is a different phrase,
    and a person who has to defeat their own keyboard to claim their bot will
    conclude the bot is broken.
    """
    folded = unicodedata.normalize("NFC", text or "").strip().casefold()
    dashed = folded.replace("\u2013", "-").replace("\u2014", "-")
    return " ".join(dashed.rstrip(".,!?;:").split())


def _digest(text: str) -> str:
    return hashlib.sha256(normalize_phrase(text).encode("utf-8")).hexdigest()


def _matches(candidate: str, stored_hash: str) -> bool:
    """Bytes, never str.

    `compare_digest` raises `TypeError` on a `str` holding anything outside
    ASCII, and a phrase typed on a non-Latin keyboard would reach it. A raise
    here is silence on the surface, which is the one thing a refusal must
    never look like.
    """
    return hmac.compare_digest(
        _digest(candidate).encode("ascii"), (stored_hash or "").encode("ascii")
    )


def new_phrase() -> str:
    """A fresh phrase. Shown once and never stored in the clear."""
    return " ".join(secrets.choice(_WORDS) for _ in range(_PHRASE_WORDS))


async def init(db: aiosqlite.Connection) -> None:
    await db.executescript(SCHEMA)
    cur = await db.execute("PRAGMA table_info(surface_claims)")
    have = {row[1] for row in await cur.fetchall()}
    if "bot_ref" not in have:
        await db.execute("ALTER TABLE surface_claims ADD COLUMN bot_ref TEXT")
    if "phrase_attempts" not in have:
        await db.execute(
            "ALTER TABLE surface_claims ADD COLUMN phrase_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "released_at" not in have:
        # When a person's hold on this bot was deliberately ended — a release
        # from the console, or a token replaced with a different bot account.
        # A row on its own cannot stand for that: `record_bot` writes one the
        # moment a token is saved, which is the documented way to set a bot
        # up, and reading that as a decision made it the way to leave the bot
        # permanently unadoptable.
        await db.execute("ALTER TABLE surface_claims ADD COLUMN released_at REAL")
    await db.commit()


async def _row(db: aiosqlite.Connection, mind_id: str, surface: str) -> aiosqlite.Row | None:
    cur = await db.execute(
        "SELECT * FROM surface_claims WHERE mind_id = ? AND surface = ?",
        (mind_id, surface),
    )
    return await cur.fetchone()


def _decided(row: aiosqlite.Row) -> bool:
    """Whether a person's hold on this bot has already been settled here.

    Three states count, and a row's bare existence is not one of them: a
    claim someone holds, a release performed on purpose, and a phrase minted
    and still outstanding. Everything else is a bot that has been registered
    and never decided, which is exactly what adoption is for.
    """
    return bool(
        (row["claimed_ref"] or "")
        or (row["phrase_hash"] or "")
        or row["released_at"] is not None
    )


async def claim_state(
    db: aiosqlite.Connection, mind_id: str, surface: str, *, now: float | None = None
) -> ClaimState:
    """Who this bot answers, and whether a claim is currently on offer.

    A phrase past its deadline reports as no phrase pending rather than as a
    pending one that will be refused: the page's remedy for the first is to
    mint another, and for the second to wait.
    """
    key = _check_surface(surface)
    moment = time.time() if now is None else now
    row = await _row(db, mind_id, key)
    if row is None:
        return ClaimState(mind_id=mind_id, surface=key)
    expires = float(row["phrase_expires_at"] or 0.0)
    pending = bool(row["phrase_hash"]) and expires > moment
    return ClaimState(
        mind_id=mind_id,
        surface=key,
        claimed_ref=str(row["claimed_ref"] or ""),
        claimed_at=float(row["claimed_at"] or 0.0),
        phrase_pending=pending,
        phrase_expires_at=expires if pending else 0.0,
    )


async def mint_phrase(
    db: aiosqlite.Connection,
    mind_id: str,
    surface: str,
    *,
    now: float | None = None,
    ttl_seconds: float = PHRASE_TTL_SECONDS,
    phrase: str | None = None,
) -> str:
    """Put a fresh phrase on offer and hand it back exactly once.

    Minting replaces whatever was on offer, so the previous phrase stops
    claiming anything the moment a new one is issued — an operator who mints
    again because the first went astray has not left two live doors open.

    A bot that is already claimed is refused: taking it over is `clear_claim`,
    which is a deliberate act with its own route, not a side effect of
    pressing the button that shows a phrase.
    """
    key = _check_surface(surface)
    moment = time.time() if now is None else now
    text = phrase or new_phrase()
    row = await _row(db, mind_id, key)
    if row is not None and str(row["claimed_ref"] or ""):
        raise ValueError("this bot is already claimed")
    await db.execute(
        "INSERT INTO surface_claims "
        "(mind_id, surface, claimed_ref, claimed_at, phrase_hash, phrase_issued_at, phrase_expires_at) "
        "VALUES (?, ?, NULL, NULL, ?, ?, ?) "
        "ON CONFLICT(mind_id, surface) DO UPDATE SET "
        "phrase_hash = excluded.phrase_hash, "
        "phrase_issued_at = excluded.phrase_issued_at, "
        "phrase_expires_at = excluded.phrase_expires_at, "
        "phrase_attempts = 0 "
        "WHERE surface_claims.claimed_ref IS NULL",
        (mind_id, key, _digest(text), moment, moment + ttl_seconds),
    )
    await db.commit()
    log.info("surface_claims: phrase minted mind=%s surface=%s", mind_id, key)
    return text


async def attempt_claim(
    db: aiosqlite.Connection,
    mind_id: str,
    surface: str,
    sender_ref: str,
    text: str,
    *,
    now: float | None = None,
) -> ClaimOutcome:
    """Decide one inbound message against the phrase on offer.

    The write is conditional on the bot still being unclaimed in the same
    statement that claims it, so two people sending the phrase at the same
    instant — which a bot handling updates concurrently will do — produce one
    claim and one refusal rather than two confirmations and a last-writer-wins
    row.
    """
    key = _check_surface(surface)
    moment = time.time() if now is None else now
    ref = str(sender_ref or "").strip()
    if not ref:
        return ClaimOutcome(False, "no_sender")
    row = await _row(db, mind_id, key)
    if row is None or not row["phrase_hash"]:
        return ClaimOutcome(False, "no_phrase")
    existing = str(row["claimed_ref"] or "")
    if existing:
        return ClaimOutcome(existing == ref, "already_claimed", existing)
    if float(row["phrase_expires_at"] or 0.0) <= moment:
        return ClaimOutcome(False, "expired")
    if not _matches(text, str(row["phrase_hash"])):
        attempts = int(row["phrase_attempts"] or 0) + 1
        if attempts >= MAX_PHRASE_ATTEMPTS:
            await db.execute(
                "UPDATE surface_claims SET phrase_hash = NULL, "
                "phrase_issued_at = NULL, phrase_expires_at = NULL, "
                "phrase_attempts = 0 WHERE mind_id = ? AND surface = ?",
                (mind_id, key),
            )
            await db.commit()
            log.warning(
                "surface_claims: phrase retired after %d wrong guesses mind=%s surface=%s",
                attempts, mind_id, key,
            )
            return ClaimOutcome(False, "too_many_attempts")
        await db.execute(
            "UPDATE surface_claims SET phrase_attempts = ? "
            "WHERE mind_id = ? AND surface = ?",
            (attempts, mind_id, key),
        )
        await db.commit()
        return ClaimOutcome(False, "mismatch")
    cur = await db.execute(
        "UPDATE surface_claims SET claimed_ref = ?, claimed_at = ?, "
        "phrase_hash = NULL, phrase_issued_at = NULL, phrase_expires_at = NULL, "
        "phrase_attempts = 0, released_at = NULL "
        "WHERE mind_id = ? AND surface = ? AND claimed_ref IS NULL",
        (ref, moment, mind_id, key),
    )
    await db.commit()
    if cur.rowcount != 1:
        state = await claim_state(db, mind_id, key, now=moment)
        return ClaimOutcome(state.claimed_ref == ref, "already_claimed", state.claimed_ref)
    log.info("surface_claims: claimed mind=%s surface=%s", mind_id, key)
    return ClaimOutcome(True, "claimed", ref)


async def clear_claim(
    db: aiosqlite.Connection, mind_id: str, surface: str, *, now: float | None = None
) -> ClaimState:
    """Release a bot so it can be claimed again.

    The pending phrase goes with it: a bot handed to a second person must not
    be claimable with a phrase the first person was shown.
    """
    key = _check_surface(surface)
    await db.execute(
        "UPDATE surface_claims SET claimed_ref = NULL, claimed_at = NULL, "
        "phrase_hash = NULL, phrase_issued_at = NULL, phrase_expires_at = NULL, "
        "phrase_attempts = 0, released_at = ? WHERE mind_id = ? AND surface = ?",
        (time.time() if now is None else now, mind_id, key),
    )
    await db.commit()
    return await claim_state(db, mind_id, key)


async def adopt_claim(
    db: aiosqlite.Connection,
    mind_id: str,
    surface: str,
    sender_ref: str,
    *,
    now: float | None = None,
) -> ClaimOutcome:
    """Carry one install's existing allow-list entry across as its claim.

    Every bot in the hive is already answering somebody, recorded as a single
    entry in that install's `config.yaml`. Without this, deploying the code
    that stops reading that file leaves every bot — including the one the
    operator fixes things through — refusing its own owner, and re-claiming
    needs a console phrase typed into a surface that is down.

    Only an unclaimed bot adopts, so a second boot changes nothing, and only
    one id: a list naming several people is not a bot that answers one, and
    guessing which of them owns it is how somebody else's bot gets handed
    over silently.
    """
    key = _check_surface(surface)
    moment = time.time() if now is None else now
    ref = str(sender_ref or "").strip()
    if not ref:
        return ClaimOutcome(False, "no_sender")
    row = await _row(db, mind_id, key)
    if row is not None and _decided(row):
        # Decided here already: claimed, deliberately released, or holding a
        # phrase somebody is about to send. Adoption runs on every boot, so
        # an adoption that overwrote any of those would undo a release at the
        # next restart — the YAML still deciding, one restart later.
        #
        # A row that is merely *present* is not a decision. `record_bot`
        # writes one the moment a token is saved, which is how every bot in
        # this hive is set up, so reading presence as a decision made the
        # documented setup path the way to leave a bot permanently
        # unadoptable and dark.
        existing = str(row["claimed_ref"] or "")
        return ClaimOutcome(existing == ref, "already_decided", existing)
    # Conditional on the row still being undecided rather than on it being
    # absent: a bot whose token was saved already has one. Two containers
    # starting at once therefore still produce one claim — the second finds
    # the row decided and is refused by the re-read below.
    await db.execute(
        "INSERT INTO surface_claims "
        "(mind_id, surface, claimed_ref, claimed_at, phrase_hash, phrase_expires_at) "
        "VALUES (?, ?, ?, ?, NULL, NULL) "
        "ON CONFLICT(mind_id, surface) DO UPDATE SET "
        "claimed_ref = excluded.claimed_ref, claimed_at = excluded.claimed_at "
        "WHERE surface_claims.claimed_ref IS NULL "
        "AND surface_claims.phrase_hash IS NULL "
        "AND surface_claims.released_at IS NULL",
        (mind_id, key, ref, moment),
    )
    await db.commit()
    state = await claim_state(db, mind_id, key, now=moment)
    if state.claimed_ref != ref:
        return ClaimOutcome(False, "already_decided", state.claimed_ref)
    log.info("surface_claims: adopted mind=%s surface=%s", mind_id, key)
    return ClaimOutcome(True, "adopted", ref)


async def record_bot(
    db: aiosqlite.Connection,
    mind_id: str,
    surface: str,
    bot_ref: str,
    *,
    now: float | None = None,
) -> ClaimState:
    """Note which bot account this mind's token belongs to.

    A claim is per mind and surface, but it is a claim on a *bot*: the person
    sent a phrase to one. So replacing the token with a different bot's
    releases the claim — otherwise a brand-new bot answers whoever claimed
    the old one, having never been claimed at all, and nothing on the page
    could tell the operator it had not.

    The same bot's token pasted again changes nothing: re-pasting is how an
    operator fixes a typo, not how they hand a bot on.
    """
    key = _check_surface(surface)
    moment = time.time() if now is None else now
    ref = (bot_ref or "").strip()
    if not ref:
        return await claim_state(db, mind_id, key, now=moment)
    row = await _row(db, mind_id, key)
    known = str((row["bot_ref"] if row is not None else "") or "")
    if row is None:
        await db.execute(
            "INSERT INTO surface_claims (mind_id, surface, bot_ref) VALUES (?, ?, ?) "
            "ON CONFLICT(mind_id, surface) DO UPDATE SET bot_ref = excluded.bot_ref",
            (mind_id, key, ref),
        )
    elif known and known != ref:
        log.info(
            "surface_claims: bot replaced, claim released mind=%s surface=%s",
            mind_id, key,
        )
        # Marked released, not merely emptied. A different bot account under
        # this mind is a bot nobody has claimed, and the next boot's adoption
        # would otherwise hand it the configured id — which is how a bot the
        # operator has just pointed somewhere else answers the previous
        # person's replacement.
        await db.execute(
            "UPDATE surface_claims SET bot_ref = ?, claimed_ref = NULL, "
            "claimed_at = NULL, phrase_hash = NULL, phrase_issued_at = NULL, "
            "phrase_expires_at = NULL, phrase_attempts = 0, released_at = ? "
            "WHERE mind_id = ? AND surface = ?",
            (ref, moment, mind_id, key),
        )
    else:
        await db.execute(
            "UPDATE surface_claims SET bot_ref = ? WHERE mind_id = ? AND surface = ?",
            (ref, mind_id, key),
        )
    await db.commit()
    return await claim_state(db, mind_id, key, now=moment)
