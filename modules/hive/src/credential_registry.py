"""Every credential in the hive, one row per holder, with what it is doing.

`credentials_data` is the writer: it knows which shared secret lives in which
bind-mounted `.env` and how to rotate one across all of them. This is the
reader the page is built from, and it answers a different question — *who*
holds a credential, and does the thing they hold actually work.

Two kinds of credential, because the hive genuinely has two:

A **shared secret** is one value every party presents to one validator. Its
holders are the files that carry it, which is what the console can see and
write; a row per mind would be four identical rows backed by one `.env` that
no row could ever report independently. The rotate control therefore belongs
to the credential, never to a row — rotating `COMMS_BEARER_TOKEN` for one
holder and not the rest is how the hive goes down.

A **per-mind credential** is a different value per mind, stored where only
that mind can see it: its own keyring key in the containerised stack, its own
`.env` on an edge host. Those rows are one per registered mind, including
minds that have never been given one, because "not set" is the thing the
operator came here to find. The console reads and writes them through the
mind's own route, since it holds no mount into a container's home directory
and a mind on another machine has none to offer.

**Presence and working are separate facts and are reported separately.** A
holder's presence is a file read for the mounted files and a round trip for
everything else, so the listing answers from what it can see immediately and
every live check is a second call the page makes per row. A check is
remembered with the moment it was taken, and the page shows that age rather
than implying the answer is current: a credentials page that silently
re-probes GitHub once per row per load earns a secondary rate limit, and the
remedy its rows then suggest — revoke and regenerate — is the wrong one
applied to tokens that are fine.

**A service that did not answer is never rendered as a credential that was
refused.** Refused means the validator explicitly rejected this value. Every
other non-answer is its own state with its own remedy, because "revoke and
regenerate" applied to a token behind a service outage destroys a working
credential.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

import credentials_data

#: Nothing is stored where this holder reads.
NOT_SET = "not_set"
#: Stored, and the validator accepted it.
WORKING = "working"
#: Stored, and the validator explicitly rejected it. The only state whose
#: remedy is a new credential.
REFUSED = "refused"
#: Stored, and whether it works is not known — no check has been taken yet.
UNCHECKED = "unchecked"
#: The holder could not be reached at all.
UNREACHABLE = "unreachable"
#: The holder answered, but has no such route: it is running older code.
#: Distinct from unreachable, whose remedy is to go and look at the machine.
PREDATES_API = "predates_api"
#: The holder is registered but carries no address, so nothing could be asked.
UNADDRESSABLE = "unaddressable"
#: The validator did not answer, or answered something other than a verdict.
#: Never folded into refused.
UNDETERMINED = "undetermined"
#: This holder *is* the validator — there is nothing for it to authenticate
#: against, and a probe would be the console asking a service about itself.
VALIDATOR = "validator"
#: The file this holder reads could not be read.
UNREADABLE = "unreadable"
#: The holder has not been seen for long enough that it is presumed gone.
#: Reported rather than probed forever: a page whose steady state is a block
#: of unreachable rows is a page where a live mind falling into that block is
#: invisible.
RETIRED = "retired"

#: A mind not heard from in this long is reported retired rather than
#: unreachable. Broker rows are an upsert with no delete, so a decommissioned
#: machine stays registered forever.
RETIRED_AFTER_S = 7 * 24 * 60 * 60

#: How long a live check's answer is served before the page asks again.
CHECK_TTL_S = 300.0

#: The wall-clock ceiling on one probe, name resolution included. A probe
#: against a blackholed address otherwise holds a connection open for as long
#: as the kernel allows, and the console serves every other page behind it.
PROBE_TIMEOUT_S = 8.0

#: Kinds of holder. A file is one the console has mounted and can write; a
#: mind holds its own and is asked; the host is the operator's own machine.
FILE = "file"
MIND = "mind"


@dataclass(frozen=True)
class PerMindCredential:
    """A credential each mind holds its own copy of.

    `route` is the mind's own path for it, `settable` whether this console
    offers a paste box, and `names` the field in the mind's answer that
    identifies whose credential it is — the GitHub account, the bot.
    """

    key: str
    label: str
    route: str
    names: str
    note: str = ""


PER_MIND_CREDENTIALS: tuple[PerMindCredential, ...] = (
    PerMindCredential(
        key="mind_github_token",
        label="GitHub token (per mind)",
        route="/github-token",
        names="login",
        note=(
            "Each mind holds its own, stored where only that mind can see it "
            "— its own keyring key in the stack, its own .env on an edge "
            "host. Verified against GitHub by the mind before it is stored, "
            "so a refused paste leaves the working token in place."
        ),
    ),
    PerMindCredential(
        key="mind_surface_token",
        label="Telegram bot token (per mind)",
        route="/surface-token",
        names="bot_username",
        note=(
            "The token this mind's own surface polls with. Verified against "
            "the bot API by the mind before it is stored. The surface keeps "
            "the token it started on until it is restarted."
        ),
    ),
)


@dataclass(frozen=True)
class SharedProbe:
    """How to ask a validator whether it accepts a shared secret's value.

    `accepted` is the set of status codes that mean the credential was taken.
    A 404 belongs there for a route addressed by a deliberately absent id: the
    service answering "no such thing" has already authenticated the caller,
    which is the only question being asked.
    """

    url_env: str
    url_default: str
    path: str
    accepted: frozenset[int] = field(default_factory=lambda: frozenset({200, 404}))

    def url(self) -> str:
        base = (os.getenv(self.url_env) or self.url_default).rstrip("/")
        return f"{base}{self.path}"


#: The validator for each shared secret, and the file label that *is* that
#: validator. A credential's own validator has nothing to authenticate
#: against, so its row reports as much rather than being probed.
SHARED_PROBES: dict[str, SharedProbe] = {
    "comms_admin_bearer_token": SharedProbe(
        "GATEWAY_API_URL", "http://hive-comms:8424", "/sessions/live"
    ),
    "lucent_admin_bearer_token": SharedProbe(
        "LUCENT_API_URL", "http://hive-lucent:8424", "/graph/schema"
    ),
    "hive_tools_token": SharedProbe(
        "HIVE_TOOLS_URL",
        "http://hive-tools:9421",
        # An id nothing will ever have: a 404 is this service saying it
        # authenticated the caller and then found no such request, which is
        # the whole question. A route that did real work would be a probe
        # with a side effect.
        "/hitl/credential-probe-no-such-request",
    ),
}

#: Which file label validates which credential. That holder presents the
#: value to nobody — it is the thing others present it to.
VALIDATORS: dict[str, str] = {
    "comms_admin_bearer_token": "nervous_system",
    "lucent_admin_bearer_token": "nervous_system",
}

#: How a holder's own file label reads on the page. The labels in
#: `credentials_data.ENV_FILES` name files; these name parties.
HOLDER_LABELS: dict[str, str] = {
    "nervous_system": "hive-comms and hive-lucent",
    "hive_mind": "the containerised minds",
    "skippy": "skippy (edge)",
    "mordecai": "mordecai (edge)",
    "hive_tools": "hive-tools",
}


# ---------------------------------------------------------------------------
# What a check found, and when
# ---------------------------------------------------------------------------
# Keyed by (credential, holder) and shared by every open tab, so two browsers
# and a left-open dashboard cost one probe rather than three per row. The age
# travels with the answer: a status with no age beside it reads as current,
# and this one may be five minutes old by design.


@dataclass
class _Checked:
    status: str
    detail: str
    present: bool | None
    names: str
    #: Where the holder said it keeps the credential — a keyring key name in
    #: the stack, its own `.env` on an edge host. Only the holder knows, and
    #: it is the operator's whole remedy path.
    where: str
    #: Whether this holder says it can take a value at all. The holder's own
    #: answer, since only it knows whether it has somewhere to put one.
    settable: bool
    #: The last four characters of the stored value, reported by the holder.
    preview: str
    taken_at: float  # wall clock, for the operator
    monotonic_at: float  # for the age, which a clock change must not move


_checks: dict[tuple[str, str], _Checked] = {}


def remember(
    credential: str,
    holder: str,
    *,
    status: str,
    detail: str = "",
    present: bool | None = None,
    names: str = "",
    where: str = "",
    settable: bool = True,
    preview: str = "",
) -> _Checked:
    entry = _Checked(
        status=status,
        detail=detail,
        present=present,
        names=names,
        where=where,
        settable=settable,
        preview=preview,
        taken_at=time.time(),
        monotonic_at=time.monotonic(),
    )
    _checks[(credential, holder)] = entry
    return entry


def recall(credential: str, holder: str) -> _Checked | None:
    return _checks.get((credential, holder))


def forget_all() -> None:
    _checks.clear()


def _age(entry: _Checked) -> float:
    return max(0.0, time.monotonic() - entry.monotonic_at)


def _checked_fields(entry: _Checked | None) -> dict[str, Any]:
    if entry is None:
        return {"checked_at": None, "age_seconds": None, "stale": True}
    age = _age(entry)
    return {
        "checked_at": entry.taken_at,
        "age_seconds": round(age, 1),
        "stale": age > CHECK_TTL_S,
    }


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


def _is_retired(mind: dict) -> bool:
    last_seen = mind.get("last_seen")
    try:
        seen = float(last_seen)
    except (TypeError, ValueError):
        # A mind that has never reported is not retired — it may have
        # registered a minute ago and not heartbeat yet.
        return False
    return (time.time() - seen) > RETIRED_AFTER_S


def _shared_rows(group: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per file that carries this shared secret."""
    rows: list[dict[str, Any]] = []
    validator = VALIDATORS.get(group["key"])
    for location in group["locations"]:
        holder = location["file"]
        entry = recall(group["key"], holder)
        if location.get("unreadable"):
            status, detail = UNREADABLE, f"{location['path']} could not be read"
        elif not location.get("present"):
            status, detail = NOT_SET, ""
        elif holder == validator:
            status, detail = VALIDATOR, "this holder is what the others authenticate to"
        elif entry is not None:
            status, detail = entry.status, entry.detail
        else:
            status, detail = UNCHECKED, ""
        rows.append(
            {
                "credential": group["key"],
                "credential_label": group["label"],
                "holder": holder,
                "holder_label": HOLDER_LABELS.get(holder, holder),
                "holder_kind": FILE,
                "where": f"{location['path']} ({location['env_key']})",
                "present": bool(location.get("present")),
                "preview": location.get("preview"),
                "status": status,
                "detail": detail,
                "names": "",
                "settable": False,
                "checkable": status not in (NOT_SET, VALIDATOR, UNREADABLE),
                **_checked_fields(entry),
            }
        )
    return rows


