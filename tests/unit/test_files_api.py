"""The contract the `/files` API owes the console's skill and hook editor.

Ported from hive-edge-minds, where this API was built, so a containerised
mind gets the same guarantees a bare-metal one already had. The module is
identical; only the wiring differs, and the fixture here is simpler for it —
`install_files_routes` mounts onto a bare app, so nothing has to reload a
whole mind server to get a client.

Nothing here monkeypatches the path resolution. Which directory a harness
reads is the whole question, so the fixtures move the environment and let
the real code compute the roots, exactly as the skills tests do.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minds import files_api, skills_api

ADMIN_TOKEN = "test-admin-token"  # secret-guard: allow


def _build(monkeypatch, tmp_path, harness: str):
    """A mind of the given harness, with real path resolution.

    `claude_home` and `codex_home` are separate directories on purpose: a
    test that pointed both environment variables at one path could not tell
    "reads the codex location" from "reads whatever is there".
    """
    project = tmp_path / "project"
    claude_home = tmp_path / "claude-home"
    codex_home = tmp_path / "codex-home"
    (project / "specs" / "skills" / "claude").mkdir(parents=True)
    (project / "specs" / "skills" / "codex").mkdir(parents=True)
    for home in (claude_home, codex_home):
        (home / "skills").mkdir(parents=True)
        (home / "hooks").mkdir(parents=True)

    monkeypatch.setenv("MIND_ID", "example")
    monkeypatch.setenv("MIND_NAME", "example")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("MIND_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(skills_api, "PROJECT_DIR", project, raising=True)

    app = FastAPI()
    files_api.install_files_routes(
        app, harness=harness, mind_id="example", log=logging.getLogger("test")
    )
    client = TestClient(app, raise_server_exceptions=False)
    home = codex_home if harness.startswith("codex") else claude_home
    return client, home, project


@pytest.fixture()
def mind(monkeypatch, tmp_path):
    yield _build(monkeypatch, tmp_path, "claude_cli")


@pytest.fixture()
def codex_mind(monkeypatch, tmp_path):
    yield _build(monkeypatch, tmp_path, "codex_cli")


def _auth() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _paths(response) -> list[str]:
    return [row["path"] for row in response.json()["files"]]


# 1. The skills browser lists every file inside each skill, not only its
#    SKILL.md.
def test_the_skills_listing_reaches_inside_each_skill(mind):
    client, home, project = mind
    skill = home / "skills" / "remember"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("how to remember\n")
    (skill / "remember.sh").write_text("#!/bin/sh\necho remembering\n")
    (skill / "references" / "classes.md").write_text("the data classes\n")
    # The repo ships a skill this mind has not installed. The editor is for
    # the copy that runs, so the repo's copy must not appear in the listing —
    # otherwise a listing pointed at the wrong root looks identical.
    repo_only = project / "specs" / "skills" / "claude" / "shipped-not-installed"
    repo_only.mkdir(parents=True)
    (repo_only / "SKILL.md").write_text("from the repo\n")
    # What a skill installed is not the skill. A real operator mind carries a
    # virtualenv inside one of these; enumerating it is thousands of rows
    # nobody opened this editor to see.
    vendored = skill / "venv" / "lib" / "python3.12"
    vendored.mkdir(parents=True)
    (vendored / "libmupdf.so").write_bytes(b"\x7fELF")

    response = client.get("/files/skills", headers=_auth())

    assert response.status_code == 200
    assert _paths(response) == [
        "remember/SKILL.md",
        "remember/references/classes.md",
        "remember/remember.sh",
    ]
    # Named, not silently dropped.
    assert response.json()["omitted"] == ["remember/venv"]


# 2. The hooks browser lists every file in the mind's hooks folder.
def test_the_hooks_listing_names_every_file_in_the_folder(mind):
    client, home, _ = mind
    hooks = home / "hooks"
    (hooks / "auto_remember.sh").write_text("#!/bin/bash\n")
    (hooks / "rotation_check.py").write_text("print('check')\n")

    response = client.get("/files/hooks", headers=_auth())

    assert response.status_code == 200
    assert _paths(response) == ["auto_remember.sh", "rotation_check.py"]


# 3. Clicking a file shows its current contents.
def test_reading_a_file_returns_the_text_that_is_on_disk(mind):
    client, home, _ = mind
    (home / "hooks" / "time_inject.sh").write_text("#!/bin/bash\ndate\n")

    body = client.get(
        "/files/hooks/content", params={"path": "time_inject.sh"}, headers=_auth()
    ).json()

    assert body["editable"] is True
    assert body["text"] == "#!/bin/bash\ndate\n"


# 4. Contents can be changed and saved, and reopening shows what was saved.
def test_saving_then_reopening_returns_the_saved_text(mind):
    client, home, _ = mind
    skill = home / "skills" / "memory"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("old body\n")

    opened = client.get(
        "/files/skills/content", params={"path": "memory/SKILL.md"}, headers=_auth()
    ).json()
    saved = client.put(
        "/files/skills/content",
        json={
            "path": "memory/SKILL.md",
            "text": "new body\n",
            "revision": opened["revision"],
        },
        headers=_auth(),
    )
    assert saved.status_code == 200

    reopened = client.get(
        "/files/skills/content", params={"path": "memory/SKILL.md"}, headers=_auth()
    ).json()
    assert reopened["text"] == "new body\n"
    # And on disk, because a save held in memory and read back through the
    # same route is self-consistent whether or not a byte ever landed.
    assert (skill / "SKILL.md").read_text() == "new body\n"


# 5. A file the mind cannot show as text opens as "not editable here"
#    rather than as garbage.
def test_a_binary_file_is_reported_as_not_editable_rather_than_decoded(mind):
    client, home, _ = mind
    cache = home / "hooks" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "rotation_check.cpython-312.pyc").write_bytes(b"\xcb\r\r\n\x00\x00\x00\x00")

    listed = client.get("/files/hooks", headers=_auth()).json()["files"]
    assert [row["editable"] for row in listed] == [False]

    body = client.get(
        "/files/hooks/content",
        params={"path": "__pycache__/rotation_check.cpython-312.pyc"},
        headers=_auth(),
    ).json()
    assert body["editable"] is False
    assert body["text"] is None
    assert body["reason"]


# 7. A path that climbs out of the tree is refused: nothing is returned and
#    nothing is written.
def test_a_path_outside_the_tree_reads_nothing_and_writes_nothing(mind):
    client, home, _ = mind
    secret = home / "settings.json"
    secret.write_text('{"hooks": "do not touch"}\n')
    outside = home.parent / "elsewhere.txt"
    outside.write_text("untouched\n")
    (home / "skills" / "escape").mkdir(parents=True)
    (home / "skills" / "escape" / "SKILL.md").write_text("bait\n")
    # A symlink is the other way out, and it is spelled like an ordinary file.
    (home / "skills" / "escape" / "link.txt").symlink_to(outside)

    for path in (
        "../settings.json",
        "../../elsewhere.txt",
        "escape/link.txt",
        "/etc/passwd",
        "//etc/passwd",
    ):
        assert client.get(
            "/files/skills/content", params={"path": path}, headers=_auth()
        ).status_code == 404
        # Refused, whatever shape the refusal takes — what requirement 7
        # claims is that nothing is returned and nothing is written.
        assert client.put(
            "/files/skills/content",
            json={"path": path, "text": "owned\n", "revision": "whatever"},
            headers=_auth(),
        ).status_code == 404

    assert secret.read_text() == '{"hooks": "do not touch"}\n'
    assert outside.read_text() == "untouched\n"
    # Nor is the escaping symlink offered as a file of this tree.
    assert _paths(client.get("/files/skills", headers=_auth())) == [
        "escape/SKILL.md"
    ]

    # A symlink landing back inside the tree is ordinary — skills carry them
    # — and refusing those would be the same bug wearing the other face.
    (home / "skills" / "escape" / "inside.md").symlink_to(
        home / "skills" / "escape" / "SKILL.md"
    )
    kept = client.get(
        "/files/skills/content", params={"path": "escape/inside.md"}, headers=_auth()
    ).json()
    assert kept["text"] == "bait\n"


# 8. Saving requires the admin credential; without it the save is refused
#    and the file is left alone.
def test_every_files_route_requires_the_admin_bearer(mind):
    client, home, _ = mind
    (home / "hooks" / "prose_reminder.sh").write_text("original\n")

    # No header at all, and a header carrying the wrong token: "a bearer is
    # required" is not the claim — "this bearer" is, on routes that return
    # the full text of every hook this mind runs, on a LAN-reachable port.
    for headers in ({}, {"Authorization": "Bearer not-the-admin-token"}):  # secret-guard: allow
        assert client.get("/files/hooks", headers=headers).status_code == 401
        assert client.get(
            "/files/hooks/content",
            params={"path": "prose_reminder.sh"},
            headers=headers,
        ).status_code == 401
        assert client.put(
            "/files/hooks/content",
            json={
                "path": "prose_reminder.sh",
                "text": "overwritten\n",
                "revision": "any",
            },
            headers=headers,
        ).status_code == 401
    assert (home / "hooks" / "prose_reminder.sh").read_text() == "original\n"


# 9. A Codex mind reads its own Codex locations, not the Claude ones.
def test_a_codex_mind_reads_the_codex_skills_and_hooks(codex_mind, tmp_path):
    client, codex_home, _ = codex_mind
    (tmp_path / "claude-home" / "hooks" / "claude_only.sh").write_text("claude\n")
    (codex_home / "hooks" / "codex_only.sh").write_text("codex\n")
    codex_skill = codex_home / "skills" / "memory"
    codex_skill.mkdir(parents=True)
    (codex_skill / "SKILL.md").write_text("codex memory\n")

    assert _paths(client.get("/files/hooks", headers=_auth())) == ["codex_only.sh"]
    assert _paths(client.get("/files/skills", headers=_auth())) == ["memory/SKILL.md"]


# 10. A save lands in the directory the harness loads from, not the repo
#     copy and not a staging area.
def test_a_save_lands_in_the_installed_tree_and_leaves_the_repo_alone(mind):
    client, home, project = mind
    installed = home / "skills" / "memory"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("installed body\n")
    repo_skill = project / "specs" / "skills" / "claude" / "memory"
    repo_skill.mkdir(parents=True)
    (repo_skill / "SKILL.md").write_text("repo body\n")

    opened = client.get(
        "/files/skills/content", params={"path": "memory/SKILL.md"}, headers=_auth()
    ).json()
    client.put(
        "/files/skills/content",
        json={
            "path": "memory/SKILL.md",
            "text": "edited body\n",
            "revision": opened["revision"],
        },
        headers=_auth(),
    )

    assert (installed / "SKILL.md").read_text() == "edited body\n"
    assert (repo_skill / "SKILL.md").read_text() == "repo body\n"
    # And nothing of the write is left behind beside it.
    assert sorted(p.name for p in installed.iterdir()) == ["SKILL.md"]


def test_a_saved_hook_keeps_the_bit_that_lets_it_run(mind):
    """Also requirement 10: a hook that loses `+x` stops firing, silently.

    A write that stages and renames drops the mode. The edit reads back
    perfectly and the memory pipeline dies on the next turn with EACCES,
    which is the failure this asserts against.
    """
    client, home, _ = mind
    hook = home / "hooks" / "auto_remember.sh"
    hook.write_text("#!/bin/bash\nold\n")
    hook.chmod(0o755)

    opened = client.get(
        "/files/hooks/content", params={"path": "auto_remember.sh"}, headers=_auth()
    ).json()
    client.put(
        "/files/hooks/content",
        json={
            "path": "auto_remember.sh",
            "text": "#!/bin/bash\nnew\n",
            "revision": opened["revision"],
        },
        headers=_auth(),
    )

    assert hook.read_text() == "#!/bin/bash\nnew\n"
    assert hook.stat().st_mode & 0o777 == 0o755


# 11. A save refuses when the file changed since it was opened, rather than
#     silently discarding whatever changed it.
def test_a_save_against_a_stale_buffer_is_refused_not_applied(mind):
    """The sync table sits directly above this editor.

    "Apply repo copy" replaces the whole skill directory. An operator who
    opens a file, reverts the skill from the table, then saves their buffer
    would silently un-revert it — and the same race is two browser tabs.
    """
    client, home, _ = mind
    skill = home / "skills" / "code"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("as opened\n")

    opened = client.get(
        "/files/skills/content", params={"path": "code/SKILL.md"}, headers=_auth()
    ).json()

    # Something else replaces it while the editor is open.
    (skill / "SKILL.md").write_text("reverted from the repo\n")

    refused = client.put(
        "/files/skills/content",
        json={
            "path": "code/SKILL.md",
            "text": "my stale edit\n",
            "revision": opened["revision"],
        },
        headers=_auth(),
    )

    assert refused.status_code == 409
    assert (skill / "SKILL.md").read_text() == "reverted from the repo\n"

    # Nor can the check be skipped by simply not sending one — a staleness
    # check that is optional on the wire is not a staleness check.
    for body in (
        {"path": "code/SKILL.md", "text": "clobbered\n"},
        {"path": "code/SKILL.md", "text": "clobbered\n", "revision": None},
        {"path": "code/SKILL.md", "text": "clobbered\n", "revision": ""},
    ):
        assert client.put(
            "/files/skills/content", json=body, headers=_auth()
        ).status_code == 400
    assert (skill / "SKILL.md").read_text() == "reverted from the repo\n"

    # Reopening yields the revision that now describes the file, and that save lands.
    fresh = client.get(
        "/files/skills/content", params={"path": "code/SKILL.md"}, headers=_auth()
    ).json()
    assert client.put(
        "/files/skills/content",
        json={
            "path": "code/SKILL.md",
            "text": "my edit, reapplied\n",
            "revision": fresh["revision"],
        },
        headers=_auth(),
    ).status_code == 200
    assert (skill / "SKILL.md").read_text() == "my edit, reapplied\n"


def test_the_skills_browser_does_not_offer_the_minds_own_state_files(mind):
    """Also requirement 1: what is under the root but is not a skill.

    `.usage.json` is written by the telemetry hook under a lock file on
    every turn, and `.curator_state` is 0600. Neither is a skill and both
    would be handed to an editor by a naive walk of the root.
    """
    client, home, _ = mind
    root = home / "skills"
    (root / ".usage.json").write_text("{}\n")
    (root / ".curator_state").write_text("state\n")
    (root / ".archived" / "old-skill").mkdir(parents=True)
    (root / ".archived" / "old-skill" / "SKILL.md").write_text("retired\n")
    live = root / "memory"
    live.mkdir()
    (live / "SKILL.md").write_text("recall\n")

    assert _paths(client.get("/files/skills", headers=_auth())) == ["memory/SKILL.md"]


def test_a_skill_symlinked_from_the_plugins_directory_is_listed_and_editable(mind):
    """Also requirement 1: a plugin skill is installed as a symlink.

    Refusing it means a skill the mind actually runs appears nowhere. The
    harness's own config file, one directory up from `plugins/`, stays out
    of reach — this is an editor for skills, not for settings.json.
    """
    client, home, _ = mind
    plugin = home / "plugins" / "marketplaces" / "someone" / "skills" / "notify"
    plugin.mkdir(parents=True)
    (plugin / "SKILL.md").write_text("how to notify\n")
    (home / "skills" / "notify").symlink_to(plugin)
    (home / "settings.json").write_text('{"hooks": {}}\n')
    (home / "skills" / "notify-settings").mkdir()
    (home / "skills" / "notify-settings" / "SKILL.md").write_text("bait\n")
    (home / "skills" / "notify-settings" / "conf.json").symlink_to(home / "settings.json")

    listed = client.get("/files/skills", headers=_auth())
    assert "notify/SKILL.md" in _paths(listed)
    assert listed.json()["outside"] == ["notify-settings/conf.json"]

    opened = client.get(
        "/files/skills/content", params={"path": "notify/SKILL.md"}, headers=_auth()
    ).json()
    assert opened["text"] == "how to notify\n"
    assert client.put(
        "/files/skills/content",
        json={
            "path": "notify/SKILL.md",
            "text": "how to notify, revised\n",
            "revision": opened["revision"],
        },
        headers=_auth(),
    ).status_code == 200
    assert (plugin / "SKILL.md").read_text() == "how to notify, revised\n"

    # And the harness's own configuration is not reachable through a skill.
    assert client.get(
        "/files/skills/content",
        params={"path": "notify-settings/conf.json"},
        headers=_auth(),
    ).status_code == 404
    assert (home / "settings.json").read_text() == '{"hooks": {}}\n'


def test_a_text_file_is_not_called_binary_by_where_the_sniff_lands(mind):
    """Also requirement 5, in the direction that mislabels a real file.

    The listing samples the head of each file rather than reading megabytes
    of every row. A multi-byte character straddling that boundary must not
    decide whether a 24 KB prose hook is editable.
    """
    client, home, _ = mind
    body = "x" * (files_api.SNIFF_BYTES - 1) + "—" + "tail of the hook\n"
    (home / "hooks" / "prose_hook.sh").write_text(body)

    listed = client.get("/files/hooks", headers=_auth()).json()["files"]

    assert [row["editable"] for row in listed] == [True]
    assert client.get(
        "/files/hooks/content", params={"path": "prose_hook.sh"}, headers=_auth()
    ).json()["text"] == body


# 12. A save body whose `text` is not a string is refused, not coerced. This
#     route writes hooks that execute every turn; a missing field, a null or
#     an object turning into "" or a Python repr on a 200 OK would truncate
#     or corrupt a live executable while telling the caller it worked.
@pytest.mark.parametrize("bad", [None, 123, {"a": 1}, ["line"], True])
def test_a_save_whose_text_is_not_a_string_is_refused(mind, bad):
    client, home, _ = mind
    hook = home / "hooks" / "stop.sh"
    original = "#!/bin/sh\necho standing\n"
    hook.write_text(original)

    opened = client.get(
        "/files/hooks/content", params={"path": "stop.sh"}, headers=_auth()
    ).json()

    body = {"path": "stop.sh", "revision": opened["revision"]}
    if bad is not None:
        body["text"] = bad
    response = client.put("/files/hooks/content", json=body, headers=_auth())

    assert response.status_code == 400
    assert "text required" in response.json()["error"]
    assert hook.read_text() == original, "a refused save must not touch the file"
