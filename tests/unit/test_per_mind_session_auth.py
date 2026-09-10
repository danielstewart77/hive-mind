"""A mind's own credential, and the gateway calls it guards.

Every call hive-comms makes to a mind carries a credential that mind can
check, and each mind's is its own: one taken off a kid's Windows box opens
that box and nothing else in the hive. The mind mints it, keeps it, and
publishes it only through the admin-guarded registration it already performs
on every boot.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from minds import runtime_api

RUNTIME = """\
name: example
mind_id: 565e5a66-d20c-4266-872a-3268c4c894fc
gateway_url: http://example:8420
harness: claude_cli
provider: anthropic
default_model: sonnet
"""


@pytest.fixture()
def mind_dir(tmp_path):
    (tmp_path / "runtime.yaml").write_text(RUNTIME)
    return tmp_path


@pytest.fixture(autouse=True)
def no_injected_token():
    """The file is the subject here; an ambient env var would mask it."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MIND_SESSION_TOKEN", None)
        yield


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "path": "/sessions"})


# ---------------------------------------------------------------------------
# R3 — the mind's credential is its own to make and its own to keep
# ---------------------------------------------------------------------------
class TestMintingTheToken:
    def test_mints_one_when_there_is_none(self, mind_dir):
        token = runtime_api.session_token(mind_dir)
        assert token
        assert (mind_dir / "session_token").read_text().strip() == token

    def test_only_the_mind_can_read_the_file(self, mind_dir):
        runtime_api.session_token(mind_dir)
        mode = (mind_dir / "session_token").stat().st_mode & 0o777
        assert mode == 0o600

    def test_the_same_token_comes_back_on_every_later_ask(self, mind_dir):
        first = runtime_api.session_token(mind_dir)
        assert runtime_api.session_token(mind_dir) == first
        # A restart is a fresh read of the same directory and nothing else.
        assert runtime_api.session_token(mind_dir) == first

    def test_an_injected_token_overrides_the_file(self, mind_dir):
        runtime_api.session_token(mind_dir)
        with patch.dict(os.environ, {"MIND_SESSION_TOKEN": "from-the-env"}):
            assert runtime_api.session_token(mind_dir) == "from-the-env"

    def test_an_unwritable_directory_yields_no_token_rather_than_raising(self, tmp_path):
        missing = tmp_path / "nowhere" / "deeper"
        assert runtime_api.session_token(missing) == ""


class TestTheTokenIsNotServed:
    def test_the_runtime_view_does_not_carry_it(self, mind_dir):
        runtime_api.session_token(mind_dir)
        view = runtime_api.public_runtime(mind_dir / "runtime.yaml")
        assert "session_token" not in view
        assert runtime_api.session_token(mind_dir) not in str(view)

    def test_the_registration_payload_does_carry_it(self, mind_dir):
        payload = runtime_api.registration_payload(mind_dir / "runtime.yaml", "example")
        assert payload["session_token"] == runtime_api.session_token(mind_dir)

    def test_a_mind_with_no_token_registers_without_the_field(self, tmp_path):
        (tmp_path / "runtime.yaml").write_text(RUNTIME)
        with patch.object(runtime_api, "session_token", return_value=""):
            payload = runtime_api.registration_payload(
                tmp_path / "runtime.yaml", "example"
            )
        assert "session_token" not in payload


