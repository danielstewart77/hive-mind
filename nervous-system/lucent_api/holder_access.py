"""Which mind is talking to the memory store, decided by the value it presented.

Lucent began by trusting the caller. `mind_id` arrived in the request body —
provenance on a write, an opt-in filter on a read — behind one shared
`LUCENT_BEARER_TOKEN` that every mind, every hook and every background process
on every host already held. So any holder of that value could file a memory
under any mind's name and read every mind's memories, and the default on
`/memory/store` was `mind_id="ada"`, which is how an unattributed write got
somebody's name on it.

## The credential names the holder, and the holder is the mind

A **holder** is one box: a mind, a service, or a person. Each holds its own
opaque value, stored here as a SHA-256 digest and held in plaintext by nothing
but the holder. For a holder whose role is `mind`, the holder *is* the mind —
so which mind a memory is filed under is read off the credential and the body
cannot say otherwise.

Keying on the mind rather than on its owner is deliberate and is not the same
choice comms makes. Comms decides *whose conversation this is*, which is a
question about a person; ten of this hive's twelve minds belong to one person,
so answering provenance with the owner would file ten minds' memories under
one name and leave every per-mind read returning the union or nothing. A
conversation answers to a person. A memory answers to the mind that formed it.

## A service reads on behalf of a mind, and says so

Comms composes every mind's system prompt — soul from the graph, decay-weighted
recent memory — which means one service legitimately reads many minds' records.
That is a third shape, neither "trust the body" nor "only your own": a holder
whose role is `service`, and an admin, may name the mind it is acting for, and
nothing else may. A `mind` holder naming any mind but itself is refused rather
than quietly rescoped, because a mind that asks for somebody else's memory has
either been misconfigured or been taken.

## The `shared` sentinel is not a mind and is kept on purpose

Hive-wide standing rules are written with `mind_id` set to the literal
`shared`, and every mind's contextual retrieval reads its own plus that one.
It has no credential and no owner, so deriving provenance strictly from the
holder would convert every hive-wide rule into a private one and make the
seven already on disk unreadable to everybody. A `mind` holder may therefore
write `shared` explicitly, and every holder reads it alongside its own.

## There is no bypass

An unset token used to mean "serve everything to everybody", as a convenience
for a container that had not been configured yet. A store that cannot
establish who is calling refuses: 503, naming the reason, rather than handing
every mind's memory to the LAN while it waits for an operator to notice.

`LUCENT_ADMIN_BEARER_TOKEN` is kept, and is the one credential that is still a
shared value. It has to be: it is how the console reaches the store, and it is
the only way the mint route is reachable on a first boot where the credentials
table is necessarily empty.

The *variable* holders present is still `LUCENT_BEARER_TOKEN`, deliberately.
It is the same slot it always was, and renaming it would mean editing every
hook, skill and service on every host to say the same thing differently. What
changed is underneath: the store no longer holds a copy to compare against, so
each host's variable carries that holder's own value and is checked against the
table. The rollout is therefore a different value written into a variable that
already exists, and a holder that was missed is refused rather than quietly
sharing somebody else's identity.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException

#: What a holder is. A mind or a service is the machinery; a person is not.
MIND = "mind"
SERVICE = "service"
PERSON = "person"
ROLES = (MIND, SERVICE, PERSON)

#: Not a mind. The provenance hive-wide standing rules are written under, read
#: by every mind alongside its own.
SHARED = "shared"

CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS credentials (
    -- NOT NULL is load-bearing: SQLite permits NULL in a non-INTEGER PRIMARY
    -- KEY column unless it is declared, so a row naming no holder would
    -- insert cleanly and then resolve by digest exactly as a real one does.
    holder      TEXT PRIMARY KEY NOT NULL,
    username    TEXT NOT NULL,
    role        TEXT NOT NULL,
    -- UNIQUE because two holders sharing one value is the resolver returning
    -- whichever row SQLite yields first, i.e. one holder silently
    -- authenticating as another.
    token_sha   TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    revoked_at  REAL
);
CREATE INDEX IF NOT EXISTS credentials_username ON credentials (username);
"""


