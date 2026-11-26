"""One holder, one value, written only where that holder reads it.

The page this backs used to treat `COMMS_BEARER_TOKEN` and
`LUCENT_BEARER_TOKEN` as shared secrets and rotated them by writing one new
value into five `.env` files. Both services now key credentials per holder
and refuse a value another holder already has, so that control would have
refused four of five holders and left a state nothing could mint out of.

What replaced it is this: the services are asked who holds a credential
(`GET /credentials`), and for each holder the console works out where that
holder's value lives by looking for it. The per-holder variable
(`COMMS_TOKEN_BILBY_BOT`) is the convention the shared `hive_mind/.env`
uses, because one file serves many holders there; a holder with an `.env` of
its own reads the plain `COMMS_BEARER_TOKEN` from it. A holder whose value is
in neither — a mind on another machine, this console's own file, the browser
terminal's — has no location this container can write, and gets no rotate
control rather than a button that mints a value nobody will ever hold.

## Why the order is read, mint, write, verify

Minting is destructive: both services replace the holder's digest in place and
neither ever returns a value, so the moment a mint succeeds the holder's
previous value stops being accepted and the new one exists only in this
process. A write that then fails — the variable not in the file yet, a
read-only mount, a full disk — leaves that holder dead with no value anywhere
and nothing on any disk to recover it from.

So the pre-rotation value is read first, and a failed or unverified write
re-mints it. That restore is possible because the clash check both services
run excludes the holder being written, so handing a holder back its own value
is accepted rather than refused as a duplicate.

The whole sequence holds `_ROTATION_LOCK`, an `asyncio.Lock`. Two tabs
rotating one holder would otherwise interleave two mints and two writes, and
the file and the service's table disagree permanently from the loser's next
restart — both reporting success. `credentials_data._WRITE_LOCK` cannot do
this job: it is a `threading.RLock`, every request on this console runs on one
event-loop thread, and a reentrant lock belongs to the *thread* — so the second
coroutine acquires it during the first one's `await` and walks straight
through. It is still taken around the write itself, where the other writer is
the GitHub-PAT path running in a threadpool.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

import credentials_data
import key_journal
import mind_key_files
from credentials_data import ENV_FILES, Unreadable

#: How long to wait on a service's own credential route. Longer than the
#: status probe's eight seconds: a mint is a write, and giving up on one that
#: has already landed is the state this module exists to avoid.
MINT_TIMEOUT_S = 20.0

#: One rotation at a time, across every holder. Per-holder would be enough for
#: the digest, but two holders commonly share one `.env` — every containerised
#: mind reads `hive_mind/.env` — and that file is read-modify-written whole.
_ROTATION_LOCK = asyncio.Lock()


#: What each service answers a holder's own value on. A 404 counts as
#: accepted for the same reason it does on the shared probes: the service
#: answering "no such thing" has already authenticated the caller, which is
#: the only question being asked.
PROBE_PATHS: dict[str, str] = {
    "comms": "/broker/minds",
    "lucent": "/graph/schema",
    "proxy": "/v1/models",
    # An id nothing will ever have: the tools answer 404 once they have
    # authenticated the caller, and a mind's key is refused nowhere else.
    "tools": "/hitl/credential-probe-no-such-request",
}
PROBE_ACCEPTED = frozenset({200, 404})

#: How long to wait on a status probe. Short, because the page asks per row
#: and the operator is looking at it.
PROBE_TIMEOUT_S = 8.0


#: The env file a mind created here has its three values written into, by
#: its label in `HIVE_ENV_FILES`. This workstation keeps the containerised
#: minds' variables in a file of their own; an installed hive has one `.env`
#: for everything and names its label in `HIVE_MIND_ENV_LABEL`.
MIND_ENV_LABEL = os.environ.get("HIVE_MIND_ENV_LABEL", "").strip() or "hive_mind"


@dataclass(frozen=True)
class Service:
    """A service that keys credentials per holder, and how to reach it.

    `prefix` is the stem of the per-holder variable name in a shared `.env`;
    `own_files` names the holders that read a plain `<PREFIX>_BEARER_TOKEN`
    out of a file of their own. `wants_mind_id` is lucent, where a holder's
    rows are keyed by provenance and a mind's is its uuid — omit it and the
    mind authenticates and is then refused its own memory.
    """

    key: str
    label: str
    url_env: str
    url_default: str
    admin_env: str
    prefix: str
    own_files: dict[str, str]
    wants_mind_id: bool
    #: A mounted env file the admin credential is read from, at each call,
    #: when the process environment does not carry it - so issuing this
    #: console that credential needs no restart of the console.
    admin_file: str | None = None

    def configured(self) -> bool:
        """Whether this console has been given this service at all.

        Only the tools can be absent: a hive installed without them names no
        address and holds no administrator credential for them, and a mind
        created there has nothing to be registered with. A service that is
        named but cannot be written is a failure, never a skip."""
        return bool(os.getenv(self.url_env, "").strip() or self.admin_token())

    def url(self) -> str:
        base = (os.getenv(self.url_env) or self.url_default).rstrip("/")
        return base

    def admin_token(self) -> str:
        value = os.getenv(self.admin_env) or ""
        if value or not self.admin_file or self.admin_file not in ENV_FILES:
            return value
        try:
            return credentials_data._read_env_value(
                ENV_FILES[self.admin_file], self.admin_env) or ""
        except Unreadable:
            return ""


SERVICES: dict[str, Service] = {
    "comms": Service(
        key="comms",
        label="the gateway",
        url_env="GATEWAY_API_URL",
        url_default="http://hive-comms:8424",
        admin_env="COMMS_ADMIN_BEARER_TOKEN",
        prefix="COMMS",
        own_files={
            "skippy": "skippy",
            "mordecai": "mordecai",
            "hive-tools": "hive_tools",
        },
        wants_mind_id=False,
    ),
    "lucent": Service(
        key="lucent",
        label="the memory store",
        url_env="LUCENT_API_URL",
        url_default="http://hive-lucent:8424",
        admin_env="LUCENT_ADMIN_BEARER_TOKEN",
        prefix="LUCENT",
        own_files={
            "skippy": "skippy",
            "mordecai": "mordecai",
            "hive-comms": "nervous_system",
        },
        wants_mind_id=True,
    ),
    # One key per mind, so each mind's usage is its own (story 2.1.9,
    # DECIDED 2026-11-15 by Daniel). The admin credential is the key of a
    # proxy client that exists only to mint for others - named in the
    # proxy's `CREDENTIAL_ADMIN_CLIENTS` - and not the console's own proxy
    # key, which it sends on ordinary calls. No holder reads a plain file of
    # its own here; a key in a mind's runtime.yaml is one this console cannot
    # write, and the page offers it revoke, not rotate.
    "proxy": Service(
        key="proxy",
        label="the inference proxy",
        url_env="INFERENCE_PROXY_URL",
        url_default="http://host.docker.internal:8899",
        admin_env="PROXY_CREDENTIAL_ADMIN_KEY",
        prefix="PROXY",
        own_files={},
        wants_mind_id=False,
        admin_file=MIND_ENV_LABEL,
    ),
    # The tools know a mind by the same key: `PUT /credentials/{holder}`
    # registers the pasted value and the holder name is the mind's name, so a
    # gated call is recorded under, and its result sent to, that mind. The
    # administrator credential is the console's alone, read from the tools'
    # own env file when the process environment lacks it.
    "tools": Service(
        key="tools",
        label="the tools",
        url_env="HIVE_TOOLS_URL",
        url_default="http://hive-tools:9421",
        admin_env="TOOLS_ADMIN_BEARER_TOKEN",
        prefix="TOOLS",
        own_files={},
        wants_mind_id=False,
        admin_file="hive_tools",
    ),
}

#: Holder names the tools never register a value under (the approver, the
#: internal replay secret, the shared service token's holder). Checked here
#: before any service is written, so a refused name leaves nothing behind on
#: the gateway or the memory store; the tools refuse them again on their own.
TOOLS_RESERVED_HOLDERS = frozenset({"hitl-approver", "internal", "skippy"})

#: What has to be restarted for a value written into each file to be read.
#: Keyed by file rather than by holder, which is the honest direction: a
#: process reads one file, and whoever reads that file is who is still
#: holding the old value.
RESTART_BY_FILE: dict[str, str] = {
    # Only the per-service lines a mind made before the one key still has. A
    # mind on its one key reads a key file at each request and needs none.
    "hive_mind": "the containerised minds still on per-service values (docker compose up -d)",
    "nervous_system": "hive-comms",
    "skippy": "skippy.service",
    "mordecai": "mordecai.service",
    "hive_tools": "the hive-tools container",
}


#: The label of a mind's own key file (not an env file): `env_key` then holds
#: the mind's name. Never in `ENV_FILES`; see `mind_key_files`.
KEY_FILE_LABEL = "key_file"


@dataclass(frozen=True)
class Location:
    """Where one holder's value for one service lives on this host."""

    file: str
    env_key: str

    @property
    def is_key_file(self) -> bool:
        return self.file == KEY_FILE_LABEL

    @property
    def path(self) -> Path:
        if self.is_key_file:
            return mind_key_files.path(self.env_key)
        return ENV_FILES[self.file]

    @property
    def restart_target(self) -> str | None:
        return RESTART_BY_FILE.get(self.file)

    @property
    def is_one_key(self) -> bool:
        """This place holds a mind's one key (its file, or a line for it)."""
        return self.is_key_file or is_one_key_variable(self.env_key)


def key_file_location(holder: str) -> Location:
    return Location(KEY_FILE_LABEL, holder.strip().lower())


def variable_name(service: Service, holder: str) -> str:
    """The per-holder variable a shared `.env` names this holder's value by.

    `bilby-bot` is `COMMS_TOKEN_BILBY_BOT`. The hyphen has to go: a `.env`
    key with one in it is not a shell identifier and compose will not
    interpolate it.
    """
    stem = holder.strip().lower().replace("-", "_").upper()
    return f"{service.prefix}_TOKEN_{stem}"


