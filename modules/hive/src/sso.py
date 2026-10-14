"""Microsoft and Windows domain sign-in for the console.

Each is switched on by configuration alone and signs in only to an account
an admin created beforehand; anyone else is told their account has not been
set up. Either yields a console session (auth.SSO_COOKIE_NAME) and nothing
else — never an auth-service `auth_session` another site would accept.

**Microsoft** is OpenID Connect, authorization code with PKCE, against one
Entra tenant. The tenant is pinned: a token is accepted only when its
signature verifies against the tenant's published keys and its issuer, `tid`,
audience, nonce and expiry are this app's. The first sign-in selects the
account whose email matches the token's (case-insensitively, and only when
the token does not say the email is unverified) and binds the account to the
token's `oid`; every later sign-in matches on `oid` alone. The binding lives
in the console's own db (`sso_bindings`) for both stores, since auth-service
has no field for it.

**Windows** is SPNEGO/Kerberos, offered only behind its own link and never
as a challenge on the plain sign-in page. A ticket's client principal
`user@REALM` names the account whose domain account is `DOMAIN\\user`.
Domain accounts are held only by the built-in store, so this method is off
when the console signs in through auth-service. A browser with no ticket, an
NTLM token, or a ticket that does not verify lands on the sign-in page with
the other methods.

Configuration (all environment):

    HIVE_MICROSOFT_TENANT_ID      the tenant (directory) id, a GUID
    HIVE_MICROSOFT_CLIENT_ID      the app registration's client id
    HIVE_MICROSOFT_CLIENT_SECRET  a client secret of that registration
    HIVE_MICROSOFT_REDIRECT_URL   https://<console host>/login/microsoft/callback
    HIVE_MICROSOFT_AUTHORITY      optional; default https://login.microsoftonline.com

    HIVE_KERBEROS_KEYTAB          path to the keytab holding the service key
    HIVE_KERBEROS_SPN             the service principal, HTTP/<console host>
    HIVE_KERBEROS_REALM_MAP       optional; REALM=DOMAIN pairs, comma-separated
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

import auth

MICROSOFT_PATH = "/login/microsoft"
MICROSOFT_CALLBACK_PATH = "/login/microsoft/callback"
WINDOWS_PATH = "/login/windows"
MICROSOFT = "microsoft"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
FLOW_COOKIE_NAME = "hive_sso_flow"
FLOW_MAX_AGE = 10 * 60
CLOCK_LEEWAY_SECONDS = 60
_METADATA_TTL = 60 * 60
_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_NTLM_SIGNATURE = b"NTLMSSP\x00"


class SignInFailed(Exception):
    """The identity could not be established; the user may try again or use
    another method. Never "not set up": that needs a verified identity."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


class KerberosRefused(Exception):
    """GSSAPI would not accept the token offered."""


# ── which methods are on ────────────────────────────────────────────────


@dataclass(frozen=True)
class MicrosoftConfig:
    authority: str
    tenant_id: str
    client_id: str
    client_secret: str
    redirect_url: str

    @property
    def issuer(self) -> str:
        return f"{self.authority}/{self.tenant_id}/v2.0"


@dataclass(frozen=True)
class WindowsConfig:
    keytab: str
    spn: str
    realm_map: dict[str, str]


def microsoft_config() -> MicrosoftConfig | None:
    values = {
        name: os.getenv(f"HIVE_MICROSOFT_{name.upper()}", "").strip()
        for name in ("tenant_id", "client_id", "client_secret", "redirect_url")
    }
    if not all(values.values()):
        return None
    # Single tenant: "common" or "organizations" would admit any directory.
    if not _GUID.match(values["tenant_id"]):
        return None
    authority = (os.getenv("HIVE_MICROSOFT_AUTHORITY", "").strip() or DEFAULT_AUTHORITY)
    return MicrosoftConfig(authority=authority.rstrip("/"), **values)


def parse_realm_map(raw: str) -> dict[str, str]:
    mapping = {}
    for pair in raw.split(","):
        realm, sep, domain = pair.partition("=")
        if sep and realm.strip() and domain.strip():
            mapping[realm.strip().upper()] = domain.strip().upper()
    return mapping


def windows_config() -> WindowsConfig | None:
    if auth.backend_name() != "builtin":
        return None
    keytab = os.getenv("HIVE_KERBEROS_KEYTAB", "").strip()
    spn = os.getenv("HIVE_KERBEROS_SPN", "").strip()
    if not keytab or not spn or not os.path.isfile(keytab):
        return None
    return WindowsConfig(
        keytab=keytab,
        spn=spn,
        realm_map=parse_realm_map(os.getenv("HIVE_KERBEROS_REALM_MAP", "")),
    )