def _per_mind_rows(
    credential: PerMindCredential, minds: list[dict]
) -> list[dict[str, Any]]:
    """One row per registered mind, whether or not it has ever been given one.

    Presence here is the mind's own answer, so an unchecked row reports
    unchecked — never "not set", which is the misreport the states exist to
    prevent and which the operator would act on by pasting a token that was
    never missing.
    """
    rows: list[dict[str, Any]] = []
    for mind in minds:
        name = str(mind.get("name") or "").strip()
        if not name:
            continue
        entry = recall(credential.key, name)
        retired = _is_retired(mind)
        if retired:
            status, detail = RETIRED, "not seen in over a week"
        elif not str(mind.get("gateway_url") or "").strip():
            status, detail = UNADDRESSABLE, "registered with no gateway URL"
        elif entry is not None:
            status, detail = entry.status, entry.detail
        else:
            status, detail = UNCHECKED, ""
        rows.append(
            {
                "credential": credential.key,
                "credential_label": credential.label,
                "holder": name,
                "holder_label": name,
                "holder_kind": MIND,
                "where": entry.where if entry is not None else "",
                "present": entry.present if entry is not None else None,
                "preview": entry.preview if entry is not None else None,
                "status": status,
                "detail": detail,
                "names": entry.names if entry is not None else "",
                # A mind that answered and said it cannot take one is not
                # offered a paste box: offering one that always refuses is
                # worse than offering none.
                "settable": status in (WORKING, REFUSED, NOT_SET, UNCHECKED, UNDETERMINED)
                and (entry is None or entry.settable),
                "checkable": not retired and status != UNADDRESSABLE,
                **_checked_fields(entry),
            }
        )
    return rows


