"""The contract a container mind's `/skills` API owes the console.

There are two independent implementations of this API — this one, mounted
per harness by `minds.mind_server` here, and the edge repo's `skills_sync` — and they
must never share code. So each proves the same contract separately: the
console asks one set of questions and gets one shape of answer back,
whichever it is talking to.

Nothing here monkeypatches `repo_root` or `installed_root`. Which directory
a harness reads *is* the contract, so the fixtures move `PROJECT_DIR` and
the environment and let the real code resolve the paths. The proxy listing
and the notifier are transports and are the only things stubbed.
"""

from __future__ import annotations

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


def test_a_harness_with_no_declared_home_is_a_409_not_a_missing_route(mind, monkeypatch):
    client, _, _ = mind
    monkeypatch.delenv("CODEX_HOME")

    response = client.get("/skills?harness=codex", headers=_auth())

    assert response.status_code == 409
    assert "no codex home declared" in response.json()["error"]


def test_the_resolve_route_makes_the_named_copy_win(mind, tmp_path):
    client, repo, installed = mind
    _write_skill(repo, "memory", "original\n")
    client.post("/skills/memory/install", headers=_auth())
    reference = tmp_path / "project" / "minds" / "example" / "reference" / "skills" / "memory" / "SKILL.md"
    reference.write_text(reference.read_text().replace("original", "newer"))
    codex = tmp_path / "codex" / "skills" / "memory" / "SKILL.md"
    codex.write_text(codex.read_text().replace("original", "from codex"))

    response = client.post("/skills/memory/resolve?from=codex", headers=_auth())

    assert response.status_code == 200
    assert "from codex" in reference.read_text()
    assert "from codex" in (installed / "memory" / "SKILL.md").read_text()


def test_mind_start_renders_the_references_and_never_raises(mind, tmp_path, monkeypatch):
    """The start-up pass: off the event loop, and a failure is logged, not fatal."""
    import asyncio

    _, repo, installed = mind
    _write_skill(tmp_path / "project" / "minds" / "example" / "reference" / "skills", "memory", "body\n")

    asyncio.run(skills_api.check_at_start(None, "test-mind"))
    assert (tmp_path / "dsh" / "skills" / "memory" / "SKILL.md").exists()

    def _boom(**_):
        raise OSError("disk gone")

    monkeypatch.setattr(skills_api, "check_all", _boom)
    asyncio.run(skills_api.check_at_start(None, "test-mind"))


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


def test_a_skill_carrying_a_build_directory_travels_without_it(mind):
    """A venv is what a skill built, not the skill: it never reaches the repo."""
    client, repo, installed = mind
    _write_skill(installed, "heavy", "body\n")
    (installed / "heavy" / "venv").mkdir()
    (installed / "heavy" / "venv" / "blob").write_bytes(
        b"x" * (skills_api.MAX_SKILL_BYTES + 1)
    )

    response = client.post("/skills/heavy/write-back", headers=_auth())

    assert response.status_code == 200
    assert (repo / "heavy" / "SKILL.md").exists()
    assert not (repo / "heavy" / "venv").exists()


def test_a_skill_past_the_size_limit_is_refused_rather_than_copied(mind):
    client, repo, installed = mind
    _write_skill(installed, "heavy", "body\n")
    (installed / "heavy" / "blob").write_bytes(b"x" * (skills_api.MAX_SKILL_BYTES + 1))

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
        lambda: client.post("/skills/memory/resolve?from=claude"),
    ):
        assert call().status_code == 401


# ---- generated skills: the install's own folder (story 858) -----------------

GENERATED_TEXT = "---\nname: made-here\ndescription: made on this install\n---\nGenerated body.\n"


def _install_folder(repo):
    """`install/workflow-skills` of the project the fixture built."""
    folder = repo.parents[1] / "install" / "workflow-skills"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_the_skills_route_lists_a_generated_skill_and_says_where_it_comes_from(mind):
    client, repo, installed = mind
    _write_skill(repo, "tracked-only", "tracked body\n")
    _write_skill(repo, "made-here", "---\nname: made-here\ndescription: tracked\n---\nTracked body.\n")
    _write_skill(_install_folder(repo), "made-here", GENERATED_TEXT)
    _write_skill(_install_folder(repo), "only-generated", GENERATED_TEXT.replace("made-here", "only-generated"))
    (_install_folder(repo) / ".as-generated" / "made-here").mkdir(parents=True)

    body = client.get("/skills", headers=_auth()).json()

    rows = {row["name"]: row for row in body["skills"]}
    assert set(rows) == {"tracked-only", "made-here", "only-generated"}
    assert rows["tracked-only"]["source"] == "tracked" and rows["tracked-only"]["shadowed"] is False
    assert rows["only-generated"]["source"] == "generated"
    assert rows["only-generated"]["state"] == skills_api.STATE_NOT_INSTALLED
    assert rows["made-here"]["source"] == "generated" and rows["made-here"]["shadowed"] is True
    assert "Generated body." in rows["made-here"]["repo"]
    assert "Tracked body." in rows["made-here"]["shadowed_text"]


