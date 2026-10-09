"""The GitHub token routes a mind in this stack serves.

The console cannot write this itself: it has to land where *this* mind's own
`git` and `gh` look, and only the mind's own filesystem can see either. The
rule about *where*, and the verify-before-store, live in the shared core and
are covered there. What these guard is the route in front of it — the guard,
the refusal shapes, the per-mind key this stack cannot do without, and the
environment value every spawned harness inherits.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hive_surfaces import github_token

from core.hive_logging import configure_logging
from minds import github_token_api, runtime_api

GOOD = "ghp_AAbbCCddEEffGGhhIIjjKKllMMnnOOpp1234"  # secret-guard: allow — invented shape, not a value
PREVIOUS = "ghp_ZZyyXXwwVVuuTTssRRqqPPooNNmmLL9876"  # secret-guard: allow — invented shape, not a value
ADMIN = {"Authorization": "Bearer s3cret"}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(runtime_api, "admin_token", lambda: "s3cret")
    monkeypatch.setenv("GITHUB_TOKEN_KEYRING_KEY", "CYPHER_GITHUB_TOKEN")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    app = FastAPI()
    github_token_api.install_github_token_routes(
        app, mind_id="cypher-uuid", log=configure_logging("test"),
    )
    return TestClient(app, raise_server_exceptions=False)


def _stored(**kwargs):
    defaults = dict(
        stored=True, accepted=True, login="danielstewart77",
        where="keyring:CYPHER_GITHUB_TOKEN",
    )
    defaults.update(kwargs)
    return github_token.GithubTokenStatus(**defaults)


class TestSettingIt:
    def test_a_verified_token_is_stored_and_no_restart_is_owed(
        self, client, monkeypatch
    ) -> None:
        """`git` and `gh` read their files per invocation, so the next
        subprocess has it — unlike a surface, which holds its token until it
        is recreated."""
        monkeypatch.setattr(
            github_token, "replace",
            AsyncMock(return_value=_stored(configured=["~/.git-credentials"])),
        )

        response = client.put("/github-token", json={"token": GOOD}, headers=ADMIN)

        assert response.status_code == 200
        body = response.json()
        assert body["saved"] is True
        assert body["login"] == "danielstewart77"
        assert body["restart_required"] is False
        assert body["configured"] == ["~/.git-credentials"]

    def test_a_stored_token_is_what_the_next_harness_inherits(
        self, client, monkeypatch
    ) -> None:
        """`gh` prefers the environment over its own `hosts.yml`, so a mind
        that stored a token and left the boot-time value in place would
        report working over a `gh` using the other one."""
        monkeypatch.setattr(
            github_token, "replace", AsyncMock(return_value=_stored()),
        )

        client.put("/github-token", json={"token": GOOD}, headers=ADMIN)

        assert os.environ["GH_TOKEN"] == GOOD

    def test_a_token_github_refused_is_a_bad_request_not_a_bad_credential(
        self, client, monkeypatch
    ) -> None:
        """A 401 here would send the operator off to check the admin token
        they just used successfully."""
        monkeypatch.setattr(
            github_token, "replace",
            AsyncMock(side_effect=github_token.TokenRefused("GitHub rejected that token")),
        )

        response = client.put("/github-token", json={"token": GOOD}, headers=ADMIN)

        assert response.status_code == 400
        assert response.json()["stored"] is False

    def test_a_refused_token_never_becomes_the_environment_value(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv("GH_TOKEN", PREVIOUS)
        monkeypatch.setattr(
            github_token, "replace",
            AsyncMock(side_effect=github_token.TokenRefused("GitHub rejected that token")),
        )

        client.put("/github-token", json={"token": GOOD}, headers=ADMIN)

        assert os.environ["GH_TOKEN"] == PREVIOUS

    def test_a_body_with_no_token_never_reaches_the_store(
        self, client, monkeypatch
    ) -> None:
        replace = AsyncMock(return_value=_stored())
        monkeypatch.setattr(github_token, "replace", replace)

        assert client.put("/github-token", json={}, headers=ADMIN).status_code == 400
        replace.assert_not_called()

    def test_an_unauthenticated_write_is_refused(self, client, monkeypatch) -> None:
        replace = AsyncMock(return_value=_stored())
        monkeypatch.setattr(github_token, "replace", replace)

        assert client.put("/github-token", json={"token": GOOD}).status_code == 401
        replace.assert_not_called()


class TestTheKeyThisStackCannotDoWithout:
    def test_a_mind_naming_no_key_of_its_own_refuses_to_store(
        self, client, monkeypatch
    ) -> None:
        """Every mind here resolves one keyring file, separated only by key
        name: a fallback to the default name stores this mind's token over
        another mind's, and both rows then read as working and wrong."""
        monkeypatch.delenv("GITHUB_TOKEN_KEYRING_KEY", raising=False)
        replace = AsyncMock(return_value=_stored())
        monkeypatch.setattr(github_token, "replace", replace)

        response = client.put("/github-token", json={"token": GOOD}, headers=ADMIN)

        assert response.status_code == 503
        replace.assert_not_called()

    def test_the_read_says_whether_this_mind_can_take_one(
        self, client, monkeypatch
    ) -> None:
        """The page offers a paste box on the strength of this, and offering
        one that always refuses is worse than offering none."""
        monkeypatch.delenv("GITHUB_TOKEN_KEYRING_KEY", raising=False)
        monkeypatch.setattr(
            github_token, "status",
            AsyncMock(return_value=github_token.GithubTokenStatus(
                stored=False, accepted=None, where="env",
            )),
        )

        body = client.get("/github-token", headers=ADMIN).json()

        assert body["settable"] is False


