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
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

import credentials_data
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
}
PROBE_ACCEPTED = frozenset({200, 404})

#: How long to wait on a status probe. Short, because the page asks per row
#: and the operator is looking at it.
PROBE_TIMEOUT_S = 8.0


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

    def url(self) -> str:
        base = (os.getenv(self.url_env) or self.url_default).rstrip("/")
        return base

    def admin_token(self) -> str:
        return os.getenv(self.admin_env) or ""


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
}

#: What has to be restarted for a value written into each file to be read.
#: Keyed by file rather than by holder, which is the honest direction: a
#: process reads one file, and whoever reads that file is who is still
#: holding the old value.
RESTART_BY_FILE: dict[str, str] = {
    "hive_mind": "the containerised minds (docker compose up -d)",
    "nervous_system": "hive-comms",
    "skippy": "skippy.service",
    "mordecai": "mordecai.service",
    "hive_tools": "the hive-tools container",
}


@dataclass(frozen=True)
class Location:
    """Where one holder's value for one service lives on this host."""

    file: str
    env_key: str

    @property
    def path(self) -> Path:
        return ENV_FILES[self.file]

    @property
    def restart_target(self) -> str | None:
        return RESTART_BY_FILE.get(self.file)


def variable_name(service: Service, holder: str) -> str:
    """The per-holder variable a shared `.env` names this holder's value by.

    `bilby-bot` is `COMMS_TOKEN_BILBY_BOT`. The hyphen has to go: a `.env`
    key with one in it is not a shell identifier and compose will not
    interpolate it.
    """
    stem = holder.strip().lower().replace("-", "_").upper()
    return f"{service.prefix}_TOKEN_{stem}"


def _loaded_value(location: Location) -> str | None:
    """The value the holder's own process loads, not the file's spelling of it.

    Quoting is ordinary and legal in a `.env`, and a CRLF file leaves a
    carriage return on the end of every line. Re-minting the raw text hands
    the holder a value it will never present, under a message saying its
    previous one was reinstated.
    """
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


def locate(service: Service, holder: str) -> Location | None:
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
                f"{service.url()}{path}",
                headers={"Authorization": f"Bearer {admin}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        # Unguarded this escapes every caller and the route turns it into a
        # detail-less 500 — on a write, with no restore and no log line.
        if method == "PUT":
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
        detail = ""
        try:
            detail = response.json().get("error") or response.text
        except ValueError:
            detail = response.text
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
) -> dict[str, Any]:
    """Give one holder a new value and write it where that holder reads it.

    `role` and `mind_id` are passed explicitly, never left to be inferred. A
    holder minted before its broker row exists is typed `person` by comms,
    which refuses every machinery route it will ever call — its turn ledger,
    its context report, its rotation — and lucent keys its memory under its
    name while every hook sends its uuid.
    """
    if location is None:
        location = locate(service, holder)
    new_value = secrets.token_urlsafe(32)

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
            )

        await _put(service, holder, username, new_value, role, mind_id)

        with credentials_data._WRITE_LOCK:
            written = credentials_data._write_location(
                location.path, location.env_key, new_value, location.file
            )
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
    holders = await list_holders(service)
    row = next(
        (r for r in holders if str(r.get("holder", "")).lower() == holder.strip().lower()),
        None,
    )
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


async def holder_rows(service: Service) -> list[dict[str, Any]]:
    """One row per holder this service reports, with where it reads and
    whether this console can rotate it."""
    rows: list[dict[str, Any]] = []
    for row in await list_holders(service):
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
                "rotatable": location is not None,
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
    text = credentials_data._read_text(path)
    if credentials_data._read_env_value(path, key) is not None:
        return
    updated = text if text.endswith("\n") or not text else text + "\n"
    credentials_data._write_file_in_place(
        path, updated + f"{key}=\n", "hive_mind", key, text
    )


async def name_held_by_something_else(name: str) -> str | None:
    """Which service already has a holder by this name, if any.

    The credential namespace is holders, which is wider than minds:
    `scheduler`, `telegram-bot` and `spark-to-bloom` hold values and are
    nobody's mind. Minting over one replaces a running app's credential and
    retypes it from `service` to `mind` — and that app reads its value from a
    file this console does not mount, so no restart fixes it.
    """
    for service in SERVICES.values():
        try:
            rows = await list_holders(service)
        except MintRefused:
            # A service that cannot be asked is not a service that said no.
            continue
        if any(str(row.get("holder", "")).lower() == name.strip().lower() for row in rows):
            return service.label
    return None


async def provision_mind(
    name: str, mind_id: str, owner: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Give a brand-new containerised mind its own two credentials.

    Role and provenance are stated, never inferred. A mind is minted before it
    has a broker row — it gets one by registering itself on first boot — and
    comms types a holder it cannot find as a `person`, which is refused every
    machinery route the mind will ever call, including the registration that
    would have fixed it. On the memory-store side the provenance is the mind's
    uuid, because every hook sends `$MIND_ID`; minted under its name instead,
    the mind authenticates and is then refused its own memory.

    Both values land in the shared `hive_mind/.env` under this mind's own
    variables, which is where its generated compose file reads them from.
    """
    minted: list[dict[str, Any]] = []
    failures: list[str] = []
    for service in SERVICES.values():
        try:
            key = variable_name(service, name)
            if "hive_mind" not in ENV_FILES:
                raise MintRefused("no hive_mind env file is configured (HIVE_ENV_FILES)")
            _ensure_key(ENV_FILES["hive_mind"], key)
            minted.append(
                await mint(
                    service,
                    name,
                    owner,
                    role="mind",
                    mind_id=mind_id,
                    location=Location("hive_mind", key),
                )
            )
        except Exception as exc:
            # Each service on its own. One of the two landing is a different
            # state from neither — a mind with a gateway credential and no
            # memory-store one authenticates, answers, and silently remembers
            # nothing — and the caller is told which it has.
            failures.append(f"{service.label}: {exc}")
    return minted, ("; ".join(failures) if failures else None)


async def check(service: Service, holder: str, role: str = "") -> dict[str, Any]:
    """Present this holder's own value to the service that validates it.

    Read off the holder's file rather than out of this process's environment:
    a rotation writes the file, and the question the operator is asking is
    whether what that box will load on its next start is accepted.

    "Could not reach the service" is never reported as a refusal. The remedy
    for a refusal is a new credential, and applying that to a value sitting
    behind an outage destroys a working one.
    """
    if role == "person":
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
    path = PROBE_PATHS.get(service.key)
    if path is None:
        return {"status": "undetermined", "detail": "nothing validates this over HTTP"}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(
                f"{service.url()}{path}", headers={"Authorization": f"Bearer {value}"}
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
