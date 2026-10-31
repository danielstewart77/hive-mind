"""Put src/ on the import path for every test module, not just the ones that
happen to insert it themselves. Without this, a single test file can only be
collected when another module has already run its own sys.path insert.

Also point HIVE_DB_PATH at a per-test temporary file for every test in the
suite. The default resolves to the repo's own data/hive.db, which on a
developer's machine is the live console database holding the real board and
the real logins — a test that forgets to redirect it does not fail, it
quietly edits production and reports green.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _isolated_hive_db(tmp_path, monkeypatch):
    """Every test on its own file, on the SQLite half, with the store open.

    The path first: the default resolves to the repo's own data/hive.db,
    which on a developer's machine is the live console database holding the
    real board and the real logins — a test that forgets to redirect it does
    not fail, it quietly edits production and reports green.

    The half second. `store.open` picks its backend from
    `HIVE_DATABASE_URL`, and on the machine that runs the console that
    variable is exported, so without this the whole suite would silently run
    against the hive's live Postgres. The Postgres half is covered on
    purpose elsewhere: `hive_data`'s own suite exercises every operation
    against a real Postgres, and `tests/test_store_parity.py` here runs the
    two halves side by side and compares what they answer.

    And the open. Nothing opens the store on demand, so a test reaching a
    route without this gets the refusal that says the application's lifespan
    never ran. Opened here rather than by entering the `TestClient` as a
    context manager, because the SQLite half is a connection on its own
    worker thread and so is reachable from whichever loop each request
    happens to run on, while every test in the suite builds its client
    without a `with`.
    """
    import asyncio

    import store

    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_DATABASE_URL", "")
    asyncio.run(store.open())
    try:
        yield
    finally:
        asyncio.run(store.close())


# ── multi-user: one console, either account store ─────────────────────

from multiuser_support import (  # noqa: E402
    AUTH_SERVICE_ADMIN_TOKEN,
    AUTH_SERVICE_REF,
    AUTH_SERVICE_REPO,
    SEED_PASSWORD,
    SEED_USERNAME,
    AuthServiceProcess,
    FakeComms,
    auth_service_ref_available,
    export_auth_service,
)


@pytest.fixture(scope="session")
def fake_comms():
    with FakeComms() as server:
        yield server


@pytest.fixture(scope="session")
def auth_service_source(tmp_path_factory):
    # A missing ref fails rather than skips: a skipped half of the shared
    # suite reads as green while one store goes untested.
    if not auth_service_ref_available():
        pytest.fail(
            f"auth-service ref {AUTH_SERVICE_REF!r} not found in {AUTH_SERVICE_REPO}; "
            "set AUTH_SERVICE_REF to the ref the console is built against"
        )
    return export_auth_service(tmp_path_factory.mktemp("auth-service"))


class Console:
    """The console under test, signed into through its own sign-in page."""

    def __init__(self, backend: str, auth_service=None):
        self.backend = backend
        self.auth_service = auth_service

    def client(self):
        from starlette.testclient import TestClient

        import main

        return TestClient(main.app, base_url="https://testserver")

    def sign_in(self, username: str, password: str):
        client = self.client()
        response = client.post(
            "/login",
            data={"username": username, "password": password, "next": "/overview"},
            follow_redirects=False,
        )
        return client, response

    def admin(self):
        client, response = self.sign_in(SEED_USERNAME, SEED_PASSWORD)
        assert response.status_code == 303, response.text
        return client

    def create_user(self, admin, username: str, temp_password: str):
        response = admin.post(
            "/api/admin/users",
            json={"username": username, "temp_password": temp_password},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def settled_user(self, admin, username: str, password: str):
        """A user who has signed in with their temporary password and changed it."""
        temp = password + "-temporary"
        self.create_user(admin, username, temp)
        client, response = self.sign_in(username, temp)
        assert response.headers["location"] == "/password", response.text
        changed = client.post(
            "/password",
            data={
                "current_password": temp,
                "new_password": password,
                "confirm_password": password,
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303, changed.text
        return client


@pytest.fixture(params=["builtin", "auth-service"])
def console(request, tmp_path, monkeypatch, fake_comms):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.setenv("HIVE_TOOLS_URL", fake_comms.url)
    monkeypatch.delenv("HIVE_SECRET_KEY", raising=False)
    auth_module = __import__("auth")
    auth_module.forget_secret_key()
    if request.param == "builtin":
        import auth

        monkeypatch.setenv("HIVE_AUTH_BACKEND", "builtin")
        monkeypatch.setenv("HIVE_SEED_USERNAME", SEED_USERNAME)
        monkeypatch.setenv("HIVE_SEED_PASSWORD", SEED_PASSWORD)
        asyncio.run(auth.seed_first_user())
        yield Console("builtin")
        return
    source = request.getfixturevalue("auth_service_source")
    with AuthServiceProcess(source, tmp_path / "auth.db") as service:
        granted = service.admin("PUT", f"/admin/users/{SEED_USERNAME}/sites/hive")
        assert granted.status_code == 204, granted.text
        monkeypatch.setenv("HIVE_AUTH_BACKEND", "auth-service")
        monkeypatch.setenv("AUTH_SERVICE_URL", service.url)
        monkeypatch.setenv("AUTH_SERVICE_ADMIN_TOKEN", AUTH_SERVICE_ADMIN_TOKEN)
        yield Console("auth-service", service)


@pytest.fixture
def builtin_console(tmp_path, monkeypatch, fake_comms):
    import auth

    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.setenv("HIVE_TOOLS_URL", fake_comms.url)
    monkeypatch.delenv("HIVE_SECRET_KEY", raising=False)
    auth.forget_secret_key()
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "builtin")
    monkeypatch.setenv("HIVE_SEED_USERNAME", SEED_USERNAME)
    monkeypatch.setenv("HIVE_SEED_PASSWORD", SEED_PASSWORD)
    asyncio.run(auth.seed_first_user())
    return Console("builtin")


def pytest_pyfunc_call(pyfuncitem):
    """Run coroutine tests without pulling in a plugin for it.

    Most of the suite is synchronous and drives the console through its own
    HTTP surface, which `TestClient` runs on a loop of its own. The tests
    that call a data module directly are coroutines now that those modules
    are, and one plugin's default event-loop scoping would then apply to
    every test in the repo.
    """
    import asyncio
    import inspect

    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
        if name in pyfuncitem.funcargs
    }
    asyncio.run(func(**kwargs))
    return True