#: A mind created now holds one key: one line in the shared env file, which its
#: compose names as `MIND_KEY` inside the container (core/mind_key.py). A mind
#: made before still has its per-service lines, and keeps reading them until
#: its key is moved; both are looked for, the one key first.
ONE_KEY_PREFIX = "MIND_KEY_"


NO_KEY_ROOT_MESSAGE = (
    "this console was given no folder to keep mind keys in (HIVE_KEY_ROOT), "
    "so a new mind's key has nowhere to go")

ONE_KEY_REFUSAL = (
    "This mind holds one key for every service, so it cannot be replaced "
    "here one service at a time or by saving the proxy key alone. Paste the "
    "new key on the mind's own page, which gives every service the same one")


def one_key_variable(holder: str) -> str:
    """The variable a shared `.env` holds this mind's one key under."""
    return ONE_KEY_PREFIX + holder.strip().lower().replace("-", "_").upper()


#: What an edge mind's own env file holds its one key under (it holds one
#: mind's values only, so the name carries no mind).
ONE_KEY_PLAIN = "MIND_KEY"


def is_one_key_variable(variable: str) -> bool:
    return variable == ONE_KEY_PLAIN or variable.startswith(ONE_KEY_PREFIX)


def own_file_label(holder: str) -> str | None:
    """The env file this mind has to itself (an edge mind on this host), if any."""
    label = SERVICES["comms"].own_files.get(holder.strip().lower())
    return label if label in ENV_FILES and label != "hive_tools" else None


def _write_stored(location: Location, value: str, backup: bool = True) -> dict[str, Any]:
    """Write one stored value where it is kept, reported like
    `credentials_data._write_location`: never raises. A key file is written
    atomically (`mind_key_files.write`); a line is rewritten in its env file."""
    if not location.is_key_file:
        extra = {} if backup else {"backup": False}
        return credentials_data._write_location(
            location.path, location.env_key, value, location.file, **extra)
    row: dict[str, Any] = {"file": location.file, "path": str(location.path),
                           "env_key": location.env_key, "written": False, "reason": None}
    try:
        mind_key_files.write(location.env_key, value)
    except mind_key_files.KeyFileError as exc:
        row["reason"] = str(exc)
        return row
    row["written"] = True
    return row


def _loaded_value(location: Location) -> str | None:
    """The value the holder's own process loads, not the file's spelling of it.

    Quoting is ordinary and legal in a `.env`, and a CRLF file leaves a
    carriage return on the end of every line. Re-minting the raw text hands
    the holder a value it will never present, under a message saying its
    previous one was reinstated.
    """
    if location.is_key_file:
        try:
            return mind_key_files.read(location.env_key)
        except mind_key_files.KeyFileError as exc:
            raise Unreadable(str(exc)) from None
    raw = credentials_data._read_env_value(location.path, location.env_key)
    if raw is None:
        return None
    value = raw.strip().strip("\r")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


class Ambiguous(Exception):
    """More than one mounted file names this holder's value.

    Nothing says the holder reads the one declared first, and writing the
    wrong file reports a working row, a restart target pointed at the wrong
    machine, and a process that dies at its next start holding a value the
    service stopped accepting.
    """


def locate(service: Service, holder: str, *, old_only: bool = False) -> Location | None:
    """Where this holder reads its value, or None if nowhere this console can
    write.

    Found by looking, not by a table of holders: the per-holder variable is
    searched for in every file mounted here, and only then does the small map
    of holders-with-their-own-file get consulted. A holder nobody here holds a
    value for is the answer this returns None for, and that is what takes its
    rotate control away — so a mind on another machine and a local directory
    this container was never given are one case, which is what they are.
    """
    own = service.own_files.get(holder.strip().lower())
    one = one_key_variable(holder)
    held: list[Location] = []
    own_label = own_file_label(holder)
    if not old_only and mind_key_files.exists(holder):
        # Its key file is the key; any line left in an env file is a leftover
        # the retirement removes, and never wins.
        return key_file_location(holder)
    if (not old_only and own_label
            and credentials_data._read_env_value(ENV_FILES[own_label], ONE_KEY_PLAIN) is not None):
        held.append(Location(own_label, ONE_KEY_PLAIN))
    for label in ENV_FILES if not old_only else ():
        # A mind that holds one key reads that, whatever the service: the
        # per-service lines are what a mind not yet moved still has.
        try:
            if credentials_data._read_env_value(ENV_FILES[label], one) is not None:
                held.append(Location(label, one))
        except Unreadable as exc:
            raise Unreadable(str(exc)) from exc
    if len(held) > 1:
        raise Ambiguous(
            f"{', '.join(location.file for location in held)} each name "
            f"{one}, so which one {holder!r} reads cannot be told from here"
        )
    if held:
        return held[0]
    key = variable_name(service, holder)
    found: list[Location] = []
    for label in ENV_FILES:
        try:
            if credentials_data._read_env_value(ENV_FILES[label], key) is not None:
                found.append(Location(label, key))
        except Unreadable as exc:
            # Not "this holder reads nowhere": the file is there and could not
            # be read, which has a different remedy and must not quietly turn
            # this holder's one control into the other one.
            raise Unreadable(str(exc)) from exc
    # A holder with a file of its own is read from that file, whatever else
    # mentions its name.
    if own is not None and own in ENV_FILES:
        own_key = f"{service.prefix}_BEARER_TOKEN"
        if credentials_data._read_env_value(ENV_FILES[own], own_key) is not None:
            return Location(own, own_key)
    if len(found) > 1:
        raise Ambiguous(
            f"{', '.join(location.file for location in found)} each name "
            f"{key}, so which one {holder!r} reads cannot be told from here"
        )
    return found[0] if found else None


class MintRefused(Exception):
    """The service would not take the value. Nothing has changed."""


class MintUncertain(Exception):
    """The service never answered, so whether the mint landed is unknown.

    Kept apart from both its neighbours on purpose. A refusal means the holder
    still has its old value and the operator may simply try again; a
    `Stranded` means the new value exists and the file does not have it. A
    read timeout on a `PUT` is neither — it is most often a request that
    landed — and reporting it as either sends the operator to the wrong
    remedy. Nothing is written in this case, so the file still holds the
    previous value; whether the service still accepts it is what the row's own
    check answers.
    """


class Stranded(Exception):
    """The mint landed and the write did not, and the old value is back.

    Carries whether the restore itself succeeded, because the two states have
    nothing in common: one is a rotation that did not happen, the other is a
    holder with no working value anywhere and a copy of its last one in
    `credentials_data.BACKUP_DIR`.
    """

    def __init__(self, message: str, restored: bool):
        super().__init__(message)
        self.restored = restored


