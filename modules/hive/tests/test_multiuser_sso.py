"""Microsoft and Windows domain sign-in: each is switched on by configuration
alone and signs in only to an account an admin created.

Microsoft runs against a stand-in Entra tenant over real HTTP (sso_support)
that signs real id tokens with a key generated per run, on both account
stores. Windows sign-in is a built-in-store feature (auth-service holds no
domain accounts); its tests stub where the process stops being ours — the
GSSAPI accept-context call — and hand back the principal a ticket named.
"""

import base64
import re
import time
import urllib.parse

import pytest
from sso_support import (
    CLIENT_ID,
    CLIENT_SECRET,
    OTHER_TENANT_ID,
    REDIRECT_URL,
    TENANT_ID,
    MockEntra,
)

import sso

NOT_SET_UP = "has not been set up"


@pytest.fixture(scope="session")
def entra_server():
    with MockEntra() as server:
        yield server


@pytest.fixture
def entra(entra_server, monkeypatch):
    monkeypatch.setenv("HIVE_MICROSOFT_AUTHORITY", entra_server.url)
    monkeypatch.setenv("HIVE_MICROSOFT_TENANT_ID", TENANT_ID)
    monkeypatch.setenv("HIVE_MICROSOFT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("HIVE_MICROSOFT_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("HIVE_MICROSOFT_REDIRECT_URL", REDIRECT_URL)
    return entra_server


def _link(page_html: str, label: str) -> str:
    match = re.search(r'href="([^"]+)"[^>]*>\s*' + re.escape(label), page_html)
    assert match, f"no {label!r} link on the sign-in page"
    return match.group(1)


def _microsoft(console, entra, **identity):
    """A browser that starts at the sign-in page, follows "Sign in with
    Microsoft", signs in at the tenant as `identity`, and comes back."""
    client = console.client()
    start = _link(client.get("/login").text, "Sign in with Microsoft")
    sent = client.get(start, follow_redirects=False)
    assert sent.status_code in (302, 303), sent.text
    back = client.get(entra.sign_in(sent.headers["location"], **identity), follow_redirects=False)
    return client, back


def _signed_in_as(client) -> str | None:
    """Who the console's own page header says is signed in, or None."""
    page = client.get("/overview", follow_redirects=False)
    if page.status_code != 200:
        return None
    match = re.search(r"<span><strong>([^<]+)</strong><small>(?:Admin|Member)</small>", page.text)
    return match.group(1) if match else None


def _set_up(admin, username, **fields):
    created = admin.post("/api/admin/users", json={"username": username, **fields})
    assert created.status_code == 201, created.text
    return created.json()


# ── Microsoft ───────────────────────────────────────────────────────────


def test_an_admin_set_up_email_signs_in_with_microsoft_as_that_account_and_later_by_oid(console, entra):
    admin = console.admin()
    _set_up(admin, "sloan", email="Sloan@Example.edu")

    client, back = _microsoft(console, entra, oid="oid-sloan", email="sloan@EXAMPLE.edu")
    assert back.status_code == 303, back.text
    assert _signed_in_as(client) == "sloan"
    # A console session only: nothing that other sparktobloom sites accept.
    assert "auth_session" not in {c.name for c in client.cookies.jar}

    # Bound to the oid: a later sign-in matches it even once the email the
    # tenant reports moves.
    again, back = _microsoft(console, entra, oid="oid-sloan", email="renamed@example.edu")
    assert back.status_code == 303, back.text
    assert _signed_in_as(again) == "sloan"


def test_changing_an_accounts_email_unbinds_its_microsoft_identity(console, entra):
    admin = console.admin()
    _set_up(admin, "sloan", email="sloan@example.edu")
    _, first = _microsoft(console, entra, oid="oid-sloan", email="sloan@example.edu")
    assert first.status_code == 303

    moved = admin.patch("/api/admin/users/sloan", json={"email": "sloan.s@example.edu"})
    assert moved.status_code == 200, moved.text

    stale, back = _microsoft(console, entra, oid="oid-sloan", email="sloan@example.edu")
    assert back.status_code == 403
    assert NOT_SET_UP in back.text
    assert _signed_in_as(stale) is None
    fresh, back = _microsoft(console, entra, oid="oid-sloan", email="Sloan.S@example.edu")
    assert back.status_code == 303, back.text
    assert _signed_in_as(fresh) == "sloan"


def test_an_admin_unbinding_a_microsoft_identity_lets_the_accounts_email_select_it_again(
    console, entra
):
    admin = console.admin()
    _set_up(admin, "sloan", email="sloan@example.edu")
    sloan, first = _microsoft(console, entra, oid="oid-sloan", email="sloan@example.edu")
    assert first.status_code == 303
    _, refused = _microsoft(console, entra, oid="oid-new-laptop", email="sloan@example.edu")
    assert refused.status_code == 403

    assert sloan.delete("/api/admin/users/sloan/microsoft").status_code == 403
    listed = {u["username"]: u for u in admin.get("/api/admin/users").json()["users"]}
    assert listed["sloan"]["microsoft_bound"] is True

    assert admin.delete("/api/admin/users/sloan/microsoft").status_code == 204
    listed = {u["username"]: u for u in admin.get("/api/admin/users").json()["users"]}
    assert listed["sloan"]["microsoft_bound"] is False
    rebound, back = _microsoft(console, entra, oid="oid-new-laptop", email="sloan@example.edu")
    assert back.status_code == 303, back.text
    assert _signed_in_as(rebound) == "sloan"


@pytest.mark.parametrize("console", ["auth-service"], indirect=True)
def test_at_home_a_microsoft_account_not_granted_the_hive_is_refused_unless_it_is_an_admin(
    console, entra
):
    service = console.auth_service
    console.admin()
    for username in ("ada", "bea"):
        made = service.admin(
            "POST", "/admin/users", json={"username": username, "email": f"{username}@example.edu"}
        )
        assert made.status_code == 201, made.text
    assert service.admin("PUT", "/admin/groups/admins/members/ada").status_code == 204

    member, back = _microsoft(console, entra, oid="oid-bea", email="bea@example.edu")
    assert back.status_code == 403
    assert NOT_SET_UP in back.text
    assert _signed_in_as(member) is None

    admin, back = _microsoft(console, entra, oid="oid-ada", email="ada@example.edu")
    assert back.status_code == 303, back.text
    assert _signed_in_as(admin) == "ada"


def test_a_microsoft_email_no_admin_set_up_is_refused_as_not_set_up(console, entra):
    console.admin()
    client, back = _microsoft(console, entra, oid="oid-stranger", email="stranger@example.edu")
    assert back.status_code == 403
    assert NOT_SET_UP in back.text
    assert _signed_in_as(client) is None


def test_a_second_microsoft_identity_presenting_a_bound_accounts_email_is_refused(console, entra):
    admin = console.admin()
    _set_up(admin, "sloan", email="sloan@example.edu")
    _, first = _microsoft(console, entra, oid="oid-sloan", email="sloan@example.edu")
    assert first.status_code == 303

    client, back = _microsoft(console, entra, oid="oid-someone-else", email="sloan@example.edu")
    assert back.status_code == 403
    assert NOT_SET_UP in back.text
    assert _signed_in_as(client) is None


def test_a_disabled_account_is_refused_microsoft_sign_in_and_its_live_session_ends(console, entra):
    admin = console.admin()
    _set_up(admin, "sloan", email="sloan@example.edu")
    live, back = _microsoft(console, entra, oid="oid-sloan", email="sloan@example.edu")
    assert _signed_in_as(live) == "sloan"

    assert admin.patch("/api/admin/users/sloan", json={"disabled": True}).status_code == 200
    assert _signed_in_as(live) is None

    client, back = _microsoft(console, entra, oid="oid-sloan", email="sloan@example.edu")
    assert back.status_code == 403
    assert "disabled" in back.text
    assert _signed_in_as(client) is None


@pytest.mark.parametrize(
    "forgery",
    [
        {"tid": OTHER_TENANT_ID},
        {"iss": f"https://login.microsoftonline.com/{OTHER_TENANT_ID}/v2.0"},
        {"aud": "some-other-app"},
        {"nonce": "a-nonce-the-console-never-sent"},
        {"exp": int(time.time()) - 600, "iat": int(time.time()) - 4200, "nbf": int(time.time()) - 4200},
        {"_sign_with": "rogue"},
        {"email_verified": False},
    ],
    ids=["wrong-tenant", "wrong-issuer", "wrong-audience", "nonce-mismatch", "expired",
         "bad-signature", "unverified-email"],
)
def test_a_microsoft_token_that_fails_verification_signs_nobody_in(console, entra, forgery):
    admin = console.admin()
    _set_up(admin, "sloan", email="sloan@example.edu")

    client, back = _microsoft(console, entra, oid="oid-sloan", email="sloan@example.edu", **forgery)
    assert back.status_code in (401, 403)
    assert "Sign in" in back.text
    assert _signed_in_as(client) is None


def test_a_microsoft_callback_whose_state_was_not_issued_to_this_browser_is_refused(console, entra):
    admin = console.admin()
    _set_up(admin, "sloan", email="sloan@example.edu")

    victim = console.client()
    start = _link(victim.get("/login").text, "Sign in with Microsoft")
    sent = victim.get(start, follow_redirects=False)
    callback = entra.sign_in(sent.headers["location"], oid="oid-sloan", email="sloan@example.edu")

    attacker = console.client()
    back = attacker.get(callback, follow_redirects=False)
    assert back.status_code in (400, 401, 403)
    assert _signed_in_as(attacker) is None


def test_a_callback_carrying_another_flows_state_is_refused_even_with_this_browsers_own_code(
    console, entra
):
    """The browser holds a flow and a code of its own, both valid; only the
    state names a different sign-in. Nothing but the state comparison stands
    between that callback and a session."""
    admin = console.admin()
    _set_up(admin, "sloan", email="sloan@example.edu")

    other = console.client()
    other_start = other.get(_link(other.get("/login").text, "Sign in with Microsoft"),
                            follow_redirects=False)
    other_state = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(other_start.headers["location"]).query)
    )["state"]

    client = console.client()
    sent = client.get(_link(client.get("/login").text, "Sign in with Microsoft"),
                      follow_redirects=False)
    callback = entra.sign_in(sent.headers["location"], oid="oid-sloan", email="sloan@example.edu")
    path, _, query = callback.partition("?")
    params = dict(urllib.parse.parse_qsl(query))
    params["state"] = other_state
    back = client.get(f"{path}?{urllib.parse.urlencode(params)}", follow_redirects=False)
    assert back.status_code in (400, 401, 403)
    assert _signed_in_as(client) is None