def test_the_diff_route_compares_a_generated_skill_not_the_tracked_one_of_that_name(mind):
    client, repo, installed = mind
    _write_skill(repo, "made-here", "---\nname: made-here\ndescription: tracked\n---\nTracked body.\n")
    _write_skill(_install_folder(repo), "made-here", GENERATED_TEXT)
    _write_skill(installed, "made-here", GENERATED_TEXT.replace("Generated body.", "Edited by the mind."))

    diff = client.get("/skills/made-here/diff", headers=_auth()).json()["diff"]

    assert "-Generated body." in diff and "+Edited by the mind." in diff
    assert "Tracked body." not in diff


def test_a_generated_skill_is_never_written_back_into_the_tracked_folder(mind):
    client, repo, installed = mind
    _write_skill(_install_folder(repo), "made-here", GENERATED_TEXT)
    _write_skill(installed, "made-here", GENERATED_TEXT)
    client.post("/skills/made-here/install", headers=_auth())

    response = client.post("/skills/made-here/write-back", headers=_auth())

    assert response.status_code == 422
    assert "generated" in response.json()["error"]
    assert list(repo.iterdir()) == []


@pytest.mark.parametrize("shape", ["incomplete", "link", "unreadable folder"])
def test_a_generated_name_is_never_written_back_even_when_its_folder_is_not_a_whole_skill(
        mind, shape):
    client, repo, installed = mind
    folder = _install_folder(repo)
    _write_skill(installed, "made-here", GENERATED_TEXT)
    if shape == "incomplete":
        (folder / "made-here").mkdir()
    elif shape == "link":
        (folder / "made-here").symlink_to(repo)
    else:
        folder.chmod(0o000)

    try:
        response = client.post("/skills/made-here/write-back", headers=_auth())
    finally:
        folder.chmod(0o755)

    assert response.status_code == 422
    assert list(repo.iterdir()) == []


def test_a_generated_skill_past_the_size_limit_is_not_delivered(mind, monkeypatch):
    client, repo, installed = mind
    _write_skill(_install_folder(repo), "made-here", GENERATED_TEXT)
    (_install_folder(repo) / "made-here" / "blob").write_bytes(b"x" * 2048)
    monkeypatch.setattr(skill_reference, "MAX_SKILL_BYTES", 1024)

    response = client.post("/skills/made-here/install", headers=_auth())

    assert response.status_code == 404
    assert "larger than a skill should be" in response.json()["error"]
    assert not (installed / "made-here").exists()


# --- install_skill(only_if_unchanged): the update the install makes of a delivered skill ---

GEN = "gen-skill"
GEN_TEXT = "---\nname: gen-skill\ndescription: generated\n---\nFirst.\n"


def _delivered(mind, tmp_path):
    """A generated skill delivered through the real install, as a mind is given it."""
    client, repo, installed = mind
    folder = _install_folder(repo)
    _write_skill(folder, GEN, GEN_TEXT)
    skills_api.install_skill("claude_cli", GEN)
    return folder, installed


def test_an_update_install_replaces_a_copy_the_mind_has_not_touched(mind, tmp_path):
    folder, installed = _delivered(mind, tmp_path)
    _write_skill(folder, GEN, GEN_TEXT.replace("First.", "Second."))

    skills_api.install_skill("claude_cli", GEN, only_if_unchanged=True)

    assert "Second." in (installed / GEN / "SKILL.md").read_text()


def test_an_update_install_refuses_a_copy_the_mind_edited_and_changes_nothing(mind, tmp_path):
    folder, installed = _delivered(mind, tmp_path)
    (installed / GEN / "SKILL.md").write_text("---\nname: gen-skill\ndescription: mine\n---\nMine.\n")
    _write_skill(folder, GEN, GEN_TEXT.replace("First.", "Second."))

    with pytest.raises(skills_api.SkillConflict):
        skills_api.install_skill("claude_cli", GEN, only_if_unchanged=True)

    assert "Mine." in (installed / GEN / "SKILL.md").read_text()
    assert "Second." not in (skills_api.reference_root() / GEN / "SKILL.md").read_text()


def test_an_update_install_refuses_a_copy_with_no_delivery_recorded(mind, tmp_path):
    client, repo, installed = mind
    folder = _install_folder(repo)
    _write_skill(folder, GEN, GEN_TEXT)
    # What every copy at home today is: rendered, with nothing said of what from.
    skill_reference.install("skill", GEN, "claude", GEN_TEXT, folder / GEN,
                            catalog=lambda h: None, notify=lambda text: None)
    _write_skill(folder, GEN, GEN_TEXT.replace("First.", "Second."))

    with pytest.raises(skills_api.SkillConflict):
        skills_api.install_skill("claude_cli", GEN, only_if_unchanged=True)

    assert "First." in (installed / GEN / "SKILL.md").read_text()