# ---------------------------------------------------------------------------
# R6 / R7 / R8 — what the guard admits
# ---------------------------------------------------------------------------
class TestTheSessionGuard:
    def test_the_right_token_is_admitted(self, mind_dir):
        token = runtime_api.session_token(mind_dir)
        headers = {"Authorization": f"Bearer {token}"}
        assert runtime_api.authorize_session(_request(headers), mind_dir) is None

    def test_no_credential_is_refused(self, mind_dir):
        runtime_api.session_token(mind_dir)
        denied = runtime_api.authorize_session(_request(), mind_dir)
        assert isinstance(denied, JSONResponse)
        assert denied.status_code == 401

    def test_the_wrong_credential_is_refused(self, mind_dir):
        runtime_api.session_token(mind_dir)
        headers = {"Authorization": "Bearer not-this-mind's-token"}
        denied = runtime_api.authorize_session(_request(headers), mind_dir)
        assert denied is not None
        assert denied.status_code == 401

    def test_the_admin_token_also_opens_a_session_route(self, mind_dir):
        runtime_api.session_token(mind_dir)
        headers = {"Authorization": "Bearer the-admin-token"}
        with patch.dict(os.environ, {"MIND_ADMIN_TOKEN": "the-admin-token"}):
            assert runtime_api.authorize_session(_request(headers), mind_dir) is None

    def test_a_subprotocol_credential_is_admitted(self, mind_dir):
        """A browser attaching directly cannot set a header."""
        token = runtime_api.session_token(mind_dir)
        headers = {"Sec-WebSocket-Protocol": f"bearer.{token}"}
        assert runtime_api.authorize_session(_request(headers), mind_dir) is None

    def test_a_mind_holding_no_token_serves_as_it_always_did(self, tmp_path):
        """The rollout: a mind that cannot write one is reachable, not dark."""
        unwritable = tmp_path / "nowhere" / "deeper"
        assert runtime_api.authorize_session(_request(), unwritable) is None
        headers = {"Authorization": "Bearer anything-at-all"}
        assert runtime_api.authorize_session(_request(headers), unwritable) is None


class TestTheGuardOnRealRoutes:
    @pytest.fixture()
    def client(self, mind_dir):
        app = FastAPI()
        runtime_api.install_session_guard(app, mind_dir=mind_dir)

        @app.post("/sessions")
        async def create():  # pragma: no cover - exercised through the client
            return {"ok": True}

        @app.delete("/sessions/{sid}")
        async def kill(sid: str):  # pragma: no cover
            return {"ok": True}

        @app.get("/runtime")
        async def runtime():  # pragma: no cover
            return {"configuration": {}}

        return TestClient(app)

    @pytest.mark.parametrize(
        ("method", "path"), [("post", "/sessions"), ("delete", "/sessions/abc")]
    )
    def test_session_routes_refuse_an_unauthenticated_caller(
        self, client, mind_dir, method, path
    ):
        assert getattr(client, method)(path).status_code == 401

    @pytest.mark.parametrize(
        ("method", "path"), [("post", "/sessions"), ("delete", "/sessions/abc")]
    )
    def test_session_routes_admit_the_mind_s_own_token(
        self, client, mind_dir, method, path
    ):
        token = runtime_api.session_token(mind_dir)
        response = getattr(client, method)(
            path, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200

    def test_the_guard_leaves_the_config_surface_alone(self, client, mind_dir):
        """R10: /runtime answers to the admin guard, not to this one."""
        runtime_api.session_token(mind_dir)
        assert client.get("/runtime").status_code == 200


# ---------------------------------------------------------------------------
# R10 — the session credential does not unlock the config surface
# ---------------------------------------------------------------------------
class TestTheConfigSurfaceIsSeparate:
    def test_a_session_token_is_refused_by_the_admin_guard(self, mind_dir):
        token = runtime_api.session_token(mind_dir)
        headers = {"Authorization": f"Bearer {token}"}
        with patch.dict(os.environ, {"MIND_ADMIN_TOKEN": "the-admin-token"}):
            denied = runtime_api.authorize_admin(_request(headers))
        assert denied is not None
        assert denied.status_code == 401

    def test_the_admin_guard_still_refuses_when_nothing_is_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            denied = runtime_api.authorize_admin(_request())
        assert denied is not None
        assert denied.status_code == 503


# ---------------------------------------------------------------------------
# R9 — refusing a terminal attach says which kind of no it is
# ---------------------------------------------------------------------------
class TestRefusingAWebsocket:
    async def test_a_denial_response_is_preferred(self):
        sent = []

        class Socket:
            async def send_denial_response(self, response):
                sent.append(response)

        denial = JSONResponse({"error": "unauthorized"}, status_code=401)
        await runtime_api.refuse_session_websocket(Socket(), denial)
        assert sent == [denial]

    async def test_a_server_without_the_extension_falls_back_to_a_close(self):
        closed = {}

        class Socket:
            async def send_denial_response(self, response):
                raise RuntimeError("extension unsupported")

            async def close(self, code, reason):
                closed.update(code=code, reason=reason)

        await runtime_api.refuse_session_websocket(
            Socket(), JSONResponse({}, status_code=401)
        )
        assert closed["code"] == 4401