def build_rows(minds: list[dict]) -> list[dict[str, Any]]:
    """Every row the page shows, in a stable order.

    Stable because the table is sortable: a server-side order that varied
    between loads would reshuffle rows under a sort the operator just chose.
    Grouped by credential and then by holder, which is also the order the
    rotate control belongs to — it sits on the credential, and a sort or a
    filter moves rows within one, never out of it.
    """
    rows: list[dict[str, Any]] = []
    for group in credentials_data.list_credentials():
        rows.extend(_shared_rows(group))
    for credential in PER_MIND_CREDENTIALS:
        rows.extend(
            sorted(_per_mind_rows(credential, minds), key=lambda r: r["holder"])
        )
    prune({(row["credential"], row["holder"]) for row in rows})
    return rows


def credential_summaries(minds: list[dict]) -> list[dict[str, Any]]:
    """The credentials themselves — the level the rotate control lives at."""
    summaries: list[dict[str, Any]] = []
    for group in credentials_data.list_credentials():
        summaries.append(
            {
                "key": group["key"],
                "label": group["label"],
                "kind": "shared",
                "rotatable": group["rotatable"],
                "settable": group.get("settable", False),
                "note": group.get("note"),
                "manual_reason": group.get("manual_reason"),
                "restart_targets": group["restart_targets"],
                "in_sync": group["in_sync"],
            }
        )
    for credential in PER_MIND_CREDENTIALS:
        summaries.append(
            {
                "key": credential.key,
                "label": credential.label,
                "kind": "per_mind",
                # Per-mind values are not one secret to fan out, so there is
                # nothing a single rotation here could mean.
                "rotatable": False,
                "settable": True,
                "note": credential.note,
                "manual_reason": None,
                "restart_targets": [],
                "in_sync": True,
            }
        )
    return summaries


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
# One row at a time, on a route the page calls per row. Not on the listing:
# the listing is what the operator sees first and must not wait on a holder
# that is down, and a page that probed every row on every load would spend a
# GitHub rate limit on tabs nobody is looking at.
#
# Every probe is `httpx` on the event loop's own IO rather than `urllib` in
# a thread, under one wall-clock deadline that covers name resolution:
# `urlopen`'s timeout does not, so a blackholed address holds a worker for
# as long as the resolver allows.