@pytest.mark.parametrize("tenant", ["common", "organizations", "consumers"])
def test_a_tenant_that_is_not_one_directory_keeps_microsoft_sign_in_off(
    builtin_console, entra, monkeypatch, tenant
):
    monkeypatch.setenv("HIVE_MICROSOFT_TENANT_ID", tenant)
    client = builtin_console.client()
    assert "Sign in with Microsoft" not in client.get("/login").text
    assert client.get("/login/microsoft", follow_redirects=False).status_code == 404


# ── Windows ─────────────────────────────────────────────────────────────


@pytest.fixture
def windows(builtin_console, tmp_path, monkeypatch):
    keytab = tmp_path / "http.keytab"
    keytab.write_bytes(b"\x05\x02")
    monkeypatch.setenv("HIVE_KERBEROS_KEYTAB", str(keytab))
    monkeypatch.setenv("HIVE_KERBEROS_SPN", "HTTP/hive.corp.example.edu")
    monkeypatch.setenv("HIVE_KERBEROS_REALM_MAP", "CORP.EXAMPLE.EDU=UTSYS")
    tickets: dict[bytes, str] = {}

    def accept(token: bytes, keytab: str, spn: str):
        principal = tickets.get(token)
        if principal is None:
            raise sso.KerberosRefused("GSSAPI: the ticket could not be verified")
        return principal, b"mutual-auth-reply"

    monkeypatch.setattr(sso, "accept_security_context", accept)
    return builtin_console, tickets


