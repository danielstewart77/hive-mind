"""Roles against their repo copies, and skills people submit (feature 10.18).

Protected behaviours 3, 4 and 5 of Role Management:

* a role that differs from the repo copy is shown as differing and is never
  changed without a choice;
* a skill submitted by somebody other than the owner becomes a pull request
  and never lands in the hive directly;
* community submissions are left out of the skill lists unless asked for.

The repo copies are a directory of this test's own (`HIVE_ROLES_DIR`), and
GitHub is an `httpx.MockTransport` recording every request: the far side of
the network is the only thing stood in for.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from hive_site import testing
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import accounts
import auth
import main as main_mod
import roles_data
import skill_submissions
import store

GATEWAY = "http://stash.local:8420"


async def _direct_for_tests(url):
    """Stand-in for the console's gateway hop in tests whose subject is the
    page, not the hop: the request goes to the stub mind at its own address
    with the key the stub expects. The hop itself is tested in
    test_send_through_gateway.py."""
    return url, {"Authorization": "Bearer the-minds-own-key"}


def _client(monkeypatch, *, admin=True, username="daniel"):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    import configuration_data

    async def own_key(url):
        return "the-minds-own-key"

    monkeypatch.setattr("mind_runtime.via_gateway", _direct_for_tests)
    user = accounts.make_user(username, "secret-pass", is_admin=admin, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


def _write(folder, slug, **role):
    (folder / f"{slug}.yaml").write_text(roles_data.render(role))


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    folder = tmp_path / "roles"
    folder.mkdir()
    monkeypatch.setenv("HIVE_ROLES_DIR", str(folder))
    return folder


def _row(client, slug):
    roles = client.get("/api/roles").json()["roles"]
    return next(r for r in roles if r["slug"] == slug)


# ── 3. a differing role is shown as differing, and changes only by a choice

def test_a_role_that_differs_from_its_repo_copy_is_marked_differs_with_a_diff(repo, monkeypatch):
    _write(repo, "developer", name="Developer", suggested_tier="high",
           tools=["shell", "files"], skills=["code"], hooks=[])
    testing.run(store.create_role("Developer", slug="developer", suggested_tier="high",
                                  tools=["files"], skills=["code", "simplify"]))
    client = _client(monkeypatch)

    row = _row(client, "developer")

    assert row["state"] == "differs"
    assert "-- shell" in row["diff"] or "- shell" in row["diff"]
    assert "+- simplify" in row["diff"]


def test_reading_the_page_and_a_repo_update_leave_a_differing_role_as_it_was(repo, monkeypatch):
    _write(repo, "developer", name="Developer", suggested_tier=None, tools=["shell"], skills=["code"], hooks=[])
    role_id = testing.run(store.create_role("Developer", slug="developer",
                                            tools=["files"], skills=["mine"]))
    client = _client(monkeypatch)
    client.get("/api/roles")
    _write(repo, "developer", name="Developer", suggested_tier="extra-high",
           tools=["shell", "web"], skills=["code", "review"], hooks=["time_inject"])
    repo_text = (repo / "developer.yaml").read_text()

    row = _row(client, "developer")
    testing.run(roles_data.seed())
    _row(client, "developer")

    hive = testing.run(store.role(role_id))
    assert row["state"] == "differs"
    assert hive["skills"] == ["mine"] and hive["tools"] == ["files"] and hive["suggested_tier"] is None
    assert (repo / "developer.yaml").read_text() == repo_text


def test_keep_yours_leaves_both_copies_and_the_role_still_differs(repo, monkeypatch):
    _write(repo, "planner", name="Planner", suggested_tier=None, tools=[], skills=["design-spec"], hooks=[])
    role_id = testing.run(store.create_role("Planner", slug="planner", skills=["brainstorm"]))
    client = _client(monkeypatch)

    assert client.post(f"/api/roles/{role_id}/keep").status_code == 200

    row = _row(client, "planner")
    assert row["state"] == "differs" and row["kept"] is True
    assert testing.run(store.role(role_id))["skills"] == ["brainstorm"]
    _write(repo, "planner", name="Planner", suggested_tier=None, tools=[], skills=["design-spec", "x"], hooks=[])
    assert _row(client, "planner")["kept"] is False


def test_take_the_repo_update_replaces_the_role_with_the_repo_copy(repo, monkeypatch):
    _write(repo, "planner", name="Planner", suggested_tier="extra-high", tools=["web"],
           skills=["design-spec"], hooks=[])
    role_id = testing.run(store.create_role("Planner", slug="planner", skills=["brainstorm"]))
    client = _client(monkeypatch)

    assert client.post("/api/roles/repo/planner/take").status_code == 200

    hive = testing.run(store.role(role_id))
    assert (hive["skills"], hive["tools"], hive["suggested_tier"]) == (["design-spec"], ["web"], "extra-high")
    assert _row(client, "planner")["state"] == "tracked"


def test_promote_yours_writes_the_role_as_the_repo_copy(repo, monkeypatch):
    role_id = testing.run(store.create_role("Trading", slug="trading", skills=["btc-ledger"],
                                            tools=["web"]))
    client = _client(monkeypatch)
    assert _row(client, "trading")["state"] == "local"

    assert client.post(f"/api/roles/{role_id}/promote").status_code == 200

    assert (repo / "trading.yaml").is_file()
    assert _row(client, "trading")["state"] == "tracked"


def test_a_repo_role_this_hive_lacks_is_offered_and_not_added_by_itself(repo, monkeypatch):
    testing.run(roles_data.seed())
    _write(repo, "optimizer", name="Optimizer", suggested_tier=None, tools=[], skills=["prune-memory"], hooks=[])
    client = _client(monkeypatch)

    row = _row(client, "optimizer")
    testing.run(roles_data.seed())

    assert row["state"] == "repo_only"
    assert testing.run(store.role_by_slug("optimizer")) is None


def test_a_fresh_hive_is_seeded_with_every_readable_repo_role_and_general(repo, monkeypatch):
    _write(repo, "operator", name="Operator", suggested_tier="high", tools=["shell"], skills=[], hooks=[])
    (repo / "broken.yaml").write_text("name: [unclosed")

    result = testing.run(roles_data.seed())

    assert sorted(result["added"]) == ["operator"]
    assert testing.run(store.role_by_slug("general")) is not None
    assert testing.run(store.role_by_slug("broken")) is None
    assert _row(_client(monkeypatch), "broken")["state"] == "unreadable"


def test_the_general_role_refuses_deletion_with_a_reason(repo, monkeypatch):
    testing.run(roles_data.seed())
    general = testing.run(store.role_by_slug("general"))
    client = _client(monkeypatch)

    response = client.delete(f"/api/roles/{general['id']}")

    assert response.status_code == 409
    assert "cannot be deleted" in response.json()["detail"]
    assert testing.run(store.role_by_slug("general")) is not None


def test_only_an_admin_changes_a_role(repo, monkeypatch):
    role_id = testing.run(store.create_role("Developer", slug="developer", skills=["code"]))
    client = _client(monkeypatch, admin=False, username="sloan")

    response = client.put(f"/api/roles/{role_id}", json={"skills": ["anything"]})

    assert response.status_code == 403
    assert testing.run(store.role(role_id))["skills"] == ["code"]


# ── 4. a non-owner's skill becomes a pull request, never a direct write

class GitHub:
    """GitHub's REST API for one repository, recording every request."""

    def __init__(self, existing=()):
        self.requests: list[httpx.Request] = []
        self.existing = set(existing)
        self.refs = {"heads/master": "base-sha"}
        self.pulls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        repo = f"/repos/{skill_submissions.REPOSITORY}"
        body = json.loads(request.content or b"{}") if request.content else {}
        if request.method == "GET" and path.startswith(f"{repo}/contents/"):
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200 if name in self.existing else 404, json={})
        if request.method == "GET" and path == f"{repo}/git/ref/heads/master":
            return httpx.Response(200, json={"object": {"sha": self.refs["heads/master"]}})
        if request.method == "GET" and path.startswith(f"{repo}/git/commits/"):
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        if request.method == "POST" and path == f"{repo}/git/blobs":
            return httpx.Response(201, json={"sha": f"blob-{len(self.requests)}"})
        if request.method == "POST" and path == f"{repo}/git/trees":
            return httpx.Response(201, json={"sha": "tree-sha"})
        if request.method == "POST" and path == f"{repo}/git/commits":
            return httpx.Response(201, json={"sha": "commit-sha"})
        if request.method == "POST" and path == f"{repo}/git/refs":
            ref = body["ref"].removeprefix("refs/")
            if ref in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[ref] = body["sha"]
            return httpx.Response(201, json={})
        if request.method == "PATCH" and path.startswith(f"{repo}/git/refs/"):
            self.refs[path.split("/git/refs/", 1)[1]] = body["sha"]
            return httpx.Response(200, json={})
        if request.method == "GET" and path == f"{repo}/pulls":
            head = request.url.params.get("head", "").split(":", 1)[-1]
            return httpx.Response(200, json=[p for p in self.pulls if p["head"] == head])
        if request.method == "POST" and path == f"{repo}/pulls":
            pull = {"number": 40 + len(self.pulls), "html_url": "https://github.com/x/pull/1",
                    "head": body["head"], "base": body["base"]}
            self.pulls.append(pull)
            return httpx.Response(201, json=pull)
        return httpx.Response(404, json={"message": "no route in this stub"})


