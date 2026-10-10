"""The contract a container mind's `/skills` API owes the console.

There are two independent implementations of this API — this one, mounted
by both harness servers here, and the edge repo's `skills_sync` — and they
must never share code. So each proves the same contract separately: the
console asks one set of questions and gets one shape of answer back,
whichever it is talking to.

Nothing here monkeypatches `repo_root` or `installed_root`. Which directory
a harness reads *is* the contract, so the fixtures move `PROJECT_DIR` and
the environment and let the real code resolve the paths. The proxy listing
and the notifier are transports and are the only things stubbed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minds import runtime_api, skill_reference, skills_api

ADMIN_TOKEN = "test-admin-token"  # secret-guard: allow
REPO_ROOT = Path(__file__).resolve().parents[2]
SENT: list[str] = []


def _write_skill(root, name: str, body: str, **extra: str) -> None:
    """A skill is a directory; `extra` puts sibling files beside the markdown."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body)
    for filename, content in extra.items():
        (directory / filename).write_text(content)


def _build(monkeypatch, tmp_path, harness: str):
    project = tmp_path / "project"
    homes = {h: tmp_path / h for h in skill_reference.HARNESSES}
    (project / "specs" / "skills").mkdir(parents=True)
    for home in homes.values():
        (home / "skills").mkdir(parents=True)

    monkeypatch.setenv("MIND_NAME", "example")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(homes["claude"]))
    monkeypatch.setenv("CODEX_HOME", str(homes["codex"]))
    monkeypatch.setenv("DSH_HOME", str(homes["dsh"]))
    monkeypatch.setattr(skills_api, "PROJECT_DIR", project, raising=True)
    monkeypatch.setattr(skill_reference, "PROJECT_DIR", project, raising=True)
    monkeypatch.setattr(runtime_api, "admin_token", lambda: ADMIN_TOKEN, raising=True)
    SENT.clear()
    monkeypatch.setattr(skill_reference, "proxy_catalog", lambda *a: (lambda h: None))
    monkeypatch.setattr(skill_reference, "telegram_notifier", lambda *a: SENT.append)

    app = FastAPI()
    skills_api.install_skills_routes(app, harness=harness, mind_id="test-mind", log=None)
    return (
        TestClient(app, raise_server_exceptions=False),
        skills_api.repo_root(),
        skills_api.installed_root(harness),
    )


@pytest.fixture()
def mind(monkeypatch, tmp_path):
    yield _build(monkeypatch, tmp_path, "claude_cli")


@pytest.fixture()
def codex_mind(monkeypatch, tmp_path):
    yield _build(monkeypatch, tmp_path, "codex_cli")


def _auth() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def test_the_skills_route_reports_every_state(mind):
    """All four states, computed by the real state machine."""
    client, repo, installed = mind
    _write_skill(repo, "in-sync", "shared\n")
    _write_skill(repo, "edited", "repo body\n")
    _write_skill(repo, "absent-here", "repo only\n")
    _write_skill(installed, "mind-only", "mind only\n")
    client.post("/skills/in-sync/install", headers=_auth())
    client.post("/skills/edited/install", headers=_auth())
    (installed / "edited" / "SKILL.md").write_text("---\nname: edited\n---\nmind body\n")

    body = client.get("/skills", headers=_auth()).json()

    assert body["harness"] == "claude"
    rows = {row["name"]: row for row in body["skills"]}
    assert rows["in-sync"]["state"] == skills_api.STATE_SAME
    assert rows["edited"]["state"] == skills_api.STATE_DIFFERS
    assert rows["edited"]["copy"] == "edited"
    assert rows["absent-here"]["state"] == skills_api.STATE_NOT_INSTALLED
    assert rows["mind-only"]["state"] == skills_api.STATE_LOCAL_ONLY
    assert rows["edited"]["repo"] == "repo body\n"
    assert rows["edited"]["installed"] == "---\nname: edited\n---\nmind body\n"


