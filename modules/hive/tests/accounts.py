"""Set up a built-in account from a synchronous test.

`auth.create_user` and `auth.create_session_token` are coroutines now that
the account store is, and most of this suite is synchronous: it drives the
console through its own HTTP surface, which `TestClient` runs on a loop of
its own. These two run the coroutine on a loop of their own so a test can go
on reading as the setup step it is.

Safe to do here and nowhere else. On the SQLite half — which the whole suite
runs on, see `conftest._isolated_hive_db` — the store is one connection
pinned to one worker thread, reached through an executor rather than through
anything bound to a particular event loop, so a write made from this loop is
a write the next request sees. The Postgres half has a pool bound to the loop
that opened it, which is exactly why no production path does this.
"""

from __future__ import annotations

import asyncio
from typing import Any

import auth


def make_user(username: str, password: str, *, is_admin: bool = False,
              replace: bool = False) -> dict[str, Any]:
    """A built-in account with a settled password."""
    return asyncio.run(
        auth.create_user(username, password, is_admin=is_admin,
                         replace=replace))


def session_token(user: dict[str, Any]) -> str:
    """A built-in session cookie value for an existing account."""
    return asyncio.run(auth.create_session_token(user))


def seed_first_user() -> None:
    """Create the initial admin from the environment, as the lifespan does."""
    asyncio.run(auth.seed_first_user())