class TestReadingIt:
    def test_a_working_token_is_named_by_its_account_and_its_home(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setattr(github_token, "status", AsyncMock(return_value=_stored()))

        body = client.get("/github-token", headers=ADMIN).json()

        assert body["login"] == "danielstewart77"
        assert body["where"] == "keyring:CYPHER_GITHUB_TOKEN"
        assert body["accepted"] is True

    def test_a_stored_token_github_rejects_is_not_reported_as_absent(
        self, client, monkeypatch
    ) -> None:
        """One is a token nobody supplied; the other is one revoked or pasted
        wrong, failing on every push behind a row that still looks set."""
        monkeypatch.setattr(
            github_token, "status",
            AsyncMock(return_value=_stored(accepted=False, login="", detail="rejected")),
        )

        body = client.get("/github-token", headers=ADMIN).json()

        assert (body["stored"], body["accepted"]) == (True, False)

    def test_an_unauthenticated_read_is_refused(self, client, monkeypatch) -> None:
        """The read names the account this mind pushes as, on a port that
        answers across the LAN."""
        monkeypatch.setattr(github_token, "status", AsyncMock(return_value=_stored()))

        assert client.get("/github-token").status_code == 401


class TestWhatABootAdopts:
    def test_this_minds_own_token_replaces_the_one_the_hive_handed_it(
        self, monkeypatch
    ) -> None:
        """The shared-secret fetch runs first and may put the hive's token in
        `GH_TOKEN`, which `gh` prefers over `hosts.yml` — so a mind holding
        its own would be shadowed on every spawn."""
        monkeypatch.setenv("GITHUB_TOKEN_KEYRING_KEY", "CYPHER_GITHUB_TOKEN")
        monkeypatch.setenv("GH_TOKEN", PREVIOUS)
        monkeypatch.setattr(github_token, "stored_token", lambda: GOOD)
        monkeypatch.setattr(github_token, "apply_stored", lambda: ["~/.git-credentials"])

        configured = github_token_api.adopt_stored_token()

        assert os.environ["GH_TOKEN"] == GOOD
        assert configured == ["~/.git-credentials"]

    def test_a_mind_with_no_token_of_its_own_keeps_the_hives(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN_KEYRING_KEY", "CYPHER_GITHUB_TOKEN")
        monkeypatch.setenv("GH_TOKEN", PREVIOUS)
        monkeypatch.setattr(github_token, "stored_token", lambda: "")

        assert github_token_api.adopt_stored_token() == []
        assert os.environ["GH_TOKEN"] == PREVIOUS