def test_the_harness_named_on_the_request_picks_the_copy_reported(codex_mind, tmp_path):
    """A codex mind reports codex by default, and any harness when asked."""
    client, repo, _ = codex_mind
    _write_skill(repo, "notes", "---\nname: notes\ndescription: d\nharness:\n  claude:\n    tools: Bash\n---\nbody\n")
    client.post("/skills/notes/install", headers=_auth())
    (tmp_path / "dsh" / "skills" / "loose").mkdir()
    (tmp_path / "dsh" / "skills" / "loose" / "SKILL.md").write_text("only under dsh\n")

    default = client.get("/skills", headers=_auth()).json()
    claude = client.get("/skills?harness=claude", headers=_auth()).json()
    dsh = client.get("/skills?harness=dsh", headers=_auth()).json()

    assert default["harness"] == "codex"
    assert "tools" not in default["skills"][0]["installed"]
    assert claude["harness"] == "claude"
    assert "tools: Bash" in claude["skills"][0]["installed"]
    assert [row["name"] for row in dsh["skills"]] == ["loose", "notes"]


def test_a_sibling_file_drifting_is_not_reported_as_in_sync(mind):
    """State is the whole directory. A matching SKILL.md is not enough."""
    client, repo, installed = mind
    _write_skill(repo, "memory", "same markdown\n", **{"helper.py": "repo version\n"})
    client.post("/skills/memory/install", headers=_auth())
    (installed / "memory" / "helper.py").write_text("mind version\n")

    row = client.get("/skills", headers=_auth()).json()["skills"][0]

    assert row["state"] == skills_api.STATE_DIFFERS


def test_installing_moves_the_whole_directory_into_every_harness(mind, tmp_path):
    client, repo, installed = mind
    _write_skill(repo, "memory", "body\n", **{"helper.py": "repo version\n"})
    _write_skill(installed, "memory", "body\n", **{"helper.py": "mind version\n"})

    client.post("/skills/memory/install", headers=_auth())

    for h in skill_reference.HARNESSES:
        assert (tmp_path / h / "skills" / "memory" / "helper.py").read_text() == "repo version\n"


def test_the_diff_names_both_sides_and_shows_the_change(mind):
    client, repo, installed = mind
    _write_skill(repo, "memory", "line one\nrepo line\n")
    _write_skill(installed, "memory", "line one\nmind line\n")

    diff = client.get("/skills/memory/diff", headers=_auth()).json()["diff"]

    assert "--- repo/memory/SKILL.md" in diff
    assert "+++ mind/memory/SKILL.md" in diff
    assert "-repo line" in diff
    assert "+mind line" in diff


def test_write_back_and_remove_answer_the_shape_the_console_expects(mind, tmp_path):
    client, repo, installed = mind
    _write_skill(repo, "memory", "repo body\n")
    _write_skill(installed, "memory", "---\nname: memory\ndescription: d\n---\nmind body\n")

    written = client.post("/skills/memory/write-back", headers=_auth())
    assert written.status_code == 200
    assert written.json()["skill"]["state"] == skills_api.STATE_SAME
    assert (repo / "memory" / "SKILL.md").read_text().endswith("mind body\n")
    assert (tmp_path / "dsh" / "skills" / "memory" / "SKILL.md").read_text().endswith("mind body\n")

    removed = client.delete("/skills/memory", headers=_auth())
    assert removed.status_code == 200
    for h in skill_reference.HARNESSES:
        assert not (tmp_path / h / "skills" / "memory").exists()
    assert (repo / "memory" / "SKILL.md").exists()