@pytest.fixture()
def github(monkeypatch):
    stub = GitHub()
    monkeypatch.setattr(skill_submissions, "_transport", httpx.MockTransport(stub))
    monkeypatch.setattr(skill_submissions, "_token", lambda: "test-token")
    return stub


def _folder(name="weather", extra=()):
    files = [(f"{name}/SKILL.md", b"---\nname: weather\n---\nAsk the sky.\n"), *extra]
    return {"files": [(path.rsplit("/", 1)[-1], data, "application/octet-stream") for path, data in files],
            "paths": [path for path, _ in files]}


def _post(client, folder, url="/api/skill-submissions"):
    files = [("files", f) for f in folder["files"]]
    return client.post(url, files=files, data={"paths": folder["paths"]})


def test_a_skill_submitted_by_another_user_becomes_a_pull_request_and_nothing_else(repo, monkeypatch, github):
    client = _client(monkeypatch, admin=False, username="sloan")

    response = _post(client, _folder(extra=[("weather/scripts/sky.sh", b"echo sun\n")]))

    assert response.status_code == 200, response.text
    assert github.refs["heads/master"] == "base-sha"
    writes = [(r.method, r.url.path) for r in github.requests if r.method != "GET"]
    assert not any("heads/master" in path or "/contents/" in path for _, path in writes)
    assert ("POST", f"/repos/{skill_submissions.REPOSITORY}/pulls") in writes
    branch = github.pulls[0]["head"]
    assert branch.startswith("skill-submission/") and github.pulls[0]["base"] == "master"
    trees = [json.loads(r.content) for r in github.requests if r.url.path.endswith("/git/trees")]
    assert sorted(e["path"] for e in trees[0]["tree"]) == [
        "specs/skills/weather/SKILL.md", "specs/skills/weather/scripts/sky.sh"]
    rows = testing.run(store.skill_submissions())
    assert [(r["skill"], r["submitted_by"], r["by_owner"], r["pr_number"]) for r in rows] == [
        ("weather", "sloan", False, github.pulls[0]["number"])]


