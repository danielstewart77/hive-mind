"""Shared browser fixtures.

A real browser, because what these suites test is what the page does with a
live socket and live fetches it is already holding — the thing that breaks is
never the handler in isolation, it is the handler wired to everything else.

Serving the real app over a real port rather than TestClient: the page has to
load its own templates, static files and xterm bundle the way a browser does.
"""

import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

import auth  # noqa: E402
import main as main_mod  # noqa: E402
import store  # noqa: E402
from hive_site import testing as accounts  # noqa: E402


@pytest.fixture(scope="session")
def scratch_database():
    """One scratch database for the run, claimed, with the schemas applied.

    Fails rather than skips without a server: a suite that skipped every
    test would read as green while testing nothing.
    """
    if not accounts.ADMIN_DSN:
        pytest.fail(accounts.NO_SERVER)
    with accounts.testclients_on_the_suite_loop():
        scratch = accounts.run(accounts.ScratchDatabase.create())
        try:
            yield scratch
        finally:
            accounts.run(scratch.close())
            accounts.stop_suite_portal()


@pytest.fixture(scope="module")
def live_app(scratch_database):
    """The real app on a real port, serving for the module.

    Served on the suite's own loop, as a task, rather than on a loop of its
    own in a thread: the server's lifespan opens the store, and a pool
    belongs to the loop that opened it, so every other test in the module —
    the ones driving `TestClient` beside the browser — has to be on the
    same one. The signed-in user is made first and the store closed again,
    so the lifespan's open is the one that holds it.
    """
    os.environ["HIVE_DATABASE_URL"] = scratch_database.dsn
    os.environ["HIVE_SECRET_KEY"] = "browser-test-secret"
    accounts.run(scratch_database.reset())
    accounts.run(store.open())
    try:
        user = accounts.make_user("browser-test", "browser-pass",
                                  is_admin=True, replace=True)
        token = accounts.session_token(user)
    finally:
        accounts.run(store.close())

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(
        uvicorn.Config(main_mod.app, host="127.0.0.1", port=port, log_level="error")
    )
    serving = accounts.suite_portal().start_task_soon(server.serve)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.skip("app did not come up")

    yield f"http://127.0.0.1:{port}", token
    server.should_exit = True
    # Waited for, not fired and forgotten. The app's own lifespan owns the
    # store, and its shutdown closes it. Left unawaited, that close lands
    # after the next module has opened its own store and every test in it
    # then fails on a closed one.
    serving.result(timeout=10)


@pytest.fixture()
def page(live_app):
    base, token = live_app
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip(f"chromium unavailable: {exc}")
        ctx = browser.new_context(viewport={"width": 1100, "height": 800})
        ctx.add_cookies([{
            "name": auth.SESSION_COOKIE_NAME, "value": token,
            "domain": "127.0.0.1", "path": "/",
        }])
        yield ctx.new_page(), base
        browser.close()


# ── multi-user: the terminal, signed into through either account store ──
#
# The same approach as the console's suite: comms is a stand-in on a real
# port, auth-service is the real app exported at AUTH_SERVICE_REF.
# The terminal has no user-management routes of its own — accounts are made
# on the console — so what the console's admin pages do is done here through
# the store both apps share.

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


@pytest.fixture(autouse=True)
def _isolated_hive_db(request, scratch_database, monkeypatch):
    """Every test on an emptied scratch database, with the store open.

    `HIVE_DATABASE_URL` is pointed at the scratch database here, whatever
    the machine exports: on the host that runs the hive that variable names
    the live database, and a suite that inherited it would edit production
    and report green.

    And the open. Nothing opens the store on demand, so a test reaching a
    route without this gets the refusal that says the lifespan never ran.

    The browser suites are the exception: their server and its signed-in
    user live for the module, on the db `live_app` chose and opened.
    """
    if "live_app" in request.fixturenames or "page" in request.fixturenames:
        yield
        return
    if store.is_open():
        # A module-scoped live server in this file owns the store, and some
        # of that module's tests do not ask for the browser. Emptying or
        # reopening it would pull the rows out from under a server that is
        # still serving them.
        yield
        return
    monkeypatch.setenv("HIVE_DATABASE_URL", scratch_database.dsn)
    accounts.run(scratch_database.reset())
    accounts.run(store.open())
    try:
        yield
    finally:
        accounts.run(store.close())


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


class Terminal:
    """The terminal site under test, signed into through its own sign-in page."""

    def __init__(self, backend: str, auth_service=None):
        self.backend = backend
        self.auth_service = auth_service

    def client(self):
        from starlette.testclient import TestClient

        return TestClient(main_mod.app, base_url="https://testserver")

    def sign_in(self, username: str, password: str):
        client = self.client()
        response = client.post(
            "/login",
            data={"username": username, "password": password, "next": "/terminal"},
            follow_redirects=False,
        )
        return client, response

    def admin(self):
        client, response = self.sign_in(SEED_USERNAME, SEED_PASSWORD)
        assert response.status_code == 303, response.text
        return client

    @staticmethod
    def store():
        return auth.get_store()

    def create_user(self, username: str, temp_password: str):
        return accounts.run(
            self.store().create_user(username, temp_password=temp_password))

    def settled_user(self, username: str, password: str):
        """A user who has signed in with their temporary password and changed it."""
        temp = password + "-temporary"
        self.create_user(username, temp)
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
def terminal(request, tmp_path, monkeypatch, fake_comms):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.delenv("HIVE_SECRET_KEY", raising=False)
    if request.param == "builtin":
        monkeypatch.setenv("HIVE_AUTH_BACKEND", "builtin")
        monkeypatch.setenv("HIVE_SEED_USERNAME", SEED_USERNAME)
        monkeypatch.setenv("HIVE_SEED_PASSWORD", SEED_PASSWORD)
        accounts.seed_first_user()
        yield Terminal("builtin")
        return
    source = request.getfixturevalue("auth_service_source")
    with AuthServiceProcess(source, tmp_path / "auth.db") as service:
        granted = service.admin("PUT", f"/admin/users/{SEED_USERNAME}/sites/hive")
        assert granted.status_code == 204, granted.text
        monkeypatch.setenv("HIVE_AUTH_BACKEND", "auth-service")
        monkeypatch.setenv("AUTH_SERVICE_URL", service.url)
        monkeypatch.setenv("AUTH_SERVICE_ADMIN_TOKEN", AUTH_SERVICE_ADMIN_TOKEN)
        yield Terminal("auth-service", service)


@pytest.fixture
def builtin_terminal(monkeypatch, fake_comms):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.delenv("HIVE_SECRET_KEY", raising=False)
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "builtin")
    monkeypatch.setenv("HIVE_SEED_USERNAME", SEED_USERNAME)
    monkeypatch.setenv("HIVE_SEED_PASSWORD", SEED_PASSWORD)
    accounts.seed_first_user()
    return Terminal("builtin")


def pytest_pyfunc_call(pyfuncitem):
    """Run coroutine tests on the suite's loop, without a plugin for it.

    Most of this suite is synchronous and drives the site through its own
    HTTP surface or a real browser. The tests that call the shared store
    directly are coroutines, and they run on the loop the store's pool
    belongs to. The same hook, for the same reason, is in the console's
    suite.
    """
    import inspect

    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
        if name in pyfuncitem.funcargs
    }
    accounts.run(func(**kwargs))
    return True
