"""One reference copy per skill and agent, rendered into every harness.

Nothing here monkeypatches where a harness reads from. Which directory a
harness loads is the whole question, so the fixture moves `PROJECT_DIR` and
the three config homes and lets the real code resolve the paths. The proxy
listing and the notifier are the transports, so those two are stubbed: the
listing as a dict of what each harness is offered, the notifier as the list
of messages that went out.
"""

from __future__ import annotations

import json
import os
import tomllib

import pytest
import yaml

from minds import skill_reference, skills_api

MIND = "example"


class _Proxy:
    """What the proxy offers each harness. Mutable: models get retired."""

    def __init__(self):
        self.offered = {
            "claude": {"claude-opus-5", "claude-sonnet-5"},
            "codex": {"gpt-5.6-terra"},
            "dsh": {"qwen3-coder"},
        }

    def __call__(self, harness):
        return sorted(self.offered.get(harness, ()))


@pytest.fixture()
def mind(monkeypatch, tmp_path):
    project = tmp_path / "project"
    homes = {h: tmp_path / h for h in skill_reference.HARNESSES}
    monkeypatch.setenv("MIND_NAME", MIND)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(homes["claude"]))
    monkeypatch.setenv("CODEX_HOME", str(homes["codex"]))
    monkeypatch.setenv("DSH_HOME", str(homes["dsh"]))
    monkeypatch.setattr(skill_reference, "PROJECT_DIR", project, raising=True)
    monkeypatch.setattr(skills_api, "PROJECT_DIR", project, raising=True)
    sent: list[str] = []
    return {
        "project": project,
        "homes": homes,
        "reference": project / "minds" / MIND / "reference",
        "proxy": _Proxy(),
        "sent": sent,
        "notify": sent.append,
    }


def _frontmatter(path):
    return skill_reference.split_frontmatter(path.read_text())


def _write_reference_skill(root, name, frontmatter, body, **siblings):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(skill_reference.compose_frontmatter(frontmatter, body))
    for filename, text in siblings.items():
        (directory / filename).write_text(text)


def _write_reference_agent(mind, name, frontmatter, body):
    path = mind["reference"] / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_reference.compose_frontmatter(frontmatter, body))
    return path


def _check(mind):
    return skill_reference.check(catalog=mind["proxy"], notify=mind["notify"])


def _snapshot(path):
    """Every file under `path` (or the file itself), for "nothing changed"."""
    if path.is_file():
        return {"": path.read_bytes()}
    if not path.exists():
        return None
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


# 31
def test_a_reference_skill_renders_each_harness_its_own_block_and_syncs_as_same(mind):
    _write_reference_skill(
        mind["project"] / "specs" / "skills", "notes",
        {
            "name": "notes",
            "description": "Keep notes.",
            "argument-hint": "[topic]",
            "harness": {
                "claude": {"user-invocable": True, "allowed-tools": "Bash"},
                "codex": {"model_reasoning_effort": "high"},
                "dsh": {"whenToUse": "When notes are asked for.", "unknown-key": 1},
            },
        },
        "# notes\n\nWrite them down.\n",
        **{"helper.sh": "echo hi\n"},
    )

    skills_api.install_skill("claude", "notes", catalog=mind["proxy"], notify=mind["notify"])

    claude_fm, claude_body = _frontmatter(mind["homes"]["claude"] / "skills" / "notes" / "SKILL.md")
    codex_fm, codex_body = _frontmatter(mind["homes"]["codex"] / "skills" / "notes" / "SKILL.md")
    dsh_fm, dsh_body = _frontmatter(mind["homes"]["dsh"] / "skills" / "notes" / "SKILL.md")
    assert set(claude_fm) == {"name", "description", "argument-hint", "user-invocable", "allowed-tools"}
    assert set(codex_fm) == {"name", "description", "argument-hint", "model_reasoning_effort"}
    assert dsh_fm == {"name": "notes", "description": "Keep notes.",
                      "whenToUse": "When notes are asked for.", "unknown-key": 1}
    assert claude_body == codex_body == dsh_body == "# notes\n\nWrite them down.\n"
    for h in skill_reference.HARNESSES:
        assert (mind["homes"][h] / "skills" / "notes" / "helper.sh").read_text() == "echo hi\n"
        rows = {row.name: row for row in skills_api.list_skills(h)}
        assert rows["notes"].state == skills_api.STATE_SAME

    codex_copy = mind["homes"]["codex"] / "skills" / "notes" / "SKILL.md"
    codex_copy.write_text(codex_copy.read_text() + "tuned\n")
    row = {r.name: r for r in skills_api.list_skills("codex")}["notes"]
    assert row.state == skills_api.STATE_DIFFERS and row.copy == "edited"


