"""A stand-in Microsoft Entra tenant, served over real HTTP.

It is the identity provider's side of the authorization-code flow and
nothing more: discovery, an authorize endpoint that "signs the user in" as
whoever the test names, a token endpoint that checks the client secret and
the PKCE verifier, and a JWKS endpoint serving the public half of a key
generated per run. Every id token it hands out is really signed, so what the
console verifies is a real signature against a key it fetched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from jwt.algorithms import RSAAlgorithm
from multiuser_support import ThreadedServer

TENANT_ID = "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
OTHER_TENANT_ID = "0a0b0c0d-1111-2222-3333-444455556666"
CLIENT_ID = "9d8c7b6a-5f4e-4d3c-2b1a-0f9e8d7c6b5a"
CLIENT_SECRET = "entra-test-client-secret"  # secret-guard: allow
REDIRECT_URL = "https://testserver/login/microsoft/callback"
KEY_ID = "test-signing-key"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mock_entra_app(state: dict) -> FastAPI:
    app = FastAPI()

    def issuer(tenant: str) -> str:
        return f"{state['url']}/{tenant}/v2.0"

    @app.get("/{tenant}/v2.0/.well-known/openid-configuration")
    async def discovery(tenant: str):
        return {
            "issuer": issuer(tenant),
            "authorization_endpoint": f"{state['url']}/{tenant}/oauth2/v2.0/authorize",
            "token_endpoint": f"{state['url']}/{tenant}/oauth2/v2.0/token",
            "jwks_uri": f"{state['url']}/{tenant}/discovery/v2.0/keys",
        }

    @app.get("/{tenant}/discovery/v2.0/keys")
    async def keys(tenant: str):
        jwk = json.loads(RSAAlgorithm.to_jwk(state["key"].public_key()))
        jwk.update({"kid": KEY_ID, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    @app.get("/{tenant}/oauth2/v2.0/authorize")
    async def authorize(tenant: str, request: Request):
        q = request.query_params
        if (
            q.get("client_id") != CLIENT_ID
            or q.get("response_type") != "code"
            or q.get("code_challenge_method") != "S256"
            or "openid" not in (q.get("scope") or "").split()
            or not q.get("code_challenge")
        ):
            return JSONResponse({"error": "invalid_request"}, 400)
        code = secrets.token_urlsafe(16)
        state["codes"][code] = {
            "tenant": tenant,
            "redirect_uri": q["redirect_uri"],
            "challenge": q["code_challenge"],
            "nonce": q.get("nonce"),
            "identity": json.loads(q.get("test_identity") or "{}"),
        }
        query = urllib.parse.urlencode({"code": code, "state": q.get("state", "")})
        return RedirectResponse(f"{q['redirect_uri']}?{query}", 302)

    @app.post("/{tenant}/oauth2/v2.0/token")
    async def token(
        tenant: str,
        grant_type: str = Form(...),
        code: str = Form(...),
        redirect_uri: str = Form(...),
        client_id: str = Form(...),
        client_secret: str = Form(""),
        code_verifier: str = Form(""),
    ):
        grant = state["codes"].pop(code, None)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if (
            grant is None
            or grant_type != "authorization_code"
            or client_id != CLIENT_ID
            or client_secret != CLIENT_SECRET
            or redirect_uri != grant["redirect_uri"]
            or challenge != grant["challenge"]
        ):
            return JSONResponse({"error": "invalid_grant"}, 400)
        identity = dict(grant["identity"])
        sign_with = identity.pop("_sign_with", "tenant")
        now = int(time.time())
        claims = {
            "iss": issuer(grant["tenant"]),
            "aud": CLIENT_ID,
            "tid": grant["tenant"],
            "sub": "sub-" + str(identity.get("oid", "")),
            "nonce": grant["nonce"],
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "ver": "2.0",
        }
        claims.update(identity)
        claims = {k: v for k, v in claims.items() if v is not None}
        key = state["key"] if sign_with == "tenant" else state["rogue_key"]
        id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": KEY_ID})
        return {"token_type": "Bearer", "access_token": "unused", "id_token": id_token}

    return app


class MockEntra(ThreadedServer):
    def __init__(self):
        self._state: dict = {"codes": {}, "key": _key(), "rogue_key": _key()}
        super().__init__(mock_entra_app(self._state))
        self._state["url"] = self.url

    def sign_in(self, authorize_url: str, **identity) -> str:
        """The user signs in at the tenant as `identity` (claims to put in
        the id token, overriding the defaults). Returns the console callback
        path the browser is sent back to."""
        extra = urllib.parse.urlencode({"test_identity": json.dumps(identity)})
        response = httpx.get(f"{authorize_url}&{extra}", follow_redirects=False, timeout=10)
        assert response.status_code == 302, response.text
        location = urllib.parse.urlsplit(response.headers["location"])
        return f"{location.path}?{location.query}"
