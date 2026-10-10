"""One reference copy per skill and agent, rendered into every harness.

Nothing here monkeypatches where a harness reads from. Which directory a
harness loads is the whole question, so the fixture moves `PROJECT_DIR` and
the three config homes and lets the real code resolve the paths. The proxy
listing and the notifier are the transports, so those two are stubbed: the
listing as a dict of what each harness is offered, the notifier as the list
of messages that went out.
"""

from __future__ import annotations

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
def test_a_reference_skill_renders_each_harness_only_the_frontmatter_it_reads_and_syncs_as_same(mind):
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
    assert set(codex_fm) == {"name", "description", "argument-hint"}
    assert set(dsh_fm) == {"name", "description", "whenToUse"}
    assert claude_body == codex_body == dsh_body == "# notes\n\nWrite them down.\n"
    for h in skill_reference.HARNESSES:
        assert (mind["homes"][h] / "skills" / "notes" / "helper.sh").read_text() == "echo hi\n"
        rows = {row.name: row for row in skills_api.list_skills(h)}
        assert rows["notes"].state == skills_api.STATE_SAME


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
            "codex": {"model_reasoning_effort": "high"},
            "dsh": {"model": "qwen3-coder"},
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
        "developer_instructions": body,
    }

    row = _dsh_rows(mind)["code_reviewer"]
    assert row["name"] == "@deepseek-ai/dsh-tool-subagent"
    assert row["config"]["persona"] == body
    assert row["config"]["agentOptions"] == {"model": "qwen3-coder"}
    assert not (mind["homes"]["dsh"] / ".agent-presets").exists()


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