def _dsh_rows(mind):
    """The delegate rows dsh would load from the mind's agents overlay."""
    overlay = yaml.safe_load((mind["homes"]["dsh"] / "agents.patch.yml").read_text())
    return {row["config"]["toolName"]: row for entry in overlay for row in entry["insert"]}


# 32
def test_a_reference_agent_renders_a_claude_file_a_codex_toml_and_a_dsh_delegate(mind):
    body = "You review diffs.\nBe terse.\n"
    _write_reference_agent(mind, "code-reviewer", {
        "name": "code-reviewer",
        "description": "Reviews a diff.",
        "harness": {
            "claude": {"tools": "Read, Grep"},
            "codex": {"model_reasoning_effort": "high", "sandbox_mode": "read-only",
                      "nickname_candidates": ["rev", "critic"]},
            "dsh": {"model": "qwen3-coder", "maxDepth": 1,
                    "toolFilter": {"deny": ["write"]}},
        },
    }, body)

    _check(mind)

    claude_fm, claude_body = _frontmatter(mind["homes"]["claude"] / "agents" / "code-reviewer.md")
    assert claude_fm == {"name": "code-reviewer", "description": "Reviews a diff.", "tools": "Read, Grep"}
    assert claude_body == body

    toml = tomllib.loads((mind["homes"]["codex"] / "agents" / "code-reviewer.toml").read_text())
    assert toml == {
        "name": "code-reviewer",
        "description": "Reviews a diff.",
        "model_reasoning_effort": "high",
        "sandbox_mode": "read-only",
        "nickname_candidates": ["rev", "critic"],
        "developer_instructions": body,
    }

    row = _dsh_rows(mind)["code_reviewer"]
    assert row["name"] == "@deepseek-ai/dsh-tool-subagent"
    assert row["config"]["persona"] == body
    assert row["config"]["agentOptions"] == {"model": "qwen3-coder"}
    assert row["config"]["maxDepth"] == 1 and row["config"]["toolFilter"] == {"deny": ["write"]}


# 33
def test_a_model_named_for_one_harness_lands_only_in_that_copy(mind):
    _write_reference_agent(mind, "builder", {
        "name": "builder",
        "description": "Builds.",
        "harness": {"codex": {"model": "gpt-5.6-terra"}},
    }, "Build it.\n")
    _write_reference_skill(mind["reference"] / "skills", "plan", {
        "name": "plan",
        "description": "Plans.",
        "harness": {"claude": {"model": "claude-sonnet-5"}},
    }, "Plan it.\n")

    _check(mind)

    toml = tomllib.loads((mind["homes"]["codex"] / "agents" / "builder.toml").read_text())
    assert toml["model"] == "gpt-5.6-terra"
    claude_agent, _ = _frontmatter(mind["homes"]["claude"] / "agents" / "builder.md")
    assert "model" not in claude_agent
    assert "agentOptions" not in _dsh_rows(mind)["builder"]["config"]

    claude_skill, _ = _frontmatter(mind["homes"]["claude"] / "skills" / "plan" / "SKILL.md")
    assert claude_skill["model"] == "claude-sonnet-5"
    for h in ("codex", "dsh"):
        fm, _ = _frontmatter(mind["homes"][h] / "skills" / "plan" / "SKILL.md")
        assert "model" not in fm