def _shared_value(credential: str, holder: str) -> str | None:
    """The value this holder actually loads, read from its own file.

    The console can do this for the mounted files and only for those, which
    is why a live check exists for shared secrets at all: the check has to
    present the holder's own copy, and a copy the console cannot read is a
    copy it cannot test.
    """
    group = next(
        (g for g in credentials_data.CREDENTIAL_GROUPS if g["key"] == credential), None
    )
    if group is None:
        return None
    for file_label, env_key in group["locations"]:
        if file_label != holder:
            continue
        try:
            raw = credentials_data._read_env_value(
                credentials_data.ENV_FILES[file_label], env_key
            )
        except credentials_data.Unreadable as exc:
            raise Unreadable(str(exc)) from exc
        if raw is None:
            return None
        # Stripped exactly as `python-dotenv`, docker compose and the mind's
        # own reader strip it. A quoted value is ordinary and legal in a
        # `.env`; probing the raw text sends `Bearer "abc"`, gets a 401, and
        # renders Refused — the one status whose remedy is to revoke and
        # regenerate — over a credential that works everywhere.
        return raw.strip().strip("'\"")
    return None


class Unreadable(Exception):
    """The file this holder reads could not be read."""


async def check_host_github_pat(holder: str) -> _Checked:
    """Ask GitHub about the token in the operator's own files.

    The one credential on the page most likely to have been revoked, and the
    only one with no service inside the hive to present it to. The old tab
    verified it against `api.github.com` on every save; leaving it as "could
    not tell" forever would have made the page quieter and less true.
    """
    rows, _in_sync = credentials_data._github_pat_locations()
    row = next((r for r in rows if r["file"] == holder), None)
    if row is None:
        return remember("github_pat", holder, status=UNDETERMINED, detail="no such file")
    if row.get("unreadable"):
        return remember(
            "github_pat", holder, status=UNREADABLE,
            detail=f"{row['path']} could not be read",
        )
    if not row.get("present"):
        return remember("github_pat", holder, status=NOT_SET, present=False)
    token = _host_github_token(holder)
    if not token:
        return remember("github_pat", holder, status=NOT_SET, present=False)
    try:
        login = await asyncio.to_thread(credentials_data._verify_github_pat, token)
    except ValueError as exc:
        # `_verify_github_pat` raises the same error for a 401 and for a
        # GitHub it could not reach, so only the 401 may be read as refused.
        message = str(exc)
        if "(401)" in message:
            return remember(
                "github_pat", holder, status=REFUSED, present=True,
                detail="GitHub rejected this token",
                preview=row.get("preview") or "",
            )
        return remember(
            "github_pat", holder, status=UNDETERMINED, present=True, detail=message,
            preview=row.get("preview") or "",
        )
    return remember(
        "github_pat", holder, status=WORKING, present=True, names=login,
        preview=row.get("preview") or "",
    )