async def _request(
    service: Service,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = MINT_TIMEOUT_S,
) -> httpx.Response:
    admin = service.admin_token()
    if not admin:
        raise MintRefused(
            f"this console holds no {service.admin_env}, so it cannot mint "
            f"or revoke on {service.label}"
        )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(
                method,
                f"{await _service_url(service)}{path}",
                headers={"Authorization": f"Bearer {admin}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        # Unguarded this escapes every caller and the route turns it into a
        # detail-less 500 — on a write, with no restore and no log line.
        # A connection never made sent nothing, so nothing can have landed.
        if method == "PUT" and not isinstance(exc, httpx.ConnectError):
            raise MintUncertain(
                f"{service.label} did not answer the mint for "
                f"{path.rsplit('/', 1)[-1]!r} ({type(exc).__name__}), so "
                "whether it landed is unknown. Nothing was written — check "
                "the row before trying again."
            ) from exc
        raise MintRefused(
            f"could not reach {service.label} ({type(exc).__name__})"
        ) from exc


async def list_holders(service: Service) -> list[dict[str, Any]]:
    """Who this service says holds a credential.

    The page is built from this rather than from a list written into the
    console: the hive has twenty-one gateway holders and nine memory-store
    ones, they are not the same set, and a console-side copy is a list that
    goes stale silently — which is how a holder gets missed and is refused the
    moment the service comes up.
    """
    # The listing is the whole content of the page. A mint may wait twenty
    # seconds; a read may not.
    response = await _request(service, "GET", "/credentials", timeout=PROBE_TIMEOUT_S)
    if response.status_code != 200:
        raise MintRefused(
            f"{service.label} answered {response.status_code} to a holder listing"
        )
    rows = response.json()
    return rows if isinstance(rows, list) else []


async def _put(
    service: Service, holder: str, username: str, token: str, role: str, mind_id: str | None
) -> None:
    body: dict[str, Any] = {"username": username, "token": token, "role": role}
    if service.wants_mind_id and mind_id:
        body["mind_id"] = mind_id
    response = await _request(service, "PUT", f"/credentials/{holder}", body)
    # Not merely "under 400". A 3xx is the shape a reverse proxy or an
    # http-to-https hop gives, httpx does not follow one on a PUT, and taking
    # it for a mint writes the file a value the service never saw.
    if response.status_code not in (200, 201):
        # Only the service's own `error` sentence. Its whole body can echo
        # the request, and the request carries the pasted key.
        detail = ""
        try:
            error = response.json().get("error")
            detail = error if isinstance(error, str) else ""
        except (ValueError, AttributeError):
            detail = ""
        raise MintRefused(
            f"{service.label} refused the mint for {holder!r} "
            f"({response.status_code}): {detail}"
        )


async def mint(
    service: Service,
    holder: str,
    username: str,
    *,
    role: str,
    mind_id: str | None = None,
    location: Location | None = None,
    value: str | None = None,
) -> dict[str, Any]:
    """Give one holder a value and write it where that holder reads it.

    `value` is a value the person supplied, used byte for byte; left out, one
    is generated. A mind's creation always supplies one: the hive does not
    invent a mind's key.

    `role` and `mind_id` are passed explicitly, never left to be inferred. A
    holder minted before its broker row exists is typed `person` by comms,
    which refuses every machinery route it will ever call — its turn ledger,
    its context report, its rotation — and lucent keys its memory under its
    name while every hook sends its uuid.
    """
    if location is None:
        location = locate(service, holder)
    await _require_managed_proxy(service, holder)
    if value is None and location is not None and location.is_one_key:
        # One line is every service's value for this mind. A fresh value
        # written to it and sent to one service leaves the others holding a
        # key nothing now matches. Replacing a mind's key is done from the
        # mind's own page, which gives every service the same new one.
        raise MintRefused(ONE_KEY_REFUSAL)
    new_value = value if value is not None else secrets.token_urlsafe(32)

    async with _ROTATION_LOCK:
        if location is None:
            # Minting with nowhere to write it is how a holder ends up dead:
            # the service stops accepting what the holder still presents.
            raise MintRefused(
                f"no file this console can write holds a "
                f"{service.label} value for {holder!r}"
            )
        try:
            previous = _loaded_value(location)
        except Unreadable:
            raise MintRefused(
                f"{location.path} cannot be read, so the value it holds for "
                f"{holder!r} could not be put back if the write failed"
            ) from None

        await _put(service, holder, username, new_value, role, mind_id)

        with credentials_data._WRITE_LOCK:
            written = _write_stored(location, new_value)
            verified = False
            if written["written"]:
                try:
                    verified = _loaded_value(location) == new_value
                except Unreadable:
                    verified = False
        if not verified:
            # Read back rather than trusted. A folded line, a duplicate key
            # lower down the file, a write that reported success onto a
            # filesystem that dropped it — each leaves the holder presenting
            # something the service has already stopped accepting.
            restored, restore_failure = False, ""
            if previous:
                try:
                    await _put(service, holder, username, previous, role, mind_id)
                    restored = True
                except Exception as exc:
                    # Every failure, not only a refusal. Whatever stopped the
                    # write often stops this too, and an exception escaping
                    # here would replace the one state this module exists to
                    # report with a blank 500.
                    restore_failure = f"{type(exc).__name__}: {exc}"
            raise Stranded(
                f"{service.label} issued {holder!r} a new value and "
                f"{location.path} did not take it"
                + (
                    f" — {written['reason']}"
                    if written.get("reason")
                    else " — the file does not read back what was written"
                )
                + (
                    "; the previous value has been reinstated"
                    if restored
                    else "; this holder now has no working value"
                    + (
                        f" — reinstating the previous one failed too "
                        f"({restore_failure})"
                        if restore_failure
                        else " — it had none to reinstate"
                        if not previous
                        else ""
                    )
                    + (
                        f". Its last value is in {credentials_data.BACKUP_DIR}"
                        if previous
                        else ""
                    )
                ),
                restored=restored,
            )

    return {
        "service": service.key,
        "holder": holder,
        "username": username,
        "role": role,
        "preview": credentials_data._redact(new_value),
        "file": location.file,
        "path": str(location.path),
        "env_key": location.env_key,
        "restart_target": location.restart_target,
    }


async def revoke(service: Service, holder: str) -> dict[str, Any]:
    """Kill one holder's credential without touching where it reads.

    The action that belongs to a holder this console cannot write: a box in a
    bedroom cannot be handed a new value from here, and leaving its old one
    live is the thing per-holder credentials existed to stop.
    """
    await _require_managed_proxy(service, holder)
    response = await _request(service, "DELETE", f"/credentials/{holder}")
    if response.status_code >= 400:
        raise MintRefused(
            f"{service.label} refused the revocation for {holder!r} "
            f"({response.status_code})"
        )
    answer = response.json()
    return {
        "service": service.key,
        "holder": holder,
        "revoked": bool(answer.get("revoked")),
    }


async def rotate(service: Service, holder: str) -> dict[str, Any]:
    """Issue one holder a new value, leaving every other holder working.

    The holder's user, role and provenance are read back off the service's own
    listing rather than guessed, so a rotation cannot quietly retype a mind as
    a person or drop the provenance its memory is filed under.
    """
    await _require_managed_proxy(service, holder)
    holders = await list_holders(service)
    row = next(
        (r for r in holders if str(r.get("holder", "")).lower() == holder.strip().lower()),
        None,
    )
    if row is None and service.key == "proxy" and _unlisted_proxy_holder(holder):
        # A mind whose proxy mint failed at creation - the proxy was down -
        # has its variable and no client. Its first key is issued here; the
        # variable is written only by mind creation, so the holder is a mind.
        return await mint(service, holder.strip().lower(), "", role="mind")
    if row is None:
        raise MintRefused(f"{service.label} has no holder called {holder!r}")
    role = str(row.get("role") or "")
    if not role:
        raise MintRefused(
            f"{service.label} reports no role for {holder!r}; minting one "
            "without it would retype the holder"
        )
    return await mint(
        service,
        str(row["holder"]),
        str(row.get("username") or ""),
        role=role,
        mind_id=row.get("mind_id"),
    )


def _proxy_variables() -> list[str]:
    """Holders a `MIND_KEY_<NAME>` or `PROXY_TOKEN_<NAME>` in the shared mind file is for."""
    path = ENV_FILES.get(MIND_ENV_LABEL)
    if path is None:
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    found = re.findall(r"(?m)^(?:PROXY_TOKEN|MIND_KEY)_([A-Z0-9_]+)=", text)
    # A mind name has hyphens where its variable has underscores.
    return [stem.lower().replace("_", "-") for stem in found]


def _unlisted_proxy_holder(holder: str) -> bool:
    return holder.strip().lower() in _proxy_variables()


async def holder_rows(service: Service) -> list[dict[str, Any]]:
    """One row per holder this service reports, with where it reads and
    whether this console can rotate it."""
    rows: list[dict[str, Any]] = []
    listed = await list_holders(service)
    if service.key == "proxy":
        # A mind created while the proxy was down has a variable here and no
        # client there: it needs a row, or its only remedy has no button.
        known = {str(row.get("holder") or "").lower() for row in listed}
        listed = listed + [
            {"holder": name, "username": None, "role": "mind", "revoked_at": "never issued"}
            for name in _proxy_variables() if name not in known
        ]
    for row in listed:
        holder = str(row.get("holder") or "")
        if not holder:
            continue
        location, reason = None, None
        try:
            location = locate(service, holder)
            if location is None:
                reason = "no file this console can write holds this holder's value"
        except Unreadable as exc:
            reason = f"a file that may hold this holder's value could not be read: {exc}"
        except Ambiguous as exc:
            reason = str(exc)
        if location is not None and location.is_one_key:
            reason = ONE_KEY_REFUSAL
        rows.append(
            {
                "service": service.key,
                "service_label": service.label,
                "holder": holder,
                "username": row.get("username"),
                "role": row.get("role"),
                "mind_id": row.get("mind_id"),
                "revoked": bool(row.get("revoked_at")),
                "file": location.file if location else None,
                "path": str(location.path) if location else None,
                "env_key": location.env_key if location else None,
                "restart_target": location.restart_target if location else None,
                "rotatable": location is not None and reason is None,
                "unrotatable_reason": reason,
            }
        )
    return rows


def _ensure_key(path: Path, key: str) -> None:
    """Make sure this file has a line for `key`, so the writer can replace it.

    `credentials_data._write_env_value` substitutes an existing line and
    reports False when there is none — correct for a rotation, useless for a
    holder being created. The append is its own step rather than a mode on the
    writer, because the two have different failure meanings: a missing key
    during a rotation is a holder reading somewhere else, and during
    provisioning it is simply a holder that is new.

    The newline before matters: a `.env` whose last line has none would
    otherwise take the new key onto the end of it, silently destroying
    whichever variable happened to be last in the file.
    """
    if not path.is_file():
        raise MintRefused(f"no file at {path} to write {key} into")
    # Read and written under the one lock every writer of these files takes:
    # two Minds made at once, or one beside a rotation, each read the file
    # and write it whole.
    with credentials_data._WRITE_LOCK:
        text = credentials_data._read_text(path)
        if credentials_data._read_env_value(path, key) is not None:
            return
        updated = text if text.endswith("\n") or not text else text + "\n"
        credentials_data._write_file_in_place(
            path, updated + f"{key}=\n", MIND_ENV_LABEL, key, text
        )


async def name_held_by_something_else(name: str) -> str | None:
    """Which service already has a holder by this name, if any.

    The credential namespace is holders, which is wider than minds:
    `scheduler`, `telegram-bot` and `spark-to-bloom` hold values and are
    nobody's mind. Minting over one replaces a running app's credential and
    retypes it from `service` to `mind` — and that app reads its value from a
    file this console does not mount, so no restart fixes it.
    """
    if name.strip().lower() in TOOLS_RESERVED_HOLDERS:
        return SERVICES["tools"].label
    for service in SERVICES.values():
        try:
            rows = await list_holders(service)
        except MintRefused:
            # A service that cannot be asked is not a service that said no.
            continue
        for row in rows:
            if str(row.get("holder", "")).lower() != name.strip().lower():
                continue
            if row.get("live") is False:
                # Every service keeps a holder's row after its credential is
                # revoked — a proxy client its usage, the gateway and the
                # memory store the record of who held what. Nothing is using
                # a revoked one, so it is not a holder a new Mind would
                # displace: a creation that failed and was taken back must
                # not take its name out of use for good.
                continue
            return service.label
    return None


class KeyHeldByAnother(ValueError):
    """The pasted key already belongs to another mind. Nothing was made."""


KEY_HELD_MESSAGE = (
    "Another mind already holds that key. Paste this mind's own key."
)
KEY_MISSING_MESSAGE = "Paste the mind's key; a mind cannot be made without one."
#: Short enough to allow any real key, long enough that a stray character
#: cannot become a gateway credential.
MIN_KEY_LENGTH = 8

_KEY_VARIABLE = re.compile(
    r"(?m)^(PROXY_TOKEN_[A-Z0-9_]+|[A-Z0-9_]*_PROXY_KEY|MIND_PROXY_KEY"
    r"|MIND_KEY_[A-Z0-9_]+|PROXY_CREDENTIAL_ADMIN_KEY)\s*=")


def _mind_runtime_files() -> list[Path]:
    try:
        import configuration_data
        root = Path(configuration_data.HIVE_MIND_MINDS_CONFIG_DIR)
        return sorted(root.glob("*/runtime.yaml"))
    except Exception:  # noqa: BLE001 - a missing folder holds no keys
        return []


#: Names a mind's runtime.yaml `env:` may carry its proxy key under, and the
#: names an edge mind's own env file may (it holds nothing else).
_RUNTIME_KEY_NAMES = frozenset({
    "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DSH_API_KEY", "MIND_PROXY_KEY"})


#: The env file label an edge mind on this host has to itself, and whose it is.
_OWN_FILE_HOLDERS = {label: holder for holder, label in SERVICES["comms"].own_files.items()
                     if label != "hive_tools"}


def key_places() -> list[tuple[str, str, str]]:
    """Every place on this machine a mind's proxy key may be kept, as
    `(place, variable, value)`, by name and never printed.

    The one enumeration creation and the move of existing minds share: the
    variables of every env file this console reads (`MIND_KEY_<NAME>`,
    `PROXY_TOKEN_<NAME>`, `<NAME>_PROXY_KEY`, `MIND_PROXY_KEY`) and the `env`
    of every mind's runtime.yaml (`OPENAI_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
    `DSH_API_KEY`, `MIND_PROXY_KEY`). `place` is `<file label>:<VARIABLE>` for
    an env file and `<mind>:<VARIABLE>` for a runtime.yaml; an unreadable env
    file raises `Unreadable`, since a key hiding there cannot be ruled out.
    """
    found: list[tuple[str, str, str]] = []
    for label, path in ENV_FILES.items():
        try:
            if not Path(path).is_file():
                continue
            text = credentials_data._read_text(path)
        except Exception:  # noqa: BLE001 - an unreadable file is reported by the caller
            raise Unreadable(f"{path}") from None
        variables = list(_KEY_VARIABLE.findall(text))
        if label in _OWN_FILE_HOLDERS:
            # An edge mind's own file holds nobody else's: its proxy key may be
            # under any of the names its harnesses read.
            variables += [n for n in sorted(_RUNTIME_KEY_NAMES)
                          if re.search(rf"(?m)^{n}\s*=", text) and n not in variables]
        for variable in variables:
            existing = _loaded_value(Location(label, variable))
            if existing:
                found.append((f"{label}:{variable}", variable, existing))
    for name in mind_key_files.names():
        try:
            existing = mind_key_files.read(name)
        except mind_key_files.KeyFileError:
            raise Unreadable(f"{mind_key_files.path(name)}") from None
        if existing:
            found.append((f"{KEY_FILE_LABEL}:{name}", "MIND_KEY", existing))
    for path in _mind_runtime_files():
        try:
            import yaml
            env = (yaml.safe_load(path.read_text()) or {}).get("env") or {}
        except Exception:  # noqa: BLE001 - a runtime file that does not parse holds no key
            continue
        if not isinstance(env, dict):
            continue
        for variable, existing in env.items():
            if ((_KEY_VARIABLE.match(f"{variable}=") or variable in _RUNTIME_KEY_NAMES)
                    and isinstance(existing, str) and existing
                    and not existing.startswith("$")):
                found.append((f"{path.parent.name}:{variable}", str(variable), existing))
    return found


def proxy_key_places(value: str, *, own: str | None = None) -> list[str]:
    """Every place that holds `value`, by name and never by content.

    `own` is the mind the value is being given to; its own places are not a
    clash. Used by creation here and by moving existing minds, so the two
    agree on what "held by another mind" means.
    """
    mine = {variable_name(SERVICES["proxy"], own), one_key_variable(own)} if own else set()
    own_name = own.strip().lower() if own else None
    own_file = own_file_label(own) if own else None
    found: list[str] = []
    for place, variable, existing in key_places():
        head = place.partition(":")[0]
        if head in ENV_FILES or head == KEY_FILE_LABEL:
            # This mind's own places: its named variables, a variable that
            # names it (`ADA_PROXY_KEY`), its own key file, and the whole of a
            # file it has to itself (an edge mind's own `.env`).
            if (variable in mine or (head != KEY_FILE_LABEL and head == own_file)
                    or (own_name and holder_from_place(place) == own_name)):
                continue
        elif own_name and head == own_name:
            continue
        if secrets.compare_digest(existing, value):
            found.append(place)
    return found


def mind_proxy_key_sources(name: str) -> list[tuple[str, str]]:
    """Where this mind's existing proxy key is kept and what it is, as
    `(place, value)`: the places `key_places` names for this mind, which for
    an edge mind with an env file of its own include the names that file may
    carry it under."""
    holder = name.strip().lower()
    found: list[tuple[str, str]] = []
    for place, variable, value in key_places():
        head = place.partition(":")[0]
        if head in ENV_FILES or head == KEY_FILE_LABEL:
            if holder_from_place(place) == holder:
                found.append((place, value))
        elif head == holder:
            found.append((place, value))
    return found


async def provision_mind(
    name: str, mind_id: str, owner: str, *, proxy_key: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Give a brand-new containerised mind its one key, on every service.

    The person pasted the key; the hive invents nothing. The gateway, the
    memory store and the tools are each made to accept exactly that value as
    this mind, and
    it is written once, to the mind's own key file (`mind_key_files`), which
    its generated compose file mounts. Nothing is written to the install's
    `.env`. The proxy is sent nothing: the key is already its client's.

    Role and provenance are stated, never inferred. A mind is minted before it
    has a broker row, and comms types a holder it cannot find as a `person`;
    the memory store keys a mind's rows by the uuid every hook sends.

    A service refusing the value because another holder has it raises
    `KeyHeldByAnother` (a 400 for the page); the caller's undo removes
    whatever landed on the services before it.
    """
    if not proxy_key:
        raise MintRefused(KEY_MISSING_MESSAGE)
    if name.strip().lower() in TOOLS_RESERVED_HOLDERS:
        # Before the first service is written: the tools would refuse the
        # name last, after the gateway and the memory store had taken it.
        raise MintRefused(
            f"{name!r} is a name the tools keep for themselves; "
            "choose another name for this mind")
    minted: list[dict[str, Any]] = []
    failures: list[str] = []
    held = False
    # The page's early "does another mind hold this key" read is not the guard
    # on its own: two creations pasting one key both pass it before either has
    # written anything. The guard is this read and the line that records the
    # key, made together under the rotation lock, first and before any service
    # is told: the second creation finds the first's line and is refused here
    # with nothing of its own written.
    if not mind_key_files.configured():
        raise MintRefused(NO_KEY_ROOT_MESSAGE)
    async with _ROTATION_LOCK:
        _refuse_if_another_holds(proxy_key, name)
        minted.append(_store_mind_proxy_key_locked(name, owner, proxy_key, creating=True))
    for service in SERVICES.values():
        if service.key == "proxy":
            continue
        if service.key == "tools" and not service.configured():
            continue
        try:
            minted.append(
                await mint(
                    service,
                    name,
                    owner,
                    role="mind",
                    mind_id=mind_id,
                    location=key_file_location(name),
                    value=proxy_key,
                )
            )
        except Exception as exc:
            if "already held by" in str(exc):
                held = True
            # Each service on its own. One of the two landing is a different
            # state from neither — a mind with a gateway credential and no
            # memory-store one authenticates, answers, and silently remembers
            # nothing — and the caller is told which it has.
            failures.append(f"{service.label}: {exc}")
    if held:
        raise KeyHeldByAnother(KEY_HELD_MESSAGE)
    return minted, ("; ".join(failures) if failures else None)


def _plain_key(supplied: Any, what: str) -> str:
    if supplied is None or (isinstance(supplied, str) and not supplied.strip()):
        raise MintRefused(KEY_MISSING_MESSAGE)
    if not isinstance(supplied, str):
        raise MintRefused(f"Paste the {what} as text")
    value = supplied.strip()
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,4096}", value):
        raise MintRefused(f"Paste only the {what}, without quotes or embedded whitespace")
    if len(value) < MIN_KEY_LENGTH:
        raise MintRefused(f"That is too short to be a mind's key. {KEY_MISSING_MESSAGE}")
    return value


def _refuse_if_another_holds(value: str, own: str | None) -> None:
    try:
        places = proxy_key_places(value, own=own)
    except Unreadable:
        raise MintRefused("The Mind credential file could not be read") from None
    if places:
        named = holder_from_place(places[0])
        raise KeyHeldByAnother(
            f"{named} already holds that key. Paste this mind's own key."
            if named else KEY_HELD_MESSAGE)


async def validate_mind_proxy_key(
    supplied: Any, *, for_mind: str | None = None, stored_here: bool = True,
) -> tuple[str, None]:
    """Verify a pasted mind key before anything is written.

    A key is required. One another mind already holds is refused, not
    warned about. This read uses the supplied key, never the
    credential-management API, and never echoes its response.
    """
    value = _plain_key(supplied, "proxy key")
    path = ENV_FILES.get(MIND_ENV_LABEL)
    if stored_here and (path is None or not path.is_file()):
        raise MintRefused("This Hive is not configured to store Mind proxy keys yet")
    _refuse_if_another_holds(value, for_mind)
    service = SERVICES["proxy"]
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(
                await _service_url(service) + "/v1/models", headers={"Authorization": f"Bearer {value}"},
            )
    except httpx.HTTPError:
        raise MintRefused("Could not verify the proxy key. Check the proxy connection and try again") from None
    if response.status_code in (401, 403):
        raise MintRefused("The proxy rejected that key. Check the key and try again")
    if response.status_code != 200:
        raise MintRefused("The proxy could not verify that key. Try again when it is available")
    return value, None


def name_has_old_proxy_line(path: Path, name: str) -> bool:
    return credentials_data._read_env_value(
        path, variable_name(SERVICES["proxy"], name)) is not None


async def store_mind_proxy_key(name: str, owner: str, value: str, *, creating: bool = False) -> dict[str, Any]:
    """Persist an already verified key without minting or rotating it upstream."""
    async with _ROTATION_LOCK:
        return _store_mind_proxy_key_locked(name, owner, value, creating=creating)


def _store_mind_proxy_key_locked(name: str, owner: str, value: str, *, creating: bool = False) -> dict[str, Any]:
    """`store_mind_proxy_key` for a caller already holding `_ROTATION_LOCK`."""
    if creating:
        # A new mind's key goes to its own key file, never into the shared
        # `.env`. Written atomically, and read back.
        place = key_file_location(name)
        written = _write_stored(place, value)
        if not written["written"] or _loaded_value(place) != value:
            raise MintRefused("The pasted proxy key could not be saved; creation cannot use it")
        return {"service": "proxy", "holder": name, "username": owner, "role": "mind",
                "file": place.file, "path": str(place.path), "env_key": place.env_key,
                "restart_target": None}
    path = ENV_FILES[MIND_ENV_LABEL]
    if stored_key_location(name) is not None:
        # Writing only the key would leave the gateway, memory store and
        # tools on the old value: the mind would go dark on its next request.
        raise MintRefused(ONE_KEY_REFUSAL)
    # A mind made before the one key has its own proxy line, and the key goes
    # there until the mind is moved.
    key = variable_name(SERVICES["proxy"], name)
    with credentials_data._WRITE_LOCK:
        _ensure_key(path, key)
        written = credentials_data._write_location(path, key, value, MIND_ENV_LABEL)
        if not written["written"] or _loaded_value(Location(MIND_ENV_LABEL, key)) != value:
            raise MintRefused("The pasted proxy key could not be saved")
    return {"service": "proxy", "holder": name, "username": owner, "role": "mind",
            "file": MIND_ENV_LABEL, "path": str(path), "env_key": key,
            "restart_target": RESTART_BY_FILE.get(MIND_ENV_LABEL, RESTART_BY_FILE["hive_mind"])}


# ---------------------------------------------------------------------------
# Replacing a mind's one key
# ---------------------------------------------------------------------------

#: What the page says about the proxy, every time: the hive does not hold the
#: mind's inference account and does not change it there.
PROXY_NOTE = (
    "The old key still buys inference billed to this mind until whoever runs "
    "the proxy revokes it there; do that now if the old key leaked."
)

_HELD_BY = re.compile(r"held by '([^']+)'")


class ReplacementFailed(Exception):
    """A replacement that stopped part-way and could not be fully put back.

    The message says which service holds which key and what to do. It never
    contains a key."""


def _one_key_services() -> list[Service]:
    """The services that each hold the mind's key: not the proxy, which is the
    person's, and the tools only where this hive has them."""
    return [s for s in SERVICES.values()
            if s.key != "proxy" and (s.key != "tools" or s.configured())]


def stored_key_location(holder: str) -> Location | None:
    """Where this mind's one key is stored on this console, or None.

    Only a `MIND_KEY_<NAME>` line counts: a mind not yet moved keeps separate
    per-service lines, which are not one key and are not replaced here."""
    one = one_key_variable(holder)
    found = []
    own_label = own_file_label(holder)
    if mind_key_files.exists(holder):
        # The key file is the key. An older line for it in an env file is a
        # leftover (`legacy_key_lines`), never a second answer.
        return key_file_location(holder)
    for label in ENV_FILES:
        try:
            if credentials_data._read_env_value(ENV_FILES[label], one) is not None:
                found.append(Location(label, one))
            if label == own_label and credentials_data._read_env_value(
                    ENV_FILES[label], ONE_KEY_PLAIN) is not None:
                found.append(Location(label, ONE_KEY_PLAIN))
        except Unreadable:
            raise MintRefused(
                f"{ENV_FILES[label]} could not be read, so this mind's stored "
                "key cannot be replaced safely. Nothing was changed") from None
    if len(found) > 1:
        raise MintRefused(
            f"{', '.join(l.file for l in found)} each hold {one}, so which one "
            "this mind reads cannot be told. Nothing was changed")
    return found[0] if found else None


def legacy_key_lines(holder: str) -> list[Location]:
    """The `MIND_KEY_<NAME>` lines this mind still has in env files: where its
    key lived before key files. They are removed when the mind is moved to a
    file and retired; until then a compose written before the mount reads
    them."""
    one = one_key_variable(holder)
    found = []
    for label in ENV_FILES:
        try:
            if credentials_data._read_env_value(ENV_FILES[label], one) is not None:
                found.append(Location(label, one))
        except Unreadable:
            continue
    return found


def storage_places(holder: str) -> list[Location]:
    """Every place this console writes this mind's key, the key file first.

    A mind whose key is kept here gets its key file (when this console was
    given a key root); a line it still has in an env file is kept in step until
    the retirement removes it, so a container made from an older compose file
    reads the right key. An edge mind's own env file is its own place, as
    before. A mind whose key is not kept here has no place."""
    current = stored_key_location(holder)
    if current is None:
        return []
    if current.file == own_file_label(holder):
        return [current]
    places: list[Location] = []
    if mind_key_files.configured():
        places.append(key_file_location(holder))
    places += [place for place in legacy_key_lines(holder) if place not in places]
    return places or [current]


_ANY_LINE = re.compile(r"(?m)^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def echo_lines(value: str) -> list[Location]:
    """Every line in the env files this console writes whose value IS `value`.

    A mind has one key (requirement 22), but the install once kept it in more
    than one line besides its own: `MIND_PROXY_KEY` (the installer's copy, and
    the hive's own proxy key where the operator gave one key) and
    `SERVICE_PROXY_KEY`. A replacement rewrites each of them in the same
    operation as the key file, so no file holds the old key afterwards. Found
    by value, not by name, so a copy under a name this code never heard of is
    caught too. An unreadable file raises `Unreadable`: a copy cannot be ruled
    out in it."""
    found: list[Location] = []
    for label, path in ENV_FILES.items():
        if label in _OWN_FILE_HOLDERS:
            continue  # an edge mind's own file is its own place, handled apart
        if not Path(path).is_file():
            continue
        text = credentials_data._read_text(path)
        names = []
        for name, raw in _ANY_LINE.findall(text):
            held = raw.strip().strip("\r")
            if len(held) >= 2 and held[0] == held[-1] and held[0] in "'\"":
                held = held[1:-1]
            if held and secrets.compare_digest(held, value) and name not in names:
                names.append(name)
        found += [Location(label, name) for name in names]
    return found


def runtime_copies(value: str) -> list[str]:
    """The mind files (`runtime.yaml` env, `container/compose.yaml`) that hold
    `value` literally, by file name. The console does not edit those, so a replacement
    that finds one refuses before anything changes (requirement 22)."""
    found = []
    for path in _mind_runtime_files():
        try:
            import yaml
            env = (yaml.safe_load(path.read_text()) or {}).get("env") or {}
        except Exception:  # noqa: BLE001 - a file that does not parse holds no key
            continue
        if isinstance(env, dict) and any(
                isinstance(held, str) and held and secrets.compare_digest(held, value)
                for held in env.values()):
            found.append(f"{path.parent.name}/runtime.yaml")
        compose_file = path.parent / "container" / "compose.yaml"
        try:
            if compose_file.is_file() and value in compose_file.read_text():
                found.append(f"{path.parent.name}/container/compose.yaml")
        except (OSError, UnicodeDecodeError):
            continue
    return found


def scrub_backups(value: str, *, check_only: bool = False) -> list[str]:
    """Blank the retired key `value` in the console's own pre-write copies of
    env files (requirement 22: no file keeps the old key). The copies are kept,
    since they are whole files and hold other credentials' last values; only
    the old key's text in them is replaced, through a private (0600) temporary
    file renamed over the copy, removed again if anything fails. With
    `check_only` nothing is written: it says which copies could not be cleaned.
    Returns those, by name."""
    failed: list[str] = []
    try:
        candidates = list(credentials_data.BACKUP_DIR.iterdir())
    except FileNotFoundError:
        return failed
    except OSError:
        return ["the backup folder"]
    needle = value.encode()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            failed.append(path.name)
            continue
        if needle not in raw:
            continue
        if check_only:
            if not os.access(credentials_data.BACKUP_DIR, os.W_OK):
                failed.append(path.name)
            continue
        temp = path.with_name(path.name + ".tmp")
        try:
            handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "wb") as out:
                out.write(raw.replace(needle, b"REPLACED-MIND-KEY"))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
        except OSError:
            failed.append(path.name)
            try:
                temp.unlink()
            except OSError:
                pass
    return failed


def leftover_files(value: str) -> list[str]:
    """Env files this console writes whose text still holds `value` in any
    spelling (an `export` line, an inline comment, a longer value)."""
    found = []
    for label, path in ENV_FILES.items():
        try:
            if Path(path).is_file() and value in credentials_data._read_text(path):
                found.append(label)
        except Unreadable:
            found.append(label)
    return found


def mind_holds_one_key_line(holder: str) -> bool:
    return stored_key_location(holder) is not None


def mind_is_not_yet_on_one_key(holder: str) -> bool:
    """A local mind that still has its older per-service lines and no one key."""
    path = ENV_FILES.get(MIND_ENV_LABEL)
    if path is None or stored_key_location(holder) is not None:
        return False
    try:
        return any(
            credentials_data._read_env_value(path, variable_name(service, holder)) is not None
            for service in SERVICES.values())
    except Unreadable:
        return False


async def _holder_row(service: Service, holder: str) -> dict[str, Any] | None:
    rows = await list_holders(service)
    return next((r for r in rows if str(r.get("holder", "")).lower() == holder), None)


async def _accepts(service: Service, value: str) -> bool | None:
    """Whether the service already accepts `value` as someone's key; None when
    it could not be asked."""
    path = PROBE_PATHS.get(service.key)
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(
                f"{await _service_url(service)}{path}",
                headers={"Authorization": f"Bearer {value}"})
    except (httpx.HTTPError, ValueError, UnicodeError):
        return None
    if response.status_code in PROBE_ACCEPTED:
        return True
    if response.status_code in (401, 403):
        return False
    return None


def _is_revoked(row: dict[str, Any] | None) -> bool:
    return bool(row) and (bool(row.get("revoked_at")) or row.get("live") is False)


async def _revoke_row(service: Service, holder: str) -> None:
    response = await _request(service, "DELETE", f"/credentials/{holder}")
    if response.status_code >= 400:
        raise MintRefused("not revoked again")


async def _put_key(
    service: Service, holder: str, owner: str, mind_id: str,
    row: dict[str, Any] | None, value: str,
) -> None:
    username = str((row or {}).get("username") or owner)
    await _put(service, holder, username, value, "mind",
               str((row or {}).get("mind_id") or mind_id) or None)


def _held_by_name(exc: BaseException) -> str | None:
    found = _HELD_BY.search(str(exc))
    return found.group(1) if found else None


def holder_from_place(place: str) -> str | None:
    """The mind a `label:VARIABLE` or `mind:VARIABLE` place from
    `proxy_key_places` belongs to, so a refusal can name it."""
    head, _, variable = place.partition(":")
    if head == KEY_FILE_LABEL:
        return variable or None
    if head not in ENV_FILES:
        return head or None
    if head in _OWN_FILE_HOLDERS:
        return _OWN_FILE_HOLDERS[head]
    for prefix in ("MIND_KEY_", "PROXY_TOKEN_"):
        if variable.startswith(prefix):
            return variable[len(prefix):].lower().replace("_", "-")
    if variable.endswith("_PROXY_KEY") and variable != "MIND_PROXY_KEY":
        return variable[:-len("_PROXY_KEY")].lower().replace("_", "-")
    return None


def key_held_message(holder: str | None) -> str:
    if holder:
        return (f"{holder} already holds that key. Paste this mind's own key. "
                "Nothing was changed.")
    return KEY_HELD_MESSAGE + " Nothing was changed."


_STATE_WORDS = {
    "new": "holds the new key",
    "old": "holds the old key",
    "none": "has no key for this mind",
    "unknown": "did not answer, so its state is unknown, check before trying again",
    "untouched": "was not changed and still holds the old key",
    "live_again": "holds the old key and is live again although this mind was cut off, revoke it",
    "unrestorable_revoked": "holds the new key, cut off as before; the old one is not known here",
    "unrestorable": "holds the new key and the old one is not known here to put back",
}


def _states_sentence(order: list[Service], state: dict[str, str]) -> str:
    return "; ".join(f"{s.label} {_STATE_WORDS[state.get(s.key, 'untouched')]}" for s in order)


async def replace_mind_key(
    name: str, owner: str, mind_id: str, supplied: str,
) -> dict[str, Any]:
    """Give a mind a new key on the gateway, the memory store and the tools.

    `supplied` is already a checked, plain key (the route validated its form,
    that no other mind's stored line holds it, and that the proxy accepts it).
    Each service replaces the mind's value in place and refuses the old one on
    its very next request, so nothing is restarted. The mind's stored key is
    then written, where this console can write it.

    Raises `MintRefused` or `KeyHeldByAnother` when nothing changed, and
    `ReplacementFailed` when something did and could not be fully put back.
    The whole operation holds `_ROTATION_LOCK`, so two replacements, or a
    replacement beside any rotation, never interleave; and it is written to
    the journal first so a console stopped half-way puts the services back in
    step with the stored key when it starts again.
    """
    holder = name.strip().lower()
    value = supplied
    services = _one_key_services()
    unscrubbed: list[str] = []
    async with _ROTATION_LOCK:
        location = stored_key_location(holder)
        places = storage_places(holder)
        # Every other line in the install's env files that holds the same key
        # (requirement 22): rewritten with the key file, so none keeps the old.
        if location is not None:
            try:
                current = _loaded_value(location)
                if current:
                    places += [p for p in echo_lines(current) if p not in places]
                    kept = runtime_copies(current)
                    if kept:
                        raise MintRefused(
                            f"{', '.join(kept)} holds this mind's key literally and this "
                            "console does not rewrite it; take the key out of it first. "
                            "Nothing was changed")
            except Unreadable:
                raise MintRefused(
                    "an env file could not be read, so a copy of this mind's key "
                    "in it cannot be ruled out. Nothing was changed") from None
        # A key file this replacement is about to make is taken away again if
        # the replacement is put back: its absence is what "old" was.
        made_here = [p for p in places if p.is_key_file and not mind_key_files.exists(holder)]
        old: str | None = None
        if location is not None:
            try:
                old = _loaded_value(location)
            except Unreadable:
                raise MintRefused(
                    f"{location.path} could not be read, so the old key could "
                    "not be put back if something failed. Nothing was changed"
                ) from None
        # Before anything changes: every service must answer and accept this
        # console's administrator credential, or a part-way failure is certain.
        rows: dict[str, dict[str, Any] | None] = {}
        for service in services:
            try:
                rows[service.key] = await _holder_row(service, holder)
            except MintRefused as exc:
                raise MintRefused(f"{exc}. Nothing was changed.") from None
        same_as_stored = old is not None and secrets.compare_digest(old, value)
        if old and not same_as_stored:
            stuck = scrub_backups(old, check_only=True)
            if stuck:
                raise MintRefused(
                    f"the console's saved copies {', '.join(stuck)} hold the old key and "
                    "cannot be cleaned. Nothing was changed")
        accepted: dict[str, bool] = {}
        if not same_as_stored:
            for service in services:
                answer = await _accepts(service, value)
                if answer is None:
                    raise MintRefused(
                        f"Could not check that key against {service.label}. "
                        "Nothing was changed.")
                accepted[service.key] = answer
            # This mind's own key is `old`. A service that accepts a different
            # value gave it to some other holder, unless an earlier replacement
            # of this mind was left part-way (a journal record for it is open
            # or needs attention), when it may be this mind's own new key.
            # Then the write below decides, naming the holder if it is not.
            if (old is not None and any(accepted.values())
                    and not key_journal.open_for(holder)):
                raise KeyHeldByAnother(key_held_message(None))
            # A service that accepts the value already has it for some holder:
            # this mind's own row (a repeat paste) or another mind's. It is
            # written first, and its own clash check refuses by naming the
            # holder before anything has changed.
            # A mind whose key is not stored here may be pasting the key it
            # already has on some services (a repeat after a failure). A
            # service that accepts the value is written first: it either is
            # this mind's own row, or it refuses by naming the holder before
            # anything has changed.
            services = sorted(services, key=lambda s: not accepted.get(s.key, False))
        try:
            record = key_journal.begin("replace", holder, [s.key for s in services])
            record["owner"], record["mind_id"] = owner, mind_id
            # A put replaces the value and clears a revocation, so a mind cut
            # off with Revoke would be live again after a put-back. Remember
            # which services had it cut off, to cut it off again.
            record["revoked"] = [k for k, r in rows.items() if _is_revoked(r)]
            # Names only, never a key: the other lines to bring in step with
            # the stored key if this console stops half-way.
            record["echo"] = [[p.file, p.env_key] for p in places if not p.is_key_file]
            key_journal.update(record)
        except OSError as exc:
            raise MintRefused(
                f"the journal of key changes could not be written "
                f"({exc.strerror or exc}). Nothing was changed.") from None

        state: dict[str, str] = {}
        failure: Exception | None = None
        cancelled = False
        for service in services:
            try:
                await _put_key(service, holder, owner, mind_id, rows[service.key], value)
            except MintUncertain as exc:
                state[service.key] = "unknown"
                failure = exc
                break
            except asyncio.CancelledError:
                state[service.key] = "unknown"
                failure = MintRefused("the request was cancelled")
                cancelled = True
                break
            except Exception as exc:  # noqa: BLE001 - every failure is put back below
                state[service.key] = "untouched"
                failure = exc
                break
            state[service.key] = "new"
            try:
                key_journal.update(record, switched=[k for k, v in state.items() if v == "new"])
            except OSError as exc:
                failure = MintRefused(f"the journal could not be written ({exc.strerror or exc})")
                break

        stored = "not_writable"
        if failure is None and location is not None:
            # The key file first (what a mind reads), then any line an older
            # compose file still reads; each read back.
            for place in places:
                with credentials_data._WRITE_LOCK:
                    written = _write_stored(place, value, backup=False)
                    try:
                        verified = bool(written["written"]) and _loaded_value(place) == value
                    except Unreadable:
                        verified = False
                if not verified:
                    failure = MintRefused(
                        f"{place.path} did not take the new key"
                        + (f" ({written['reason']})" if written.get("reason") else ""))
                    break
            if failure is None and old and not secrets.compare_digest(old, value):
                with credentials_data._WRITE_LOCK:
                    left = leftover_files(old)
                if left:
                    failure = MintRefused(
                        f"{', '.join(left)} still holds the old key in a form this "
                        "console does not rewrite")
            if failure is None:
                stored = "written"
                if old and not secrets.compare_digest(old, value):
                    # Only now that everything verified: the saved copies are
                    # what a failed replacement would have been recovered from.
                    with credentials_data._WRITE_LOCK:
                        unscrubbed = scrub_backups(old)

        if failure is None:
            try:
                key_journal.finish(record)
                key_journal.clear(holder)
            except OSError:
                pass  # the replacement is complete; a stale record is harmless
            if location is None:
                # A mind whose key is not kept here: now registered at every
                # service, and the console cannot read its file. Remembered, so
                # the move report can say "registered, key not stored here".
                try:
                    key_journal.mark_registered_not_stored(holder)
                except OSError:
                    pass
            return {
                "services": [{"service": s.key, "label": s.label, "status": "replaced"}
                             for s in services],
                "stored": stored,
                "restart_target": location.restart_target if location else None,
                "key_file": bool(location and location.is_key_file),
                # Saved copies that still hold the old key (should be none).
                "copies_still_holding_old_key": unscrubbed,
                "proxy_note": PROXY_NOTE,
            }

        async def put_back() -> bool:
            # Put back, in the reverse order, whatever may have taken the new key.
            for service in reversed(services):
                if state.get(service.key) not in ("new", "unknown"):
                    continue
                row = rows[service.key]
                try:
                    if row is None and state[service.key] in ("new", "unknown"):
                        response = await _request(
                            service, "DELETE", f"/credentials/{holder}?remove=true")
                        if response.status_code >= 400:
                            raise MintRefused("not removed")
                        state[service.key] = "none"
                    elif old is not None:
                        await _put_key(service, holder, owner, mind_id, row, old)
                        state[service.key] = "live_again"
                        if _is_revoked(row):
                            await _revoke_row(service, holder)
                        state[service.key] = "old"
                    elif row is None:
                        response = await _request(
                            service, "DELETE", f"/credentials/{holder}?remove=true")
                        if response.status_code >= 400:
                            raise MintRefused("not removed")
                        state[service.key] = "none"
                    else:
                        was = state[service.key]
                        state[service.key] = "unrestorable" if was == "new" else "unknown"
                        if _is_revoked(row):
                            # Cut off before: the new key must not be live either.
                            await _revoke_row(service, holder)
                            state[service.key] = "unrestorable_revoked"
                except Exception:  # noqa: BLE001 - reported below, by service
                    pass
            file_ok = True
            if location is not None and old is not None:
                for place in places:
                    if place in made_here:
                        try:
                            mind_key_files.remove(holder)
                        except Exception:  # noqa: BLE001
                            file_ok = False
                        continue
                    try:
                        if _loaded_value(place) != old:
                            with credentials_data._WRITE_LOCK:
                                _write_stored(place, old, backup=False)
                            file_ok = file_ok and _loaded_value(place) == old
                    except Exception:  # noqa: BLE001
                        file_ok = False
            return file_ok

        # A cancelled request still puts the services back: the put-back runs
        # as its own task and is awaited to the end before cancelling goes on.
        task = asyncio.ensure_future(put_back())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    pass
        file_ok = task.result() if not task.cancelled() and task.exception() is None else False
        clean = file_ok and all(
            state.get(s.key, "untouched") in ("old", "none", "untouched") for s in services)
        reason = _failure_sentence(failure)
        if cancelled:
            if clean:
                try:
                    key_journal.finish(record)
                except OSError:
                    pass
            else:
                try:
                    key_journal.update(record, status="needs_attention",
                                       detail=_states_sentence(services, state))
                except OSError:
                    pass
            raise asyncio.CancelledError()
        if clean:
            try:
                key_journal.finish(record)
            except OSError:
                pass
            holder_name = _held_by_name(failure)
            if holder_name or "already held by" in str(failure):
                raise KeyHeldByAnother(key_held_message(holder_name))
            raise MintRefused(f"{reason}. Every service is back on the old key.")
        sentence = _states_sentence(services, state)
        if not file_ok:
            sentence += f"; the stored key in {location.path} could not be put back"
        try:
            key_journal.update(record, status="needs_attention", detail=sentence)
        except OSError:
            pass
        again = ("Paste the same new key again to finish; it is safe to repeat."
                 if old is None else
                 "Check each service, then paste the key you want this mind to use.")
        raise ReplacementFailed(f"{reason}. Now: {sentence}. {again}")


def _failure_sentence(exc: BaseException) -> str:
    """One sentence about why, never carrying the key (the services' own
    refusals are already cut down to their `error` sentence by `_put`)."""
    text = str(exc).strip() or type(exc).__name__
    return text[:300]


async def recover_replacements() -> list[dict[str, Any]]:
    """On console start: bring the services back in step with the stored key
    for every replacement this console was stopped in the middle of.

    The journal holds no key, so the one source is the mind's stored line:
    every service is given that value, which finishes a replacement whose file
    was already written and undoes one whose file was not. A mind whose key is
    not stored here cannot be recovered from here and is marked for attention.
    """
    handled = []
    for record in key_journal.unfinished():
        holder = record["mind"]
        try:
            async with _ROTATION_LOCK:
                if not key_journal.is_open(record):
                    # Settled while this waited for the lock (a replacement
                    # finished, or a cut-off cleared it): re-applying the
                    # stored key would bring a cut-off mind back.
                    continue
                location = stored_key_location(holder)
                if location is None:
                    raise MintRefused(
                        "this mind's key is not stored on this console, so the "
                        "services cannot be put back in step from here")
                value = _loaded_value(location)
                if not value:
                    raise MintRefused("the stored key is empty")
                for key in record.get("planned", []):
                    service = SERVICES[key]
                    row = await _holder_row(service, holder)
                    await _put_key(service, holder, str(record.get("owner") or ""),
                                   str(record.get("mind_id") or ""), row, value)
                    if key in record.get("revoked", []):
                        await _revoke_row(service, holder)
                for label, name in record.get("echo", []):
                    if label in ENV_FILES:
                        with credentials_data._WRITE_LOCK:
                            done = _write_stored(Location(label, name), value, backup=False)
                        if not done["written"]:
                            raise MintRefused(
                                f"{label} did not take the stored key: {done['reason']}")
            key_journal.finish(record)
            handled.append({"mind": holder, "recovered": True})
        except Exception as exc:  # noqa: BLE001 - recorded, never fatal to the console
            try:
                # Left as started: the next start tries again. Under the lock,
                # and only while the record is still there, so a replacement
                # that finished since the snapshot is not written back.
                async with _ROTATION_LOCK:
                    if key_journal.is_open(record):
                        key_journal.update(record, detail=_failure_sentence(exc))
            except OSError:
                pass
            handled.append({"mind": holder, "recovered": False, "detail": _failure_sentence(exc)})
    return handled


async def cut_off_mind(name: str) -> dict[str, Any]:
    """Refuse a mind's key on the gateway, the memory store and the tools.

    One action, each service on its own: a service that cannot be reached or
    refuses does not stop the others being cut off, and the answer says which
    one still accepts the key. Each revocation keeps the holder's row marked
    revoked (nothing is deleted, the key stays burned for every other mind)
    and lands on the service's next request, so nothing is restarted. The
    stored key lines stay, so the page can still say where the mind reads,
    and pasting a key for the mind again brings it back. Revoking a holder
    already cut off is a success. The proxy is never called here: cutting off
    inference belongs to whoever runs the proxy and is its own step.

    `ok` is true only when every service answered that the holder is cut off.
    """
    holder = name.strip().lower()
    results: list[dict[str, Any]] = []
    async with _ROTATION_LOCK:
        for service in _one_key_services():
            entry: dict[str, Any] = {"service": service.key, "label": service.label}
            if service.key == "tools" and holder in TOOLS_RESERVED_HOLDERS:
                entry.update(
                    status="shared token",
                    detail="the tools know this name by the shared service token, which is not cut off here")
                results.append(entry)
                continue
            try:
                response = await _request(service, "DELETE", f"/credentials/{holder}")
                if response.status_code >= 400:
                    entry.update(
                        status="still accepting",
                        detail=f"{service.label} answered {response.status_code} to the cut-off")
                else:
                    try:
                        revoked = bool(response.json().get("revoked"))
                    except (ValueError, AttributeError):
                        revoked = False
                    if revoked:
                        entry.update(status="cut off")
                    else:
                        # Nothing was live: either already cut off, or this
                        # service never held a key for the mind.
                        row = await _holder_row(service, holder)
                        if row is not None and row.get("live") is False:
                            entry.update(status="cut off")
                        elif row is None:
                            entry.update(
                                status="no key here",
                                detail=f"{service.label} holds no key for this mind, so "
                                       "it may reach it by another credential")
                        else:
                            entry.update(status="still accepting",
                                         detail=f"{service.label} did not revoke it")
            except Exception as exc:  # noqa: BLE001 - reported per service, the rest still runs
                entry.update(status="unreachable", detail=str(exc)[:300] or type(exc).__name__)
            results.append(entry)
        if any(r["status"] == "cut off" for r in results):
            try:
                # A replacement left unfinished must not be re-applied on the next
                # start and quietly bring this mind back.
                key_journal.clear(holder)
            except OSError as exc:
                results.append({
                    "service": "journal", "label": "the key journal",
                    "status": "still accepting",
                    "detail": f"an unfinished key replacement could not be cleared ({exc.__class__.__name__}); "
                              "the next console start could bring this mind back"})
    if not any(r["service"] == "tools" for r in results):
        results.append({"service": "tools", "label": SERVICES["tools"].label,
                        "status": "not held", "detail": "this hive has no tools configured"})
    return {
        "mind": holder,
        "ok": all(r["status"] in ("cut off", "not held") for r in results),
        "services": results,
    }


async def inference_cut_off_step(name: str) -> dict[str, Any]:
    """Where this mind's inference is cut off, for the page to say.

    `offered` is true when the mind runs on this hive's proxy and this console
    holds the proxy's key-management credential, so the page may offer the
    proxy revoke as its own button; otherwise `where` names who to ask."""
    import provider_data
    connection = await provider_data.connection_for_mind(name.strip().lower())
    if connection and connection["id"] != "hive-proxy":
        return {"offered": False, "where": str(connection.get("name") or "its provider")}
    if not SERVICES["proxy"].admin_token():
        return {"offered": False, "where": "whoever runs the inference proxy"}
    return {"offered": True, "where": "the inference proxy"}


async def _service_url(service: Service) -> str:
    if service.key == "proxy":
        import provider_data
        return provider_data.root_url(await provider_data.get_connection("hive-proxy"))
    return service.url()


async def _require_managed_proxy(service: Service, holder: str) -> None:
    if service.key == "proxy":
        import provider_data
        connection = await provider_data.connection_for_mind(holder)
        if connection and connection["id"] != "hive-proxy":
            raise MintRefused("This Mind uses a direct or custom provider. Manage its key with that provider, then paste it on the Mind's page")


async def accept_mind_provider_token(
    supplied: Any, *, for_mind: str | None = None, stored_here: bool = True,
) -> tuple[str, None]:
    """Accept a mind's key on a direct or custom provider; ask no upstream.

    A key is required and one another mind holds is refused. The value is
    known to whoever runs that provider, and the same value opens the hive's
    services as this mind; the page says so.
    """
    import provider_data
    try:
        value = provider_data.normalize_token(supplied)
    except ValueError as exc:
        raise MintRefused(str(exc)) from None
    value = _plain_key(value, "token")
    path = ENV_FILES.get(MIND_ENV_LABEL)
    if stored_here and (path is None or not path.is_file()):
        raise MintRefused("This Hive is not configured to store Mind tokens yet")
    _refuse_if_another_holds(value, for_mind)
    return value, None


async def unprovision_mind(name: str) -> list[str]:
    """Undo `provision_mind` for a mind whose creation failed, or a mind
    deleted: the gateway and the memory store drop the mind's row outright
    (a revoked row would keep the pasted key refused for every other mind),
    and the mind's three lines leave the shared env file. The proxy is sent
    nothing: its client is the person's, and was never the hive's to revoke. Returns what could not be undone, in words; empty is all
    of it.

    Each service and each line on its own, as minting is: one failing must
    not leave the others live. A service that never had this holder is not a
    failure — the mint may have been what failed.
    """
    failures: list[str] = []
    holder = name.strip().lower()
    key_journal.unmark_registered_not_stored(holder)
    for service in SERVICES.values():
        keys = (one_key_variable(holder), variable_name(service, holder))
        try:
            # A service this console holds no admin credential for is one it
            # cannot have issued anything at: the value there, if any, was
            # pasted in, and is its owner's to keep.
            response = (await _request(
                service, "DELETE", f"/credentials/{holder}?remove=true")
                if service.admin_token() and service.key != "proxy" else None)
            if (response is not None and response.status_code >= 400
                    and response.status_code != 404):
                failures.append(
                    f"{service.label} still accepts {holder}'s credential "
                    f"(it answered {response.status_code} to the revocation)")
            elif response is not None and response.status_code != 404:
                try:
                    answered = response.json()
                except ValueError:
                    answered = None
                if not isinstance(answered, dict) or "removed" not in answered:
                    # A service older than this console ignores the request
                    # to remove and merely revokes, which leaves the pasted
                    # key refused for every other mind.
                    failures.append(
                        f"{service.label} revoked {holder}'s credential but "
                        "did not remove it, so the key stays refused for other "
                        "minds; update that service")
        except Exception as exc:  # noqa: BLE001 - reported, the rest still runs
            failures.append(
                f"{service.label} could not be asked to revoke {holder}'s "
                f"credential ({exc})")
        path = ENV_FILES.get(MIND_ENV_LABEL)
        if path is None:
            continue
        try:
            with credentials_data._WRITE_LOCK:
                text = credentials_data._read_text(path)
                kept = [line for line in text.splitlines(keepends=True)
                        if not line.startswith(tuple(f"{key}=" for key in keys))]
                if len(kept) != len(text.splitlines(keepends=True)):
                    credentials_data._write_file_in_place(
                        path, "".join(kept), MIND_ENV_LABEL, keys[0], text)
        except Exception as exc:  # noqa: BLE001 - reported, the rest still runs
            failures.append(f"the line {keys[0]} is still in {path} ({exc})")
    try:
        # Its key file and folder go with it: a key left in a folder nothing
        # mounts is still a key on this host.
        mind_key_files.remove(holder)
    except Exception as exc:  # noqa: BLE001 - reported, like every other leftover
        failures.append(f"the key file of {holder} is still in {mind_key_files.KEY_ROOT} ({exc})")
    return failures


# ---------------------------------------------------------------------------
# A person's own key (feature 834, requirement 19)
# ---------------------------------------------------------------------------

#: A person's key is held like a mind's: a credential at the gateway with the
#: person's username as holder and kind `user`, and a line in the shared env
#: file this console keeps for it. Random, never pasted (no proxy is involved
#: for people), shown to nobody.
USER_KEY_PREFIX = "USER_KEY_"
USER_ROLE = "user"


def user_key_variable(username: str) -> str:
    # Injective: every character other than a letter or digit becomes its own
    # hex code, so `dan.s`, `dan-s` and `dan_s` never share one line.
    stem = "".join(c if c.isalnum() and c.isascii() else f"_{ord(c):X}_"
                   for c in username.strip().upper())
    return USER_KEY_PREFIX + stem


def _user_key_location(username: str) -> Location:
    if MIND_ENV_LABEL not in ENV_FILES:
        raise MintRefused(f"no {MIND_ENV_LABEL} env file is configured (HIVE_ENV_FILES)")
    return Location(MIND_ENV_LABEL, user_key_variable(username))


async def _issue_user_key(username: str, location: Location, previous: str | None) -> str:
    """Register a fresh random key for this person at the gateway and keep it
    in the file. Under `_ROTATION_LOCK`, taken by the caller. The gateway is
    told first and put back when the file cannot take it, so the file never
    holds a value the gateway does not."""
    gateway = SERVICES["comms"]
    # A holder of that name that is not a person's key is somebody else's
    # credential (a mind, a surface bot, a service): never taken over.
    row = await _holder_row(gateway, username)
    if row is not None and str(row.get("role") or "") != USER_ROLE:
        raise MintRefused(
            f"the gateway already has a holder called {username!r} that is not "
            "a person's key, so this person's key cannot be made under that name")
    value = secrets.token_urlsafe(32)
    _ensure_key(location.path, location.env_key)
    await _put(gateway, username, username, value, USER_ROLE, None)
    with credentials_data._WRITE_LOCK:
        written = credentials_data._write_location(
            location.path, location.env_key, value, location.file)
        try:
            verified = bool(written["written"]) and _loaded_value(location) == value
        except Unreadable:
            verified = False
    if not verified:
        # Put the gateway back to what the file still holds (or cut it off
        # when this person had no key before): never a key nothing can read.
        try:
            if previous:
                await _put(gateway, username, username, previous, USER_ROLE, None)
            else:
                await _request(gateway, "DELETE", f"/credentials/{username}")
        except Exception:  # noqa: BLE001 - the refusal below says it
            pass
        raise MintRefused(
            f"{location.path} did not take the new key"
            + (f" ({written['reason']})" if written.get("reason") else ""))
    return value


async def user_key(username: str) -> str | None:
    """This person's key, made on their first need and then kept.

    None when it cannot be had (no gateway admin credential, no env file this
    console can write): the caller falls back to the mind's own key rather
    than failing the change."""
    name = username.strip().lower()
    if not name:
        return None
    try:
        location = _user_key_location(name)
        # Without the lock when the key exists: a settings read must not queue
        # behind a key move. Only making one is serialised.
        existing = _loaded_value(location) if location.path.is_file() else None
        if existing:
            return existing
        async with _ROTATION_LOCK:
            existing = _loaded_value(location) if location.path.is_file() else None
            if existing:
                return existing
            return await _issue_user_key(name, location, None)
    except (MintRefused, MintUncertain, Unreadable, OSError) as exc:
        import logging
        logging.getLogger(__name__).warning(
            "no key could be made for user %r: %s", name, type(exc).__name__ + ": " + str(exc)[:200])
        return None


async def replace_user_key(username: str) -> dict[str, Any]:
    """Cut a person's key off and give them a new one. The old one is refused
    by every mind at its next request (the gateway holds one value per holder);
    the new one is made now. Small on purpose: no page, no history."""
    name = username.strip().lower()
    if not name:
        raise MintRefused("a key belongs to a person")
    location = _user_key_location(name)
    async with _ROTATION_LOCK:
        try:
            previous = _loaded_value(location) if location.path.is_file() else None
        except Unreadable:
            raise MintRefused(
                f"{location.path} could not be read, so the old key could not be "
                "put back if something failed. Nothing was changed") from None
        await _issue_user_key(name, location, previous)
    return {"replaced": True, "holder": name}


async def check(service: Service, holder: str, role: str = "") -> dict[str, Any]:
    """Present this holder's own value to the service that validates it.

    Read off the holder's file rather than out of this process's environment:
    a rotation writes the file, and the question the operator is asking is
    whether what that box will load on its next start is accepted.

    "Could not reach the service" is never reported as a refusal. The remedy
    for a refusal is a new credential, and applying that to a value sitting
    behind an outage destroys a working one.
    """
    if role in ("person", USER_ROLE):
        # The routes a value can be presented to here are machine-classified,
        # so a person's credential is refused on every one of them — and a row
        # reading "refused" over a perfectly good sign-in is worse than one
        # reading "could not tell".
        return {
            "status": "undetermined",
            "detail": "a person's credential is not presented to a machine route",
        }
    try:
        location = locate(service, holder)
    except Unreadable as exc:
        return {"status": "unreadable", "detail": f"{exc} could not be read"}
    except Ambiguous as exc:
        return {"status": "undetermined", "detail": str(exc)}
    if location is None:
        return {
            "status": "unaddressable",
            "detail": "no file this console can write holds this holder's value",
        }
    try:
        value = credentials_data._read_env_value(location.path, location.env_key)
    except Unreadable:
        return {"status": "unreadable", "detail": f"{location.path} could not be read"}
    if not value:
        return {"status": "not_set", "detail": f"{location.env_key} is empty"}
    if service.key == "proxy":
        import provider_data
        connection = await provider_data.connection_for_mind(holder)
        if connection and connection["id"] != "hive-proxy":
            try:
                await provider_data.fetch_models(connection, value, "dsh")
                return {"status": "working", "detail": "Accepted by this Mind's configured provider"}
            except ValueError as exc:
                return {"status": "undetermined", "detail": str(exc)}
    path = PROBE_PATHS.get(service.key)
    if path is None:
        return {"status": "undetermined", "detail": "nothing validates this over HTTP"}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(
                f"{await _service_url(service)}{path}", headers={"Authorization": f"Bearer {value}"}
            )
    except (ValueError, UnicodeError) as exc:
        # A stray carriage return from a CRLF `.env`, or a non-ASCII byte:
        # the header cannot be built at all. Neither is an httpx.HTTPError.
        return {
            "status": "undetermined",
            "detail": f"this value cannot be sent as a header: {type(exc).__name__}",
        }
    except httpx.HTTPError as exc:
        # Never a refusal. The remedy for a refusal is a new credential, and
        # applying that to a value sitting behind an outage destroys a working
        # one.
        return {
            "status": "undetermined",
            "detail": f"could not reach {service.label}: {type(exc).__name__}",
        }
    if response.status_code in PROBE_ACCEPTED:
        return {"status": "working", "detail": ""}
    if response.status_code in (401, 403):
        return {
            "status": "refused",
            "detail": f"{service.label} rejected this value ({response.status_code})",
        }
    return {
        "status": "undetermined",
        "detail": f"{service.label} answered {response.status_code}",
    }
