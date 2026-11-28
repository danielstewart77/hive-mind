"""Put src/ on the import path for every test module, not just the ones that
happen to insert it themselves. Without this, a single test file can only be
collected when another module has already run its own sys.path insert.

And put every test on a scratch Postgres, on the suite's one event loop —
see `hive_site.testing` for why both.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

# The suite connects to nothing but this machine and its own scratch database.
#
# Every service the console calls falls back to its address on the hive's own
# network. On a bare host those names do not resolve, so a test that creates a
# mind without asking for the fake gateway simply fails to register it. Inside
# a container on that network they do resolve and the container's credentials
# are real: on 2026-11-17 a run there registered atlas, bilby-two and borealis
# on the live broker. Every connection that is not to loopback or to the
# scratch database's own server is therefore refused, before anything imports
# the code under test, so a run in a container sees the network a run on a
# host sees. The fakes all listen on loopback and are unaffected.
import ipaddress  # noqa: E402
import socket  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402


def _scratch_database_addresses() -> set[str]:
    try:
        host = urlsplit(os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "")).hostname
        return {info[4][0] for info in socket.getaddrinfo(host, None)} if host else set()
    except (ValueError, OSError):
        return set()


_SCRATCH_DATABASE = _scratch_database_addresses()


def _refuse_unless_local(address) -> None:
    if not isinstance(address, tuple) or not address:
        return  # a unix socket: this machine by construction
    host = str(address[0])
    try:
        local = ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local and host not in _SCRATCH_DATABASE:
        raise ConnectionRefusedError(
            f"the console suite connects to no live service: {host}"
        )


class _LocalOnlySocket(socket.socket):
    def connect(self, address):
        _refuse_unless_local(address)
        return super().connect(address)

    def connect_ex(self, address):
        _refuse_unless_local(address)
        return super().connect_ex(address)


socket.socket = _LocalOnlySocket

from hive_site import testing  # noqa: E402


@pytest.fixture(scope="session")
def scratch_database():
    """One scratch database for the run, claimed, with the schemas applied.

    Fails rather than skips without a server: a suite that skipped every
    test would read as green while testing nothing.
    """
    if not testing.ADMIN_DSN:
        pytest.fail(testing.NO_SERVER)
    with testing.testclients_on_the_suite_loop():
        scratch = testing.run(testing.ScratchDatabase.create())
        try:
            yield scratch
        finally:
            testing.run(scratch.close())
            testing.stop_suite_portal()


@pytest.fixture(autouse=True)
def _mind_creation_records_in_a_folder_the_test_owns(tmp_path, monkeypatch):
    """The record a creation is made under defaults to `/data`, which inside
    the console's own container is the live one."""
    import mind_creation
    import mind_rename

    monkeypatch.setattr(mind_creation, "CREATION_DIR", tmp_path / "mind-creations")
    monkeypatch.setattr(mind_rename, 'RENAME_DIR', tmp_path / 'mind-renames')


@pytest.fixture(autouse=True)
def _mind_keys_in_a_folder_the_test_owns(tmp_path, monkeypatch):
    """A mind's key is a file under a key root (`mind_key_files`), which
    defaults to the console's live `/mind_keys`: every test gets its own."""
    import mind_key_files

    root = tmp_path / "mind-keys"
    root.mkdir()
    monkeypatch.setattr(mind_key_files, "KEY_ROOT", root)
    monkeypatch.setenv("HIVE_MIND_UID", str(os.getuid()))
    monkeypatch.setenv("HIVE_MIND_GID", str(os.getgid()))
    return root


RELEASE_CONTEXT = "https://github.com/danielstewart77/hive-mind.git#" + "c" * 40


@pytest.fixture(autouse=True)
def _a_release_to_build_new_minds_from(monkeypatch):
    """A mind is built from the release this machine is on, which the
    environment (or the minds folder) names; the tests that make minds run on
    one. The tests of how it is chosen unset this."""
    monkeypatch.setenv("HIVE_MIND_BUILD_CONTEXT", RELEASE_CONTEXT)


@pytest.fixture(autouse=True)
def _isolated_hive_db(scratch_database, monkeypatch):
    """Every test on an emptied scratch database, with the store open.

    `HIVE_DATABASE_URL` is pointed at the scratch database here, whatever
    the machine exports: on the host that runs the console that variable
    names the live database, and a suite that inherited it would edit
    production and report green.

    And the open. Nothing opens the store on demand, so a test reaching a
    route without this gets the refusal that says the application's lifespan
    never ran.
    """
    import store

    monkeypatch.setenv("HIVE_DATABASE_URL", scratch_database.dsn)
    testing.run(scratch_database.reset())
    testing.run(store.open())
    try:
        yield
    finally:
        testing.run(store.close())


# ── multi-user: one console, either account store ─────────────────────

from multiuser_support import (  # noqa: E402
    SEED_PASSWORD,
    SEED_USERNAME,
    FakeComms,
)


@pytest.fixture(scope="session")
def fake_comms():
    with FakeComms() as server:
        yield server


class Console:
    """The console under test, signed into through its own sign-in page."""

    backend = "builtin"

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


@pytest.fixture
def console(tmp_path, monkeypatch, fake_comms):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.setenv("HIVE_TOOLS_URL", fake_comms.url)
    monkeypatch.delenv("HIVE_SECRET_KEY", raising=False)
    __import__("auth").forget_secret_key()
    monkeypatch.setenv("HIVE_SEED_USERNAME", SEED_USERNAME)
    monkeypatch.setenv("HIVE_SEED_PASSWORD", SEED_PASSWORD)
    testing.run(__import__("auth").seed_first_user())
    return Console()


@pytest.fixture
def builtin_console(tmp_path, monkeypatch, fake_comms):
    import auth

    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.setenv("HIVE_TOOLS_URL", fake_comms.url)
    monkeypatch.delenv("HIVE_SECRET_KEY", raising=False)
    auth.forget_secret_key()
    monkeypatch.setenv("HIVE_SEED_USERNAME", SEED_USERNAME)
    monkeypatch.setenv("HIVE_SEED_PASSWORD", SEED_PASSWORD)
    testing.run(auth.seed_first_user())
    return Console()


def pytest_pyfunc_call(pyfuncitem):
    """Run coroutine tests on the suite's loop, without a plugin for it.

    Most of the suite is synchronous and drives the console through its own
    HTTP surface. The tests that call a data module directly are coroutines,
    and they run on the loop the store's pool belongs to.
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
    testing.run(func(**kwargs))
    return True


@pytest.fixture(autouse=True)
def _a_person_is_signed_in_for_calls_the_site_makes_for_one(monkeypatch):
    """Story 905: this site asks the gateway only as a signed-in person, with
    that person's own key. Tests that are not about whose key it is run as an
    administrator with a key of their own; the key store is the boundary
    replaced, the sending is real."""
    import holder_credentials
    import mind_runtime

    async def key(person):
        return f"{person}-key-for-tests"

    monkeypatch.setattr(holder_credentials, "_asking_person_key", key)
    token = mind_runtime._PERSON.set("daniel")
    yield
    mind_runtime._PERSON.reset(token)