def sign_in_methods(next_path: str = "/overview") -> list[dict[str, str]]:
    """What the sign-in page offers beside the password form."""
    query = "" if next_path == "/overview" else "?" + urllib.parse.urlencode({"next": next_path})
    methods = list(auth.SIGN_IN_METHODS)
    if microsoft_config() is not None:
        methods.append({"label": "Sign in with Microsoft", "href": MICROSOFT_PATH + query})
    if windows_config() is not None:
        methods.append({"label": "Sign in with Windows", "href": WINDOWS_PATH + query})
    return methods


# ── matching an outside identity to an account ─────────────────────────


def _bindings() -> sqlite3.Connection:
    conn = sqlite3.connect(auth.init_auth_db(), isolation_level=None, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sso_bindings ("
        "provider TEXT NOT NULL, subject TEXT NOT NULL, username TEXT NOT NULL, "
        "created_at INTEGER NOT NULL, PRIMARY KEY (provider, subject), "
        "UNIQUE (provider, username))"
    )
    return conn


def _bound_username(provider: str, subject: str) -> str | None:
    conn = _bindings()
    try:
        row = conn.execute(
            "SELECT username FROM sso_bindings WHERE provider = ? AND subject = ?",
            (provider, subject),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _bind(provider: str, subject: str, username: str) -> None:
    conn = _bindings()
    try:
        conn.execute(
            "INSERT INTO sso_bindings (provider, subject, username, created_at) "
            "VALUES (?, ?, ?, ?)",
            (provider, subject, username, int(time.time())),
        )
    except sqlite3.IntegrityError as exc:
        # The account is already bound to a different identity: the email
        # now belongs to someone else in the tenant.
        raise auth.NotSetUp() from exc
    finally:
        conn.close()


def _admit(store: auth.Store, user: dict | None) -> dict:
    if user is None:
        raise auth.NotSetUp()
    if user.get("disabled"):
        raise auth.AccountDisabled()
    return user


def match_microsoft(store: auth.Store, *, subject: str, email: str | None) -> dict:
    """The account a verified Microsoft identity signs in as."""
    bound = _bound_username(MICROSOFT, subject)
    if bound is not None:
        try:
            return _admit(store, store.get_user(bound))
        except auth.NotFound as exc:
            raise auth.NotSetUp() from exc
    user = _admit(store, store.find_user(email=email) if email else None)
    _bind(MICROSOFT, subject, user["username"])
    return user


def match_domain_account(store: auth.Store, domain_account: str | None) -> dict:
    return _admit(store, store.find_user(domain_account=domain_account) if domain_account else None)


def start_session(store: auth.Store, user: dict, via: str) -> str:
    """A console session for the account; refuses one the store would not
    honour on the next request (an account not granted the hive)."""
    token = auth.issue_sso_token(store, user["username"], via)
    if auth.user_for_sso_token(store, token) is None:
        raise auth.NotSetUp()
    return token


# ── Microsoft ───────────────────────────────────────────────────────────

_metadata: dict[str, tuple[float, dict]] = {}
_jwks: dict[str, tuple[float, dict]] = {}


async def _get_json(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SignInFailed("Microsoft sign-in is unavailable right now.", 502) from exc
    if not isinstance(data, dict):
        raise SignInFailed("Microsoft sign-in is unavailable right now.", 502)
    return data


async def _cached(cache: dict, url: str, refresh: bool = False) -> dict:
    hit = cache.get(url)
    if hit and not refresh and time.monotonic() - hit[0] < _METADATA_TTL:
        return hit[1]
    data = await _get_json(url)
    cache[url] = (time.monotonic(), data)
    return data


async def _openid_metadata(config: MicrosoftConfig) -> dict:
    return await _cached(_metadata, f"{config.issuer}/.well-known/openid-configuration")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def begin_microsoft(config: MicrosoftConfig, next_path: str) -> tuple[str, str]:
    """(authorize URL to send the browser to, flow cookie value)."""
    metadata = await _openid_metadata(config)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = _pkce_pair()
    query = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": config.redirect_url,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    flow = auth.sign_flow(
        {"state": state, "nonce": nonce, "verifier": verifier, "next": next_path}
    )
    return f"{metadata['authorization_endpoint']}?{query}", flow


async def _signing_key(config: MicrosoftConfig, metadata: dict, kid: str) -> Any:
    uri = str(metadata.get("jwks_uri") or "")
    for refresh in (False, True):
        keys = await _cached(_jwks, uri, refresh=refresh)
        for jwk in keys.get("keys") or []:
            if isinstance(jwk, dict) and jwk.get("kid") == kid:
                return jwt.PyJWK.from_dict(jwk).key
    raise SignInFailed("Microsoft sign-in could not be verified.")


async def verify_id_token(config: MicrosoftConfig, id_token: str, nonce: str) -> dict:
    """The id token's claims once every check passes; SignInFailed otherwise."""
    metadata = await _openid_metadata(config)
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise SignInFailed("Microsoft sign-in could not be verified.") from exc
    if header.get("alg") != "RS256":
        raise SignInFailed("Microsoft sign-in could not be verified.")
    key = await _signing_key(config, metadata, str(header.get("kid") or ""))
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=config.client_id,
            issuer=config.issuer,
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "aud", "iss", "nonce", "tid", "oid"]},
        )
    except jwt.PyJWTError as exc:
        raise SignInFailed("Microsoft sign-in could not be verified.") from exc
    if claims.get("tid") != config.tenant_id:
        raise SignInFailed("That Microsoft account belongs to another organization.", 403)
    if not hmac.compare_digest(str(claims.get("nonce")), nonce):
        raise SignInFailed("Microsoft sign-in could not be verified.")
    return claims