def _negotiate(client, token: bytes):
    header = "Negotiate " + base64.b64encode(token).decode("ascii")
    return client.get("/login/windows", headers={"Authorization": header}, follow_redirects=False)


def test_a_domain_ticket_for_an_admin_set_up_account_signs_in_as_that_account(windows):
    console, tickets = windows
    admin = console.admin()
    _set_up(admin, "sloan", domain_account="utsys\\Sloan")
    tickets[b"sloan-ticket"] = "sloan@CORP.EXAMPLE.EDU"

    client = console.client()
    start = _link(client.get("/login").text, "Sign in with Windows")
    back = _negotiate(client, b"sloan-ticket") if start == "/login/windows" else None
    assert back is not None, start
    assert back.status_code == 303, back.text
    assert _signed_in_as(client) == "sloan"


def test_a_realm_with_no_configured_mapping_takes_its_first_label_as_the_domain(windows, monkeypatch):
    console, tickets = windows
    monkeypatch.delenv("HIVE_KERBEROS_REALM_MAP")
    admin = console.admin()
    _set_up(admin, "aiden", domain_account="CORP\\aiden")
    tickets[b"aiden-ticket"] = "aiden@CORP.EXAMPLE.EDU"

    client = console.client()
    assert _negotiate(client, b"aiden-ticket").status_code == 303
    assert _signed_in_as(client) == "aiden"


def test_a_realm_the_configured_map_does_not_name_signs_nobody_in(windows):
    console, tickets = windows
    admin = console.admin()
    # The account a first-label guess at the foreign realm would select.
    _set_up(admin, "sloan", domain_account="OTHER\\sloan")
    tickets[b"foreign-ticket"] = "sloan@OTHER.EXAMPLE.EDU"

    client = console.client()
    back = _negotiate(client, b"foreign-ticket")
    assert back.status_code == 403
    assert NOT_SET_UP in back.text
    assert _signed_in_as(client) is None