def test_a_submission_cut_off_before_its_pull_request_is_retried_as_the_same_one(repo, monkeypatch, github):
    client = _client(monkeypatch, admin=False, username="sloan")
    real = github.__call__

    def no_pulls(request):
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            return httpx.Response(500, json={"message": "GitHub fell over"})
        return real(request)

    monkeypatch.setattr(skill_submissions, "_transport", httpx.MockTransport(no_pulls))
    assert _post(client, _folder()).status_code == 502
    submission = testing.run(store.skill_submissions())[0]
    assert submission["pr_number"] is None

    monkeypatch.setattr(skill_submissions, "_transport", httpx.MockTransport(github))
    response = _post(client, _folder(), url=f"/api/skill-submissions/{submission['id']}/retry")

    assert response.status_code == 200, response.text
    assert len(github.pulls) == 1
    assert len(testing.run(store.skill_submissions())) == 1
    assert github.refs["heads/master"] == "base-sha"


def test_a_folder_without_a_skill_file_is_refused_before_github_is_asked(repo, monkeypatch, github):
    client = _client(monkeypatch, admin=False, username="sloan")
    folder = {"files": [("notes.md", b"hi", "text/markdown")], "paths": ["weather/notes.md"]}

    response = _post(client, folder)

    assert response.status_code == 400
    assert github.requests == []
    assert testing.run(store.skill_submissions()) == []


