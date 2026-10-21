"""Who this stack's bots answer: the claim, not the shared config file.

An edge mind is one process hosting both surfaces for one mind, so its
launcher installs two resolvers and reads its own `config.yaml` for the
person to carry across. This stack is the other shape: one image, one
bind-mounted project directory and one `config.yaml` serving several minds,
with a container per mind per surface. Two things follow, and both are why
this module exists rather than the launcher's loop being copied.

A container installs a resolver for the surface it actually runs. Installing
both would write a claim for a bot that does not exist, and the bots page
would then report a Discord bot nobody has registered as claimed.

The person a bot adopts comes from the deployment, named per container, and
only falls back to the surface's allow-list when that list names exactly one
person. The shared file cannot answer the question: it holds one list for
five minds, so reading it would hand one person's id to every mind's bot —
the silent change of hands the exactly-one rule exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import os

from hive_surfaces import surface_claim

from config import config as stack_config

log = logging.getLogger("hive-mind-surfaces")

#: How long adoption waits between asks. It does not give up: nothing orders a
#: bot container after the gateway — every one declares `depends_on` for a
#: voice server and nothing else — so a cold `docker compose up` and a host
#: reboot both start bots while comms is still binding its port, next to two
#: GPU voice servers and a migration.
#:
#: A budget was worse than no retry at all. Running out does not leave the bot
#: on its allow-list: `_is_allowed_user` reads the claim before it consults
#: `supported`, so the first inbound message after comms comes up succeeds
#: with an empty claim, clears the backoff, and from that moment the bot
#: refuses its own owner permanently — the process never exits, so
#: `restart: unless-stopped` never rescues it, and the only trace is one
#: warning at boot. Waiting here costs a bot that could not have served a
#: message anyway.
ADOPT_DELAY_SECONDS = 5.0

#: A gateway that is up and has no claim routes at all. Worth a couple of
#: asks in case it is mid-restart, and then not worth waiting on: the routes
#: are not going to appear, and the surface falling back to its configured
#: list is exactly what a half-deployed hive needs.
UNSUPPORTED_ATTEMPTS = 3

#: The gateway never answered: refused the connection, timed out, or failed.
#: Asked again for as long as it takes.
_UNREACHABLE = "unreachable"
#: The gateway answered 404 — it has no claim routes.
_UNSUPPORTED = "unsupported"


def _configured_list(surface: str) -> list:
    """This stack's allow-list for `surface`, as configured."""
    if surface == "discord":
        return list(stack_config.discord_allowed_users or [])
    return list(stack_config.telegram_allowed_users or [])


def _as_surface_id(ref: str) -> int | None:
    """`ref` as the integer the surfaces compare against, or None.

    ASCII digits only. `str.isdigit` is true of superscripts and circled
    numerals, which raise inside `int`, and true of fullwidth digits, which
    `int` silently normalises — so a pasted fullwidth id produced a fallback
    list that looked right beside a claim that could never match.
    """
    body = ref[1:] if ref.startswith("-") else ref
    if not body.isascii() or not body.isdigit():
        return None
    return int(ref)


def adopt_reference(surface: str) -> str:
    """The person this container's bot adopts if nobody has claimed it.

    Named by the deployment first. The allow-list is the fallback and only
    when it names exactly one person, which is the rule the hive already
    carries: guessing which of several owns the bot is how somebody else's
    bot changes hands in silence.
    """
    named = os.environ.get("SURFACE_CLAIM_ADOPT_REF", "").strip()
    if named:
        return named
    allowed = _configured_list(surface)
    if len(allowed) == 1:
        return str(allowed[0]).strip()
    return ""


def fallback_list(surface: str) -> list:
    """What the surface is handed to authorize by when the claim is unreadable.

    Narrowed to the adopted person. The surfaces fall back to this list for 45
    seconds after every failed read, so leaving the shared two-name list here
    would re-authorise the second person on every mind in the stack at once,
    every time the gateway blips — and a claim cleared on purpose would come
    back for the duration.

    Not narrowed to nothing when no reference resolves: an empty list is a bot
    that goes dark on the first blip rather than on a decision, and this stack
    is where a list naming several people is the normal case.
    """
    allowed = _configured_list(surface)
    ref = adopt_reference(surface)
    if not ref:
        return allowed
    kept = [entry for entry in allowed if str(entry).strip() == ref]
    if kept:
        dropped = [entry for entry in allowed if str(entry).strip() != ref]
        if dropped:
            # Said out loud, because it is a person losing access. Nothing
            # else in the logs distinguishes somebody dropped from the list
            # from a stranger: both arrive as one rejection line.
            log.warning(
                "surfaces: %s now answers %s alone; %s dropped from the "
                "fallback list by this mind's claim",
                surface, ref, ", ".join(str(entry) for entry in dropped),
            )
        return kept
    # A reference the list never held — a container named one explicitly.
    numeric = _as_surface_id(ref)
    if numeric is None:
        # Narrowing to a value the surfaces can never match is a bot that
        # goes dark on the first blip, so the configured list stands and the
        # disagreement is said rather than enacted.
        log.warning(
            "surfaces: %s adoption reference %r is not a surface id; the "
            "configured allow-list stands as the fallback",
            surface, ref,
        )
        return allowed
    return [numeric]


