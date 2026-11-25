"""The skill and hook file editor, as the console relays it to a Mind.

Requirement 6 is the one this repo can prove and the other repo cannot: a
Mind that does not answer must say so rather than render as an empty
folder. Only a caller can observe a Mind that is down, so the numbered test
for it lives here; the other nine are proved against a real filesystem in
each Mind repo's own contract test.

The stub answers the same routes a real `mind_server` does, so the console
code under test is the real thing end to end — only the far side of the
socket is fake.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod
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
    """A Mind whose two trees live in a dict instead of on a disk."""

    def __init__(self):
        self.trees = {
            "skills": {"memory/SKILL.md": "recall things\n"},
            "hooks": {"auto_remember.sh": "#!/bin/bash\n"},
        }
        self.unreachable = False
        # A Mind predating this API has no such route at all, which
        # Starlette answers with its own 404.
        self.has_files_api = True

    async def handle(self, method, url, headers=None, **kwargs):
        if self.unreachable:
            raise httpx.ConnectError("connection refused")
        path = url.path if hasattr(url, "path") else str(url)[len(GATEWAY):]
        if (headers or {}).get("Authorization") != "Bearer admin-token":
            return httpx.Response(401, json={"error": "unauthorized"})
        if not self.has_files_api:
            return httpx.Response(404, text="Not Found")

        parts = [p for p in str(path).split("/") if p]
        if parts[:1] != ["files"] or len(parts) < 2:
            return httpx.Response(404, json={"error": "no route"})
        tree = parts[1]
        files = self.trees.get(tree)
        if files is None:
            return httpx.Response(404, json={"error": "unknown tree"})

        if method == "GET" and len(parts) == 2:
            return httpx.Response(
                200,
                json={
                    "tree": tree,
                    "root": f"/home/mind/.claude/{tree}",
                    "exists": True,
                    "omitted": [],
                    "truncated": False,
                    "files": [
                        {"path": name, "size": len(body), "editable": True}
                        for name, body in sorted(files.items())
                    ],
                },
            )
        if method == "GET" and parts[2:] == ["content"]:
            name = dict(kwargs.get("params") or {}).get("path", "")
            if name not in files:
                return httpx.Response(404, json={"error": "no such file"})
            return httpx.Response(
                200,
                json={
                    "tree": tree,
                    "path": name,
                    "editable": True,
                    "reason": None,
                    "text": files[name],
                    "revision": str(len(files[name])),
                },
            )
        if method == "PUT" and parts[2:] == ["content"]:
            body = kwargs.get("json") or {}
            name = body.get("path", "")
            if name not in files:
                return httpx.Response(404, json={"error": "no such file"})
            if body.get("revision") and body["revision"] != str(len(files[name])):
                return httpx.Response(409, json={"error": "changed since you opened it"})
            files[name] = body.get("text", "")
            return httpx.Response(
                200,
                json={
                    "tree": tree,
                    "path": name,
                    "saved": True,
                    "size": len(files[name]),
                    "revision": str(len(files[name])),
                },
            )
        return httpx.Response(404, json={"error": "no route"})


@pytest.fixture()
def hive(tmp_path, monkeypatch):
    mind = StubMind()
    client = _authed_client(tmp_path, monkeypatch)

    async def _gateway_url(name):
        del name
        return GATEWAY

    with patch.object(main_mod, "_mind_gateway_url", _gateway_url):
        with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=mind.handle)):
            yield client, mind


# 6. A Mind that cannot be reached says so, instead of showing an empty
#    folder that reads as "this mind has no skills".
def test_a_mind_that_cannot_be_reached_says_so_rather_than_showing_nothing(hive):
    client, mind = hive

    # The contrast: reachable, and the browsers work.
    listed = client.get("/api/minds/nagatha/files/hooks").json()
    assert [row["path"] for row in listed["files"]] == ["auto_remember.sh"]
    opened = client.get(
        "/api/minds/nagatha/files/skills/content", params={"path": "memory/SKILL.md"}
    ).json()
    assert opened["text"] == "recall things\n"
    saved = client.put(
        "/api/minds/nagatha/files/skills/content",
        json={
            "path": "memory/SKILL.md",
            "text": "recall better things\n",
            "revision": opened["revision"],
        },
    )
    assert saved.status_code == 200
    assert mind.trees["skills"]["memory/SKILL.md"] == "recall better things\n"

    # Down: every route says unreachable, and none of them answers "empty".
    mind.unreachable = True
    for response in (
        client.get("/api/minds/nagatha/files/skills"),
        client.get("/api/minds/nagatha/files/hooks"),
        client.get(
            "/api/minds/nagatha/files/hooks/content",
            params={"path": "auto_remember.sh"},
        ),
        client.put(
            "/api/minds/nagatha/files/hooks/content",
            json={"path": "auto_remember.sh", "text": "x\n", "revision": "12"},
        ),
    ):
        assert response.status_code == 502
        assert "unreachable" in response.json()["detail"]

    # And a Mind that is up but predates this API is a different sentence
    # again, because "no route here" must never render as "no files there".
    mind.unreachable = False
    mind.has_files_api = False
    stale = client.get("/api/minds/nagatha/files/skills")
    assert stale.status_code == 501
    assert "without the file editor API" in stale.json()["detail"]

    # And the confusion must not run the other way. A row clicked from a
    # listing whose file has since been deleted is a missing file, not an
    # out-of-date Mind — only the listing route may read a 404 that way.
    mind.has_files_api = True
    missing = client.get(
        "/api/minds/nagatha/files/skills/content", params={"path": "gone/SKILL.md"}
    )
    assert missing.status_code == 404
    assert "no such file" in missing.json()["detail"]


def test_a_stale_save_is_refused_rather_than_overwriting_the_mind(hive):
    """Requirement 11, at the console boundary: the 409 reaches the browser.

    The Mind decides staleness; what this proves is that the console carries
    the revision across and does not flatten the refusal into a 500.
    """
    client, mind = hive
    opened = client.get(
        "/api/minds/nagatha/files/skills/content", params={"path": "memory/SKILL.md"}
    ).json()
    mind.trees["skills"]["memory/SKILL.md"] = "changed by something else\n"

    refused = client.put(
        "/api/minds/nagatha/files/skills/content",
        json={
            "path": "memory/SKILL.md",
            "text": "my stale edit\n",
            "revision": opened["revision"],
        },
    )

    assert refused.status_code == 409
    # The Mind's own sentence, not just its status code: a 409 exists to be
    # read by the person who is about to lose their edit.
    assert "changed since you opened it" in refused.json()["detail"]
    assert mind.trees["skills"]["memory/SKILL.md"] == "changed by something else\n"


def test_the_editor_routes_require_a_logged_in_console_user(tmp_path, monkeypatch):
    """The Mind's admin bearer is a different credential on a different hop.

    It is attached server-side, so it says nothing about who is driving the
    browser — and these routes return the full text of every hook the Mind
    runs, on a console that is reverse-proxied publicly.
    """
    _authed_client(tmp_path, monkeypatch)
    anonymous = TestClient(main_mod.app)

    for response in (
        anonymous.get("/api/minds/nagatha/files/skills"),
        anonymous.get(
            "/api/minds/nagatha/files/skills/content", params={"path": "memory/SKILL.md"}
        ),
        anonymous.put(
            "/api/minds/nagatha/files/skills/content",
            json={"path": "memory/SKILL.md", "text": "x\n"},
        ),
    ):
        assert response.status_code in (401, 403)
