"""The one person this bot answers, asked of the record rather than the boot.

A bot is never shared. Which person it answers used to be an entry in the
host's `config.yaml`, handed over in `SurfaceConfig` once before either
surface started — so the answer was a snapshot taken at boot, and a person
claiming their own new bot was refused by the process already running until
somebody restarted the mind.

So it travels the path the voice engine travels: the gateway's row is the
truth and this asks it on a short cache, exactly as `voice_routing` does. The
cache is what keeps a per-message check from being a per-message round trip.

Two refusals are deliberately different. A bot nobody has claimed refuses
everyone, because an unclaimed bot that answered the first person to find it
is a bot anybody can take. A gateway that cannot be reached keeps serving the
last claim it gave us, however old: the alternative is that a blip in the
gateway locks a person out of their own mind, and the bot cannot do anything
useful without that gateway anyway — it can only answer honestly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("hive-mind.surface-claim")

#: How long one claim is reused. Short, because a person who has just sent
#: the phrase is waiting for the bot to start answering them, and a minute of
#: "Not authorized" after a successful claim reads as a failed claim.
DEFAULT_TTL_SECONDS = 15.0

#: How long a refused or unreachable gateway stops this resolver deciding.
#: Long enough that a restart loop does not log per message, short enough
#: that a gateway coming back is believed within a minute.
BACKOFF_SECONDS = 45.0

#: How long to wait on the gateway. A surface holds the user's message while
#: this runs, so a hung gateway must not hold it for long.
DEFAULT_TIMEOUT_S = 5.0


class _RouteAbsent(Exception):
    """The gateway has no claim routes — it predates them."""


@dataclass(frozen=True)
class ClaimAnswer:
    """What came back about one inbound message on an unclaimed bot."""

    claimed: bool
    reason: str
    claimed_ref: str = ""


class ClaimResolver:
    """Resolves, and caches, which surface id this mind's bot answers.

    Never raises. Every failure path answers "not this person", because the
    one thing an authorization check must not do is throw inside a handler
    and leave the sender with silence.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        mind_id: str,
        surface: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock=time.monotonic,
        session_factory=None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self._mind_id = mind_id or ""
        self._surface = surface
        self._ttl = ttl_seconds
        self._timeout = timeout_s
        self._clock = clock
        self._session_factory = session_factory
        self._claimed_ref = ""
        self._fetched_at = 0.0
        self._ever_fetched = False
        self._unsupported_until = 0.0

    def _back_off(self, exc: Exception) -> None:
        """Stop deciding for a while, and say so once per spell.

        Logged at warning rather than debug: this is the mind falling back to
        a file it is meant to have stopped reading, and the operator's only
        chance of connecting "my kid's bot refuses him" to "that mind has no
        credential" is this line.
        """
        now = self._clock()
        if now >= self._unsupported_until:
            log.warning(
                "claim unreadable (%s); falling back to the allow-list for %.0fs",
                exc, BACKOFF_SECONDS,
            )
        self._unsupported_until = now + BACKOFF_SECONDS

    # -- transport ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        if self._session_factory is not None:
            session_cm = self._session_factory()
        else:
            session_cm = aiohttp.ClientSession(timeout=timeout)
        async with session_cm as session:
            async with session.request(
                method, url, json=payload, headers=self._headers()
            ) as response:
                if response.status == 404:
                    raise _RouteAbsent(f"{method} {path} answered 404")
                if response.status >= 400:
                    raise RuntimeError(f"{method} {path} answered {response.status}")
                body = await response.json()
        return body if isinstance(body, dict) else {}

    # -- reads -------------------------------------------------------------
    @property
    def supported(self) -> bool:
        """Whether this resolver can decide anything yet.

        False until the gateway has answered once, and false again while a
        refusal is being backed off. The surface then falls back to the
        allow-list it was configured with, which is what keeps a mind dark
        only when it has no answer from either source.

        That covers more than a gateway which predates the claim routes. A
        mind with no credential of its own gets 401 on every call, and a mind
        behind a proxy that closes `/broker` gets 404 — in both the console
        shows an accepted token and an unclaimed bot, and no screen anywhere
        says the word credential. Refusing its owner on that evidence is the
        one outcome worth avoiding, and the backoff is why a gateway that
        comes back is believed again rather than written off for the life of
        the process.
        """
        if not self._ever_fetched:
            return False
        return self._clock() >= self._unsupported_until

    async def claimed_ref(self, *, force: bool = False) -> str:
        """The surface id this bot answers, or empty if it is unclaimed.

        A fetch that fails leaves whatever we had in place rather than
        reporting an unclaimed bot: "nobody has claimed this" and "I could not
        ask" have the same shape on screen and opposite remedies.
        """
        now = self._clock()
        fresh = self._ever_fetched and (now - self._fetched_at) < self._ttl
        if fresh and not force:
            return self._claimed_ref
        if not self._base_url or not self._mind_id:
            return self._claimed_ref
        try:
            body = await self._request(
                "GET",
                f"/broker/minds/{self._mind_id}/surface-claim?surface={self._surface}",
            )
        except _RouteAbsent as exc:
            self._back_off(exc)
            return ""
        except Exception as exc:  # noqa: BLE001 — an authorizer never raises
            # Keep the last answer, and stop deciding if we never had one.
            # A 401 from a mind with no credential looks exactly like this.
            self._back_off(exc)
            return self._claimed_ref
        self._claimed_ref = str(body.get("claimed_ref") or "")
        self._fetched_at = now
        self._ever_fetched = True
        self._unsupported_until = 0.0
        return self._claimed_ref

    async def is_allowed(self, user_id) -> bool:
        """Whether this is the one person, compared as text.

        Text because Discord's snowflakes travel as strings and Telegram's ids
        as integers, and the record holds one column for both.
        """
        ref = str(user_id or "").strip()
        if not ref:
            return False
        return ref == await self.claimed_ref()

    async def is_claimed(self) -> bool:
        return bool(await self.claimed_ref())

    # -- writes ------------------------------------------------------------
    async def attempt_claim(self, sender_ref, text: str) -> ClaimAnswer:
        """Offer one message for judgement, and take the claim if it matches.

        The gateway decides: the deadline belongs to its clock, and the write
        that claims is conditional on the bot still being unclaimed, so two
        people sending the phrase at once produce one claim rather than two
        confirmations.
        """
        ref = str(sender_ref or "").strip()
        if not ref or not self._base_url or not self._mind_id:
            return ClaimAnswer(False, "unreachable")
        try:
            body = await self._request(
                "POST",
                f"/broker/minds/{self._mind_id}/surface-claim/attempt",
                {"surface": self._surface, "sender_ref": ref, "text": text or ""},
            )
        except _RouteAbsent as exc:
            self._back_off(exc)
            return ClaimAnswer(False, "unsupported")
        except Exception as exc:  # noqa: BLE001
            self._back_off(exc)
            return ClaimAnswer(False, "unreachable")
        answer = ClaimAnswer(
            bool(body.get("claimed")),
            str(body.get("reason") or ""),
            str(body.get("claimed_ref") or ""),
        )
        if answer.reason == "already_claimed" and answer.claimed:
            # This person already owns the bot; a stale cache is why we were
            # asking at all. Record it and say so, so the caller admits the
            # message rather than answering it with a claim confirmation and
            # dropping what they typed.
            self._claimed_ref = answer.claimed_ref or ref
            self._fetched_at = self._clock()
            self._ever_fetched = True
            self._unsupported_until = 0.0
            return ClaimAnswer(False, "already_yours", self._claimed_ref)
        if answer.claimed:
            # The person is waiting for their next message to be answered, so
            # the new claim is installed here rather than waited for.
            self._claimed_ref = answer.claimed_ref or ref
            self._fetched_at = self._clock()
            self._ever_fetched = True
            self._unsupported_until = 0.0
        return answer

    async def adopt(self, sender_ref) -> ClaimAnswer:
        """Carry this install's existing single allow-list entry across.

        Called once at start by the host, so the deploy that stops reading
        `config.yaml` does not leave every bot in the hive refusing its own
        owner — including the one the operator fixes things through.
        """
        ref = str(sender_ref or "").strip()
        if not ref or not self._base_url or not self._mind_id:
            return ClaimAnswer(False, "unreachable")
        try:
            body = await self._request(
                "POST",
                f"/broker/minds/{self._mind_id}/surface-claim/adopt",
                {"surface": self._surface, "sender_ref": ref},
            )
        except _RouteAbsent as exc:
            self._back_off(exc)
            return ClaimAnswer(False, "unsupported")
        except Exception as exc:  # noqa: BLE001
            self._back_off(exc)
            return ClaimAnswer(False, "unreachable")
        answer = ClaimAnswer(
            bool(body.get("claimed")),
            str(body.get("reason") or ""),
            str(body.get("claimed_ref") or ""),
        )
        if answer.claimed:
            self._claimed_ref = answer.claimed_ref or ref
            self._fetched_at = self._clock()
            self._ever_fetched = True
        return answer


# ---------------------------------------------------------------------------
# Which resolver this process uses
# ---------------------------------------------------------------------------
# Module state rather than a `SurfaceConfig` field. Only the host knows its
# gateway and its own mind id, so the host still supplies it — but a surface
# asking `config` for it would be handed whatever a test stood in for the
# whole configuration object, and an authorizer that awaits a mock raises
# inside a handler, which is silence on the surface.
_installed: dict[str, ClaimResolver] = {}


def install(resolver: ClaimResolver | None, surface: str = "telegram") -> None:
    """Make `resolver` the authority for `surface` in this process.

    Keyed by surface because one process hosts both bots, and a mind holding
    a Telegram bot and a Discord bot has two claims — one per bot, since each
    is claimed by sending a phrase to that bot.
    """
    if resolver is None:
        _installed.pop(surface, None)
    else:
        _installed[surface] = resolver


def installed(surface: str = "telegram") -> ClaimResolver | None:
    """The resolver this process was given for `surface`, or None."""
    return _installed.get(surface)


def forget_all() -> None:
    """Drop every installed resolver. For a host rebuilding its surfaces."""
    _installed.clear()
