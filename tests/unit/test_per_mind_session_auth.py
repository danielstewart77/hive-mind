"""What the gateway shows a mind, and the gateway calls it guards.

Every call hive-comms makes to a mind carries a value that mind can check. It is
made from the mind's one key (feature 834, story 902): the mind works out the
same value from the key it holds and compares. Nothing else is stored on the
mind: no second secret file, no MIND_SESSION_TOKEN.
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core import mind_key
from minds import runtime_api

RUNTIME = """\
name: example
mind_id: 565e5a66-d20c-4266-872a-3268c4c894fc
gateway_url: http://example:8420
harness: claude_cli
provider: anthropic
default_model: sonnet
"""


MIND_KEY = "the-one-key-of-this-mind-0001"
OTHER_KEY = "the-one-key-of-another-mind-02"


@pytest.fixture()
def mind_dir(tmp_path):
    (tmp_path / "runtime.yaml").write_text(RUNTIME)
    return tmp_path


@pytest.fixture(autouse=True)
def the_minds_one_key(monkeypatch):
    """The mind's one key is the subject; ambient values would mask it."""
    for gone in ("MIND_SESSION_TOKEN", "MIND_KEY_FILE", "COMMS_BEARER_TOKEN"):
        monkeypatch.delenv(gone, raising=False)
    monkeypatch.setenv("MIND_KEY", MIND_KEY)
    yield


def shown(key):
    """What the gateway presents for a mind holding this key."""
    return mind_key.gateway_credential(key)


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "path": "/sessions"})


# ---------------------------------------------------------------------------
# B13 — what the gateway shows a mind is made from the mind's one key
# ---------------------------------------------------------------------------
class TestWhatTheGatewayShowsIsMadeFromTheKey:
    def test_the_value_made_from_this_minds_key_is_accepted(self, mind_dir):
        headers = {"Authorization": f"Bearer {shown(MIND_KEY)}"}
        assert runtime_api.authorize_session(_request(headers), mind_dir) is None

    def test_the_value_made_from_another_minds_key_is_refused(self, mind_dir):
        headers = {"Authorization": f"Bearer {shown(OTHER_KEY)}"}
        denied = runtime_api.authorize_session(_request(headers), mind_dir)
        assert denied is not None and denied.status_code == 401

    def test_after_the_key_is_replaced_the_old_value_is_refused_and_the_new_one_taken(
        self, mind_dir, tmp_path, monkeypatch
    ):
        key_file = tmp_path / "key"
        key_file.write_text(MIND_KEY + "\n")
        monkeypatch.delenv("MIND_KEY")
        monkeypatch.setenv("MIND_KEY_FILE", str(key_file))
        old = {"Authorization": f"Bearer {shown(MIND_KEY)}"}
        assert runtime_api.authorize_session(_request(old), mind_dir) is None

        replaced = tmp_path / "key.new"
        replaced.write_text(OTHER_KEY + "\n")
        os.replace(replaced, key_file)

        denied = runtime_api.authorize_session(_request(old), mind_dir)
        assert denied is not None and denied.status_code == 401
        new = {"Authorization": f"Bearer {shown(OTHER_KEY)}"}
        assert runtime_api.authorize_session(_request(new), mind_dir) is None

    def test_the_raw_key_is_not_what_the_guard_takes(self, mind_dir):
        headers = {"Authorization": f"Bearer {MIND_KEY}"}
        denied = runtime_api.authorize_session(_request(headers), mind_dir)
        assert denied is not None and denied.status_code == 401

    def test_the_value_does_not_contain_or_equal_the_key_and_differs_from_its_fingerprint(self):
        import hashlib

        value = shown(MIND_KEY)
        assert MIND_KEY not in value
        assert value != hashlib.sha256(MIND_KEY.encode()).hexdigest()
        assert value != shown(OTHER_KEY)

    def test_no_session_token_file_is_made_by_asking_for_the_value(self, mind_dir):
        runtime_api.session_token(mind_dir)
        runtime_api.authorize_session(_request(), mind_dir)
        assert not (mind_dir / "session_token").exists()

    def test_a_session_token_env_value_is_not_read(self, mind_dir, monkeypatch):
        monkeypatch.setenv("MIND_SESSION_TOKEN", "injected-by-an-old-install")
        headers = {"Authorization": "Bearer injected-by-an-old-install"}
        denied = runtime_api.authorize_session(_request(headers), mind_dir)
        assert denied is not None and denied.status_code == 401

    def test_an_old_file_is_not_a_fallback_and_is_deleted_when_the_mind_starts_on_its_key(
        self, mind_dir
    ):
        old = mind_dir / "session_token"
        old.write_text("the-old-second-secret\n")
        headers = {"Authorization": "Bearer the-old-second-secret"}
        denied = runtime_api.authorize_session(_request(headers), mind_dir)
        assert denied is not None and denied.status_code == 401
        assert runtime_api.retire_session_token_file(mind_dir) is True
        assert not old.exists()

    def test_a_mind_with_no_key_leaves_an_old_file_alone_and_refuses_everyone(
        self, mind_dir, monkeypatch
    ):
        monkeypatch.delenv("MIND_KEY")
        old = mind_dir / "session_token"
        old.write_text("the-old-second-secret\n")
        assert runtime_api.retire_session_token_file(mind_dir) is False
        for bearer in ("the-old-second-secret", shown(MIND_KEY), ""):
            headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
            denied = runtime_api.authorize_session(_request(headers), mind_dir)
            assert denied is not None and denied.status_code == 503


