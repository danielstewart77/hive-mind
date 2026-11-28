"""Where the database is, as one value per process.

A service names no host, no port and no file. It names nothing at all: it
reads this configuration, which reads the environment, so pointing a service
at a different database is an environment change and not an edit.

One value *per process*, not one for the hive. The hive's own Postgres
publishes no host port and answers on a container network, while the edge
minds run in a host venv and the console runs in a third place - so "the
address of the database" is genuinely different text in each of them, and a
single shared constant would be wrong for two of the three.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import NotConfigured

#: The environment variable naming the database. A URL rather than a set of
#: parts, because a URL is one thing to get wrong instead of five.
DSN_VAR = "HIVE_DATABASE_URL"

#: Pool bounds. The server's `max_connections` is the real ceiling and it is
#: shared: comms, the console, lucent, hive-tools and any edge mind pointed
#: here all draw from it. A default of 10 against a ceiling of 100 leaves
#: room for five such services plus the superuser reservation, which is why
#: the maximum is configuration rather than a constant somebody has to find.
POOL_MIN_VAR = "HIVE_DATABASE_POOL_MIN"
POOL_MAX_VAR = "HIVE_DATABASE_POOL_MAX"
DEFAULT_POOL_MIN = 1
DEFAULT_POOL_MAX = 10

#: Deadlines, in seconds. Every one of these exists because the live server
#: has `statement_timeout` and `idle_in_transaction_session_timeout` set to
#: zero, so without them a Postgres restart under a live pool leaves a
#: connection blocked in `recv` forever and pool exhaustion makes every
#: operation wait on `acquire` with no deadline. Both of those present to the
#: operator as a mind still thinking, which is the one failure mode nothing
#: on screen can explain.
CONNECT_TIMEOUT_VAR = "HIVE_DATABASE_CONNECT_TIMEOUT"
STATEMENT_TIMEOUT_VAR = "HIVE_DATABASE_STATEMENT_TIMEOUT"
ACQUIRE_TIMEOUT_VAR = "HIVE_DATABASE_ACQUIRE_TIMEOUT"
TRANSACTION_TIMEOUT_VAR = "HIVE_DATABASE_TRANSACTION_TIMEOUT"
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_STATEMENT_TIMEOUT = 30.0
DEFAULT_ACQUIRE_TIMEOUT = 10.0
#: A unit of work may not idle in a transaction longer than this. Enforced by
#: the server as well as by us, because the process holding it may be the one
#: that has stopped running.
DEFAULT_TRANSACTION_TIMEOUT = 60.0


def _number(var: str, default: float) -> float:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise NotConfigured(
            f"{var}={raw!r} is not a number of seconds.") from None
    if value <= 0:
        raise NotConfigured(
            f"{var}={raw!r}: a deadline of zero or less is not a deadline. "
            "Unset it to take the default rather than disabling it.")
    return value


def _count(var: str, default: int) -> int:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise NotConfigured(f"{var}={raw!r} is not a number.") from None
    if value < 1:
        raise NotConfigured(f"{var}={raw!r}: a pool holds at least one.")
    return value


@dataclass(frozen=True)
class Settings:
    """Everything this package needs to reach a database."""

    dsn: str
    pool_min: int = DEFAULT_POOL_MIN
    pool_max: int = DEFAULT_POOL_MAX
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    statement_timeout: float = DEFAULT_STATEMENT_TIMEOUT
    acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT
    transaction_timeout: float = DEFAULT_TRANSACTION_TIMEOUT

    def __post_init__(self) -> None:
        if self.pool_max < self.pool_min:
            raise NotConfigured(
                f"pool maximum {self.pool_max} is below its minimum "
                f"{self.pool_min}.")

    @property
    def describes(self) -> str:
        """The database this is pointed at, with no credential in it.

        Reported when the pool opens. The target is a configuration value, so
        a service can be pointed at a database that is loaded, half-loaded or
        empty and answer plausibly either way - a session listing that is
        8,000 rows short looks exactly like a quiet week. The log line is how
        that is noticed.
        """
        rest = self.dsn.split("://", 1)[-1]
        if "@" in rest:
            rest = rest.rsplit("@", 1)[-1]
        return rest or "an unnamed database"


def from_environment() -> Settings:
    """The settings this process is configured with."""
    dsn = os.environ.get(DSN_VAR, "").strip()
    if not dsn:
        raise NotConfigured(
            f"{DSN_VAR} is unset, so there is no database to reach. It is "
            "named in the environment rather than passed in, because a "
            "service that could pass a host could also hard-code one.")
    return Settings(
        dsn=dsn,
        pool_min=_count(POOL_MIN_VAR, DEFAULT_POOL_MIN),
        pool_max=_count(POOL_MAX_VAR, DEFAULT_POOL_MAX),
        connect_timeout=_number(CONNECT_TIMEOUT_VAR, DEFAULT_CONNECT_TIMEOUT),
        statement_timeout=_number(
            STATEMENT_TIMEOUT_VAR, DEFAULT_STATEMENT_TIMEOUT),
        acquire_timeout=_number(ACQUIRE_TIMEOUT_VAR, DEFAULT_ACQUIRE_TIMEOUT),
        transaction_timeout=_number(
            TRANSACTION_TIMEOUT_VAR, DEFAULT_TRANSACTION_TIMEOUT),
    )
