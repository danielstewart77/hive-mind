"""Step 4 — proposer coverage check, draft template, create, enable-flag, run.

Fixture training_turns.db built in tmp_path via core.training_capture (never the
live DB). Config dirs are tmp_path .claude/.codex trees. Asserts disabled-by-
default, single create on a repeated sequence, covered-skip, cap, agent
provenance + curation eligibility, and template validity.
"""

import importlib.util
import sys
from pathlib import Path

from core import training_capture as tc

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_proposer/skill_proposer.py")
TELEMETRY_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")
CURATOR_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_curator/skill_curator.py")
VALIDATOR_PATH = str(_PROJECT_ROOT / "tools/stateless/learn_skill/learn_validator.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _proposer():
    return _load(SCRIPT_PATH, "skill_proposer_run_under_test")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_prun")


def _curator():
    return _load(CURATOR_PATH, "skill_curator_prun")


def _validator():
    return _load(VALIDATOR_PATH, "learn_validator_prun")


def _seed_seq(db, *, session_id, turn_index, mind_id, tools, captured_at):
    turn = tc.TrainingTurn.from_blocks(
        session_id=session_id,
        turn_index=turn_index,
        harness=tc.HARNESS_CLAUDE_CODE,
        mind_id=mind_id,
        user_content="x",
        assistant_blocks=[{"type": "tool_use", "name": n, "input": {}} for n in tools],
        captured_at=captured_at,
    )
    tc.upsert_turns(str(db), [turn])


def _enable(config_dir: Path, **overrides):
    skills = config_dir / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    lines = ["enabled: true"]
    for k, v in overrides.items():
        lines.append(f"{k}: {v}")
    (skills / "proposer.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_disabled_by_default_proposes_nothing(tmp_path):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    db = tmp_path / "training_turns.db"
    for i in range(3):
        _seed_seq(db, session_id="s", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], captured_at=100 + i)
    result = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    assert result == {"enabled": False, "proposed": []}
    assert not (cfg / "skills").exists() or list(
        p for p in (cfg / "skills").iterdir() if p.is_dir() and not p.name.startswith(".")
    ) == []


def test_enabled_creates_one_skill_for_repeated_sequence(tmp_path):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg)
    db = tmp_path / "training_turns.db"
    for i in range(3):
        _seed_seq(db, session_id="s", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], captured_at=100 + i)
    result = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    assert result["enabled"] is True
    created = [p for p in result["proposed"] if p["created"]]
    assert len(created) == 1
    name = created[0]["name"]
    assert (cfg / "skills" / name / "SKILL.md").is_file()
    assert not (cfg / "skills" / name).is_symlink()


def test_skips_sequence_covered_by_existing_skill(tmp_path):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg)
    db = tmp_path / "training_turns.db"
    for i in range(3):
        _seed_seq(db, session_id="s", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], captured_at=100 + i)
    # Pre-create a skill whose name matches the cluster signature.
    sig = ("Read", "Grep", "Edit")
    covered_name = prop._derive_proposed_name(sig)
    pre = cfg / "skills" / covered_name
    pre.mkdir(parents=True, exist_ok=True)
    (pre / "SKILL.md").write_text("---\nname: x\ndescription: y.\n---\n# x\n", encoding="utf-8")

    result = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    assert covered_name not in [p["name"] for p in result["proposed"] if p["created"]]


def test_max_proposals_per_run_caps_creates(tmp_path):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg, max_proposals_per_run=1)
    db = tmp_path / "training_turns.db"
    # Two distinct over-threshold clusters.
    for i in range(3):
        _seed_seq(db, session_id="a", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep"], captured_at=100 + i)
    for i in range(3):
        _seed_seq(db, session_id="b", turn_index=i, mind_id="ada",
                  tools=["Bash", "Edit"], captured_at=200 + i)
    result = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    created = [p for p in result["proposed"] if p["created"]]
    assert len(created) == 1


def test_created_skill_is_agent_provenance_and_curation_eligible(tmp_path):
    prop, tel, cur = _proposer(), _telemetry(), _curator()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg)
    db = tmp_path / "training_turns.db"
    for i in range(3):
        _seed_seq(db, session_id="s", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], captured_at=100 + i)
    result = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    name = [p["name"] for p in result["proposed"] if p["created"]][0]

    record = tel.get_record(cfg, name)
    assert record["created_by"] == "agent"
    assert cur.is_curation_eligible(cfg, name, record) is True


def test_drafted_skill_md_passes_validator(tmp_path):
    prop, val = _proposer(), _validator()
    cluster = prop.SequenceCluster(
        signature=("Read", "Grep", "Edit"),
        count=4,
        example_session_id="s",
        proposed_name=prop._derive_proposed_name(("Read", "Grep", "Edit")),
    )
    for harness in ("claude_cli", "codex_cli"):
        content = prop.draft_skill_md(cluster, harness=harness)
        result = val.validate_skill_md(content, harness=harness)
        assert result["valid"] is True, (harness, result)


def test_load_proposer_config_defaults_and_override(tmp_path):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    # Missing file → defaults.
    conf = prop.load_proposer_config(cfg)
    assert conf["enabled"] is False
    assert conf["min_frequency"] == 3
    assert conf["max_proposals_per_run"] == 1

    skills = cfg / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "proposer.yaml").write_text(
        "enabled: true\nmin_frequency: 5\n", encoding="utf-8"
    )
    conf2 = prop.load_proposer_config(cfg)
    assert conf2["enabled"] is True
    assert conf2["min_frequency"] == 5
    # Unspecified fields fall back to defaults.
    assert conf2["min_sequence_length"] == 2
    assert conf2["lookback_turns"] == 500