class TestTheTokenIsNotServed:
    def test_the_runtime_view_does_not_carry_it(self, mind_dir):
        view = runtime_api.public_runtime(mind_dir / "runtime.yaml")
        assert "session_token" not in view
        assert runtime_api.session_token(mind_dir) not in str(view)

    def test_the_registration_payload_carries_no_secret(self, mind_dir):
        payload = runtime_api.registration_payload(mind_dir / "runtime.yaml", "example")
        assert "session_token" not in payload
        assert MIND_KEY not in str(payload)
        assert shown(MIND_KEY) not in str(payload)


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

    def test_a_hive_administrator_key_opens_no_session_route(self, mind_dir):
        runtime_api.session_token(mind_dir)
        headers = {"Authorization": "Bearer the-admin-token"}
        with patch.dict(os.environ, {"MIND_ADMIN_TOKEN": "the-admin-token",
                                     "COMMS_ADMIN_BEARER_TOKEN": "the-admin-token"}):
            denied = runtime_api.authorize_session(_request(headers), mind_dir)
        assert denied is not None and denied.status_code == 401

    def test_a_subprotocol_credential_is_admitted(self, mind_dir):
        """A browser attaching directly cannot set a header."""
        token = runtime_api.session_token(mind_dir)
        headers = {"Sec-WebSocket-Protocol": f"bearer.{token}"}
        assert runtime_api.authorize_session(_request(headers), mind_dir) is None

    def test_a_non_credential_subprotocol_is_the_one_echoed_back(self):
        """Whatever is echoed lands in the response headers and the proxy's
        logs, so a client offering its credential alongside a plain protocol
        gets the plain one back."""
        request = _request({"Sec-WebSocket-Protocol": "bearer.sekrit, hive.terminal"})
        assert runtime_api.negotiated_protocol(request) == "hive.terminal"

    def test_a_client_offering_only_its_credential_still_gets_a_handshake(self):
        """A usable terminal beats a tidy log."""
        request = _request({"Sec-WebSocket-Protocol": "bearer.sekrit"})
        assert runtime_api.negotiated_protocol(request) == "bearer.sekrit"

    def test_a_client_offering_nothing_negotiates_nothing(self):
        assert runtime_api.negotiated_protocol(_request()) is None

    def test_a_bare_subprotocol_credential_is_admitted(self, mind_dir):
        """The other minds in the hive accept the bare form; a console that
        works against one must work against all of them."""
        token = runtime_api.session_token(mind_dir)
        headers = {"Sec-WebSocket-Protocol": token}
        assert runtime_api.authorize_session(_request(headers), mind_dir) is None

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

    def test_the_guard_leaves_the_config_surface_alone(self, client, mind_dir):
        """R10: /runtime answers to the admin guard, not to this one."""
        runtime_api.session_token(mind_dir)
        assert client.get("/runtime").status_code == 200