@dataclass(frozen=True)
class Caller:
    """Who the store decided is asking."""

    holder: str = ""
    username: str = ""
    role: str = ""
    is_admin: bool = False

    @property
    def recognised(self) -> bool:
        return bool(self.holder) or self.is_admin

    @property
    def may_act_for_another_mind(self) -> bool:
        """An admin, or a service acting on a mind's behalf.

        Comms composing a prompt is the whole of this: it reads each mind's
        soul and recent memory to build the dispatch. A `mind` holder is
        excluded on purpose — a mind asking for another mind's record is
        either misconfigured or compromised, and there is no third reading.
        """
        return self.is_admin or self.role == SERVICE

    def mind_for_write(self, named: object = None) -> str:
        """Which mind a write is filed under.

        For a mind, itself — the body cannot say otherwise, except to name the
        `shared` sentinel explicitly. For a service or an admin, the mind it
        named, because it has no mind of its own to file under.
        """
        asked = str(named or "").strip()
        if self.role == MIND:
            if asked and asked.lower() == SHARED:
                return SHARED
            if asked and asked.lower() not in (self.holder, SHARED):
                raise HTTPException(
                    403,
                    "a mind files memories under itself; "
                    f"{self.holder!r} may not write as {asked!r}",
                )
            return self.holder
        if not self.may_act_for_another_mind:
            raise HTTPException(
                403, "this credential has no mind to file a memory under"
            )
        if not asked:
            raise HTTPException(
                400, "a service writing on a mind's behalf must name the mind"
            )
        return asked

    def minds_for_read(self, named: object = None) -> list[str] | None:
        """Which minds a read may see, or None for every one of them.

        A mind sees itself and the `shared` sentinel. A service or an admin
        sees the mind it named, or everything when it names none — which is
        what the console's own pages and the nightly pruner need.
        """
        asked = str(named or "").strip()
        if self.role == MIND:
            if asked and asked.lower() not in (self.holder, SHARED):
                raise HTTPException(
                    403,
                    f"{self.holder!r} may not read {asked!r}'s memory",
                )
            if asked and asked.lower() == SHARED:
                return [SHARED]
            return [self.holder, SHARED]
        if not self.may_act_for_another_mind:
            raise HTTPException(403, "this credential reaches no mind's memory")
        return [asked] if asked else None


