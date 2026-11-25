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