# ---------------------------------------------------------------------------
# Feature 834, requirements 23, 25, 26 (B6): the configuration surface takes
# the gateway's credential and the mind's own key, and no one else's
# ---------------------------------------------------------------------------
class TestTheConfigSurfaceTakesTheGatewayAndTheMindOnly:
    @pytest.fixture(autouse=True)
    def bound(self, mind_dir):
        runtime_api.bind_mind_dir(mind_dir)
        yield
        runtime_api._mind_dir = None

    @staticmethod
    def _ask(bearer, **env):
        headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
        with patch.dict(os.environ, env, clear=False):
            for gone in ("MIND_KEY", "COMMS_BEARER_TOKEN"):
                if gone not in env:
                    os.environ.pop(gone, None)
            return asyncio.run(runtime_api.authorize_admin(_request(headers)))

    def test_the_gateways_credential_is_taken(self, mind_dir):
        token = shown("the-mind-key")
        assert self._ask(token, MIND_KEY="the-mind-key") is None

    def test_the_minds_own_key_is_taken(self):
        assert self._ask("the-mind-key", MIND_KEY="the-mind-key") is None

    def test_a_key_that_is_neither_is_refused_and_the_gateway_is_never_asked(self, monkeypatch):
        # Any value a person's key, an operator's key or a stranger's could
        # be: the mind has no way to ask anyone, so it is a plain 401.
        monkeypatch.setenv("COMMS_URL", "http://127.0.0.1:9")
        for presented in ("a-persons-key", "an-operator-minds-key", "x", ""):
            denied = self._ask(presented, MIND_KEY="the-mind-key")
            assert denied is not None and denied.status_code == 401, presented
        assert self._ask(None, MIND_KEY="the-mind-key").status_code == 401

    def test_a_keyless_mind_has_no_gateway_credential_and_refuses_everyone(self):
        for bearer in (shown(MIND_KEY), "a-persons-key", ""):
            denied = self._ask(bearer)
            assert denied is not None and denied.status_code == 503, bearer

    def test_the_owner_key_question_is_gone_from_the_mind(self):
        assert not hasattr(runtime_api, "owner_key_check")

    def test_the_admin_guard_still_refuses_when_nothing_is_configured(self):
        denied = self._ask(None)
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


