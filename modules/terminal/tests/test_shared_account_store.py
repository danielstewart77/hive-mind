"""The terminal signs in against the hive's store, and a change there lands.

The requirements this guards are the ones the move was for. The terminal
used to open `hive.db` itself, with its own `sqlite3` calls and its own
signing key minted into that file, while the console moved onto a store
named by `HIVE_DATABASE_URL`. Two halves of one account store then disagree,
and what that looks like from a chair is being signed out of the terminal
and nowhere else.

So: an account made where the console makes it can sign in here, a password
reset done where the console does it ends the session held here, and the
store answering is whichever one this site's own variable names — on
Postgres as well as on the file, because the file half passing proves
nothing about the half the hive is moving to.

The Postgres tests need a server to create a scratch database on and are
skipped without `HIVE_DATA_TEST_ADMIN_DSN`, which has no default because a
default would be a credential in a tracked file.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from starlette.testclient import TestClient

import auth
import main
import store
from hive_site import testing as accounts


def _client() -> TestClient:
    return TestClient(main.app, base_url="https://testserver")


def _sign_in(client: TestClient, username: str, password: str):
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": "/terminal"},
        follow_redirects=False,
    )


# ── the file half, which every other test in this suite runs on ─────────


def test_an_account_made_where_the_console_makes_it_signs_in_here(
        builtin_terminal):
    """One store, not two.

    The account is created through the shared store — the operation the
    console's admin page calls — and then signed in at this site's own login
    page. A terminal holding a store of its own answers "invalid username or
    password" here, which is the truthful-looking message for entirely the
    wrong reason.
    """
    accounts.run(auth.get_store().create_user(
        "sloan", temp_password="sloans-temporary"))  # secret-guard: allow

    response = _sign_in(_client(), "sloan", "sloans-temporary")

    assert response.status_code == 303, response.text
    # Straight to the password page, because the temporary one must change.
    assert response.headers["location"] == "/password"
    assert auth.SESSION_COOKIE_NAME in response.cookies
    # And the account is in the file the console names, not in one only this
    # site knows about: within one process the sign-in alone cannot tell one
    # store from two, the console's own file can.
    import os
    import sqlite3

    console_file = sqlite3.connect(os.environ["HIVE_DB_PATH"])
    try:
        rows = console_file.execute(
            "SELECT username FROM users WHERE username = 'sloan'").fetchall()
    finally:
        console_file.close()
    assert rows == [("sloan",)]


def test_a_cookie_signed_by_one_site_verifies_on_the_other(
        builtin_terminal, monkeypatch):
    """The signing key is the store's, not the process's.

    The terminal used to mint a key of its own into its own file, so each
    site refused the other's cookie. Here the key is minted, the remembered
    copy is dropped — which is what the other process looks like — and the
    cookie must still verify, because the key is read back out of the store
    rather than invented again.
    """
    monkeypatch.delenv("HIVE_SECRET_KEY", raising=False)
    auth.forget_secret_key()
    user = accounts.make_user("sloan", "sloans-own-pass")
    token = accounts.session_token(user)

    auth.forget_secret_key()
    payload = accounts.run(auth.read_session_token(token))

    assert payload is not None
    assert payload["username"] == "sloan"


def test_a_password_reset_where_the_console_does_it_ends_the_session_here(
        builtin_terminal):
    """The defect this story exists to close, driven end to end.

    The reset moves `session_version` in the store. A terminal reading that
    column out of a different file honours the old value, keeps serving the
    browser, and the operator sees one site signed out and the other not.
    """
    signed_in = builtin_terminal.settled_user("sloan", "sloans-own-pass")
    assert signed_in.get("/terminal", follow_redirects=False).status_code == 200

    accounts.run(auth.get_store().reset_password("sloan", "a-reset-password"))

    turned_away = signed_in.get("/terminal", follow_redirects=False)
    assert turned_away.status_code == 303
    assert turned_away.headers["location"].startswith("/login")
    assert signed_in.get("/api/minds").status_code == 401


# ── the half the hive is moving to ──────────────────────────────────────


@pytest.fixture
def postgres_terminal(monkeypatch, fake_comms, tmp_path):
    """This site on a scratch Postgres, signed in through its own pages.

    `TestClient` as a context manager, so the app's own lifespan opens the
    pool on the portal's loop and every request in the test runs on that
    same loop. A pool opened anywhere else belongs to a loop the requests
    are not on, which is why the rest of this suite stays on the file.
    """
    if not accounts.ADMIN_DSN:
        pytest.skip(accounts.NO_SERVER)
    name, dsn = accounts.run(accounts.create_scratch_database("terminal_site"))
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "builtin")
    monkeypatch.setenv("HIVE_DATABASE_URL", dsn)
    # Named, so a stray SQLite open has somewhere to go — SQLite creates a
    # missing file and answers, so this is not a guard by itself. The tests
    # prove Postgres answered by reading the row back out of Postgres.
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "must-not-be-used.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "postgres-half-test-secret")
    monkeypatch.setenv("HIVE_SEED_USERNAME", "daniel")
    monkeypatch.setenv("HIVE_SEED_PASSWORD", "daniels-own-password")
    auth.forget_secret_key()
    try:
        yield dsn
    finally:
        accounts.run(accounts.drop_scratch_database(name))


def test_the_terminal_signs_in_against_postgres(postgres_terminal):
    """Requirement one, on the store the hive is cutting over to.

    The account is the one this site's lifespan seeds, so the whole path is
    the real one: seed, sign in, hold a session, reach a signed-in route.
    """
    with _client() as client:
        signed_in = _sign_in(client, "daniel", "daniels-own-password")
        assert signed_in.status_code == 303, signed_in.text
        assert signed_in.headers["location"] == "/terminal"

        page = client.get("/terminal", follow_redirects=False)
        assert page.status_code == 200

        signed_out = client.post("/logout", follow_redirects=False)
        assert signed_out.status_code == 303
        assert client.get("/terminal", follow_redirects=False).status_code == 303

    import asyncpg

    async def seeded() -> list:
        conn = await asyncpg.connect(postgres_terminal)
        try:
            return await conn.fetch(
                "SELECT username FROM core.users WHERE username = 'daniel'")
        finally:
            await conn.close()

    assert [r["username"] for r in asyncio.run(seeded())] == ["daniel"]


@pytest.mark.parametrize("action", ["reset", "disable", "change"])
def test_what_the_console_does_to_an_account_on_postgres_ends_the_session_here(
        postgres_terminal, action):
    """Requirement three, on Postgres, through the console's own operations.

    Each is the store operation the console's admin and password pages call,
    run on the loop the app's lifespan opened the pool on — the portal —
    because a pool belongs to the loop that opened it.
    """
    with _client() as client:
        assert _sign_in(client, "daniel",
                        "daniels-own-password").status_code == 303
        assert client.get("/terminal",
                          follow_redirects=False).status_code == 200
        held = client.cookies.get(auth.SESSION_COOKIE_NAME)

        account_store = auth.get_store()
        if action == "reset":
            client.portal.call(account_store.reset_password, "daniel",
                               "a-reset-password")
        elif action == "disable":
            # The last enabled admin cannot be disabled, rightly, so there is
            # a second one first.
            client.portal.call(lambda: auth.create_user(
                "deputy", "deputys-own-password", is_admin=True))
            client.portal.call(
                lambda: account_store.update_user("daniel", disabled=True))
        else:
            # The console's own password page, from another browser holding
            # the same account: it mints that browser a fresh cookie and must
            # end this one.
            token = client.portal.call(
                account_store.change_password, held,
                "daniels-own-password", "daniels-newer-password")
            assert token != held

        client.cookies.set(auth.SESSION_COOKIE_NAME, held)
        turned_away = client.get("/terminal", follow_redirects=False)
        assert turned_away.status_code == 303
        assert turned_away.headers["location"].startswith("/login")


# ── which store answers, and the way back ───────────────────────────────


def test_a_terminal_pointed_at_a_database_it_cannot_reach_refuses_to_start(
        tmp_path, monkeypatch):
    """It must not come up on the file beside it.

    A site that silently fell back would serve from a store nothing else
    reads, show an empty hive, and report itself healthy — the exact failure
    the fence in 1.2.18 was built for, arrived at from the other direction.
    """
    monkeypatch.setenv(
        "HIVE_DATABASE_URL",
        "postgresql://nobody:nothing@127.0.0.1:1/not-a-database")
    # A path of its own, not the one the suite's own fixture has already
    # opened a store on: the question is whether this open creates a file.
    fallback = tmp_path / "fallback" / "hive.db"
    monkeypatch.setenv("HIVE_DB_PATH", str(fallback))

    from hive_data.errors import Unreachable

    with pytest.raises(Unreachable):
        asyncio.run(store.open())

    assert not fallback.exists(), "it created the file it was told not to use"


def test_unsetting_the_variable_returns_this_site_to_the_file(
        tmp_path, monkeypatch):
    """Requirement four: the revert, and the two sites pointed apart.

    The choice is read when this process opens its store and from this
    process's own environment, which is what lets the terminal be put back
    on the file while the console stays on Postgres.
    """
    # The suite's fixture already has a store open on `tmp_path/hive.db`;
    # close it and name a different file, so what is asserted is this open.
    asyncio.run(store.close())
    reverted = tmp_path / "reverted" / "hive.db"
    monkeypatch.setenv("HIVE_DATABASE_URL", "")
    monkeypatch.setenv("HIVE_DB_PATH", str(reverted))

    asyncio.run(store.open())
    try:
        assert not store.on_postgres()
        assert reverted.is_file()
        # And it is a working store, not merely a chosen one.
        made = accounts.make_user("sloan", "sloans-own-pass")
        assert accounts.run(store.get_user("sloan"))["id"] == made["id"]
    finally:
        asyncio.run(store.close())


def test_a_cookie_from_another_store_never_signs_in_as_whoever_holds_its_id(
        tmp_path, monkeypatch):
    """Two stores, one signing key, independently numbered accounts.

    Pointing the two sites apart is permitted, and their account ids then
    drift. A cookie minted for one person on the first store must not
    resolve to a different person on the second merely because the number
    matches — on the second store that number can be an admin.
    """
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "builtin")
    monkeypatch.setenv("HIVE_SECRET_KEY", "one-key-two-stores")
    asyncio.run(store.close())

    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "console" / "hive.db"))
    asyncio.run(store.open())
    newcomer = accounts.make_user("newjoiner", "newjoiners-pass")
    cookie = accounts.session_token(newcomer)
    asyncio.run(store.close())

    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "terminal" / "hive.db"))
    asyncio.run(store.open())
    holder = accounts.make_user("contractor", "contractors-pass", is_admin=True)
    assert holder["id"] == newcomer["id"], "the ids have to collide to test this"

    resolved = accounts.run(auth.get_store().user_for_token(cookie))

    assert resolved is None