# ── 5. community submissions are left out of the skill lists unless asked

class StubMind:
    def __init__(self, names):
        self.names = names

    async def handle(self, method, url, headers=None, **kwargs):
        del headers, kwargs
        if method == "GET" and url == f"{GATEWAY}/skills":
            return httpx.Response(200, json={"harness": "dsh", "skills": [
                {"name": n, "state": "same", "repo": "x", "installed": "x"} for n in self.names]})
        return httpx.Response(404, json={"error": "no route"})


@pytest.fixture()
def mind_listing(monkeypatch):
    async def _gateway_url(name):
        del name
        return GATEWAY
    mind = StubMind(["memory", "weather", "owners-own", "changed"])
    monkeypatch.setattr(skill_submissions, "_token", lambda: "")
    with patch.object(main_mod, "_mind_gateway_url", _gateway_url):
        with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=mind.handle)):
            yield mind


def _submitted(skill, *, by_owner=False, new_skill=True, pr=7, state="merged"):
    """A submission as GitHub last described it: merged, open, closed, or
    never answered about (`unknown`)."""
    sid = testing.run(store.add_skill_submission(
        skill=skill, submitted_by="daniel" if by_owner else "sloan", by_owner=by_owner,
        new_skill=new_skill, repository=skill_submissions.REPOSITORY, branch=f"b-{skill}-{pr}"))
    fields = {"pr_number": pr}
    if state != "unknown":
        fields["checked_at"] = 9e12
    if state == "merged":
        fields["merged_at"] = 1.0
    if state == "closed":
        fields["closed_at"] = 1.0
    testing.run(store.update_skill_submission(sid, **fields))
    return sid


def test_community_submissions_are_left_out_of_a_minds_skill_list_by_default(repo, monkeypatch, mind_listing):
    _submitted("weather")
    _submitted("owners-own", by_owner=True)
    _submitted("changed", new_skill=False)
    client = _client(monkeypatch)

    body = client.get("/api/minds/stash/skills").json()

    assert [s["name"] for s in body["skills"]] == ["memory", "owners-own", "changed"]
    assert body["community_hidden"] == 1
    assert not any(s["community"] for s in body["skills"])


def test_asking_for_community_submissions_lists_them_labelled(repo, monkeypatch, mind_listing):
    _submitted("weather")
    client = _client(monkeypatch)

    body = client.get("/api/minds/stash/skills?community=1").json()

    labelled = {s["name"]: s["community"] for s in body["skills"]}
    assert labelled == {"memory": False, "weather": True, "owners-own": False, "changed": False}
    assert body["community_hidden"] == 0


def test_the_role_editors_skill_list_labels_a_community_submission(repo, monkeypatch, mind_listing):
    _submitted("weather")

    async def registry():
        return [{"name": "stash", "id": "mind-stash", "gateway_url": GATEWAY},
                {"name": "ada", "id": "mind-ada", "gateway_url": "http://ada.local:8420"}]

    async def no_proxy(path):
        raise httpx.ConnectError(f"no proxy for {path}")

    monkeypatch.setattr(main_mod, "_broker_minds_safe", registry)
    monkeypatch.setattr(main_mod, "_proxy_json", no_proxy)
    client = _client(monkeypatch)

    body = client.get("/api/roles/catalog").json()

    labelled = {s["name"]: s["community"] for s in body["skills"]}
    assert labelled == {"changed": False, "memory": False, "owners-own": False, "weather": True}
    assert next(s for s in body["skills"] if s["name"] == "weather")["minds"] == ["stash"]
    assert body["skills_unanswered"] == ["ada"]
    assert body["hooks_unanswered"] == ["ada", "stash"]


def test_a_community_submission_closed_without_merging_is_not_hidden(repo, monkeypatch, mind_listing):
    _submitted("weather", state="closed")
    client = _client(monkeypatch)

    body = client.get("/api/minds/stash/skills").json()

    assert "weather" in [s["name"] for s in body["skills"]]


def test_a_community_submission_github_has_not_answered_about_is_hidden(repo, monkeypatch, mind_listing):
    _submitted("weather", state="unknown")
    client = _client(monkeypatch)

    body = client.get("/api/minds/stash/skills").json()

    assert "weather" not in [s["name"] for s in body["skills"]]