def digest(token: str) -> str:
    """The stored form. A value this process is handed is never written down
    as it arrived, so a reader of the table cannot present what it finds."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_holder(name: object) -> str:
    return str(name or "").strip().lower()


def init_credentials(conn: sqlite3.Connection) -> None:
    conn.executescript(CREDENTIALS_DDL)
    conn.commit()


def put_credential(
    conn: sqlite3.Connection,
    holder: str,
    username: str,
    token: str,
    role: str = MIND,
) -> dict:
    """Mint or replace one holder's credential.

    Replacing clears a revocation: an operator issuing a new value has not
    asked for it to arrive dead.
    """
    key = normalize_holder(holder)
    if not key:
        raise ValueError("a credential names the holder it belongs to")
    name = normalize_holder(username)
    if not name:
        raise ValueError("a credential names the user its holder answers to")
    if not token:
        raise ValueError("a credential needs a value")
    if key == SHARED:
        raise ValueError(
            "'shared' is a provenance every mind writes to, not a holder"
        )
    if role not in ROLES:
        raise ValueError(f"unknown credential role: {role!r}")
    sha = digest(token)
    clash = conn.execute(
        "SELECT holder FROM credentials WHERE token_sha = ? AND holder <> ?",
        (sha, key),
    ).fetchone()
    if clash:
        raise ValueError(
            f"that value is already held by {clash[0]!r}; "
            "a credential belongs to one holder"
        )
    now = time.time()
    conn.execute(
        "INSERT INTO credentials "
        "(holder, username, role, token_sha, created_at, revoked_at) "
        "VALUES (?, ?, ?, ?, ?, NULL) "
        "ON CONFLICT(holder) DO UPDATE SET "
        "username=excluded.username, role=excluded.role, "
        "token_sha=excluded.token_sha, created_at=excluded.created_at, "
        "revoked_at=NULL",
        (key, name, role, sha, now),
    )
    conn.commit()
    return {"holder": key, "username": name, "role": role, "created_at": now}


def revoke_credential(conn: sqlite3.Connection, holder: str) -> bool:
    """Mark one holder's credential dead, and only that holder's."""
    cursor = conn.execute(
        "UPDATE credentials SET revoked_at = ? "
        "WHERE holder = ? AND revoked_at IS NULL",
        (time.time(), normalize_holder(holder)),
    )
    conn.commit()
    return cursor.rowcount > 0


def credential_for_token(conn: sqlite3.Connection, token: str) -> Caller | None:
    """The holder a presented value belongs to, or None.

    Read per call, never cached: a revocation that needs a restart to land is
    not a revocation.
    """
    if not token:
        return None
    presented = digest(token)
    rows = conn.execute(
        "SELECT holder, username, role, token_sha FROM credentials "
        "WHERE revoked_at IS NULL"
    ).fetchall()
    for holder, username, role, token_sha in rows:
        # Constant-time, on bytes: `compare_digest` raises TypeError on a str
        # holding anything outside ASCII.
        if hmac.compare_digest(
            presented.encode("ascii"), str(token_sha).encode("ascii")
        ):
            return Caller(
                holder=normalize_holder(holder),
                username=normalize_holder(username),
                role=str(role or PERSON),
            )
    return None


def _bearer(authorization: str) -> str:
    """The presented value, decoded the same way on every path.

    Decoded as the header arrived rather than re-encoded: a value pasted onto
    a Windows box through a document picks up a non-breaking hyphen or a smart
    quote without showing it, and digesting the two spellings differently
    refuses that holder forever with the same message a wrong value gives.
    """
    if not authorization.startswith("Bearer "):
        return ""
    return authorization[7:].strip()


def resolve_caller(conn: sqlite3.Connection, token: str) -> Caller:
    admin = os.environ.get("LUCENT_ADMIN_BEARER_TOKEN", "")
    if admin and token and hmac.compare_digest(
        token.encode("utf-8"), admin.encode("utf-8")
    ):
        return Caller(is_admin=True, role=SERVICE)
    found = credential_for_token(conn, token)
    return found or Caller()


def _store_configured(conn: sqlite3.Connection) -> bool:
    if os.environ.get("LUCENT_ADMIN_BEARER_TOKEN"):
        return True
    row = conn.execute(
        "SELECT 1 FROM credentials WHERE revoked_at IS NULL LIMIT 1"
    ).fetchone()
    return row is not None


def connection() -> sqlite3.Connection:
    """Where the credentials table lives.

    Its own function rather than a direct call into `lucent`, so a test can
    point the credential store at a connection without redirecting every
    other reader of the database — which is how patching the store's one
    connection factory took the schema tables down with it.
    """
    from lucent_api import lucent

    return lucent._get_connection()


def require_holder(authorization: str = Header(default="")) -> Caller:
    """The FastAPI dependency every memory and graph route is gated by.

    Returns the caller rather than None, because which mind is asking is the
    answer the routes need and reading it off the body is the thing this
    replaced.
    """
    conn = connection()
    init_credentials(conn)
    if not _store_configured(conn):
        # Not a 401: nothing the caller presented was wrong. The store cannot
        # establish who anybody is, and serving open while it waits is how
        # every mind's memory ends up readable from the LAN.
        raise HTTPException(
            503,
            "Memory store has no credentials configured and will not serve open",
        )
    token = _bearer(authorization)
    if not token:
        raise HTTPException(401, "Missing or invalid Authorization header")
    caller = resolve_caller(conn, token)
    if not caller.recognised:
        raise HTTPException(401, "Invalid token")
    return caller


def require_admin(caller: Caller = None) -> Caller:  # pragma: no cover - thin
    if caller is None or not caller.is_admin:
        raise HTTPException(403, "Admin credential required")
    return caller
