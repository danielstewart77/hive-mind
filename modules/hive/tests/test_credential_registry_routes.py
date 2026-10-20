"""The credentials tab's own routes: the listing, one row's check, and the
per-mind GitHub token the console relays rather than writes.

What the registry builds has its own suite. These guard the route layer —
that the listing takes no live check, that a hive whose broker cannot be read
says so instead of rendering a clean page with no holders on it, that a
Mind's own refusal keeps its meaning, and that no value reaches a log line.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi import HTTPException

import auth
import credential_registry as registry
import main as main_mod


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


MINDS = [
    {
        "name": "cypher",
        "id": "cypher-uuid",
        "gateway_url": "http://cypher:8420",
        "last_seen": None,
    }
]


class TestTheListing:
    def test_it_carries_a_row_per_holder_beside_the_credentials(
        self, tmp_path, monkeypatch
    ) -> None:
        client = _authed_client(tmp_path, monkeypatch)
        registry.forget_all()

        with patch.object(main_mod, "_gateway_json", AsyncMock(return_value=MINDS)):
            body = client.get("/api/settings/credentials").json()

        assert body["registry"] == "ok"
        assert any(
            row["credential"] == "mind_github_token" and row["holder"] == "cypher"
            for row in body["rows"]
        )
        assert any(c["key"] == "comms_admin_bearer_token" for c in body["credentials"])

    def test_a_broker_it_cannot_read_is_said_rather_than_shown_as_empty(
        self, tmp_path, monkeypatch
    ) -> None:
        """A credentials page with no holders on it looks exactly like a
        credentials page with nothing wrong."""
        client = _authed_client(tmp_path, monkeypatch)
        registry.forget_all()

        with patch.object(
            main_mod, "_gateway_json", AsyncMock(side_effect=RuntimeError("down"))
        ):
            body = client.get("/api/settings/credentials").json()

        assert body["registry"] == "unreadable"
        # The shared secrets are read off this console's own mounts and are
        # still every bit as true.
        assert any(c["key"] == "comms_admin_bearer_token" for c in body["credentials"])

    def test_it_takes_no_live_check(self, tmp_path, monkeypatch) -> None:
        """This is the whole content of the page; waiting on a mind that is
        down, or on GitHub once per row, would hang it."""
        client = _authed_client(tmp_path, monkeypatch)
        registry.forget_all()
        checked = AsyncMock()
        monkeypatch.setattr(registry, "check", checked)

        with patch.object(main_mod, "_gateway_json", AsyncMock(return_value=MINDS)):
            body = client.get("/api/settings/credentials").json()

        checked.assert_not_awaited()
        row = next(r for r in body["rows"] if r["credential"] == "mind_github_token")
        assert row["status"] == registry.UNCHECKED

    def test_it_needs_a_session(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
        monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")

        assert TestClient(main_mod.app).get(
            "/api/settings/credentials"
        ).status_code == 401


class TestOneRowsCheck:
    def test_it_checks_the_credential_and_holder_it_was_asked_about(
        self, tmp_path, monkeypatch
    ) -> None:
        client = _authed_client(tmp_path, monkeypatch)
        answer = {"credential": "mind_github_token", "holder": "cypher",
                  "status": registry.WORKING, "names": "danielstewart77"}
        checked = AsyncMock(return_value=answer)
        monkeypatch.setattr(registry, "check", checked)

        with patch.object(main_mod, "_gateway_json", AsyncMock(return_value=MINDS)):
            response = client.post(
                "/api/settings/credentials/mind_github_token/check",
                json={"holder": "cypher"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == registry.WORKING
        assert checked.await_args.args[:2] == ("mind_github_token", "cypher")

    def test_a_credential_the_hive_does_not_have_is_refused(
        self, tmp_path, monkeypatch
    ) -> None:
        """The key arrives on a URL, and an entry per invented name is a
        dictionary that grows for as long as the console is up."""
        client = _authed_client(tmp_path, monkeypatch)
        checked = AsyncMock()
        monkeypatch.setattr(registry, "check", checked)

        response = client.post(
            "/api/settings/credentials/not_a_credential/check",
            json={"holder": "cypher"},
        )

        assert response.status_code == 404
        checked.assert_not_awaited()

    def test_a_broker_it_cannot_read_is_not_reported_as_an_absent_holder(
        self, tmp_path, monkeypatch
    ) -> None:
        """Otherwise every mind at once is remembered as "not in the
        registry" — a confident sentence, false, with nothing on screen
        attributing it to comms."""
        client = _authed_client(tmp_path, monkeypatch)
        registry.forget_all()
        checked = AsyncMock()
        monkeypatch.setattr(registry, "check", checked)

        with patch.object(
            main_mod, "_gateway_json", AsyncMock(side_effect=RuntimeError("down"))
        ):
            response = client.post(
                "/api/settings/credentials/mind_github_token/check",
                json={"holder": "cypher"},
            )

        assert response.status_code == 502
        checked.assert_not_awaited()
        assert registry.recall("mind_github_token", "cypher") is None

    def test_a_check_with_no_holder_is_refused(self, tmp_path, monkeypatch) -> None:
        client = _authed_client(tmp_path, monkeypatch)
        checked = AsyncMock()
        monkeypatch.setattr(registry, "check", checked)

        response = client.post(
            "/api/settings/credentials/mind_github_token/check", json={}
        )

        assert response.status_code == 400
        checked.assert_not_awaited()


class TestTheTokenTheConsoleRelays:
    def test_a_stored_token_names_the_account_and_owes_no_restart(
        self, tmp_path, monkeypatch
    ) -> None:
        client = _authed_client(tmp_path, monkeypatch)
        pushed = AsyncMock(return_value={
            "login": "danielstewart77",
            "where": "keyring:CYPHER_GITHUB_TOKEN",
            "configured": ["/home/hivemind/.git-credentials"],
            "restart_required": False,
        })
        with patch.object(
            main_mod, "_mind_gateway_url", AsyncMock(return_value="http://cypher:8420")
        ), patch.object(main_mod.mind_runtime, "push_github_token", pushed):
            response = client.put(
                "/api/minds/cypher/github-token", json={"token": "ghp_pasted"}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["login"] == "danielstewart77"
        assert body["restart_required"] is False
        assert pushed.await_args.args[1] == "ghp_pasted"

    def test_storing_it_fills_the_row_in_without_a_second_round_trip(
        self, tmp_path, monkeypatch
    ) -> None:
        """The Mind just verified. Asking again would spend another GitHub
        call to learn what it already said."""
        client = _authed_client(tmp_path, monkeypatch)
        registry.forget_all()
        pushed = AsyncMock(return_value={
            "login": "danielstewart77", "where": "keyring:CYPHER_GITHUB_TOKEN",
        })
        with patch.object(
            main_mod, "_mind_gateway_url", AsyncMock(return_value="http://cypher:8420")
        ), patch.object(main_mod.mind_runtime, "push_github_token", pushed):
            client.put("/api/minds/cypher/github-token", json={"token": "ghp_pasted"})

        entry = registry.recall("mind_github_token", "cypher")
        assert entry.status == registry.WORKING
        assert entry.names == "danielstewart77"

    def test_a_mind_that_refused_the_token_keeps_its_own_status(
        self, tmp_path, monkeypatch
    ) -> None:
        """A 400 from the Mind means the token was refused, not the
        credential this console presented."""
        client = _authed_client(tmp_path, monkeypatch)
        with patch.object(
            main_mod, "_mind_gateway_url", AsyncMock(return_value="http://cypher:8420")
        ), patch.object(
            main_mod.mind_runtime, "push_github_token",
            AsyncMock(side_effect=HTTPException(status_code=400, detail="GitHub rejected that token")),
        ):
            response = client.put(
                "/api/minds/cypher/github-token", json={"token": "ghp_bad"}
            )

        assert response.status_code == 400
        assert "rejected" in response.json()["detail"]

    def test_a_mind_that_cannot_store_one_keeps_that_status_too(
        self, tmp_path, monkeypatch
    ) -> None:
        """503 means this mind names no key of its own — a compose change,
        not a new token and not a trip to the machine."""
        client = _authed_client(tmp_path, monkeypatch)
        with patch.object(
            main_mod, "_mind_gateway_url", AsyncMock(return_value="http://bob:8420")
        ), patch.object(
            main_mod.mind_runtime, "push_github_token",
            AsyncMock(side_effect=HTTPException(status_code=503, detail="names no key")),
        ):
            response = client.put(
                "/api/minds/bob/github-token", json={"token": "ghp_pasted"}
            )

        assert response.status_code == 503

    def test_an_empty_token_never_reaches_the_mind(self, tmp_path, monkeypatch) -> None:
        client = _authed_client(tmp_path, monkeypatch)
        pushed = AsyncMock()
        with patch.object(main_mod.mind_runtime, "push_github_token", pushed):
            response = client.put("/api/minds/cypher/github-token", json={"token": "  "})

        assert response.status_code == 400
        pushed.assert_not_awaited()

    def test_it_needs_a_session(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
        monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")

        assert TestClient(main_mod.app).put(
            "/api/minds/cypher/github-token", json={"token": "ghp_x"}
        ).status_code == 401

    def test_the_audit_line_names_the_mind_and_the_account_and_not_the_token(
        self, tmp_path, monkeypatch
    ) -> None:
        """This log is shipped and searchable, and a value in it outlives
        every page that was careful with it."""
        client = _authed_client(tmp_path, monkeypatch)
        emitted: list[dict] = []
        monkeypatch.setattr(
            main_mod, "log_event",
            lambda logger, event, **fields: emitted.append({"event": event, **fields}),
        )
        with patch.object(
            main_mod, "_mind_gateway_url", AsyncMock(return_value="http://cypher:8420")
        ), patch.object(
            main_mod.mind_runtime, "push_github_token",
            AsyncMock(return_value={"login": "danielstewart77", "where": "keyring:CYPHER_GITHUB_TOKEN"}),
        ):
            client.put(
                "/api/minds/cypher/github-token", json={"token": "ghp_secretpaste"}
            )

        line = next(e for e in emitted if e["event"] == "mind.github_token.updated")
        assert line["mind"] == "cypher"
        assert line["login"] == "danielstewart77"
        assert "ghp_secretpaste" not in repr(line)


class TestRotation:
    def test_a_rotation_discards_every_remembered_check(
        self, tmp_path, monkeypatch
    ) -> None:
        """A stale "working" beside a freshly rotated holder is the one thing
        on this page nobody should believe."""
        client = _authed_client(tmp_path, monkeypatch)
        registry.remember("comms_admin_bearer_token", "skippy", status=registry.WORKING)

        async def rotated(service, holder):
            return {"service": service.key, "holder": holder, "file": "hive_mind",
                    "env_key": "COMMS_TOKEN_ADA", "restart_target": None}

        monkeypatch.setattr(main_mod.holder_credentials, "rotate", rotated)

        client.post("/api/settings/holders/comms/ada/rotate")

        assert registry.recall("comms_admin_bearer_token", "skippy") is None

    def test_a_rotation_on_a_service_this_console_does_not_know_is_a_404(
        self, tmp_path, monkeypatch
    ) -> None:
        client = _authed_client(tmp_path, monkeypatch)

        answer = client.post("/api/settings/holders/quokka/ada/rotate")

        assert answer.status_code == 404

    def test_a_stranded_rotation_is_not_reported_as_a_bad_request(
        self, tmp_path, monkeypatch
    ) -> None:
        """A 400 reads as "nothing happened", and the one thing this failure
        guarantees is that something did."""
        client = _authed_client(tmp_path, monkeypatch)

        async def stranded(service, holder):
            raise main_mod.holder_credentials.Stranded("mint landed, write did not", restored=False)

        monkeypatch.setattr(main_mod.holder_credentials, "rotate", stranded)

        answer = client.post("/api/settings/holders/comms/ada/rotate")

        assert answer.status_code == 500


class TestTheRelayItself:
    """`push_github_token` and `_unwrap_github_token` in `src/mind_runtime.py`.

    Driven through a transport rather than by stubbing the relay: a test that
    hands the route a hand-raised `HTTPException` puts its stub on the status
    mapping, which is the whole behaviour.
    """

    def _relay(self, monkeypatch, handler):
        import httpx

        import mind_runtime

        monkeypatch.setattr(
            mind_runtime, "admin_headers", lambda: {"Authorization": "Bearer a"}
        )
        original = httpx.AsyncClient

        class _Client(original):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        return mind_runtime

    def test_a_refused_token_keeps_its_own_four_hundred(self, monkeypatch) -> None:
        import asyncio

        import httpx

        relay = self._relay(
            monkeypatch,
            lambda request: httpx.Response(400, json={"error": "GitHub rejected that token"}),
        )

        with pytest.raises(HTTPException) as refusal:
            asyncio.run(relay.push_github_token("http://cypher:8420", "ghp_x"))

        assert refusal.value.status_code == 400
        assert "rejected" in refusal.value.detail

    def test_a_mind_that_cannot_store_one_keeps_its_five_oh_three(
        self, monkeypatch
    ) -> None:
        import asyncio

        import httpx

        relay = self._relay(
            monkeypatch, lambda request: httpx.Response(503, json={"error": "names no key"})
        )

        with pytest.raises(HTTPException) as refusal:
            asyncio.run(relay.push_github_token("http://bob:8420", "ghp_x"))

        assert refusal.value.status_code == 503

    def test_a_mind_without_the_route_is_not_a_bad_request(self, monkeypatch) -> None:
        """501, so the console can say "redeploy this mind" rather than
        "your token is malformed"."""
        import asyncio

        import httpx

        relay = self._relay(monkeypatch, lambda request: httpx.Response(404, json={}))

        with pytest.raises(HTTPException) as refusal:
            asyncio.run(relay.push_github_token("http://old:8420", "ghp_x"))

        assert refusal.value.status_code == 501

    def test_a_mind_that_is_down_is_a_bad_gateway(self, monkeypatch) -> None:
        import asyncio

        import httpx

        def _down(request):
            raise httpx.ConnectError("no route")

        relay = self._relay(monkeypatch, _down)

        with pytest.raises(HTTPException) as refusal:
            asyncio.run(relay.push_github_token("http://gone:8420", "ghp_x"))

        assert refusal.value.status_code == 502

    def test_a_mind_answering_something_other_than_a_state_is_not_passed_through(
        self, monkeypatch
    ) -> None:
        import asyncio

        import httpx

        relay = self._relay(monkeypatch, lambda request: httpx.Response(200, json=[1, 2]))

        with pytest.raises(HTTPException) as refusal:
            asyncio.run(relay.push_github_token("http://odd:8420", "ghp_x"))

        assert refusal.value.status_code == 502

    def test_a_mind_with_no_gateway_url_is_refused_rather_than_guessed_at(
        self, monkeypatch
    ) -> None:
        import asyncio

        import httpx

        relay = self._relay(monkeypatch, lambda request: httpx.Response(200, json={}))

        with pytest.raises(HTTPException) as refusal:
            asyncio.run(relay.push_github_token("", "ghp_x"))

        assert refusal.value.status_code == 400


class TestWhatTheRouteRelaysBack:
    def test_an_edge_mind_that_owes_a_restart_says_so_on_the_row_too(
        self, tmp_path, monkeypatch
    ) -> None:
        """The alert said it and the row did not, so the row read Working
        over a token the running process has never loaded."""
        client = _authed_client(tmp_path, monkeypatch)
        registry.forget_all()
        pushed = AsyncMock(return_value={
            "login": "danielstewart77", "where": "env", "restart_required": True,
        })
        with patch.object(
            main_mod, "_mind_gateway_url", AsyncMock(return_value="http://skippy:8420")
        ), patch.object(main_mod.mind_runtime, "push_github_token", pushed):
            body = client.put(
                "/api/minds/skippy/github-token", json={"token": "ghp_pasted"}
            ).json()

        assert body["restart_required"] is True
        assert "restarted" in registry.recall("mind_github_token", "skippy").detail


class TestWhatAStoreSaysAboutWhatIsRunning:
    def test_a_container_mind_is_told_what_an_open_conversation_still_holds(
        self, tmp_path, monkeypatch
    ) -> None:
        """`git` reads its file per invocation and a newly spawned harness
        inherits the new value, but a conversation already running keeps the
        one it booted with — `gh` prefers that variable over its hosts file.
        Left unsaid, the operator discovers it when a push in an open session
        authenticates as the previous account."""
        client = _authed_client(tmp_path, monkeypatch)
        registry.forget_all()
        pushed = AsyncMock(return_value={
            "login": "danielstewart77", "where": "keyring:CYPHER_GITHUB_TOKEN",
            "restart_required": False,
        })
        with patch.object(
            main_mod, "_mind_gateway_url", AsyncMock(return_value="http://cypher:8420")
        ), patch.object(main_mod.mind_runtime, "push_github_token", pushed):
            body = client.put(
                "/api/minds/cypher/github-token", json={"token": "ghp_pasted"}
            ).json()

        assert "already running" in body["note"]

    def test_an_edge_mind_owed_a_restart_is_not_also_told_that(
        self, tmp_path, monkeypatch
    ) -> None:
        client = _authed_client(tmp_path, monkeypatch)
        registry.forget_all()
        pushed = AsyncMock(return_value={
            "login": "danielstewart77", "where": "env", "restart_required": True,
        })
        with patch.object(
            main_mod, "_mind_gateway_url", AsyncMock(return_value="http://skippy:8420")
        ), patch.object(main_mod.mind_runtime, "push_github_token", pushed):
            body = client.put(
                "/api/minds/skippy/github-token", json={"token": "ghp_pasted"}
            ).json()

        assert body["note"] == ""
        assert body["restart_required"] is True