# 34
def test_a_model_the_proxy_no_longer_offers_leaves_that_copy_and_notifies(mind):
    path = _write_reference_agent(mind, "builder", {
        "name": "builder",
        "description": "Builds.",
        "harness": {"codex": {"model": "gpt-5.6-terra"}},
    }, "Build it.\n")
    _check(mind)
    codex_copy = mind["homes"]["codex"] / "agents" / "builder.toml"
    before = codex_copy.read_bytes()

    mind["proxy"].offered["codex"] = {"gpt-6"}
    path.write_text(path.read_text().replace("Build it.", "Build it well."))
    outcome = _check(mind)

    assert codex_copy.read_bytes() == before
    assert "Build it well." in (mind["homes"]["claude"] / "agents" / "builder.md").read_text()
    assert outcome.blocked == [{"item": "agent builder", "harness": "codex", "model": "gpt-5.6-terra"}]
    assert len(mind["sent"]) == 1
    assert "gpt-5.6-terra" in mind["sent"][0] and "codex" in mind["sent"][0]
    # T34a: the blocked codex copy does not stop the dsh delegate rendering.
    assert _dsh_rows(mind)["builder"]["config"]["persona"] == "Build it well.\n"

    # T34b: the same reason on the next pass is not told again.
    path.write_text(path.read_text().replace("Build it well.", "Build it very well."))
    _check(mind)
    assert len(mind["sent"]) == 1


# 34
def test_an_empty_proxy_listing_leaves_the_copy_and_says_nothing(mind):
    path = _write_reference_agent(mind, "builder", {
        "name": "builder", "description": "Builds.",
        "harness": {"codex": {"model": "gpt-5.6-terra"}},
    }, "Build it.\n")
    _check(mind)
    codex_copy = mind["homes"]["codex"] / "agents" / "builder.toml"
    before = codex_copy.read_bytes()

    mind["proxy"].offered["codex"] = set()
    path.write_text(path.read_text().replace("Build it.", "Build it well."))
    outcome = _check(mind)

    assert codex_copy.read_bytes() == before
    assert outcome.blocked == [] and mind["sent"] == []


# 35
def test_an_edited_copy_merges_its_body_and_own_fields_and_regenerates_the_others(mind):
    _write_reference_skill(mind["reference"] / "skills", "notes", {
        "name": "notes",
        "description": "Keep notes.",
        "harness": {
            "claude": {"user-invocable": True},
            "dsh": {"whenToUse": "When notes are asked for."},
        },
    }, "Old body.\n")
    _check(mind)

    claude_copy = mind["homes"]["claude"] / "skills" / "notes" / "SKILL.md"
    claude_copy.write_text(skill_reference.compose_frontmatter(
        {"name": "notes", "description": "Keep notes.", "user-invocable": False, "model": "sonnet"},
        "New body.\n",
    ))
    outcome = _check(mind)

    assert outcome.merged == ["skill notes (claude)"]
    ref = skill_reference.load_reference("skill", "notes")
    assert ref.body == "New body.\n"
    assert ref.harness["claude"] == {"user-invocable": False, "model": "sonnet"}
    assert ref.harness["dsh"] == {"whenToUse": "When notes are asked for."}
    dsh_fm, dsh_body = _frontmatter(mind["homes"]["dsh"] / "skills" / "notes" / "SKILL.md")
    assert dsh_body == "New body.\n"
    assert dsh_fm["whenToUse"] == "When notes are asked for."
    _, codex_body = _frontmatter(mind["homes"]["codex"] / "skills" / "notes" / "SKILL.md")
    assert codex_body == "New body.\n"
    for h in skill_reference.HARNESSES:
        assert skill_reference.copy_status("skill", "notes", h) == "rendered"


# 36
def test_a_write_back_against_a_reference_changed_since_render_is_a_conflict(mind):
    _write_reference_skill(mind["reference"] / "skills", "notes", {
        "name": "notes", "description": "Keep notes.",
    }, "Original.\n")
    _check(mind)

    reference_md = mind["reference"] / "skills" / "notes" / "SKILL.md"
    reference_md.write_text(reference_md.read_text().replace("Original.", "Changed in the reference."))
    codex_copy = mind["homes"]["codex"] / "skills" / "notes" / "SKILL.md"
    codex_copy.write_text(codex_copy.read_text().replace("Original.", "Changed under codex."))

    with pytest.raises(skill_reference.RenderConflict):
        skill_reference.merge_copy("skill", "notes", catalog=mind["proxy"], notify=mind["notify"])

    assert "Changed in the reference." in reference_md.read_text()
    assert "Changed under codex." in codex_copy.read_text()
    assert "Original." in (mind["homes"]["claude"] / "skills" / "notes" / "SKILL.md").read_text()
    assert len(mind["sent"]) == 1 and "codex" in mind["sent"][0]