@pytest.mark.parametrize(
    "principal", ["sloan/admin@CORP.EXAMPLE.EDU", "HTTP/hive.corp.example.edu@CORP.EXAMPLE.EDU"]
)
def test_a_principal_with_an_instance_names_no_domain_account(principal):
    config = sso.WindowsConfig(
        keytab="unused", spn="unused", realm_map=sso.parse_realm_map("CORP.EXAMPLE.EDU=UTSYS")
    )
    assert sso.domain_account_for(principal, config) is None
    assert sso.domain_account_for("sloan@CORP.EXAMPLE.EDU", config) == "UTSYS\\sloan"


def test_windows_sign_in_stays_off_behind_auth_service_whatever_is_configured(
    windows, monkeypatch
):
    console, tickets = windows
    tickets[b"sloan-ticket"] = "sloan@CORP.EXAMPLE.EDU"
    monkeypatch.setenv("HIVE_AUTH_BACKEND", "auth-service")
    monkeypatch.setenv("AUTH_SERVICE_URL", "http://127.0.0.1:9")

    client = console.client()
    assert "Sign in with Windows" not in client.get("/login").text
    assert _negotiate(client, b"sloan-ticket").status_code == 404


def test_a_domain_ticket_for_an_account_nobody_set_up_is_refused_as_not_set_up(windows):
    console, tickets = windows
    console.admin()
    tickets[b"stranger-ticket"] = "stranger@CORP.EXAMPLE.EDU"

    client = console.client()
    back = _negotiate(client, b"stranger-ticket")
    assert back.status_code == 403
    assert NOT_SET_UP in back.text
    assert _signed_in_as(client) is None


def test_a_domain_ticket_for_a_disabled_account_is_refused(windows):
    console, tickets = windows
    admin = console.admin()
    _set_up(admin, "sloan", domain_account="UTSYS\\sloan")
    assert admin.patch("/api/admin/users/sloan", json={"disabled": True}).status_code == 200
    tickets[b"sloan-ticket"] = "sloan@CORP.EXAMPLE.EDU"

    client = console.client()
    back = _negotiate(client, b"sloan-ticket")
    assert back.status_code == 403
    assert "disabled" in back.text
    assert _signed_in_as(client) is None


def test_the_plain_sign_in_page_never_issues_a_negotiate_challenge(windows):
    console, _ = windows
    page = console.client().get("/login")
    assert page.status_code == 200
    assert "Sign in with Windows" in page.text
    assert "www-authenticate" not in page.headers


@pytest.mark.parametrize(
    "token",
    [None, b"NTLMSSP\x00\x01\x00\x00\x00\x07\x82\x08\xa2", b"not-a-ticket-anyone-issued"],
    ids=["no-ticket", "ntlm-token", "unverifiable-ticket"],
)
def test_a_browser_with_no_usable_domain_ticket_gets_the_sign_in_page_not_an_error(
    windows, entra, token
):
    console, tickets = windows
    admin = console.admin()
    _set_up(admin, "sloan", domain_account="UTSYS\\sloan")
    if token is not None and token.startswith(b"NTLMSSP"):
        # Even a Kerberos library that would take NTLM is never handed it.
        tickets[token] = "sloan@CORP.EXAMPLE.EDU"
    client = console.client()
    back = client.get("/login/windows", follow_redirects=False) if token is None else _negotiate(
        client, token
    )
    assert back.status_code < 500
    assert 'action="/login"' in back.text
    assert "Sign in with Microsoft" in back.text
    assert _signed_in_as(client) is None
    if token is None:
        assert back.headers.get("www-authenticate") == "Negotiate"
    else:
        # Challenging again would loop a browser that has only NTLM to give.
        assert "www-authenticate" not in back.headers


# ── switched by configuration alone ─────────────────────────────────────


@pytest.mark.parametrize(
    "label,path,setting",
    [("Sign in with Microsoft", "/login/microsoft", "HIVE_MICROSOFT_CLIENT_SECRET"),
     ("Sign in with Microsoft", "/login/microsoft/callback?code=x&state=y",
      "HIVE_MICROSOFT_TENANT_ID"),
     ("Sign in with Windows", "/login/windows", "HIVE_KERBEROS_KEYTAB")],
)
def test_a_method_appears_and_answers_only_while_it_is_configured(
    windows, entra, monkeypatch, label, path, setting
):
    console, _ = windows
    client = console.client()
    assert label in client.get("/login").text
    assert client.get(path, follow_redirects=False).status_code != 404

    monkeypatch.delenv(setting)
    assert label not in client.get("/login").text
    assert client.get(path, follow_redirects=False).status_code == 404
