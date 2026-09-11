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
import ipaddress
import time
from typing import Any, Awaitable, Callable, Optional

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


def ordered_columns(live: list[dict]) -> list[dict]:
    """Every conversation putting words on screen, oldest first.

    There is no cap and no waiting list. A mind that is producing output is
    the whole population this page is about, so holding one back behind a
    slot limit hides the thing the page exists to show; ordering by start
    time keeps the arrangement stable across polls rather than reshuffling
    whenever a re-sort surfaces a different row first.

    A conversation marked quiet is generating but has emitted nothing for
    a while — a long tool chain, a build. It is dropped: this page is a
    wall of live prose, and a column holding a frozen paragraph under a
    label nobody reads is the thing that makes the wall untrustworthy.
    """
    producing = [row for row in live if not row.get("quiet")]
    return sorted(
        producing, key=lambda row: (row.get("started_at") or 0, row["session_id"])
    )


def name_columns(live: list[dict], registry: list[dict]) -> list[dict]:
    """A column is headed by a name, falling back to the id it was keyed on.

    A mind the broker has not registered still has a live conversation, and
    heading that column with an empty string loses the only handle anyone
    has on it. The id is ugly; blank is useless.
    """
    names = {
        mind.get("id"): mind.get("name")
        for mind in registry
        if mind.get("id") and mind.get("name")
    }
    for row in live:
        mind_id = row.get("mind_id")
        row["mind_name"] = names.get(mind_id) or mind_id or "unknown mind"
    return live


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
            "gpu": gpu_usage(reading.get("gpu")),
            "memory": figure_usage(reading.get("memory")),
            "disk": figure_usage(reading.get("disk")),
            "observed_at": reading.get("observed_at"),
            "last_seen": report.get("last_seen"),
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
    # Where the rotation point sits on a window-scaled track. Both figures
    # are tested for truth, not for None: a rotation point of zero is a
    # misconfiguration, and a mark drawn hard at the left edge claims this
    # conversation rotates immediately — the one reading worse than no mark.
    mark_fraction = threshold / window if threshold and window else None

    # Which measurement the bar is drawn against. The window is the wanted
    # answer, but it comes from a proxy catalog that is legitimately absent
    # — an unreachable proxy, or a deployment name the catalog has never
    # been told about. Falling through to the rotation point keeps a bar
    # that means something instead of hatching a conversation at 95% of
    # rotation as though nobody had measured it.
    if fraction_of_window := _fraction(window):
        bar_fraction, bar_basis = fraction_of_window, "window"
    elif fraction_of_threshold is not None:
        bar_fraction, bar_basis = fraction_of_threshold, "threshold"
    else:
        bar_fraction, bar_basis = None, None

    # Past the line however the line was found. With no rotation point
    # reported at all, overflowing the window is the only "too full" there
    # is, and a bar that can never turn red is a bar that cannot warn.
    if threshold:
        over = tokens is not None and tokens >= threshold
    else:
        over = bool(fraction_of_window and fraction_of_window > 1.0)

    return {
        "status": "unknown" if tokens is None else "reporting",
        "tokens": tokens,
        "threshold": threshold,
        "window": window,
        "fraction_of_threshold": fraction_of_threshold,
        "fraction_of_window": _fraction(window),
        "mark_fraction": mark_fraction,
        # A rotation point past the end of the window is a real reading —
        # a mind reporting a 200k window against a 300k threshold, which is
        # the dropped [1m] pin. Clamped to the edge it reads as "rotates
        # right at the end", which is reassuring and wrong, so it is
        # flagged rather than quietly squared away.
        "mark_beyond_window": bool(mark_fraction and mark_fraction > 1.0),
        "bar_fraction": bar_fraction,
        "bar_basis": bar_basis,
        "over": over,

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


# --------------------------------------------------------------------------
# Provider allowance
# --------------------------------------------------------------------------

WARN_PERCENT = 60.0
HOT_PERCENT = 85.0


def _percent(value) -> Optional[float]:
    """A spend figure as a number, or nothing.

    A window whose figure is a string, absent or not finite is dropped
    rather than drawn: the browser formats this with `toFixed`, which
    throws on anything else and takes the whole panel — and the machines
    panel rendered after it — down with one bad row.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and value not in (float("inf"), float("-inf")) else None


def pressure_band(used_percent: Optional[float]) -> Optional[str]:
    """Which band a spend figure falls in, decided once and server-side.

    The gauge in the browser reads this rather than re-deriving it from the
    number, so the two thresholds live in exactly one place and can be
    asserted on without a browser.
    """
    if used_percent is None:
        return None
    if used_percent > HOT_PERCENT:
        return "hot"
    if used_percent >= WARN_PERCENT:
        return "warn"
    return "ok"


def is_local_provider(host: Optional[str]) -> bool:
    """Whether a provider runs on this machine or this network.

    Keyed on the host rather than on the provider's name: a locally-served
    model has no subscription allowance to spend and never will, so it has
    nothing this panel could meter. Matching "ollama" by name instead would
    miss the next local runtime and would hide a hosted provider unlucky
    enough to be called something similar.
    """
    if not host:
        return False
    bare = str(host).strip().lower().split("://")[-1].split("/")[0]
    bare = bare.rsplit(":", 1)[0].strip("[]")
    if bare in {"localhost", "::1"} or bare.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(bare).is_private or ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


def annotate_providers(payload: dict) -> dict:
    """Each reporting window tagged with its band; the rest left untouched.

    A provider that has reported nothing keeps no windows at all, so the
    page has nothing it could draw as an empty gauge — an unfilled ring
    reads as headroom, which is the opposite of "nobody has measured".
    """
    providers = []
    rows = payload.get("providers")
    # A 200 carrying the wrong body is a proxy answering, not a proxy down,
    # so it must not raise out of the route and take the conversations and
    # the machines with it. Everything unrecognisable is dropped here.
    for provider in rows if isinstance(rows, list) else []:
        if not isinstance(provider, dict):
            continue
        # Nothing to meter: not shown, rather than shown as an empty
        # state. A provider with no allowance to spend is not a provider
        # whose allowance nobody has measured.
        if is_local_provider(provider.get("host")):
            continue
        row = dict(provider)
        if not row.get("reporting"):
            row["windows"] = []
            providers.append(row)
            continue
        windows = row.get("windows")
        row["windows"] = [
            {**window, "band": pressure_band(_percent(window.get("used_percent")))}
            for window in (windows if isinstance(windows, list) else [])
            if isinstance(window, dict) and _percent(window.get("used_percent")) is not None
        ]
        providers.append(row)
    return {**payload, "providers": providers}


#: A mind the broker has not heard from in this long is not a machine that
#: went down — it is one that was never brought into service, or has been
#: out of it for so long that its silence is the status quo. A box rebooted
#: overnight is back inside a day.
IN_SERVICE_SECONDS = 24 * 60 * 60


def in_service(last_seen: Optional[float], now: Optional[float] = None) -> bool:
    """Whether the broker has heard from this mind recently enough to care.

    A mind checking in minutes ago and refusing a probe right now is an
    outage worth shouting about. One silent for two months is a machine
    nobody has finished setting up, and putting it beside the first teaches
    the reader to ignore both.
    """
    if not isinstance(last_seen, (int, float)) or isinstance(last_seen, bool):
        return False
    return (time.time() if now is None else now) - last_seen <= IN_SERVICE_SECONDS


def watchable_hosts(hosts: list[dict], now: Optional[float] = None) -> dict:
    """The machines this page can actually watch, and a count of the rest.

    A mind running a build with no host route answers 404 forever. Its card
    holds no reading, will never hold one, and is not a machine fault — so
    a wall of them buries the one box that is genuinely refusing or down.
    They are counted rather than dropped silently: "seven minds have no
    host route" is a fact worth one line, and a panel that quietly omitted
    them would teach its reader to distrust it about absence.
    """
    watchable, no_route, out_of_service = [], [], []
    for host in hosts:
        status = host.get("status")
        name = host.get("hostname") or ""
        if status == "reporting":
            watchable.append(host)
        elif status == "unsupported":
            no_route.append(name)
        elif not in_service(host.get("last_seen"), now):
            out_of_service.append(name)
        else:
            watchable.append(host)
    return {
        "hosts": watchable,
        "without_route": no_route,
        "out_of_service": out_of_service,
    }


def figure_usage(figure: Optional[dict]) -> Optional[dict]:
    """A memory or disk reading as a percentage of itself, plus its band.

    Computed here rather than in the browser for the same reason the
    allowance bands are: one place holds the two thresholds, and a dial
    that changes colour at the wrong number is not something a test in
    this repo could otherwise catch.

    A figure that is not reporting, or whose total is zero, gets no
    percentage at all — a dial drawn at zero on a machine nobody measured
    reads as a machine with everything free.
    """
    if not isinstance(figure, dict) or figure.get("status") != "reporting":
        return figure
    used, total = figure.get("used_bytes"), figure.get("total_bytes")
    if not isinstance(used, (int, float)) or not isinstance(total, (int, float)):
        return figure
    if total <= 0:
        return figure
    percent = used / total * 100
    return {**figure, "usage_percent": percent, "band": pressure_band(percent)}


def gpu_usage(gpu: Optional[dict]) -> Optional[dict]:
    """Each device's utilisation banded, the probe's own states untouched.

    Absent, tool-missing and probe-failed stay exactly as they arrived:
    three different sentences about a machine, and collapsing any of them
    into a dial at zero says the card is idle when nobody looked.
    """
    if not isinstance(gpu, dict) or gpu.get("status") != "reporting":
        return gpu
    devices = []
    for device in gpu.get("devices") or []:
        if not isinstance(device, dict):
            continue
        percent = device.get("utilization_percent")
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            devices.append({**device, "band": pressure_band(float(percent))})
        else:
            devices.append(dict(device))
    return {**gpu, "devices": devices}
