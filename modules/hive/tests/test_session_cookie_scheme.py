"""The session cookie is Secure exactly when the browser used https.

A browser drops a Secure cookie set over plain http at any address but
localhost, so a hive reached by IP address would loop back to the login page.
Behind a TLS-terminating proxy the site itself sees http, and the proxy's
X-Forwarded-Proto says what the browser used.
"""

from starlette.testclient import TestClient

from multiuser_support import SEED_PASSWORD, SEED_USERNAME


def _sign_in(base_url: str, headers: dict | None = None):
    import main

    client = TestClient(main.app, base_url=base_url, headers=headers or {})
    response = client.post(
        "/login",
        data={"username": SEED_USERNAME, "password": SEED_PASSWORD,
              "next": "/overview"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client, response.headers["set-cookie"]


def test_a_sign_in_over_plain_http_at_an_ip_address_is_kept_by_the_next_request(
    builtin_console,
):
    client, set_cookie = _sign_in("http://192.168.50.7:8710")

    assert "secure" not in set_cookie.lower()
    page = client.get("/overview", follow_redirects=False)
    assert page.status_code == 200, page.headers.get("location")


def test_a_sign_in_over_https_marks_the_session_cookie_secure(builtin_console):
    _, set_cookie = _sign_in("https://192.168.50.7:8710")

    assert "secure" in set_cookie.lower()


def test_a_sign_in_through_a_tls_terminating_proxy_marks_the_session_cookie_secure(
    builtin_console,
):
    _, set_cookie = _sign_in("http://192.168.50.7:8710",
                             headers={"X-Forwarded-Proto": "https"})

    assert "secure" in set_cookie.lower()


# --- the parent domain the terminal shares ------------------------------------

def test_a_sign_in_outside_the_cookie_domain_gets_a_host_only_cookie(
    builtin_console, monkeypatch
):
    """Daniel, 2026-11-26: signing in at localhost:8710 was accepted and
    landed back on the login page. The cookie carried the parent domain, and
    a browser drops a cookie whose domain does not cover its host."""
    monkeypatch.setenv("HIVE_COOKIE_DOMAIN", "sparktobloom.com")

    for address in ("http://localhost:8710", "http://192.168.50.7:8710",
                    "https://evilsparktobloom.com"):
        _, set_cookie = _sign_in(address)
        assert "domain=" not in set_cookie.lower(), address


def test_a_sign_in_inside_the_cookie_domain_gets_a_cookie_on_that_domain(
    builtin_console, monkeypatch
):
    monkeypatch.setenv("HIVE_COOKIE_DOMAIN", "sparktobloom.com")

    for address in ("https://hive.sparktobloom.com", "https://sparktobloom.com",
                    "https://hive.sparktobloom.com:8443"):
        _, set_cookie = _sign_in(address)
        assert "domain=sparktobloom.com" in set_cookie.lower(), address
