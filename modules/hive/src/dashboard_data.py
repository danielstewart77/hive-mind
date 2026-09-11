"""What the dashboard page is made of, below the routes.

The console's job here is assembly, not measurement. Comms knows which
conversations are producing and how full each one is; each mind knows its
own machine; the proxy knows the model map and what the subscription
allowances have been told. This module decides what to ask, how often, and
how to present the answers so that three different failures do not render
as one.

Four decisions live here rather than in the browser:

*Which conversations hold the columns.* A bounded number fit on a screen,
and the selection is stable by start time — one that reshuffled on each
poll would move a conversation out of view mid-sentence.

*One machine, one row.* Skippy runs bare metal on this workstation and
Mordecai runs in a container on it. They report the same host id, and the
reading that can see the real disk wins.

*Failure has shapes.* Refused, unsupported, unreachable and reporting are
four different remedies applied to four different machines; folding them
sends somebody to reboot a healthy box over a missing token.

*One snapshot, shared.* Every open tab reads the same reading. Without that,
ten tabs is ten `nvidia-smi` invocations per interval on a child's laptop.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

#: How many conversations get a column. The rest are named as waiting, never
#: dropped — six minds working and four columns must not read as a hive
#: where two of them stopped.
COLUMN_CAP = 4

#: Host figures are cheap and change fast.
HOST_TTL_SECONDS = 5.0

#: Allowance figures are a byproduct of traffic the proxy already carried,
#: so this bounds chatter with the proxy rather than with any vendor.
PROVIDER_TTL_SECONDS = 60.0

#: The model map changes when somebody edits a row, which is rare.
CATALOG_TTL_SECONDS = 300.0

#: Per-mind deadline for the host probe. Under the refresh interval on
#: purpose: a sleeping Windows box DROPs rather than refuses, so without a
#: deadline shorter than the cycle one asleep machine holds up every other
#: machine's figures and the page shows them all as current.
HOST_PROBE_TIMEOUT = 3.0


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------


def split_columns(live: list[dict], cap: int = COLUMN_CAP) -> dict:
    """Which live conversations get columns, and which are named as waiting.

    Ordered by start time, so the selection is stable across polls and a
    freed slot goes to the conversation that has been waiting longest rather
    than to whichever one a re-sort happened to surface.
    """
    ordered = sorted(live, key=lambda row: (row.get("started_at") or 0, row["session_id"]))
    return {"shown": ordered[:cap], "waiting": ordered[cap:], "cap": cap}


# --------------------------------------------------------------------------
# Hosts
# --------------------------------------------------------------------------


def host_status_for(http_status: int) -> str:
    """What a mind's answer to the host probe means.

    Four states, because they are four different things to do:

    - `reporting` — figures below.
    - `refused` — 401 or 503. The mind is up and would not answer. A token,
      not a machine. Folding this into "unreachable" is how somebody ends up
      rebooting a healthy box, and this repo's own rule forbids it.
    - `unsupported` — 404. This mind is running a build without the route.
      A deploy away, not a fault; reading it as a fault starts a hardware
      investigation into a version skew.
    - `unreachable` — anything else, including no answer at all.
    """
    if http_status == 200:
        return "reporting"
    if http_status in (401, 403, 503):
        return "refused"
    if http_status == 404:
        return "unsupported"
    return "unreachable"


def _prefers(candidate: dict, incumbent: Optional[dict]) -> bool:
    """Whether `candidate`'s reading should replace `incumbent`'s on a host.

    A reading from outside a container beats one from inside it, because
    inside a container the root filesystem is the image layer — a four
    gigabyte disk drawn for a machine holding four terabytes is not a
    rounding error, it is a number somebody would act on.
    """
    if incumbent is None:
        return True
    if incumbent.get("status") != "reporting":
        return True
    if candidate.get("status") != "reporting":
        return False
    return bool(incumbent.get("containerized")) and not candidate.get("containerized")


def merge_hosts(reports: list[dict]) -> list[dict]:
    """One row per machine, whatever the shape of each mind's answer.

    A mind that failed still produces a row: naming the machine and what
    went wrong is the point, and dropping it would make a dead box
    indistinguishable from one nobody configured.
    """
    hosts: dict[str, dict] = {}
    for report in reports:
        reading = report.get("reading") or {}
        identity = reading.get("host") or {}
        # A mind that never answered has no host id to give, so it is keyed
        # to itself — one row for that machine, named by the mind on it.
        # Keyed to the mind when there is no host id to key to. A shared
        # fallback would merge every machine that failed to answer into one
        # row, so two boxes asleep at once would show as one and the second
        # would be invisible.
        key = identity.get("host_id") or f"mind:{report.get('mind') or id(report)}"
        candidate = {
            "host_id": key,
            "hostname": identity.get("hostname") or report.get("mind") or "",
            "containerized": bool(identity.get("containerized")),
            "platform": identity.get("platform"),
            "status": report.get("status"),
            "error": report.get("error"),
            "gpu": reading.get("gpu"),
            "memory": reading.get("memory"),
            "disk": reading.get("disk"),
            "observed_at": reading.get("observed_at"),
        }
        existing = hosts.get(key)
        if existing is None:
            candidate["minds"] = [report.get("mind")]
            hosts[key] = candidate
            continue
        minds = existing["minds"] + [report.get("mind")]
        if _prefers(candidate, existing):
            candidate["minds"] = minds
            hosts[key] = candidate
        else:
            existing["minds"] = minds
    return sorted(hosts.values(), key=lambda host: host["hostname"])


# --------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------


def window_for(model: str, catalog: dict[str, Optional[int]]) -> Optional[int]:
    """The context window the proxy's model map declares for `model`."""
    return catalog.get(model)


