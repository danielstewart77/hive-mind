"""A mind's own credential, and the gateway calls it guards.

Every call hive-comms makes to a mind carries a credential that mind can
check, and each mind's is its own: one taken off a kid's Windows box opens
that box and nothing else in the hive. The mind mints it, keeps it, and
publishes it only through the admin-guarded registration it already performs
on every boot.
"""

from __future__ import annotations

import os
import time
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
        open because a migration chowned its own directory.

        The message is asserted, not just the raise: an unreadable file is also
        unwritable, so a build that folded unreadable into absent would still
        raise — from the reclaim path, a second later, having first decided the
        file was abandoned and tried to mint over a credential that is sitting
        right there.
        """
        runtime_api.session_token(mind_dir)
        (mind_dir / "session_token").chmod(0o000)
        runtime_api._token_cache.clear()
        try:
            with pytest.raises(runtime_api.SessionTokenUnavailable) as refused:
                runtime_api.session_token(mind_dir)
            assert "cannot read" in str(refused.value)
        finally:
            (mind_dir / "session_token").chmod(0o600)

    def test_a_file_holding_non_utf8_bytes_is_refused_not_crashed_through(
        self, mind_dir
    ):
        (mind_dir / "session_token").write_bytes(b"\xff\xfe not text")
        runtime_api._token_cache.clear()
        with pytest.raises(runtime_api.SessionTokenUnavailable):
            runtime_api.session_token(mind_dir)

    def test_an_abandoned_empty_file_is_reclaimed_rather_than_waited_on(
        self, mind_dir
    ):
        """A mint killed between the create and the write leaves a zero-byte
        file no amount of waiting will fill. Waiting anyway cost a second of
        the event loop on every request, forever, for something only `rm` could
        fix."""
        path = mind_dir / "session_token"
        # Created wide, the way an older build or a loose umask would leave it.
        # Asserting 0600 against a file the test itself made 0600 asserts the
        # fixture, since writing to an existing file preserves its mode.
        path.touch(mode=0o644)
        stale = time.time() - 60
        os.utime(path, (stale, stale))

        token = runtime_api.session_token(mind_dir)
        assert token
        assert path.read_text().strip() == token
        assert path.stat().st_mode & 0o777 == 0o600

    def test_reclaiming_does_not_stall_the_event_loop(self, mind_dir):
        path = mind_dir / "session_token"
        path.touch(mode=0o600)
        stale = time.time() - 60
        os.utime(path, (stale, stale))

        started = time.monotonic()
        runtime_api.session_token(mind_dir)
        assert time.monotonic() - started < 0.1

    def test_a_fresh_empty_file_is_given_its_window_and_then_reclaimed(
        self, mind_dir, monkeypatch
    ):
        """It might be a race in flight, so it gets the window. Nothing
        arrives, so it is reclaimed — bounded, and never a wait that repeats on
        every later request."""
        monkeypatch.setattr(runtime_api, "_RACE_WINDOW_S", 0.05)
        monkeypatch.setattr(runtime_api, "_RACE_PAUSE_S", 0.01)
        path = mind_dir / "session_token"
        path.touch(mode=0o600)

        started = time.monotonic()
        token = runtime_api.session_token(mind_dir)
        waited = time.monotonic() - started

        assert token
        assert waited >= 0.05, "a file that might be mid-write was not waited on"
        assert waited < 1.0

    def test_a_token_written_mid_race_is_adopted_not_overwritten(
        self, mind_dir, monkeypatch
    ):
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
class TestTheDeployedAppsAreGuarded:
    """`install_session_guard` is one line in each harness server, and every
    other test of those apps supplies the credential. Without these, both files
    that *are* the deployed minds can lose the guard and nothing says so."""

    @pytest.mark.parametrize("module", ["claude_cli", "codex_cli"])
    def test_an_uncredentialed_spawn_is_refused(self, module):
        import importlib

        harness = importlib.import_module(f"minds.harness.{module}")
        client = TestClient(harness.app)
        response = client.post(
            "/sessions",
            json={"session_id": "s1", "resume_sid": "c1", "model": "sonnet"},
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("module", ["claude_cli", "codex_cli"])
    def test_an_uncredentialed_kill_is_refused(self, module):
        import importlib

        harness = importlib.import_module(f"minds.harness.{module}")
        assert TestClient(harness.app).delete("/sessions/s1").status_code == 401

    @pytest.mark.parametrize("module", ["claude_cli", "codex_cli"])
    def test_the_config_surface_is_still_reachable(self, module):
        """The guard is scoped to /sessions and must not have swallowed the
        routes the console reads."""
        import importlib

        harness = importlib.import_module(f"minds.harness.{module}")
        assert TestClient(harness.app).get("/runtime").status_code == 200
