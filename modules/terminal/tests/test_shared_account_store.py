"""The terminal signs in against the hive's store, and a change there lands.

The requirements this guards are the ones the move was for. The terminal
used to open `hive.db` itself, with its own `sqlite3` calls and its own
signing key minted into that file, while the console moved onto a store
named by `HIVE_DATABASE_URL`. Two halves of one account store then disagree,
and what that looks like from a chair is being signed out of the terminal
and nowhere else.

So: an account made where the console makes it can sign in here, and a
password reset done where the console does it ends the session held here.
The store is the hive's Postgres — here the suite's scratch database.
"""

from __future__ import annotations

import asyncio
import os

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


# ── one store, shared with the console ──────────────────────────────────


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
    # And the account is in the database the console reads, not in one only
    # this site knows about: within one process the sign-in alone cannot
    # tell one store from two, the database itself can.
    assert _usernames_in_the_database("sloan") == ["sloan"]


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


def _usernames_in_the_database(username: str) -> list[str]:
    """Read straight out of Postgres, not through the store."""
    import asyncpg

    async def read() -> list[str]:
        conn = await asyncpg.connect(accounts.refuse_unless_scratch(
            os.environ["HIVE_DATABASE_URL"]))
        try:
            rows = await conn.fetch(
                "SELECT username FROM core.users WHERE username = $1",
                username)
        finally:
            await conn.close()
        return [r["username"] for r in rows]

    return asyncio.run(read())


# ── through the app's own lifespan ──────────────────────────────────────


@pytest.fixture
def postgres_terminal(monkeypatch, fake_comms):
    """This site signed in through its own pages, its lifespan seeding.

    `TestClient` as a context manager, so the app's own lifespan opens the
    pool and seeds the first account, on the suite's loop that every request
    in the test runs on.
    """
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "builtin")
    monkeypatch.setenv("HIVE_SECRET_KEY", "postgres-half-test-secret")
    monkeypatch.setenv("HIVE_SEED_USERNAME", "daniel")
    monkeypatch.setenv("HIVE_SEED_PASSWORD", "daniels-own-password")
    auth.forget_secret_key()
    yield


def test_the_terminal_signs_in_against_postgres(postgres_terminal):
    """Requirement one, through the whole real path.

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

    assert _usernames_in_the_database("daniel") == ["daniel"]


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


# ── what this site opens ────────────────────────────────────────────────


def test_a_terminal_pointed_at_a_database_it_cannot_reach_refuses_to_start(
        monkeypatch):
    """A site that came up anyway would serve from nothing, show an empty
    hive, and report itself healthy."""
    # The suite's own fixture has a store open; this asks about a fresh open.
    accounts.run(store.close())
    monkeypatch.setenv(
        "HIVE_DATABASE_URL",
        "postgresql://nobody:nothing@127.0.0.1:1/not-a-database")

    from hive_data.errors import Unreachable

    with pytest.raises(Unreachable):
        accounts.run(store.open())


def test_a_cookie_minted_before_the_store_was_rebuilt_never_signs_in_as_whoever_holds_its_id(
        scratch_database, monkeypatch):
    """A cookie outlives the rows it was minted against.

    The store's ids are its own sequence, so a store rebuilt beneath a live
    cookie hands that cookie's number to somebody else. A cookie minted for
    one person must not resolve to a different person merely because the
    number matches — the second can be an admin.
    """
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "builtin")
    monkeypatch.setenv("HIVE_SECRET_KEY", "one-key-two-stores")

    newcomer = accounts.make_user("newjoiner", "newjoiners-pass")
    cookie = accounts.session_token(newcomer)

    accounts.run(store.close())
    accounts.run(scratch_database.reset())
    accounts.run(store.open())
    holder = accounts.make_user("contractor", "contractors-pass", is_admin=True)
    assert holder["id"] == newcomer["id"], "the ids have to collide to test this"

    resolved = accounts.run(auth.get_store().user_for_token(cookie))

    assert resolved is None