def _host_github_token(holder: str) -> str:
    """The operator's own GitHub token as the named file carries it."""
    try:
        if holder == "git_credentials":
            match = credentials_data._GIT_CREDENTIALS_GITHUB_RE.search(
                credentials_data._read_text(credentials_data.GIT_CREDENTIALS_PATH)
            )
            return match.group("token") or "" if match else ""
        tokens = credentials_data._github_oauth_tokens(
            credentials_data._read_text(credentials_data.GH_HOSTS_PATH)
        )
        return tokens[0] if tokens else ""
    except (credentials_data.Unreadable, OSError):
        return ""


async def check_shared(credential: str, holder: str) -> _Checked:
    """Present this holder's own copy to the service that validates it."""
    if credential == "github_pat":
        return await check_host_github_pat(holder)
    probe = SHARED_PROBES.get(credential)
    if probe is None:
        return remember(
            credential, holder, status=UNDETERMINED,
            detail="nothing in the hive validates this credential over HTTP",
        )
    if VALIDATORS.get(credential) == holder:
        return remember(
            credential, holder, status=VALIDATOR,
            detail="this holder is what the others authenticate to",
        )
    try:
        value = _shared_value(credential, holder)
    except Unreadable as exc:
        # Not "not set": the remedy offered for an absent value is to write
        # one, over a credential that is present and fine.
        return remember(
            credential, holder, status=UNREADABLE, detail=f"could not be read: {exc}",
        )
    if not value:
        return remember(credential, holder, status=NOT_SET, present=False)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(
                probe.url(), headers={"Authorization": f"Bearer {value}"}
            )
    except (ValueError, UnicodeError) as exc:
        # A stray carriage return from a CRLF `.env`, or a non-ASCII
        # character: the header cannot be built. Neither `httpx.HTTPError`,
        # so unguarded this escaped and 500'd the check route.
        return remember(
            credential, holder, status=UNDETERMINED, present=True,
            detail=f"this value cannot be sent as a header: {type(exc).__name__}",
        )
    except httpx.HTTPError as exc:
        # The service did not answer. Never "refused": the remedy for a
        # refusal is a new credential, and applying it to a token behind an
        # outage destroys a working one.
        return remember(
            credential, holder, status=UNDETERMINED, present=True,
            detail=f"could not reach the service: {type(exc).__name__}",
        )
    if response.status_code in probe.accepted:
        return remember(credential, holder, status=WORKING, present=True)
    if response.status_code in (401, 403):
        return remember(
            credential, holder, status=REFUSED, present=True,
            detail=f"the service rejected this value ({response.status_code})",
        )
    return remember(
        credential, holder, status=UNDETERMINED, present=True,
        detail=f"the service answered {response.status_code}",
    )


def _per_mind(credential: str) -> PerMindCredential | None:
    return next((c for c in PER_MIND_CREDENTIALS if c.key == credential), None)