def test_a_submission_still_open_does_not_hide_a_skill_of_the_same_name(repo, monkeypatch, mind_listing):
    _submitted("weather", state="open")
    client = _client(monkeypatch)

    body = client.get("/api/minds/stash/skills").json()

    assert "weather" in [s["name"] for s in body["skills"]]


def test_reading_the_lists_learns_from_github_that_a_pull_request_merged(repo, monkeypatch, github):
    sid = _submitted("weather", state="unknown")
    testing.run(store.update_skill_submission(sid, checked_at=1.0))
    real = github.__call__

    def merged(request):
        if request.method == "GET" and request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json={"number": 7, "state": "closed", "merged_at": "2026-11-16T00:00:00Z"})
        return real(request)

    monkeypatch.setattr(skill_submissions, "_transport", httpx.MockTransport(merged))

    testing.run(skill_submissions.refresh())

    row = testing.run(store.skill_submission(sid))
    assert row["merged_at"] is not None and row["closed_at"] is None


@pytest.mark.parametrize("method,url", [
    ("post", "/api/roles"), ("delete", "/api/roles/{id}"), ("post", "/api/roles/{id}/keep"),
    ("post", "/api/roles/{id}/promote"), ("post", "/api/roles/repo/developer/take")])
def test_only_an_admin_defines_or_settles_a_role(repo, monkeypatch, method, url):
    _write(repo, "developer", name="Developer", suggested_tier=None, tools=[], skills=["x"], hooks=[])
    role_id = testing.run(store.create_role("Developer", slug="developer", skills=["code"]))
    client = _client(monkeypatch, admin=False, username="sloan")

    response = getattr(client, method)(url.format(id=role_id), **({"json": {"name": "New"}} if method == "post" else {}))

    assert response.status_code == 403
    assert testing.run(store.role(role_id))["skills"] == ["code"]
    assert not (repo / "new.yaml").exists()


@pytest.mark.parametrize("path", ["../weather/SKILL.md", "/etc/weather/SKILL.md", "weather/../../x/SKILL.md"])
def test_a_path_outside_the_skill_folder_is_refused_before_github_is_asked(repo, monkeypatch, github, path):
    client = _client(monkeypatch, admin=False, username="sloan")
    folder = {"files": [("SKILL.md", b"x", "text/markdown")], "paths": [path]}

    assert _post(client, folder).status_code == 400
    assert github.requests == []


def test_a_folder_larger_than_a_skill_is_refused_before_github_is_asked(repo, monkeypatch, github):
    client = _client(monkeypatch, admin=False, username="sloan")
    big = b"x" * (skill_submissions.MAX_BYTES + 1)

    assert _post(client, _folder(extra=[("weather/data.bin", big)])).status_code == 400
    assert github.requests == []


def test_a_submission_that_has_its_pull_request_is_not_retried_onto_it(repo, monkeypatch, github):
    client = _client(monkeypatch, admin=False, username="sloan")
    assert _post(client, _folder()).status_code == 200
    submission = testing.run(store.skill_submissions())[0]
    head = dict(github.refs)

    response = _post(client, _folder(), url=f"/api/skill-submissions/{submission['id']}/retry")

    assert response.status_code == 409
    assert github.refs == head and len(github.pulls) == 1


def test_a_script_in_a_submitted_skill_lands_executable(repo, monkeypatch, github):
    client = _client(monkeypatch, admin=False, username="sloan")

    _post(client, _folder(extra=[("weather/sky.sh", b"#!/bin/sh\necho sun\n"), ("weather/notes.md", b"plain")]))

    tree = [json.loads(r.content) for r in github.requests if r.url.path.endswith("/git/trees")][0]["tree"]
    modes = {e["path"].rsplit("/", 1)[-1]: e["mode"] for e in tree}
    assert modes == {"SKILL.md": "100644", "sky.sh": "100755", "notes.md": "100644"}


