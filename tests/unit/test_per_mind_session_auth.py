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
    # The token is cached per process, so a test inheriting the previous
    # test's value would never touch the file it means to be asserting on.
    runtime_api._token_cache.clear()
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

    def test_the_token_is_read_once_per_process(self, mind_dir):
        """The guard asks on every request; the file is read on the first."""
        token = runtime_api.session_token(mind_dir)
        (mind_dir / "session_token").unlink()
        assert runtime_api.session_token(mind_dir) == token

    def test_an_unwritable_directory_refuses_rather_than_serving_open(self, tmp_path):
        missing = tmp_path / "nowhere" / "deeper"
        with pytest.raises(runtime_api.SessionTokenUnavailable):
            runtime_api.session_token(missing)

    def test_an_unreadable_file_is_not_treated_as_an_absent_one(self, mind_dir):
        """Folding the two together is how a mind serves every session route
        open because a migration chowned its own directory."""
        runtime_api.session_token(mind_dir)
        (mind_dir / "session_token").chmod(0o000)
        runtime_api._token_cache.clear()
        try:
            with pytest.raises(runtime_api.SessionTokenUnavailable):
                runtime_api.session_token(mind_dir)
        finally:
            (mind_dir / "session_token").chmod(0o600)

    def test_a_file_holding_non_utf8_bytes_is_refused_not_crashed_through(
        self, mind_dir
    ):
        (mind_dir / "session_token").write_bytes(b"\xff\xfe not text")
        runtime_api._token_cache.clear()
        with pytest.raises(runtime_api.SessionTokenUnavailable):
            runtime_api.session_token(mind_dir)

    def test_an_empty_file_left_by_a_lost_race_is_refused_not_overwritten(
        self, mind_dir, monkeypatch
    ):
        """`O_EXCL` creates the file before its winner writes into it. Minting
        a second token here would clobber a credential another process is
        already enforcing."""
        monkeypatch.setattr(runtime_api, "_RACE_READS", 2)
        monkeypatch.setattr(runtime_api, "_RACE_PAUSE_S", 0)
        path = mind_dir / "session_token"
        path.touch(mode=0o600)
        with pytest.raises(runtime_api.SessionTokenUnavailable):
            runtime_api.session_token(mind_dir)
        assert path.read_text() == "", "the empty file was overwritten"

    def test_a_token_written_mid_race_is_adopted(self, mind_dir, monkeypatch):
        path = mind_dir / "session_token"
        path.touch(mode=0o600)
        reads = {"n": 0}

        def _late_writer(_pause):
            reads["n"] += 1
            if reads["n"] == 2:
                path.write_text("the-winner-s-token\n")

        monkeypatch.setattr(runtime_api.time, "sleep", _late_writer)
        assert runtime_api.session_token(mind_dir) == "the-winner-s-token"


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

    def test_a_bare_subprotocol_credential_is_admitted(self, mind_dir):
        """The other minds in the hive accept the bare form; a console that
        works against one must work against all of them."""
        token = runtime_api.session_token(mind_dir)
        headers = {"Sec-WebSocket-Protocol": token}
        assert runtime_api.authorize_session(_request(headers), mind_dir) is None

    def test_a_mind_that_cannot_read_its_own_token_refuses_rather_than_opens(
        self, tmp_path
    ):
        """Serving open here would answer every caller on the LAN while the
        gateway went on presenting a token nobody checked."""
        denied = runtime_api.authorize_session(
            _request(), tmp_path / "nowhere" / "deeper"
        )
        assert denied is not None
        assert denied.status_code == 503

    def test_a_non_ascii_credential_is_refused_rather_than_raising(self, mind_dir):
        """On `str`, compare_digest raises TypeError — a 500 where a 401
        belongs, which the gateway then reads as a missing terminal route."""
        runtime_api.session_token(mind_dir)
        denied = runtime_api.authorize_session(
            _request({"Authorization": "Bearer \u00fc\u00e9"}), mind_dir
        )
        assert denied is not None
        assert denied.status_code == 401


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

    def test_a_host_header_cannot_move_a_route_out_of_the_guard_s_view(
        self, client, mind_dir
    ):
        """Starlette builds `request.url` from the Host header, so a Host
        carrying a "/" or "#" used to hide the path from the guard while the
        router still matched it."""
        runtime_api.session_token(mind_dir)
        for host in ("mind.test/", "mind.test#", "mind.test?x"):
            assert client.post("/sessions", headers={"Host": host}).status_code == 401

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
