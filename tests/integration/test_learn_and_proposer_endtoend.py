"""Phase 4.3 acceptance — end-to-end /learn + autonomous proposer.

Tmp_path config dirs for both harnesses, fixture training_turns.db built via
core.training_capture (NEVER the live data/training_turns.db). Proves: /learn
produces a valid dialect SKILL.md per harness; the proposer clusters one repeated
sequence and skips an already-covered one; an auto-created skill is agent-stamped,
immediately Curator-eligible, and archived by the Curator once unused; two minds
stay independent.
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import training_capture as tc

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROPOSER_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_proposer/skill_proposer.py")
MANAGE_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_manage/skill_manage.py")
VALIDATOR_PATH = str(_PROJECT_ROOT / "tools/stateless/learn_skill/learn_validator.py")
TELEMETRY_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")
CURATOR_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_curator/skill_curator.py")

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _proposer():
    return _load(PROPOSER_PATH, "skill_proposer_e2e")


def _manage():
    return _load(MANAGE_PATH, "skill_manage_e2e")


def _validator():
    return _load(VALIDATOR_PATH, "learn_validator_e2e")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_e2e_p4")


def _curator():
    return _load(CURATOR_PATH, "skill_curator_e2e_p4")


# A fixture SKILL.md of the kind `learn-skill` would author.
_AUTHORED = """\
---
name: planka-card-create
description: Create a Planka card from a title and list ID.
---

# Planka Card Create

Create a card on a Planka board. Does not move or archive cards. Stdlib only.

## When to Use

- "make a planka card for X"
- "add a card to the board"

## Procedure

1. Resolve the target list ID.
2. POST the card title to the Planka API.
3. Confirm the returned card ID.

## Verification

Fetch the card by ID and confirm the title matches.
"""


def _seed_seq(db, *, session_id, turn_index, mind_id, tools, harness, captured_at):
    turn = tc.TrainingTurn.from_blocks(
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
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


import pytest


@pytest.mark.parametrize(
    "harness,config_rel",
    [("claude_cli", "ada/.claude"), ("codex_cli", "nagatha/.codex")],
)
def test_learn_produces_valid_skill_md_per_harness(tmp_path, harness, config_rel):
    val, mng, tel = _validator(), _manage(), _telemetry()
    cfg = tmp_path / config_rel

    # /learn validates the authored content first.
    result = val.validate_skill_md(_AUTHORED, harness=harness)
    assert result["valid"] is True, result

    # Then saves via skill_manage create.
    import json
    out = json.loads(mng.skill_manage(
        "create", cfg, harness, name="planka-card-create", content=_AUTHORED
    ))
    assert out["success"] is True, out

    skill_md = cfg / "skills" / "planka-card-create" / "SKILL.md"
    assert skill_md.is_file()
    assert not (cfg / "skills" / "planka-card-create").is_symlink()

    written = skill_md.read_text(encoding="utf-8")
    fields, _body = mng.split_frontmatter(written)
    assert fields["name"] == "planka-card-create"
    if harness == "claude_cli":
        # Full Claude dialect carries user-invocable + provenance metadata.
        assert fields.get("user-invocable") is False
        assert fields.get("metadata", {}).get("provenance") == "agent"
    else:
        # Minimal Codex dialect: only the portable keep-set, no extras.
        assert "user-invocable" not in fields
        assert "metadata" not in fields

    # Sidecar provenance is agent.
    assert tel.get_record(cfg, "planka-card-create")["created_by"] == "agent"


def test_proposer_clusters_one_and_skips_covered(tmp_path):
    prop = _proposer()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg, max_proposals_per_run=5)
    db = tmp_path / "training_turns.db"

    # One repeated sequence to propose.
    for i in range(3):
        _seed_seq(db, session_id="new", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], harness=tc.HARNESS_CLAUDE_CODE,
                  captured_at=100 + i)
    # A second repeated sequence already covered by a pre-created skill.
    for i in range(3):
        _seed_seq(db, session_id="cov", turn_index=i, mind_id="ada",
                  tools=["Bash", "Write"], harness=tc.HARNESS_CLAUDE_CODE,
                  captured_at=200 + i)
    covered_name = prop._derive_proposed_name(("Bash", "Write"))
    pre = cfg / "skills" / covered_name
    pre.mkdir(parents=True, exist_ok=True)
    (pre / "SKILL.md").write_text(
        "---\nname: x\ndescription: y.\n---\n# x\n", encoding="utf-8"
    )

    result = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    created = [p["name"] for p in result["proposed"] if p["created"]]
    assert len(created) == 1
    new_name = prop._derive_proposed_name(("Read", "Grep", "Edit"))
    assert created == [new_name]
    assert covered_name not in created


def test_auto_created_skill_curation_eligible_then_archives(tmp_path):
    prop, tel, cur = _proposer(), _telemetry(), _curator()
    cfg = tmp_path / "ada" / ".claude"
    _enable(cfg)
    db = tmp_path / "training_turns.db"
    for i in range(3):
        _seed_seq(db, session_id="s", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], harness=tc.HARNESS_CLAUDE_CODE,
                  captured_at=100 + i)
    result = prop.run(cfg, "claude_cli", db_path=str(db), mind_id="ada")
    name = [p["name"] for p in result["proposed"] if p["created"]][0]

    # Immediately curation-eligible.
    record = tel.get_record(cfg, name)
    assert cur.is_curation_eligible(cfg, name, record) is True

    # Seed it 91 days stale, then run the curator → archived.
    from datetime import timedelta
    tel._mutate(
        cfg, name,
        lambda rec: rec.__setitem__("last_used_at", (NOW - timedelta(days=91)).isoformat()),
    )
    tel._mutate(
        cfg, name,
        lambda rec: rec.__setitem__("created_at", (NOW - timedelta(days=120)).isoformat()),
    )
    summary = cur.run(cfg, "claude_cli", now=NOW)
    assert summary["counts"]["archived"] == 1
    assert tel.get_record(cfg, name)["state"] == "archived"
    assert (cfg / "skills" / ".archive" / name / "SKILL.md").is_file()


def test_two_mind_independence(tmp_path):
    prop = _proposer()
    ada = tmp_path / "ada" / ".claude"
    nag = tmp_path / "nagatha" / ".codex"
    _enable(ada)
    _enable(nag)
    db = tmp_path / "training_turns.db"

    for i in range(3):
        _seed_seq(db, session_id="a", turn_index=i, mind_id="ada",
                  tools=["Read", "Grep", "Edit"], harness=tc.HARNESS_CLAUDE_CODE,
                  captured_at=100 + i)
    for i in range(3):
        _seed_seq(db, session_id="n", turn_index=i, mind_id="nagatha",
                  tools=["Bash", "Write"], harness=tc.HARNESS_CODEX,
                  captured_at=200 + i)

    ada_result = prop.run(ada, "claude_cli", db_path=str(db), mind_id="ada")
    nag_result = prop.run(nag, "codex_cli", db_path=str(db), mind_id="nagatha")

    ada_name = [p["name"] for p in ada_result["proposed"] if p["created"]][0]
    nag_name = [p["name"] for p in nag_result["proposed"] if p["created"]][0]

    # Each created exactly one in its own tree, nothing in the other's.
    assert (ada / "skills" / ada_name / "SKILL.md").is_file()
    assert (nag / "skills" / nag_name / "SKILL.md").is_file()
    assert not (nag / "skills" / ada_name).exists()
    assert not (ada / "skills" / nag_name).exists()