def resolve_window(
    reported: Optional[int], catalog_window: Optional[int]
) -> Optional[int]:
    """The window to draw against, preferring what the mind reported.

    The catalog holds the *deployment's* window; a mind holds the running
    process's. A Claude conversation spawned without the long-context pin
    holds 200k on a row that says a million, and only the mind can tell.
    """
    return reported if reported else catalog_window


def context_reading(context: dict) -> dict:
    """A conversation's fullness, with every absence left as an absence.

    Nothing here is zero-filled. A count of zero is a claim that a
    conversation is empty; an absent count is a claim that nobody has
    measured it. Drawn as a bar, the first says there is room and the second
    says nothing — and a session sitting at 138,000 tokens rendered at zero
    is the one wrong answer that looks like good news.
    """
    tokens = context.get("tokens")
    # `is None`, never a truth test. Zero is a measurement — a conversation
    # that has genuinely just started — and absence is the lack of one. A
    # falsy check renders the second as the first, which is the whole
    # distinction this function exists to keep.
    threshold = context.get("threshold")
    window = context.get("window")

    def _fraction(denominator: Optional[int]) -> Optional[float]:
        if tokens is None or not denominator:
            return None
        return tokens / denominator

    fraction_of_threshold = _fraction(threshold)
    return {
        "status": "unknown" if tokens is None else "reporting",
        "tokens": tokens,
        "threshold": threshold,
        "window": window,
        "fraction_of_threshold": fraction_of_threshold,
        "fraction_of_window": _fraction(window),
        # Past the line is a real state that can last hours — a rotation
        # fires on the next typed message, not on the crossing — so it is
        # reported rather than clamped. Clamping hides the conversation that
        # most needs attention behind a bar that looks merely full.
        "over_threshold": bool(
            fraction_of_threshold is not None and fraction_of_threshold > 1.0
        ),
        "observed_at": context.get("observed_at"),
        "age_seconds": context.get("age_seconds"),
    }


# --------------------------------------------------------------------------
# The shared reading
# --------------------------------------------------------------------------


class SharedSnapshot:
    """One reading, shared by every viewer, refreshed at most once per TTL.

    Two properties matter and neither is the caching. The first is
    single-flight: ten tabs opening together must be one probe of each
    machine, not ten, because on the kid boxes the difference is a dashboard
    versus a denial of service against a laptop. The second is that a failed
    refresh keeps the last reading and says it is stale — blanking the page
    over one bad poll is worse, and presenting old numbers as current is
    worse than both.
    """

    def __init__(
        self,
        fetch: Callable[[], Awaitable[dict]],
        *,
        ttl: float,
        clock: Callable[[], float] = time.time,
    ):
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._value: Optional[dict] = None
        self._fetched_at: float = 0.0
        self._fetched: bool = False
        self._error: Optional[str] = None
        self._errored_at: float = 0.0

    def _fresh(self) -> bool:
        """Whether the window is still open — after a success or a failure.

        `_fetched_at` is stamped on both, so a run of failures is throttled
        at the same rate as a run of successes rather than turning every
        poll into an upstream call.
        """
        # An explicit flag rather than a zero sentinel on `_fetched_at`: a
        # clock that legitimately reads 0.0 would otherwise make the first
        # reading look like no reading, and every caller would refetch.
        if not self._fetched:
            return False
        return (self._clock() - self._fetched_at) < self._ttl

    def _last(self) -> dict:
        if self._value is not None:
            stale = dict(self._value)
            if self._error:
                stale["stale"] = True
                stale["error"] = self._error
            return stale
        return {"observed_at": self._fetched_at, "stale": True, "error": self._error}

    async def get(self) -> dict:
        if self._fresh():
            return self._last()
        async with self._lock:
            # Checked again inside the lock: everyone who queued behind the
            # one in-flight fetch is now holding a fresh value, and running
            # their own would be the stampede the lock exists to prevent.
            if self._fresh():
                return self._last()
            now = self._clock()
            try:
                value = dict(await self._fetch())
                value["observed_at"] = now
                value["stale"] = False
                self._value = value
                self._fetched_at = now
                self._fetched = True
                self._error = None
                return value
            except Exception as exc:  # noqa: BLE001 — a reading, never a fault
                # A failure holds the window open too. Only marking success
                # means a proxy answering 401, or an unset key raising every
                # time, leaves `_fresh()` false forever — and then every poll
                # from every tab goes upstream. The failing case is precisely
                # the one where hammering is worst, so it is rate-limited on
                # the same clock as the succeeding one.
                self._fetched_at = now
                self._fetched = True
                self._error = str(exc)
                self._errored_at = now
                if self._value is not None:
                    stale = dict(self._value)
                    stale["stale"] = True
                    stale["error"] = str(exc)
                    return stale
                self._value = None
                return {"observed_at": now, "stale": True, "error": str(exc)}

    def invalidate(self) -> None:
        self._value = None
        self._fetched_at = 0.0
        self._fetched = False
        self._error = None
        self._errored_at = 0.0