def test_a_write_back_over_a_newer_reference_answers_409(mind, tmp_path):
    client, repo, installed = mind
    _write_skill(repo, "memory", "original\n")
    client.post("/skills/memory/install", headers=_auth())
    reference = tmp_path / "project" / "minds" / "example" / "reference" / "skills" / "memory" / "SKILL.md"
    reference.write_text(reference.read_text().replace("original", "newer"))
    copy = installed / "memory" / "SKILL.md"
    copy.write_text(copy.read_text().replace("original", "edited"))

    response = client.post("/skills/memory/write-back", headers=_auth())

    assert response.status_code == 409
    assert (repo / "memory" / "SKILL.md").read_text() == "original\n"
    assert "newer" in reference.read_text()
    assert "edited" in copy.read_text()


def test_an_unreadable_skill_is_not_reported_as_an_absent_one(mind):
    """The remedy offered for "absent" overwrites the directory."""
    client, repo, installed = mind
    _write_skill(repo, "memory", "repo body\n")
    _write_skill(installed, "memory", "mind body\n")
    (installed / "memory").chmod(0o000)
    try:
        row = client.get("/skills", headers=_auth()).json()["skills"][0]
    finally:
        (installed / "memory").chmod(0o755)

    assert row["state"] == skills_api.STATE_UNREADABLE


def test_an_unreadable_skills_directory_is_not_reported_as_an_empty_one(mind):
    client, repo, installed = mind
    installed.chmod(0o000)
    try:
        response = client.get("/skills", headers=_auth())
    finally:
        installed.chmod(0o755)

    assert response.status_code == 503
    assert "cannot be listed" in response.json()["error"]


def test_a_symlinked_skill_can_be_removed_and_replaced(mind, tmp_path):
    """hive_mind's curator maintains plugin skills as symlinks."""
    client, repo, installed = mind
    real = tmp_path / "plugin" / "notify"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("from a plugin\n")
    (installed / "notify").symlink_to(real)
    _write_skill(repo, "notify", "from the repo\n")

    assert client.post("/skills/notify/install", headers=_auth()).status_code == 200
    assert not (installed / "notify").is_symlink()
    assert (installed / "notify" / "SKILL.md").read_text().endswith("from the repo\n")
    assert (real / "SKILL.md").read_text() == "from a plugin\n"

    assert client.delete("/skills/notify", headers=_auth()).status_code == 200


def test_a_skill_carrying_a_build_directory_is_refused_rather_than_copied(mind):
    """Following a venv into the repo is hundreds of megabytes git ignores."""
    client, repo, installed = mind
    _write_skill(installed, "heavy", "body\n")
    (installed / "heavy" / "venv").mkdir()
    (installed / "heavy" / "venv" / "blob").write_bytes(
        b"x" * (skills_api.MAX_SKILL_BYTES + 1)
    )

    response = client.post("/skills/heavy/write-back", headers=_auth())

    assert response.status_code == 404
    assert "larger than a skill should be" in response.json()["error"]
    assert not (repo / "heavy").exists()


def test_every_route_requires_the_admin_bearer(mind):
    """Reads included — these return the full text of every skill."""
    client, repo, _ = mind
    _write_skill(repo, "memory", "repo body\n")

    for call in (
        lambda: client.get("/skills"),
        lambda: client.get("/skills/memory/diff"),
        lambda: client.post("/skills/memory/install"),
        lambda: client.post("/skills/memory/write-back"),
        lambda: client.delete("/skills/memory"),
    ):
        assert call().status_code == 401


@pytest.mark.parametrize("harness", ["claude_cli", "codex_cli"])
def test_both_harness_servers_mount_the_skills_routes(harness):
    """Otherwise a mind ships with the API absent and nothing notices.

    Read as source rather than imported: importing a harness server runs its
    module-level setup, which wants a real mind directory and a running
    gateway.
    """
    source = (REPO_ROOT / "minds" / "harness" / f"{harness}.py").read_text()

    assert re.search(
        r"skills_api\.install_skills_routes\(\s*app,\s*harness=\"" + harness + r"\"",
        source,
    ), f"{harness}.py does not mount the skills routes"