async def check_mind(credential: str, holder: str, gateway_url: str) -> _Checked:
    """Ask a mind about its own credential.

    The mind is the only party that can answer: it holds the keyring or the
    `.env`, and it is the one that verifies against the real service. Its
    three answers — nothing stored, stored and accepted, stored and refused —
    come back as they are, and the ways it can fail to answer at all stay
    distinct from every one of them.
    """
    spec = _per_mind(credential)
    if spec is None:
        return remember(credential, holder, status=UNDETERMINED, detail="no such credential")
    base = (gateway_url or "").strip().rstrip("/")
    if not base:
        return remember(
            credential, holder, status=UNADDRESSABLE,
            detail="registered with no gateway URL",
        )
    try:
        from mind_runtime import admin_headers

        headers = admin_headers()
    except Exception as exc:  # noqa: BLE001
        # No admin token on this console. That is this console's problem, not
        # the holder's, and must not read as a mind that is down.
        return remember(
            credential, holder, status=UNDETERMINED,
            detail=f"this console cannot authenticate to minds: {exc}",
        )
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(f"{base}{spec.route}", headers=headers)
    except httpx.HTTPError as exc:
        return remember(
            credential, holder, status=UNREACHABLE,
            detail=f"the mind could not be reached: {type(exc).__name__}",
        )
    if response.status_code == 404:
        return remember(
            credential, holder, status=PREDATES_API,
            detail="this mind is running a version without the route",
        )
    if response.status_code in (401, 403):
        return remember(
            credential, holder, status=UNDETERMINED,
            detail="the mind refused this console's credential",
        )
    if response.status_code != 200:
        return remember(
            credential, holder, status=UNDETERMINED,
            detail=f"the mind answered {response.status_code}",
        )
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return remember(
            credential, holder, status=UNDETERMINED,
            detail="the mind answered something other than a credential state",
        )
    where = str(body.get("where") or "")
    # Whether this mind can take one at all is the mind's own answer, not a
    # guess from its status: a container naming no keyring key of its own
    # refuses every write, and a page offering it a paste box offers one that
    # can only ever fail.
    settable = bool(body.get("settable", True))
    preview = str(body.get("preview") or "")
    if not body.get("stored"):
        return remember(
            credential, holder, status=NOT_SET, present=False, where=where,
            settable=settable, detail=str(body.get("detail") or ""),
        )
    accepted = body.get("accepted")
    if accepted is True:
        return remember(
            credential, holder, status=WORKING, present=True, where=where,
            names=str(body.get(spec.names) or ""), settable=settable, preview=preview,
        )
    if accepted is False:
        return remember(
            credential, holder, status=REFUSED, present=True, where=where,
            settable=settable, preview=preview,
            detail=str(body.get("detail") or "the service rejected the stored value"),
        )
    return remember(
        credential, holder, status=UNDETERMINED, present=True, where=where,
        settable=settable, preview=preview,
        detail=str(body.get("detail") or "the mind could not get a verdict on it"),
    )


def is_declared(credential: str) -> bool:
    """Whether this is a credential the hive actually has.

    Checked before anything is remembered: the key arrives on a URL, and an
    entry per invented name is a dictionary that grows for as long as the
    console is up.
    """
    if _per_mind(credential) is not None:
        return True
    return any(g["key"] == credential for g in credentials_data.CREDENTIAL_GROUPS)


def prune(known: set[tuple[str, str]]) -> None:
    """Drop remembered answers about holders the page no longer shows.

    A machine decommissioned and a different one registered under the same
    name would otherwise inherit the old verdict and the old account name,
    and a credential dropped from the declaration would keep its entries for
    as long as the process lives.
    """
    for key in [k for k in _checks if k not in known]:
        del _checks[key]


async def check(credential: str, holder: str, minds: list[dict]) -> dict[str, Any]:
    """One row's live check, whichever kind of credential it is."""
    spec = _per_mind(credential)
    if spec is None:
        entry = await check_shared(credential, holder)
    else:
        mind = next(
            (m for m in minds if str(m.get("name") or "").lower() == holder.lower()),
            None,
        )
        if mind is None:
            entry = remember(
                credential, holder, status=UNDETERMINED,
                detail="this holder is not in the registry",
            )
        elif _is_retired(mind):
            entry = remember(
                credential, holder, status=RETIRED, detail="not seen in over a week",
            )
        else:
            entry = await check_mind(
                credential, holder, str(mind.get("gateway_url") or "")
            )
    return {
        "credential": credential,
        "holder": holder,
        "status": entry.status,
        "detail": entry.detail,
        "present": entry.present,
        "names": entry.names,
        "where": entry.where,
        "preview": entry.preview,
        "settable": entry.settable,
        **_checked_fields(entry),
    }
