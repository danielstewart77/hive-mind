"""Which account store a console signs in against, and what it refuses."""

import asyncio

import accounts
import pytest
from itsdangerous import BadSignature, URLSafeTimedSerializer
from multiuser_support import (
    AUTH_SERVICE_ADMIN_TOKEN,
    SEED_PASSWORD,
    SEED_USERNAME,
    AuthServiceProcess,
    free_port,
)

import auth
from tests.conftest import Console

SHIPPED_SECRET = "change-this-in-production"  # secret-guard: allow


def _use_auth_service(monkeypatch, url):
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "auth-service")
    monkeypatch.setenv("AUTH_SERVICE_URL", url)
    monkeypatch.setenv("AUTH_SERVICE_ADMIN_TOKEN", AUTH_SERVICE_ADMIN_TOKEN)


def _no_auth_service(monkeypatch):
    for name in ("HIVE_AUTH_BACKEND", "AUTH_SERVICE_URL", "AUTH_SERVICE_ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_the_console_signs_in_against_hive_db_unless_auth_service_is_configured(
    tmp_path, monkeypatch, fake_comms, auth_service_source
):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    _no_auth_service(monkeypatch)
    accounts.make_user("local-only", "local-only-pass")
    console = Console("builtin")

    _, local = console.sign_in("local-only", "local-only-pass")
    assert local.status_code == 303
    assert local.headers["location"] == "/overview"

    with AuthServiceProcess(auth_service_source, tmp_path / "auth.db") as service:
        service.admin("PUT", f"/admin/users/{SEED_USERNAME}/sites/hive")
        # AUTH_SERVICE_URL alone selects it: no backend named.
        monkeypatch.setenv("AUTH_SERVICE_URL", service.url)
        monkeypatch.setenv("AUTH_SERVICE_ADMIN_TOKEN", AUTH_SERVICE_ADMIN_TOKEN)

        _, refused = console.sign_in("local-only", "local-only-pass")
        assert refused.status_code == 401
        _, home = console.sign_in(SEED_USERNAME, SEED_PASSWORD)
        assert home.status_code == 303
        assert "auth_session" in home.cookies


def test_an_old_hive_session_cookie_is_refused_once_auth_service_is_the_store(
    tmp_path, monkeypatch, fake_comms, auth_service_source
):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    _no_auth_service(monkeypatch)
    accounts.make_user(SEED_USERNAME, "the-old-local-pass", is_admin=True)
    console = Console("builtin")
    client, signed_in = console.sign_in(SEED_USERNAME, "the-old-local-pass")
    assert signed_in.status_code == 303
    assert client.get("/overview", follow_redirects=False).status_code == 200

    with AuthServiceProcess(auth_service_source, tmp_path / "auth.db") as service:
        _use_auth_service(monkeypatch, service.url)
        page = client.get("/overview", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"].startswith("/login")
        assert client.get("/api/settings/minds").status_code == 401


def test_sign_in_service_down_is_a_503_and_never_falls_back_to_hive_db(
    monkeypatch, fake_comms
):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    accounts.make_user(SEED_USERNAME, SEED_PASSWORD, is_admin=True)
    _use_auth_service(monkeypatch, f"http://127.0.0.1:{free_port()}")
    console = Console("auth-service")

    _, signed_in = console.sign_in(SEED_USERNAME, SEED_PASSWORD)
    assert signed_in.status_code == 503
    assert "sign-in service unavailable" in signed_in.text.lower()

    client = console.client()
    client.cookies.set("auth_session", "a-token-from-before-the-outage")
    page = client.get("/overview", follow_redirects=False)
    assert page.status_code == 503
    assert "sign-in service unavailable" in page.text.lower()
    api = client.get("/api/settings/minds")
    assert api.status_code == 503


def test_a_fresh_install_with_no_key_never_signs_with_the_shipped_constant(
    monkeypatch, fake_comms
):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    _no_auth_service(monkeypatch)
    monkeypatch.delenv("HIVE_SECRET_KEY", raising=False)
    accounts.make_user(SEED_USERNAME, SEED_PASSWORD, is_admin=True)
    console = Console("builtin")

    client, signed_in = console.sign_in(SEED_USERNAME, SEED_PASSWORD)
    cookie = signed_in.cookies[auth.SESSION_COOKIE_NAME]

    with pytest.raises(BadSignature):
        URLSafeTimedSerializer(SHIPPED_SECRET, salt="hive-session").loads(cookie)
    # The key it did use outlives the request that made it.
    assert client.get("/overview", follow_redirects=False).status_code == 200


def test_the_sign_in_client_is_one_per_event_loop() -> None:
    """Not one per store, and not one per call.

    An `httpx.AsyncClient` holds a connection pool bound to the loop that
    opened it: shared across loops it raises "event loop is closed" on the
    first reuse, and rebuilt per call it costs a TLS handshake on `/verify`,
    which runs on every single request the console serves.
    """
    store = auth.AuthServiceStore("http://auth-service.invalid", "a-token")

    async def twice() -> tuple[object, object]:
        return store._loop_client(), store._loop_client()

    first, again = asyncio.run(twice())
    other_loop, _ = asyncio.run(twice())

    assert first is again, "a client was built per call"
    assert other_loop is not first, "a client was shared across loops"
