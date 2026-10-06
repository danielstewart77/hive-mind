"""The surface-token routes a mind in this stack serves.

The console cannot write a bot token itself: it has to land where *this*
mind's surface looks for it, which here is a per-mind keyring key — several
surfaces run from one image on one machine and the environment cannot hold
several values under one name — and only the mind's own filesystem can see
that keyring.

The rule about *where* lives in the shared core and is covered there. What
these guard is the route in front of it: the guard, the refusal shapes, and
that no response ever carries the token.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hive_surfaces import token_store

from core.hive_logging import configure_logging
from minds import runtime_api, surface_token_api

GOOD = "8394092434:AAEhsomethingthatlookslikeatokenXYZ123"
ADMIN = {"Authorization": "Bearer s3cret"}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(runtime_api, "admin_token", lambda: "s3cret")
    app = FastAPI()
    surface_token_api.install_surface_token_routes(
        app, mind_id="cypher-uuid", log=configure_logging("test"),
    )
    return TestClient(app, raise_server_exceptions=False)


class TestSettingIt:
    def test_a_verified_token_is_stored_and_a_restart_is_owed(
        self, client, monkeypatch
    ) -> None:
        """The surface read its token at startup and keeps it until
        recreated, so a reply implying otherwise is a lie the operator acts
        on."""
        replace = AsyncMock(return_value=token_store.TokenStatus(
            stored=True, accepted=True,
            bot_username="hivemind_cypher_bot",
            where="keyring:CYPHER_TELEGRAM_BOT_TOKEN",
        ))
        monkeypatch.setattr(token_store, "replace", replace)

        response = client.put("/surface-token", json={"token": GOOD}, headers=ADMIN)

        assert response.status_code == 200
        body = response.json()
        assert body["saved"] is True
        assert body["bot_username"] == "hivemind_cypher_bot"
        assert body["restart_required"] is True
        assert replace.await_args.args[0] == GOOD

    def test_a_refused_token_answers_400_rather_than_401(
        self, client, monkeypatch
    ) -> None:
        """The credential that reached this route was fine. A 401 would send
        the operator off to check the admin token they just used."""
        monkeypatch.setattr(token_store, "replace", AsyncMock(
            side_effect=token_store.TokenRefused("Unauthorized"),
        ))

        response = client.put("/surface-token", json={"token": GOOD}, headers=ADMIN)

        assert response.status_code == 400
        assert response.json()["stored"] is False
        assert "Unauthorized" in response.json()["error"]

    def test_an_empty_body_is_refused_before_anything_is_attempted(
        self, client, monkeypatch
    ) -> None:
        replace = AsyncMock()
        monkeypatch.setattr(token_store, "replace", replace)

        assert client.put("/surface-token", json={}, headers=ADMIN).status_code == 400
        replace.assert_not_awaited()

    def test_a_missing_admin_token_cannot_set_one(self, client, monkeypatch) -> None:
        replace = AsyncMock()
        monkeypatch.setattr(token_store, "replace", replace)

        assert client.put("/surface-token", json={"token": GOOD}).status_code == 401
        replace.assert_not_awaited()

    def test_a_mind_with_no_admin_token_configured_refuses_rather_than_opens(
        self, client, monkeypatch
    ) -> None:
        """503, like every other write route on a mind: an unconfigured guard
        is not an absent one."""
        monkeypatch.setattr(runtime_api, "admin_token", lambda: "")

        response = client.put("/surface-token", json={"token": GOOD}, headers=ADMIN)

        assert response.status_code == 503


class TestReadingIt:
    def test_the_state_is_reported_without_the_token(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setattr(token_store, "status", AsyncMock(
            return_value=token_store.TokenStatus(
                stored=True, accepted=True,
                bot_username="hivemind_cypher_bot",
                where="keyring:CYPHER_TELEGRAM_BOT_TOKEN",
            ),
        ))

        response = client.get("/surface-token", headers=ADMIN)

        assert response.status_code == 200
        assert GOOD not in response.text
        assert response.json()["bot_username"] == "hivemind_cypher_bot"
        assert response.json()["where"] == "keyring:CYPHER_TELEGRAM_BOT_TOKEN"

    def test_absent_and_rejected_are_distinguishable_to_the_console(
        self, client, monkeypatch
    ) -> None:
        """One is a token nobody supplied; the other is one revoked or pasted
        wrong that is failing on every poll behind a surface that still looks
        up."""
        monkeypatch.setattr(token_store, "status", AsyncMock(
            return_value=token_store.TokenStatus(
                stored=True, accepted=False,
                where="keyring:CYPHER_TELEGRAM_BOT_TOKEN", detail="Unauthorized",
            ),
        ))
        rejected = client.get("/surface-token", headers=ADMIN).json()

        monkeypatch.setattr(token_store, "status", AsyncMock(
            return_value=token_store.TokenStatus(
                stored=False, accepted=None,
                where="keyring:CYPHER_TELEGRAM_BOT_TOKEN",
            ),
        ))
        absent = client.get("/surface-token", headers=ADMIN).json()

        assert (rejected["stored"], rejected["accepted"]) == (True, False)
        assert (absent["stored"], absent["accepted"]) == (False, None)

    def test_the_read_is_guarded_too(self, client, monkeypatch) -> None:
        """It names the bot a mind authenticates as, on a port that answers
        across the LAN."""
        status = AsyncMock()
        monkeypatch.setattr(token_store, "status", status)

        assert client.get("/surface-token").status_code == 401
        status.assert_not_awaited()
