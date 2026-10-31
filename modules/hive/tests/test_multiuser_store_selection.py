"""Which account store a console signs in against, and what it refuses."""

import asyncio

import httpx
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
import accounts

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


@pytest.mark.parametrize(
    "method, path", [("GET", "/"), ("GET", "/login"), ("POST", "/logout")]
)
def test_a_public_route_asks_the_sign_in_service_through_an_async_client(
    monkeypatch, fake_comms, method, path
):
    """Who is signed in is asked of auth-service on every public route, and
    asked with a client that does not hold the event loop while it waits.

    The console used to make this call on a worker thread, because the store
    was synchronous. It is a coroutine now, so the call is made on the loop
    and must not block it — which is a property of the transport, not of
    where the code runs. A sync `httpx.Client` put back here would never see
    this mock transport, so the ask would not be recorded at all.
    """
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    _use_auth_service(monkeypatch, "http://auth-service.invalid")
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(401, json={"detail": "expired"})

    store = auth.get_store()
    transport = httpx.MockTransport(answer)

    def _async_only() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    monkeypatch.setattr(store, "_loop_client", _async_only)
    client = Console("auth-service").client()
    client.cookies.set("auth_session", "a-token")

    client.request(method, path, follow_redirects=False)

    assert asked, "the console did not ask auth-service who is signed in"
    # The ask itself is `/verify`; the logout route goes on to end the
    # session, which is the second call on that path and not this one.
    assert asked[0].endswith("/verify?site=hive"), asked