def install_claim_resolver(surface: str) -> surface_claim.ClaimResolver | None:
    """Point this container's surface at the gateway row that decides for it.

    Only the deployment knows its gateway and which mind this container is,
    so the host builds it. Missing either, the surface keeps answering the
    configured list and this says so — that line is the only thing connecting
    "my bot refuses me" to a container with no mind id.
    """
    comms_url = os.environ.get("COMMS_URL", "").strip()
    token = os.environ.get("COMMS_BEARER_TOKEN", "").strip()
    mind_id = os.environ.get("MIND_ID", "").strip()
    if not comms_url or not mind_id:
        log.warning(
            "surfaces: no %s claim resolver installed (COMMS_URL or MIND_ID unset); "
            "falling back to the configured allow-list",
            surface,
        )
        return None
    resolver = surface_claim.ClaimResolver(comms_url, token, mind_id, surface)
    surface_claim.install(resolver, surface)
    return resolver


async def adopt_claim(
    surface: str,
    *,
    attempts: int | None = None,
    delay: float = ADOPT_DELAY_SECONDS,
    sleep=None,
) -> str:
    """Carry this container's configured person across as the claim.

    Runs before the surface starts polling, and keeps asking until the
    gateway answers one way or the other. `already_decided` is the expected
    answer on every boot after the first and is not a failure.

    `attempts` caps the asks, for tests and for a deployment that would
    rather its bots came up unclaimed than waited. Unset means a gateway that
    is merely not up yet is waited for, which is the only shape in which
    requirement "nothing goes dark" survives a cold boot.
    """
    waiter = asyncio.sleep if sleep is None else sleep
    ref = adopt_reference(surface)
    if not ref:
        log.info(
            "surfaces: %s adopts nobody (no reference named and the allow-list "
            "does not name exactly one person); the bot answers whoever holds "
            "its claim",
            surface,
        )
        return "no_reference"
    resolver = surface_claim.installed(surface)
    if resolver is None:
        return "no_resolver"
    reason = "no_resolver"
    asked = 0
    unsupported_asks = 0
    while True:
        answer = await resolver.adopt(ref)
        reason = answer.reason or ""
        asked += 1
        if answer.claimed:
            log.info("surfaces: %s claim adopted for this mind", surface)
            return "adopted"
        if reason == _UNSUPPORTED:
            unsupported_asks += 1
            if unsupported_asks >= UNSUPPORTED_ATTEMPTS:
                log.warning(
                    "surfaces: %s claim routes absent after %d asks; this bot "
                    "is answering its configured allow-list",
                    surface, unsupported_asks,
                )
                return reason
        elif reason != _UNREACHABLE:
            # The gateway decided, and decided against us. Expected on every
            # boot after the first, so this is not a warning — unless the
            # claim it is holding is somebody other than the one this
            # container is configured to adopt, which is the shape a wrong
            # digit in the deployment takes and is otherwise indistinguishable
            # from a healthy boot.
            standing = (answer.claimed_ref or "").strip()
            if standing and standing != ref:
                log.warning(
                    "surfaces: %s is claimed by %s, not the %s this container "
                    "is configured to adopt; the claim wins — release it to "
                    "change hands",
                    surface, standing, ref,
                )
            else:
                log.info(
                    "surfaces: %s claim left as it stands (%s)", surface, reason
                )
            return reason
        if attempts is not None and asked >= max(1, attempts):
            log.warning(
                "surfaces: %s claim not adopted after %d asks (%s)",
                surface, asked, reason,
            )
            return reason
        if asked == 1 or asked % 12 == 0:
            log.warning(
                "surfaces: %s waiting on the gateway to adopt this bot's claim "
                "(%s, ask %d); it will not start answering until this settles",
                surface, reason, asked,
            )
        await waiter(delay)