def verified_email(claims: dict) -> str | None:
    """The token's email, unless the token itself says it is unverified."""
    if claims.get("email_verified") is False or claims.get("xms_edov") is False:
        return None
    email = str(claims.get("email") or "").strip()
    return email or None


async def finish_microsoft(
    config: MicrosoftConfig, *, code: str, state: str, flow_cookie: str
) -> tuple[dict, dict]:
    """Exchange the code and verify the id token.

    Returns (verified flow, claims). Raises SignInFailed.
    """
    flow = auth.read_flow(flow_cookie)
    if not flow or not state or not hmac.compare_digest(str(flow.get("state")), state):
        raise SignInFailed("Microsoft sign-in was not started from this browser. Try again.", 400)
    if not code:
        raise SignInFailed("Microsoft sign-in was cancelled.")
    metadata = await _openid_metadata(config)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                str(metadata.get("token_endpoint") or ""),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": config.redirect_url,
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "code_verifier": str(flow.get("verifier")),
                },
            )
    except httpx.HTTPError as exc:
        raise SignInFailed("Microsoft sign-in is unavailable right now.", 502) from exc
    if response.status_code != 200:
        raise SignInFailed("Microsoft sign-in could not be verified.")
    try:
        id_token = str(response.json().get("id_token") or "")
    except ValueError as exc:
        raise SignInFailed("Microsoft sign-in could not be verified.") from exc
    claims = await verify_id_token(config, id_token, str(flow.get("nonce")))
    return flow, claims


def microsoft_subject(claims: dict) -> str:
    return f"{claims['tid']}/{claims['oid']}"


# ── Windows ─────────────────────────────────────────────────────────────


def negotiate_token(header: str) -> bytes | None:
    scheme, _, value = (header or "").partition(" ")
    if scheme.lower() != "negotiate" or not value.strip():
        return None
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (ValueError, TypeError):
        return b""


def is_ntlm(token: bytes) -> bool:
    return token.startswith(_NTLM_SIGNATURE)


def accept_security_context(token: bytes, keytab: str, spn: str) -> tuple[str, bytes | None]:
    """Accept a SPNEGO/Kerberos token with the service key from `keytab`.

    Returns (client principal, reply token). This is where the process stops
    being ours: everything past it is the Kerberos library.
    """
    try:
        import gssapi
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise KerberosRefused("python-gssapi is not installed") from exc
    try:
        name = gssapi.Name(spn, gssapi.NameType.kerberos_principal)
        creds = gssapi.Credentials(name=name, usage="accept", store={"keytab": keytab})
        context = gssapi.SecurityContext(creds=creds, usage="accept")
        reply = context.step(token)
        if not context.complete:
            raise KerberosRefused("the token needs another round; only Kerberos is accepted")
        return str(context.initiator_name), reply
    except gssapi.exceptions.GSSError as exc:  # pragma: no cover - library
        raise KerberosRefused(str(exc)) from exc


def domain_account_for(principal: str, config: WindowsConfig) -> str | None:
    """`user@REALM` as `DOMAIN\\user`.

    The realm's domain is HIVE_KERBEROS_REALM_MAP's entry for it; with no
    map configured, the realm's first label. With a map configured, a realm
    it does not name maps to nothing, so a trusted foreign realm cannot
    sign in by accident. A principal with an instance (`user/admin@REALM`)
    is a service or admin principal, never a person's account.
    """
    user, sep, realm = principal.rpartition("@")
    if not sep or not user or not realm or "/" in user:
        return None
    realm = realm.upper()
    domain = config.realm_map.get(realm) if config.realm_map else realm.split(".", 1)[0]
    return f"{domain}\\{user}" if domain else None
