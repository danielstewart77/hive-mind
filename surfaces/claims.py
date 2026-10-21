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

#: How many times adoption asks before it gives up, and how long it waits
#: between. Nothing orders a bot container after the gateway — every one of
#: them declares `depends_on` for a voice server and nothing else — so a cold
#: `docker compose up` and a host reboot both start bots while comms is still
#: binding its port. One un-retried call there leaves the resolver answering
#: "nobody" for the life of the process: the 45-second fallback expires, the
#: next read succeeds with an empty claim, and from that moment the bot
#: refuses its own owner with nothing further in the log.
ADOPT_ATTEMPTS = 6
ADOPT_DELAY_SECONDS = 5.0

#: Answers from `ClaimResolver.adopt` that mean the gateway never decided, as
#: opposed to deciding against us. Only these are worth asking again.
_RETRYABLE = ("unreachable", "unsupported")


def _configured_list(surface: str) -> list:
    """This stack's allow-list for `surface`, as configured."""
    if surface == "discord":
        return list(stack_config.discord_allowed_users or [])
    return list(stack_config.telegram_allowed_users or [])


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
        return kept
    # A reference the list never held — a container named one explicitly. Keep
    # the element type the surfaces compare against rather than the string it
    # arrived as.
    return [int(ref)] if ref.lstrip("-").isdigit() else [ref]


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
    attempts: int = ADOPT_ATTEMPTS,
    delay: float = ADOPT_DELAY_SECONDS,
    sleep=None,
) -> str:
    """Carry this container's configured person across as the claim.

    Runs before the surface starts polling, and keeps asking while the
    gateway has not answered either way. `already_decided` is the expected
    answer on every boot after the first and is not a failure.
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
    for attempt in range(1, max(1, attempts) + 1):
        answer = await resolver.adopt(ref)
        reason = answer.reason or ""
        if answer.claimed:
            log.info("surfaces: %s claim adopted for this mind", surface)
            return "adopted"
        if reason not in _RETRYABLE:
            # The gateway decided, and decided against us. Expected on every
            # boot after the first, so this is not a warning.
            log.info("surfaces: %s claim left as it stands (%s)", surface, reason)
            return reason
        if attempt < max(1, attempts):
            await waiter(delay)
    log.warning(
        "surfaces: %s claim not adopted after %d attempts (%s); the bot is "
        "answering its configured allow-list until the gateway answers",
        surface, max(1, attempts), reason,
    )
    return reason
