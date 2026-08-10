"""Startup creates the configured account when nothing else has.

The hive deployment shares its auth database with the hive site, which
owns it and creates users. A standalone deployment has no such sibling,
so without this it comes up with an empty users table and a login page
that reports every correct password as wrong.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    """A deployment with nobody else to have made an account."""
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-only-signing-key")
    monkeypatch.delenv("HIVE_COOKIE_DOMAIN", raising=False)
    return tmp_path


def load_app():
    import auth
    import main

    importlib.reload(auth)
    return importlib.reload(main)


def test_startup_creates_the_configured_account(standalone, monkeypatch):
    """Asserted through the app's own startup, not by calling the helper.

    Calling `seed_first_user()` directly tests a function that already
    existed and was already covered; the change under test is that
    starting the application performs it.
    """
    monkeypatch.setenv("HIVE_SEED_USERNAME", "daniel")
    monkeypatch.setenv("HIVE_SEED_PASSWORD", "a-real-password")
    main = load_app()

    with TestClient(main.app) as client:
        response = client.post(
            "/login",
            data={"username": "daniel", "password": "a-real-password"},
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    assert "hive_session" in response.cookies


def test_the_seeded_account_is_an_administrator(standalone, monkeypatch):
    monkeypatch.setenv("HIVE_SEED_USERNAME", "daniel")
    monkeypatch.setenv("HIVE_SEED_PASSWORD", "a-real-password")
    main = load_app()

    with TestClient(main.app):
        import auth

        user = auth.get_user_by_username("daniel")

    assert user is not None
    assert user["is_admin"]


def test_startup_creates_nothing_when_no_credentials_are_configured(
    standalone, monkeypatch
):
    """The hive deployment's case: the variables are unset and this is inert."""
    monkeypatch.delenv("HIVE_SEED_USERNAME", raising=False)
    monkeypatch.delenv("HIVE_SEED_PASSWORD", raising=False)
    main = load_app()

    with TestClient(main.app):
        import auth

        conn = auth._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()

    assert count == 0


def test_a_second_start_does_not_replace_an_existing_account(standalone, monkeypatch):
    """Changing the configured password later must not reset the account.

    Otherwise every restart silently rewrites the administrator's
    password to whatever is in the environment, and an operator who
    rotated it by hand is locked out by a reboot.
    """
    monkeypatch.setenv("HIVE_SEED_USERNAME", "daniel")
    monkeypatch.setenv("HIVE_SEED_PASSWORD", "the-original")
    main = load_app()
    with TestClient(main.app):
        pass

    monkeypatch.setenv("HIVE_SEED_PASSWORD", "a-different-one")
    main = load_app()
    with TestClient(main.app) as client:
        response = client.post(
            "/login",
            data={"username": "daniel", "password": "the-original"},
            follow_redirects=False,
        )

    assert "hive_session" in response.cookies