def test_another_user_cannot_write_a_skill_back_into_the_hives_own_checkout(repo, monkeypatch, tmp_path):
    import configuration_data
    minds = tmp_path / "hive-minds"
    (minds / "stash").mkdir(parents=True)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", minds)
    reached = []

    async def write_back(url, skill):
        reached.append(skill)
        return {"saved": True}

    async def gateway(name):
        return GATEWAY

    monkeypatch.setattr(main_mod.mind_skills, "write_back", write_back)
    monkeypatch.setattr(main_mod, "_mind_gateway_url", gateway)
    # Sloan owns this Mind, as the gateway's registry would say.
    monkeypatch.setattr(main_mod.route_access, "registered_minds", AsyncMock(
        return_value=[{"name": "stash", "mind_id": "mind-stash", "owner": "sloan"}]))
    client = _client(monkeypatch, admin=False, username="sloan")

    response = client.post("/api/minds/stash/skills/weather/write-back")

    assert response.status_code == 403
    assert "pull request" in response.json()["detail"]
    assert reached == []


def test_a_mind_holding_a_role_is_shown_the_tier_that_role_suggests_for_its_model(repo, monkeypatch):
    """Feature 893: where a Mind's model is chosen, its roles' suggested tiers
    are what the page is handed to show above the model box."""
    testing.run(roles_data.seed())
    coder = testing.run(store.create_role("Coder", slug="coder", suggested_tier="medium"))
    silent = testing.run(store.create_role("Silent", slug="silent"))
    judge = testing.run(store.create_role("Judge", slug="judge", suggested_tier="extra-high"))

    async def registry():
        return [{"name": "stash", "id": "mind-stash", "owner": "daniel", "gateway_url": GATEWAY}]

    monkeypatch.setattr(main_mod, "_broker_minds_safe", registry)
    client = _client(monkeypatch)
    assert client.put("/api/minds/stash/roles",
                      json={"role_ids": [coder, silent]}).status_code == 200

    shown = client.get("/api/minds/stash/roles").json()["suggested_tiers"]

    assert shown == [{"role": "Coder", "tier": "medium"}]
    assert client.put("/api/minds/stash/roles",
                      json={"role_ids": [judge]}).status_code == 200
    assert client.get("/api/minds/stash/roles").json()["suggested_tiers"] == [
        {"role": "Judge", "tier": "extra high"}]


def test_an_owner_gives_their_mind_roles_and_its_grants_are_their_union(repo, monkeypatch):
    testing.run(roles_data.seed())
    writer = testing.run(store.create_role("Writer", slug="writer", skills=["remember"], tools=["files"]))
    builder = testing.run(store.create_role("Builder", slug="builder", skills=["code"], tools=["shell"]))

    # The registry's own row for a Mind: its id is filed under `id`.
    async def registry():
        return [{"name": "stash", "id": "mind-stash", "owner": "daniel", "gateway_url": GATEWAY}]

    monkeypatch.setattr(main_mod, "_broker_minds_safe", registry)
    client = _client(monkeypatch)

    response = client.put("/api/minds/stash/roles", json={"role_ids": [writer, builder]})

    assert response.status_code == 200, response.text
    grants = client.get("/api/minds/stash/roles").json()["grants"]
    assert grants["managed"] is True
    assert {"remember", "code"} <= set(grants["skills"])
    assert set(grants["tools"]) >= {"files", "shell"}


def test_a_fresh_hive_has_its_ready_made_roles_once_the_console_has_started(monkeypatch):
    monkeypatch.setenv("HIVE_ROLES_DIR", str(roles_data.REPO_DIR))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")

    with TestClient(main_mod.app):
        slugs = {r["slug"] for r in testing.run(store.roles_list())}

    assert slugs == {"general", "chief-of-staff", "operator", "developer", "planner", "optimizer"}


def test_the_role_editors_catalog_does_not_call_a_generated_skill_a_repo_one():
    reports = [{"mind": "stash", "hooks": None, "skills": {"skills": [
        {"name": "tracked", "repo": "text", "installed": "text", "source": "tracked"},
        {"name": "made-here", "repo": "text", "installed": "text", "source": "generated",
         "shadowed": False},
        {"name": "both", "repo": "text", "installed": "text", "source": "generated",
         "shadowed": True},
        {"name": "older-mind", "repo": "text", "installed": None},
    ]}}]

    rows = {r["name"]: r for r in roles_data.catalog(reports, set())["skills"]}

    assert (rows["tracked"]["repo"], rows["tracked"]["generated"]) == (True, False)
    assert (rows["made-here"]["repo"], rows["made-here"]["generated"]) == (False, True)
    assert (rows["both"]["repo"], rows["both"]["generated"]) == (True, True)
    assert (rows["older-mind"]["repo"], rows["older-mind"]["generated"]) == (True, False)
