"""Skill sync between a Mind's repo checkout and the copy its harness runs.

One test per numbered requirement, named for the requirement it proves.

A Mind reports both sides of every skill over its own HTTP API, so these
drive a stub Mind rather than a filesystem: what the console owes Daniel is
that the pair is rendered honestly and that his actions reach the Mind, not
that a particular directory exists on a particular host.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod
import mind_skills
import accounts

GATEWAY = "http://nagatha.local:8421"


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
    user = accounts.make_user("daniel", "secret-pass", is_admin=True,
                              replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME,
                       accounts.session_token(user))
    return client


class StubMind:
    """A Mind whose skills live in a dict instead of on a disk.

    It answers the same routes a real `mind_server` does, so the console
    code under test is the real thing end to end — only the far side of the
    socket is fake.
    """

    def __init__(self, harness="claude", repo=None, installed=None):
        self.harness = harness
        self.repo = dict(repo or {})
        self.installed = dict(installed or {})
        self.unreachable = False

    def _state(self, name):
        if name not in self.repo:
            return "local_only"
        if name not in self.installed:
            return "not_installed"
        return "same" if self.repo[name] == self.installed[name] else "differs"

    def _rows(self):
        return [
            {
                "name": name,
                "state": self._state(name),
                "repo": self.repo.get(name),
                "installed": self.installed.get(name),
            }
            for name in sorted(set(self.repo) | set(self.installed))
        ]

    async def handle(self, method, url, headers=None, **kwargs):
        del kwargs
        if self.unreachable:
            raise httpx.ConnectError("connection refused")
        path = url[len(GATEWAY):]
        # Every route is guarded on a real Mind, reads included.
        if (headers or {}).get("Authorization") != "Bearer admin-token":
            return httpx.Response(401, json={"error": "unauthorized"})

        if method == "GET" and path == "/skills":
            return httpx.Response(
                200, json={"harness": self.harness, "skills": self._rows()}
            )
        if method == "GET" and path.endswith("/diff"):
            name = path.split("/")[2]
            repo_text, mind_text = self.repo.get(name, ""), self.installed.get(name, "")
            diff = f"--- repo/{name}\n+++ mind/{name}\n-{repo_text}+{mind_text}"
            return httpx.Response(200, json={"name": name, "diff": diff})
        if method == "POST" and path.endswith("/install"):
            name = path.split("/")[2]
            if name not in self.repo:
                return httpx.Response(404, json={"error": "no such skill"})
            self.installed[name] = self.repo[name]
            return httpx.Response(200, json={"saved": True, "skill": self._row(name)})
        if method == "POST" and path.endswith("/write-back"):
            name = path.split("/")[2]
            if name not in self.installed:
                return httpx.Response(404, json={"error": "no such skill"})
            self.repo[name] = self.installed[name]
            return httpx.Response(200, json={"saved": True, "skill": self._row(name)})
        if method == "DELETE":
            name = path.split("/")[2]
            if name not in self.installed:
                return httpx.Response(404, json={"error": "no such skill"})
            del self.installed[name]
            return httpx.Response(200, json={"removed": True, "name": name})
        return httpx.Response(404, json={"error": "no route"})

    def _row(self, name):
        return {
            "name": name,
            "state": self._state(name),
            "repo": self.repo.get(name),
            "installed": self.installed.get(name),
        }


@pytest.fixture()
def hive(tmp_path, monkeypatch):
    """The console, wired to a stub Mind registered under one gateway URL."""
    mind = StubMind()
    client = _authed_client(tmp_path, monkeypatch)

    async def _gateway_url(name):
        del name
        return GATEWAY

    with patch.object(main_mod, "_mind_gateway_url", _gateway_url):
        with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=mind.handle)):
            yield client, mind


# 1. The mind configuration page has a skills section listing every skill in
#    the repo alongside that mind's copy of it.
def test_the_mind_page_lists_every_skill_from_either_side(hive):
    client, mind = hive
    mind.repo = {"memory": "a", "end-session": "b"}
    mind.installed = {"memory": "a", "notify": "c"}

    body = client.get("/api/minds/nagatha/skills").json()

    assert [row["name"] for row in body["skills"]] == [
        "end-session",
        "memory",
        "notify",
    ]


# 2. Each row shows the repo side and the mind side.
def test_each_row_carries_both_sides_not_just_a_verdict(hive):
    client, mind = hive
    mind.repo = {"memory": "repo body"}
    mind.installed = {"memory": "mind body"}

    row = client.get("/api/minds/nagatha/skills").json()["skills"][0]

    assert row["repo"] == "repo body"
    assert row["installed"] == "mind body"


# 3. Every row says which of four states it is in.
def test_every_row_reports_one_of_the_four_states(hive):
    client, mind = hive
    mind.repo = {"same-one": "x", "edited": "repo", "not-installed": "y"}
    mind.installed = {"same-one": "x", "edited": "mind", "mind-only": "z"}

    states = {
        row["name"]: row["state"]
        for row in client.get("/api/minds/nagatha/skills").json()["skills"]
    }

    assert states == {
        "same-one": "same",
        "edited": "differs",
        "not-installed": "not_installed",
        "mind-only": "local_only",
    }


# 4. A row that differs can be opened to see exactly what changed.
def test_a_differing_skill_can_be_opened_to_see_what_changed(hive):
    client, mind = hive
    mind.repo = {"memory": "repo body"}
    mind.installed = {"memory": "mind body"}

    diff = client.get("/api/minds/nagatha/skills/memory/diff").json()["diff"]

    assert "repo body" in diff
    assert "mind body" in diff


# 5. Skills are chosen one at a time, independently of each other.
def test_applying_one_skill_installs_that_one_and_only_that_one(hive):
    """Both halves matter.

    Asserting only that the neighbour is untouched passes against a console
    that does nothing at all — which is exactly how this test read before
    an adversarial reviewer broke `install` to a constant and watched it
    stay green.
    """
    client, mind = hive
    mind.repo = {"memory": "repo memory", "notify": "repo notify"}
    mind.installed = {"memory": "mind memory", "notify": "mind notify"}

    client.post("/api/minds/nagatha/skills/notify/install")

    assert mind.installed["notify"] == "repo notify"
    assert mind.installed["memory"] == "mind memory"


# 6. The repo version can be copied onto the mind — apply and revert alike.
def test_installing_copies_the_repo_version_onto_the_mind(hive):
    client, mind = hive
    mind.repo = {"memory": "repo body"}
    mind.installed = {"memory": "locally edited"}

    result = client.post("/api/minds/nagatha/skills/memory/install").json()

    assert mind.installed["memory"] == "repo body"
    assert result["skill"]["state"] == "same"


# 7. A skill the mind has customised can be written back into the repo.
def test_writing_back_copies_the_mind_version_into_the_repo(hive):
    client, mind = hive
    mind.repo = {"memory": "repo body"}
    mind.installed = {"memory": "locally edited"}

    result = client.post("/api/minds/nagatha/skills/memory/write-back").json()

    assert mind.repo["memory"] == "locally edited"
    assert result["skill"]["state"] == "same"


# 8. A skill that exists only on the mind can be removed from the mind.
def test_a_mind_only_skill_can_be_removed(hive):
    client, mind = hive
    mind.repo = {}
    mind.installed = {"scratch": "experiment"}

    client.delete("/api/minds/nagatha/skills/scratch")

    assert "scratch" not in mind.installed
    assert client.get("/api/minds/nagatha/skills").json()["skills"] == []


# 9. Applying writes the skill into the directory that mind's harness loads
#    skills from. When the harness picks it up is the harness's business,
#    not this feature's — the original wording promised timing nobody here
#    controls.
def test_applying_writes_into_the_directory_the_harness_loads_from(hive):
    """Proved against the real path resolution in each mind repo's contract
    test; here, that the console's install reaches the Mind's installed side
    rather than reporting success over an unchanged one."""
    client, mind = hive
    mind.repo = {"memory": "repo body"}
    mind.installed = {}

    client.post("/api/minds/nagatha/skills/memory/install")

    assert mind.installed == {"memory": "repo body"}


# 10. A Claude mind lists only Claude skills; a Codex mind only Codex ones.
def test_each_mind_sees_only_its_own_harness_skills(hive):
    client, mind = hive
    mind.harness = "codex"
    mind.repo = {"codex-only": "x"}

    body = client.get("/api/minds/nagatha/skills").json()

    assert body["harness"] == "codex"
    assert [row["name"] for row in body["skills"]] == ["codex-only"]


# 11. An unreachable mind yields an error, not an empty list.
def test_an_unreachable_mind_says_so_rather_than_showing_nothing(hive):
    client, mind = hive
    mind.unreachable = True

    response = client.get("/api/minds/nagatha/skills")

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"].lower()


def test_writes_carry_the_admin_bearer_to_the_mind(hive):
    """Not a numbered requirement — the Mind refuses these without it.

    Requirements 6, 7 and 8 would pass against a console that sent no
    Authorization header only because the stub is lenient about *which*
    calls it checks. It is not.
    """
    client, mind = hive
    mind.repo = {"memory": "repo body"}
    mind.installed = {"memory": "mind body"}

    with patch.object(mind_skills, "admin_headers", side_effect=HTTPException(503, "x")):
        for call in (
            lambda: client.get("/api/minds/nagatha/skills"),
            lambda: client.get("/api/minds/nagatha/skills/memory/diff"),
            lambda: client.post("/api/minds/nagatha/skills/memory/install"),
            lambda: client.post("/api/minds/nagatha/skills/memory/write-back"),
            lambda: client.delete("/api/minds/nagatha/skills/memory"),
        ):
            assert call().status_code == 503
