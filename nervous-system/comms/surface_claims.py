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

import hive_data
from hive_data import broker_ops, core

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


async def _row(mind_id: str, surface: str) -> dict | None:
    return await core.get_claim(mind_id, surface)


def _decided(row: dict, now: float) -> bool:
    """Whether a person's hold on this bot has already been settled here.

    Three states count, and a row's bare existence is not one of them: a
    claim someone holds, a release performed on purpose, and a phrase minted
    and *still outstanding*. Everything else is a bot that has been
    registered and never decided, which is exactly what adoption is for.

    Still outstanding does the work. Nothing clears an expired `phrase_hash`
    — `attempt_claim` answers "expired" and leaves it — so counting any
    phrase at all meant one minted and never sent made the bot permanently
    unadoptable, while the console read it as unclaimed with no phrase
    pending and offered minting another, which reproduced the same row.
    """
    expires = row["phrase_expires_at"]
    pending = bool(row["phrase_hash"] or "") and (
        expires is None or float(expires) > now
    )
    return bool(
        (row["claimed_ref"] or "")
        or pending
        or row["released_at"] is not None
    )


async def claim_state(
    mind_id: str, surface: str, *, now: float | None = None
) -> ClaimState:
    """Who this bot answers, and whether a claim is currently on offer.

    A phrase past its deadline reports as no phrase pending rather than as a
    pending one that will be refused: the page's remedy for the first is to
    mint another, and for the second to wait.
    """
    key = _check_surface(surface)
    moment = time.time() if now is None else now
    row = await _row(mind_id, key)
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
    row = await _row(mind_id, key)
    if row is not None and str(row["claimed_ref"] or ""):
        raise ValueError("this bot is already claimed")
    offered = await broker_ops.offer_claim_phrase(
        mind_id=mind_id, surface=key, phrase_hash=_digest(text),
        issued_at=moment, expires_at=moment + ttl_seconds,
    )
    if offered["rows"] == 0:
        # Claimed between the read above and this write. The write was
        # conditional, so nothing landed; the phrase must not be shown.
        raise ValueError("this bot is already claimed")
    log.info("surface_claims: phrase minted mind=%s surface=%s", mind_id, key)
    return text


async def attempt_claim(
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
    row = await _row(mind_id, key)
    if row is None or not row["phrase_hash"]:
        return ClaimOutcome(False, "no_phrase")
    existing = str(row["claimed_ref"] or "")
    if existing:
        return ClaimOutcome(existing == ref, "already_claimed", existing)
    if float(row["phrase_expires_at"] or 0.0) <= moment:
        return ClaimOutcome(False, "expired")
    if not _matches(text, str(row["phrase_hash"])):
        # Counted in the statement that reads it. Read-modify-write counts
        # rounds rather than guesses: parallel attempts all read one number
        # and all write the next, and the lockout never arrives.
        #
        # The count that reaches the limit and the phrase's retirement are one
        # change: apart, a correct guess landing between them still claimed
        # the bot past its own lockout.
        async def _count_and_maybe_retire(unit) -> int:
            await unit.hold(f"claim:{mind_id}:{key}")
            counted = await core.count_phrase_attempt(
                mind_id=mind_id, surface=key, unit=unit)
            attempts = int((counted.get("row") or {}).get("phrase_attempts") or 0)
            if attempts >= MAX_PHRASE_ATTEMPTS:
                # Marked released as well as emptied. Without it the row reads
                # as undecided, so the next container restart hands the bot to
                # the configured adoption reference — a bot changing hands in
                # silence, triggered by anybody who can message an unclaimed
                # bot and guess wrong eight times. The operator mints again
                # deliberately.
                await broker_ops.update_claim(
                    mind_id, key, phrase_hash=None, phrase_issued_at=None,
                    phrase_expires_at=None, phrase_attempts=0,
                    released_at=moment, unit=unit,
                )
            return attempts

        async with hive_data.transaction() as unit:
            attempts = await _count_and_maybe_retire(unit)
        if attempts >= MAX_PHRASE_ATTEMPTS:
            log.warning(
                "surface_claims: phrase retired after %d wrong guesses mind=%s surface=%s",
                attempts, mind_id, key,
            )
            return ClaimOutcome(False, "too_many_attempts")
        return ClaimOutcome(False, "mismatch")
    won = await core.claim_if(
        # Only while the phrase that matched is still the one on offer: one
        # retired by the lockout, or replaced by a fresh mint, claims nothing.
        mind_id=mind_id, surface=key,
        expected={"claimed_ref": None, "phrase_hash": row["phrase_hash"]},
        claimed_ref=ref, claimed_at=moment, phrase_hash=None,
        phrase_issued_at=None, phrase_expires_at=None, phrase_attempts=0,
        released_at=None,
    )
    if won["rows"] != 1:
        state = await claim_state(mind_id, key, now=moment)
        return ClaimOutcome(state.claimed_ref == ref, "already_claimed", state.claimed_ref)
    log.info("surface_claims: claimed mind=%s surface=%s", mind_id, key)
    return ClaimOutcome(True, "claimed", ref)


async def clear_claim(
    mind_id: str, surface: str, *, now: float | None = None
) -> ClaimState:
    """Release a bot so it can be claimed again.

    The pending phrase goes with it: a bot handed to a second person must not
    be claimable with a phrase the first person was shown.
    """
    key = _check_surface(surface)
    await broker_ops.update_claim(
        mind_id, key, claimed_ref=None, claimed_at=None, phrase_hash=None,
        phrase_issued_at=None, phrase_expires_at=None, phrase_attempts=0,
        released_at=time.time() if now is None else now,
    )
    return await claim_state(mind_id, key)


async def adopt_claim(
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
    row = await _row(mind_id, key)
    if row is not None and _decided(row, moment):
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
    await broker_ops.adopt_claim_if_undecided(
        mind_id=mind_id, surface=key, claimed_ref=ref, claimed_at=moment,
        now=moment,
    )
    state = await claim_state(mind_id, key, now=moment)
    if state.claimed_ref != ref:
        return ClaimOutcome(False, "already_decided", state.claimed_ref)
    log.info("surface_claims: adopted mind=%s surface=%s", mind_id, key)
    return ClaimOutcome(True, "adopted", ref)


async def record_bot(
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
        return await claim_state(mind_id, key, now=moment)
    row = await _row(mind_id, key)
    known = str((row["bot_ref"] if row is not None else "") or "")
    if row is None:
        await core.upsert_claim(mind_id=mind_id, surface=key, bot_ref=ref)
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
        await broker_ops.update_claim(
            mind_id, key, bot_ref=ref, claimed_ref=None, claimed_at=None,
            phrase_hash=None, phrase_issued_at=None, phrase_expires_at=None,
            phrase_attempts=0, released_at=moment,
        )
    else:
        await broker_ops.update_claim(mind_id, key, bot_ref=ref)
    return await claim_state(mind_id, key, now=moment)