# 37
@pytest.mark.parametrize("change, named", [
    ({"agents": ["ghost"]}, "ghost"),
    ({"body": "Use the Agent tool with subagent_type reviewer.\n"}, "Agent tool"),
])
def test_a_skill_naming_a_missing_agent_or_a_spawning_phrase_is_refused_and_no_copy_changes(
    mind, change, named,
):
    _write_reference_skill(mind["reference"] / "skills", "delegate", {
        "name": "delegate", "description": "Delegates.",
    }, "Hand it to the reviewer.\n")
    _check(mind)
    copies = {h: skill_reference.copy_path("skill", "delegate", h) for h in skill_reference.HARNESSES}
    before = {h: _snapshot(p) for h, p in copies.items()}

    frontmatter = {"name": "delegate", "description": "Delegates."}
    frontmatter.update({k: v for k, v in change.items() if k != "body"})
    _write_reference_skill(mind["reference"] / "skills", "delegate", frontmatter,
                           change.get("body", "Hand it to the reviewer.\n"))
    outcome = _check(mind)

    assert {h: _snapshot(p) for h, p in copies.items()} == before
    assert len(outcome.refused) == 1 and named in outcome.refused[0]["reason"]
    assert len(mind["sent"]) == 1 and named in mind["sent"][0]


# 35
def test_an_edited_dsh_delegate_row_merges_its_persona_into_the_body(mind):
    _write_reference_agent(mind, "builder", {
        "name": "builder",
        "description": "Builds.",
        "harness": {"codex": {"model": "gpt-5.6-terra"}},
    }, "Build it.\n")
    _check(mind)
    overlay = mind["homes"]["dsh"] / "agents.patch.yml"
    edited = yaml.safe_load(overlay.read_text())
    config = edited[0]["insert"][0]["config"]
    config["persona"] = "Build it in small steps.\n"
    config["agentOptions"] = {"model": "qwen3-coder"}
    overlay.write_text(yaml.safe_dump(edited))

    outcome = _check(mind)

    assert outcome.merged == ["agent builder (dsh)"]
    ref = skill_reference.load_reference("agent", "builder")
    assert ref.body == "Build it in small steps.\n"
    assert ref.harness == {"codex": {"model": "gpt-5.6-terra"}, "dsh": {"model": "qwen3-coder"}}
    assert "Build it in small steps." in (mind["homes"]["claude"] / "agents" / "builder.md").read_text()
    assert skill_reference.copy_status("agent", "builder", "dsh") == "rendered"


# 37
def test_a_body_dsh_would_read_as_a_template_leaves_the_dsh_delegate_and_notifies(mind):
    path = _write_reference_agent(mind, "builder", {"name": "builder", "description": "Builds."},
                                  "Build it.\n")
    _check(mind)
    overlay = mind["homes"]["dsh"] / "agents.patch.yml"
    before = overlay.read_bytes()

    path.write_text(path.read_text().replace("Build it.", "Build {{target}}."))
    outcome = _check(mind)

    assert overlay.read_bytes() == before
    assert "Build {{target}}." in (mind["homes"]["claude"] / "agents" / "builder.md").read_text()
    assert outcome.blocked[0]["harness"] == "dsh" and "{{" in outcome.blocked[0]["reason"]
    assert len(mind["sent"]) == 1 and "dsh" in mind["sent"][0]



def _render_skill(mind, name="notes", body="Body.\n", **siblings):
    _write_reference_skill(mind["reference"] / "skills", name,
                           {"name": name, "description": "d"}, body, **siblings)
    return _check(mind)


# 37
def test_a_copy_edited_to_name_a_spawning_tool_is_refused_and_not_merged(mind):
    _render_skill(mind)
    reference_before = (mind["reference"] / "skills" / "notes" / "SKILL.md").read_text()
    copy = mind["homes"]["claude"] / "skills" / "notes" / "SKILL.md"
    copy.write_text(copy.read_text().replace("Body.", "Delegate with subagent_fork."))

    outcome = _check(mind)

    assert (mind["reference"] / "skills" / "notes" / "SKILL.md").read_text() == reference_before
    assert "subagent_fork" in outcome.refused[0]["reason"]
    assert "Body." in (mind["homes"]["dsh"] / "skills" / "notes" / "SKILL.md").read_text()