# ---------------------------------------------------------------------------
# R9 — the terminal attach, as the route rather than as the guard function
# ---------------------------------------------------------------------------
class TestTheAttachRouteIsGuarded:
    """`install_pty_attach` takes `mind_dir` optionally, so a caller that
    forgets it mounts an unguarded interactive shell. Nothing else in the suite
    passes it — every other pty test installs without one — so without these
    the guard block can be deleted outright and the suite stays green."""

    @pytest.fixture()
    def attach_client(self, mind_dir):
        from minds import pty_attach

        app = FastAPI()
        pty_attach.install_pty_attach(
            app, mind_name="testmind", terminals=_FakeTerminals(),
            spawn=_refuse_to_spawn, mind_dir=mind_dir,
        )
        return TestClient(app)

    def test_an_uncredentialed_attach_is_refused(self, attach_client, mind_dir):
        from starlette.testclient import WebSocketDenialResponse

        runtime_api.session_token(mind_dir)
        with pytest.raises(WebSocketDenialResponse) as refused:
            with attach_client.websocket_connect(
                "/sessions/abc/attach-pty?resume_sid=conv-1&model=sonnet"
            ):
                pass
        assert refused.value.status_code == 401

    def test_a_wrong_credential_is_refused(self, attach_client, mind_dir):
        from starlette.testclient import WebSocketDenialResponse

        runtime_api.session_token(mind_dir)
        with pytest.raises(WebSocketDenialResponse) as refused:
            with attach_client.websocket_connect(
                "/sessions/abc/attach-pty?resume_sid=conv-1&model=sonnet",
                headers={"Authorization": "Bearer not-this-mind's-token"},
            ):
                pass
        assert refused.value.status_code == 401

    def test_the_mind_s_own_token_gets_past_the_guard(self, attach_client, mind_dir):
        """Past the guard specifically: the spawn beyond it refuses on purpose,
        so what this proves is that the credential is not what stopped it."""
        from starlette.testclient import WebSocketDenialResponse

        token = runtime_api.session_token(mind_dir)
        with pytest.raises(Exception) as stopped:
            with attach_client.websocket_connect(
                "/sessions/abc/attach-pty?resume_sid=conv-1&model=sonnet",
                headers={"Authorization": f"Bearer {token}"},
            ) as socket:
                socket.receive_bytes()
        assert not isinstance(stopped.value, WebSocketDenialResponse), (
            "the mind's own token was refused by the guard"
        )

    def test_the_handshake_echoes_a_non_credential_subprotocol(self, mind_dir):
        """Asserted through the route, not just on the pure function: without
        the wiring a browser offering subprotocols fails the handshake, and the
        pure-function tests would not notice."""
        from minds import pty_attach

        token = runtime_api.session_token(mind_dir)
        app = FastAPI()
        pty_attach.install_pty_attach(
            app, mind_name="testmind", terminals=_FakeTerminals(),
            spawn=_refuse_to_spawn, mind_dir=mind_dir,
        )
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/sessions/abc/attach-pty?resume_sid=conv-1&model=sonnet",
                subprotocols=[f"bearer.{token}", "hive.terminal"],
            ) as socket:
                assert socket.accepted_subprotocol == "hive.terminal"
                socket.receive_bytes()


class _FakeTerminals:
    def session_name(self, session_id):
        return f"mind-{session_id}"

    def is_live(self, session_id):
        return False


def _refuse_to_spawn(**kwargs):
    from minds import pty_attach

    raise pty_attach.PtyUnavailable("no tmux in this test")


# ---------------------------------------------------------------------------
# The guard on the apps that actually ship
# ---------------------------------------------------------------------------
class TestTheDeployedAppIsGuarded:
    """`install_session_guard` is one line in `minds.mind_server`, and every
    other test of that app supplies the credential. Without these, the file
    that *is* the deployed mind can lose the guard and nothing says so."""

    def test_an_uncredentialed_spawn_is_refused(self):
        from minds import mind_server

        response = TestClient(mind_server.app).post(
            "/sessions",
            json={"session_id": "s1", "resume_sid": "c1", "model": "sonnet"},
        )
        assert response.status_code == 401

    def test_an_uncredentialed_kill_is_refused(self):
        from minds import mind_server

        assert TestClient(mind_server.app).delete("/sessions/s1").status_code == 401

    def test_the_config_surface_answers_its_own_guard_not_the_session_one(self, monkeypatch):
        """The session guard is scoped to `/sessions` and must not have
        swallowed the routes the console reads. `/runtime` has an admin guard
        of its own, so what this pins is that it is reached and refused on
        those terms rather than by the middleware."""
        # The admin guard answers 503 until a mind has an admin token at all;
        # the suite names one rather than borrowing the invoking shell's.
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "an-admin-token")
        from minds import mind_server, runtime_api

        client = TestClient(mind_server.app)
        assert client.get("/runtime").status_code == 401
        # The session credential does not open it, which is what tells the
        # two guards apart: the session middleware would have admitted this.
        assert client.get(
            "/runtime", headers={"Authorization": "Bearer session-token"}
        ).status_code == 401
