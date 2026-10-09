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
        assert any(c["key"] == "comms_bearer_token" for c in body["credentials"])

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
        assert any(c["key"] == "comms_bearer_token" for c in body["credentials"])

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
    def test_it_reports_what_the_check_found(self, tmp_path, monkeypatch) -> None:
        client = _authed_client(tmp_path, monkeypatch)
        answer = {"credential": "mind_github_token", "holder": "cypher",
                  "status": registry.WORKING, "names": "danielstewart77"}
        monkeypatch.setattr(registry, "check", AsyncMock(return_value=answer))

        with patch.object(main_mod, "_broker_minds_safe", AsyncMock(return_value=MINDS)):
            response = client.post(
                "/api/settings/credentials/mind_github_token/check",
                json={"holder": "cypher"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == registry.WORKING

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
        """A stale "working" beside a freshly rotated secret is the one thing
        on this page nobody should believe."""
        client = _authed_client(tmp_path, monkeypatch)
        registry.remember("comms_bearer_token", "hive_mind", status=registry.WORKING)
        monkeypatch.setattr(
            main_mod, "rotate_credential",
            lambda key: {"key": key, "written": [], "complete": True, "locations": []},
        )

        client.post("/api/settings/credentials/comms_bearer_token/rotate")

        assert registry.recall("comms_bearer_token", "hive_mind") is None