# 37
def test_an_agent_named_inside_a_harness_block_must_have_a_reference(mind):
    _write_reference_skill(mind["reference"] / "skills", "notes", {
        "name": "notes", "description": "d", "harness": {"claude": {"agents": ["ghost"]}},
    }, "Body.\n")

    outcome = _check(mind)

    assert "ghost" in outcome.refused[0]["reason"]
    assert not (mind["homes"]["claude"] / "skills" / "notes").exists()


# S1
def test_an_undeclared_codex_or_dsh_home_is_never_rendered_into(mind, monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.delenv("DSH_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    outcome = _render_skill(mind)

    assert not (tmp_path / "home" / ".codex").exists()
    assert not (tmp_path / "home" / ".dsh").exists()
    assert (mind["homes"]["claude"] / "skills" / "notes").is_dir()
    reasons = {s["harness"]: s["reason"] for s in outcome.skipped}
    assert "no codex home declared" in reasons["codex"] and "dsh" in reasons
    with pytest.raises(skills_api.SkillHarnessUndeclared):
        skills_api.list_skills("codex")


# S2
def test_a_copy_that_was_never_rendered_is_a_conflict_not_an_overwrite(mind):
    tuned = mind["homes"]["codex"] / "skills" / "notes" / "SKILL.md"
    tuned.parent.mkdir(parents=True)
    tuned.write_text("---\nname: notes\ndescription: d\n---\nTuned by hand.\n")

    outcome = _render_skill(mind)

    assert tuned.read_text().endswith("Tuned by hand.\n")
    assert outcome.conflicts[0]["harnesses"] == ["codex"]
    assert "resolve notes" in mind["sent"][0]


# S2
def test_installing_for_one_harness_overwrites_only_that_harness(mind):
    repo = mind["project"] / "specs" / "skills"
    _write_reference_skill(repo, "notes", {"name": "notes", "description": "d"}, "Shipped.\n")
    for h in ("claude", "codex"):
        copy = mind["homes"][h] / "skills" / "notes" / "SKILL.md"
        copy.parent.mkdir(parents=True)
        copy.write_text(f"---\nname: notes\ndescription: d\n---\nTuned under {h}.\n")

    skills_api.install_skill("claude", "notes", catalog=mind["proxy"], notify=mind["notify"])

    assert (mind["homes"]["claude"] / "skills" / "notes" / "SKILL.md").read_text().endswith("Shipped.\n")
    assert (mind["homes"]["codex"] / "skills" / "notes" / "SKILL.md").read_text().endswith("Tuned under codex.\n")


# S3
def test_a_corrupt_render_record_refuses_the_pass_and_is_told_once(mind):
    _render_skill(mind)
    records = mind["reference"] / ".rendered.json"
    records.write_bytes(b"\xff\xfe not json")
    reference = mind["reference"] / "skills" / "notes" / "SKILL.md"
    reference.write_text(reference.read_text().replace("Body.", "Changed."))

    for _ in range(2):
        with pytest.raises(skill_reference.RecordsCorrupt):
            _check(mind)

    assert records.read_bytes() == b"\xff\xfe not json"
    assert "Body." in (mind["homes"]["claude"] / "skills" / "notes" / "SKILL.md").read_text()
    assert len(mind["sent"]) == 1


# S4
def test_a_reason_the_notifier_failed_to_send_is_sent_again(mind):
    failures: list[str] = []
    _write_reference_skill(mind["reference"] / "skills", "notes",
                           {"name": "notes", "description": "d", "agents": ["ghost"]}, "Body.\n")

    skill_reference.check(catalog=mind["proxy"], notify=lambda m: failures.append(m) or False)
    _check(mind)

    assert len(failures) == 1 and len(mind["sent"]) == 1


def test_the_production_notifier_runs_the_minds_own_venv_and_reports_failure(tmp_path, monkeypatch):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\necho \"$0\" > " + str(tmp_path / "ran") + "\nexit 3\n")
    venv_python.chmod(0o755)
    monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path))

    sent = skill_reference.telegram_notifier(tmp_path)("hello")

    assert sent is False
    assert (tmp_path / "ran").read_text().strip() == str(venv_python)


# S5
def test_identical_edits_in_two_copies_merge_without_a_conflict(mind):
    _render_skill(mind)
    for h in ("claude", "codex"):
        copy = mind["homes"][h] / "skills" / "notes" / "SKILL.md"
        copy.write_text(copy.read_text().replace("Body.", "Same edit."))

    outcome = _check(mind)

    assert outcome.conflicts == []
    assert skill_reference.load_reference("skill", "notes").body == "Same edit.\n"
    assert "Same edit." in (mind["homes"]["dsh"] / "skills" / "notes" / "SKILL.md").read_text()


# S5
def test_built_files_and_symlink_targets_do_not_make_a_copy_look_edited(mind, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("one\n")
    _write_reference_skill(mind["reference"] / "skills", "notes", {"name": "notes", "description": "d"},
                           "Body.\n")
    os.symlink(outside, mind["reference"] / "skills" / "notes" / "link.txt")
    _check(mind)
    copy = mind["homes"]["claude"] / "skills" / "notes"

    outside.write_text("two\n")
    (copy / "__pycache__").mkdir()
    (copy / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\0")
    assert skill_reference.copy_status("skill", "notes", "claude") == "rendered"

    (copy / "link.txt").unlink()
    os.symlink(tmp_path / "elsewhere.txt", copy / "link.txt")
    assert skill_reference.copy_status("skill", "notes", "claude") == "edited"


# S5
def test_resolve_makes_the_named_copy_the_reference_and_regenerates_the_rest(mind):
    _render_skill(mind)
    reference = mind["reference"] / "skills" / "notes" / "SKILL.md"
    reference.write_text(reference.read_text().replace("Body.", "Reference edit."))
    copy = mind["homes"]["codex"] / "skills" / "notes" / "SKILL.md"
    copy.write_text(copy.read_text().replace("Body.", "Codex edit."))
    assert _check(mind).conflicts

    skills_api.resolve_skill("codex", "notes", catalog=mind["proxy"], notify=mind["notify"])

    assert skill_reference.load_reference("skill", "notes").body == "Codex edit.\n"
    for h in skill_reference.HARNESSES:
        assert skill_reference.copy_status("skill", "notes", h) == "rendered"
        assert "Codex edit." in (mind["homes"][h] / "skills" / "notes" / "SKILL.md").read_text()


# S5
def test_fixing_only_a_blocked_copys_model_merges_it_though_the_reference_moved(mind):
    path = _write_reference_agent(mind, "builder", {
        "name": "builder", "description": "Builds.",
        "harness": {"codex": {"model": "gpt-5.6-terra"}},
    }, "Build it.\n")
    _check(mind)
    mind["proxy"].offered["codex"] = {"gpt-6"}
    path.write_text(path.read_text().replace("Build it.", "Build it well."))
    _check(mind)
    codex_copy = mind["homes"]["codex"] / "agents" / "builder.toml"
    codex_copy.write_text(codex_copy.read_text().replace("gpt-5.6-terra", "gpt-6"))

    outcome = _check(mind)

    assert outcome.conflicts == []
    assert skill_reference.load_reference("agent", "builder").harness["codex"]["model"] == "gpt-6"
    toml = tomllib.loads(codex_copy.read_text())
    assert toml["model"] == "gpt-6" and toml["developer_instructions"] == "Build it well.\n"


# S7
def test_a_copy_removed_from_a_harness_excludes_it_until_installed_again(mind):
    repo = mind["project"] / "specs" / "skills"
    _write_reference_skill(repo, "notes", {"name": "notes", "description": "d"}, "Body.\n")
    skills_api.install_skill("claude", "notes", catalog=mind["proxy"], notify=mind["notify"])
    codex_copy = mind["homes"]["codex"] / "skills" / "notes"
    import shutil
    shutil.rmtree(codex_copy)

    _check(mind)
    _check(mind)

    assert not codex_copy.exists()
    assert skill_reference.load_reference("skill", "notes").fields["excluded"] == ["codex"]

    skills_api.install_skill("codex", "notes", catalog=mind["proxy"], notify=mind["notify"])
    assert codex_copy.is_dir()
    assert "excluded" not in skill_reference.load_reference("skill", "notes").fields


# S8
def test_what_a_copy_built_never_enters_the_reference(mind):
    copy = mind["homes"]["claude"] / "skills" / "notes"
    (copy / "venv" / "lib").mkdir(parents=True)
    (copy / "venv" / "lib" / "big.so").write_bytes(b"x" * 100)
    (copy / "SKILL.md").write_text("---\nname: notes\ndescription: d\n---\nBody.\n")
    skill_reference.adopt("skill", "notes", "claude", catalog=mind["proxy"], notify=mind["notify"])

    assert not (mind["reference"] / "skills" / "notes" / "venv").exists()
    assert not (mind["homes"]["dsh"] / "skills" / "notes" / "venv").exists()


# S8 / S10
def test_an_oversized_edit_is_that_items_error_and_the_pass_goes_on(mind, monkeypatch):
    _render_skill(mind, "big")
    _render_skill(mind, "small")
    monkeypatch.setattr(skill_reference, "MAX_SKILL_BYTES", 64)
    (mind["homes"]["claude"] / "skills" / "big" / "blob").write_bytes(b"x" * 200)
    small = mind["reference"] / "skills" / "small" / "SKILL.md"
    small.write_text(small.read_text().replace("Body.", "Moved."))

    outcome = _check(mind)

    assert outcome.errors[0]["item"] == "skill big"
    assert "larger than a skill should be" in outcome.errors[0]["reason"]
    assert "Moved." in (mind["homes"]["dsh"] / "skills" / "small" / "SKILL.md").read_text()
    assert not (mind["reference"] / "skills" / "big" / "blob").exists()


# S8
def test_an_unchanged_pass_rewrites_nothing(mind):
    _render_skill(mind)
    copy = mind["homes"]["claude"] / "skills" / "notes"
    inode = copy.stat().st_ino

    assert _check(mind).rendered == []
    assert copy.stat().st_ino == inode


# S9
def test_a_relative_symlink_resolves_the_same_in_every_copy(mind):
    _write_reference_skill(mind["reference"] / "skills", "notes", {"name": "notes", "description": "d"},
                           "Body.\n", **{"run.sh": "echo run\n"})
    os.symlink("run.sh", mind["reference"] / "skills" / "notes" / "alias.sh")

    _check(mind)

    for h in skill_reference.HARNESSES:
        assert (mind["homes"][h] / "skills" / "notes" / "alias.sh").read_text() == "echo run\n"


# S9
@pytest.mark.parametrize("names, refused", [
    (["Notes_Tool"], "Notes_Tool"),
    (["code-review", "code_review"], "code_review"),
    (["bash"], "bash"),
])
def test_names_dsh_cannot_carry_are_refused_for_dsh_and_told(mind, names, refused):
    if names == ["Notes_Tool"]:
        _render_skill(mind, "Notes_Tool")
        assert not (mind["homes"]["dsh"] / "skills" / "Notes_Tool").exists()
        assert (mind["homes"]["claude"] / "skills" / "Notes_Tool").is_dir()
    else:
        for name in names:
            _write_reference_agent(mind, name, {"name": name, "description": "d"}, "Work.\n")
        outcome = _check(mind)
        assert all(b["harness"] == "dsh" for b in outcome.blocked)
        assert not (mind["homes"]["dsh"] / "agents.patch.yml").exists()
    assert mind["sent"] and all("dsh" in m for m in mind["sent"])
    assert any(refused in m for m in mind["sent"])


# S10
def test_one_unreadable_reference_does_not_stop_the_others(mind):
    broken = mind["reference"] / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: broken\nharness: [not, a, mapping]\n---\nx\n")

    outcome = _render_skill(mind)

    assert outcome.errors[0]["item"] == "skill broken"
    assert (mind["homes"]["dsh"] / "skills" / "notes").is_dir()
    records = json.loads((mind["reference"] / ".rendered.json").read_text())
    assert records["skill"]["notes"]["dsh"]["fingerprint"]


# S13
def test_a_refused_install_changes_nothing(mind):
    repo = mind["project"] / "specs" / "skills"
    _write_reference_skill(repo, "notes", {"name": "notes", "description": "d"}, "Body.\n")
    skills_api.install_skill("claude", "notes", catalog=mind["proxy"], notify=mind["notify"])
    watched = [mind["reference"], *[mind["homes"][h] for h in skill_reference.HARNESSES]]
    before = [_snapshot(p) for p in watched]

    _write_reference_skill(repo, "notes", {"name": "notes", "description": "d"},
                           "Use the Task tool.\n")
    with pytest.raises(skills_api.SkillRefused):
        skills_api.install_skill("claude", "notes", catalog=mind["proxy"], notify=mind["notify"])

    assert [_snapshot(p) for p in watched] == before


# S11
def test_a_codex_agent_field_added_in_place_round_trips_into_the_reference(mind):
    _write_reference_agent(mind, "builder", {"name": "builder", "description": "Builds."}, "Build.\n")
    _check(mind)
    toml = mind["homes"]["codex"] / "agents" / "builder.toml"
    toml.write_text('sandbox_mode = "read-only"\n' + toml.read_text())

    _check(mind)

    assert skill_reference.load_reference("agent", "builder").harness["codex"] == {
        "sandbox_mode": "read-only"
    }


# Q10 / S9
def test_a_copy_is_staged_beside_its_target(mind, monkeypatch):
    _render_skill(mind)
    reference = mind["reference"] / "skills" / "notes" / "SKILL.md"
    reference.write_text(reference.read_text().replace("Body.", "Moved."))
    renames = []
    real_rename = os.rename

    def spy(src, dst):
        renames.append((os.fspath(src), os.fspath(dst)))
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", spy)
    _check(mind)

    into_targets = [(s, d) for s, d in renames if d.endswith("/skills/notes")]
    assert into_targets
    assert all(os.path.dirname(s) == os.path.dirname(d) for s, d in into_targets)


# Q10 / S9
def test_a_failed_swap_puts_the_old_copy_back(mind, monkeypatch):
    _render_skill(mind)
    reference = mind["reference"] / "skills" / "notes" / "SKILL.md"
    reference.write_text(reference.read_text().replace("Body.", "Moved."))
    target = mind["homes"]["claude"] / "skills" / "notes"
    real_rename = os.rename

    def failing(src, dst):
        if os.fspath(dst) == os.fspath(target) and ".incoming." in os.fspath(src):
            raise OSError("disk full")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", failing)
    _check(mind)

    assert (target / "SKILL.md").read_text().endswith("Body.\n")


# Q10 / S10
def test_an_item_finished_before_the_pass_dies_stays_recorded(mind):
    _write_reference_skill(mind["reference"] / "skills", "a-first", {"name": "a-first", "description": "d"}, "A.\n")
    _write_reference_skill(mind["reference"] / "skills", "b-second",
                           {"name": "b-second", "description": "d", "agents": ["ghost"]}, "B.\n")

    def killed(_message):
        raise SystemExit("mind stopped mid-pass")

    with pytest.raises(SystemExit):
        skill_reference.check(catalog=mind["proxy"], notify=killed)

    records = json.loads((mind["reference"] / ".rendered.json").read_text())
    assert records["skill"]["a-first"]["claude"]["fingerprint"]


# Q11 / S8
def test_an_unchanged_pass_stages_nothing(mind, monkeypatch):
    _render_skill(mind)
    import shutil
    copies = []
    real_copytree = shutil.copytree
    monkeypatch.setattr(shutil, "copytree", lambda *a, **k: copies.append(a) or real_copytree(*a, **k))

    _check(mind)

    assert copies == []


# Q11 / S3
def test_a_record_that_is_json_but_not_an_object_refuses_the_pass(mind):
    _render_skill(mind)
    (mind["reference"] / ".rendered.json").write_text("[]")

    with pytest.raises(skill_reference.RecordsCorrupt):
        _check(mind)


# Q11 / S7
def test_an_excluded_harness_reports_excluded_and_stays_empty(mind):
    _render_skill(mind)
    import shutil
    shutil.rmtree(mind["homes"]["dsh"] / "skills" / "notes")
    reference = mind["reference"] / "skills" / "notes" / "SKILL.md"
    reference.write_text(reference.read_text().replace("Body.", "Moved."))

    _check(mind)
    _check(mind)

    assert skill_reference.copy_status("skill", "notes", "dsh") == "excluded"
    assert not (mind["homes"]["dsh"] / "skills" / "notes").exists()
    assert "Moved." in (mind["homes"]["codex"] / "skills" / "notes" / "SKILL.md").read_text()


# Q11 / S9
def test_an_agent_whose_name_makes_no_dsh_tool_name_is_refused_for_dsh(mind):
    _write_reference_agent(mind, "9lives", {"name": "9lives", "description": "d"}, "Work.\n")

    outcome = _check(mind)

    assert outcome.blocked[0]["harness"] == "dsh" and "9lives" in outcome.blocked[0]["reason"]
    assert not (mind["homes"]["dsh"] / "agents.patch.yml").exists()
    assert (mind["homes"]["claude"] / "agents" / "9lives.md").exists()
