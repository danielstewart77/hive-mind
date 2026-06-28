"""Phase 5 — richer-signal clustering for harness-native minds.

Covers the five improvements over bare-name clustering:
  1. semantic action-token extraction (name + input), incl. http method/path
     granularity and inline-env resolution / unresolvable-$VAR drop;
  2. run-collapse of consecutive identical tokens;
  3. order-canonical cluster key (reversed-order turns merge);
  4. parameter extraction into a templated ``## Procedure`` body;
  5. success gating (errored turns excluded from cluster counts).

Fixture training_turns.db built via core.training_capture (never the live DB).
"""

import importlib.util
import sys
from pathlib import Path

from core import training_capture as tc

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_proposer/skill_proposer.py")


def _load():
    name = "skill_proposer_richer_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_blocks(db, *, session_id, turn_index, mind_id, blocks, captured_at):
    turn = tc.TrainingTurn.from_blocks(
        session_id=session_id,
        turn_index=turn_index,
        harness=tc.HARNESS_CLAUDE_CODE,
        mind_id=mind_id,
        user_content="x",
        assistant_blocks=blocks,
        captured_at=captured_at,
    )
    tc.upsert_turns(str(db), [turn])


def _bash(cmd):
    return {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}


# ---------------------------------------------------------------------------
# 1 — semantic action-token extraction
# ---------------------------------------------------------------------------

def test_token_stateless_tool_script():
    mod = _load()
    blk = _bash("python3 tools/stateless/reminders/reminders.py due")
    assert mod._extract_action_token(blk) == "tool:reminders"


def test_token_curl_method_and_two_path_segments():
    mod = _load()
    get = _bash("curl -s http://127.0.0.1:8425/memory/retrieve")
    post = _bash("curl -s -X POST http://127.0.0.1:8425/memory/store -d '{}'")
    post_d = _bash("curl -s http://127.0.0.1:8425/memory/store --data '{}'")
    assert mod._extract_action_token(get) == "http:GET:memory/retrieve"
    assert mod._extract_action_token(post) == "http:POST:memory/store"
    assert mod._extract_action_token(post_d) == "http:POST:memory/store"
    # Distinct lucent ops do NOT collapse to one token.
    assert mod._extract_action_token(get) != mod._extract_action_token(post)


def test_token_read_skill_md_and_extension():
    mod = _load()
    sk = {"type": "tool_use", "name": "Read",
          "input": {"file_path": "/x/.claude/skills/planka/SKILL.md"}}
    py = {"type": "tool_use", "name": "Edit",
          "input": {"file_path": "/x/tools/foo.py"}}
    assert mod._extract_action_token(sk) == "skill:planka"
    assert mod._extract_action_token(py) == "edit:py"


def test_token_mcp_kept_verbatim_and_skill_agent():
    mod = _load()
    mcp = {"type": "tool_use", "name": "mcp__hive-mind-tools__graph_query", "input": {}}
    skill = {"type": "tool_use", "name": "Skill", "input": {"skill": "hivemind:planka"}}
    agent = {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "Explore"}}
    assert mod._extract_action_token(mcp) == "mcp__hive-mind-tools__graph_query"
    assert mod._extract_action_token(skill) == "skill:hivemind:planka"
    assert mod._extract_action_token(agent) == "agent:Explore"


def test_token_inline_env_resolved():
    mod = _load()
    blk = _bash("TOOL=reminders python3 $TOOL.py due")
    assert mod._extract_action_token(blk) == "tool:reminders"


def test_token_unresolvable_var_dropped():
    mod = _load()
    blk = _bash("$TOOL due")  # exe itself is an unresolved variable
    assert mod._extract_action_token(blk) is None


# ---------------------------------------------------------------------------
# 2 — run-collapse
# ---------------------------------------------------------------------------

def test_run_collapse_folds_consecutive_duplicates():
    mod = _load()
    assert mod._collapse_runs(("sh:grep",) * 10) == ("sh:grep",)
    assert mod._collapse_runs(("a", "a", "b", "b", "a")) == ("a", "b", "a")


# ---------------------------------------------------------------------------
# 3 — order-canonical cluster key
# ---------------------------------------------------------------------------

def test_reversed_order_turns_cluster_together():
    mod = _load()
    seqs = (
        [("sh:docker", "read:txt")] * 2
        + [("read:txt", "sh:docker")] * 2
    )
    clusters = mod.cluster_sequences(seqs, min_frequency=3, min_sequence_length=2)
    assert len(clusters) == 1
    assert clusters[0].count == 4


# ---------------------------------------------------------------------------
# 4 — parameter extraction
# ---------------------------------------------------------------------------

def test_template_from_details_varies_to_placeholder():
    mod = _load()
    tmpl = mod._template_from_details(
        ["reminders.py due", "reminders.py add", "reminders.py list"]
    )
    assert tmpl == "reminders.py {{arg}}"


def test_template_from_details_identical_is_literal():
    mod = _load()
    assert mod._template_from_details(["docker ps", "docker ps"]) == "docker ps"


def test_extract_procedure_emits_templated_body():
    mod = _load()
    members = [
        mod.TurnRecord(("tool:reminders",), ("reminders.py due",), True),
        mod.TurnRecord(("tool:reminders",), ("reminders.py add",), True),
        mod.TurnRecord(("tool:reminders",), ("reminders.py list",), True),
    ]
    lines = mod._extract_procedure(("tool:reminders",), members)
    assert lines == ("`reminders.py {{arg}}`",)


# ---------------------------------------------------------------------------
# 5 — success gating
# ---------------------------------------------------------------------------

def test_turn_succeeded_reads_terminal_error():
    mod = _load()
    ok = [_bash("ls"), {"type": "tool_result", "is_error": False}]
    bad = [_bash("ls"), {"type": "tool_result", "is_error": True}]
    legacy = [_bash("ls")]  # no tool_result → treated as success
    assert mod._turn_succeeded(ok) is True
    assert mod._turn_succeeded(bad) is False
    assert mod._turn_succeeded(legacy) is True


def test_failed_turns_excluded_from_records(tmp_path):
    mod = _load()
    db = tmp_path / "training_turns.db"
    ok_blocks = [_bash("docker ps"), {"type": "tool_result", "is_error": False}]
    bad_blocks = [_bash("docker ps"), {"type": "tool_result", "is_error": True}]
    _seed_blocks(db, session_id="s", turn_index=0, mind_id="skip",
                 blocks=ok_blocks, captured_at=100)
    _seed_blocks(db, session_id="s", turn_index=1, mind_id="skip",
                 blocks=bad_blocks, captured_at=101)
    records = mod.read_recent_turn_records(str(db), "skip", lookback_turns=500)
    succeeded = [r for r in records if r.succeeded]
    assert len(records) == 2
    assert len(succeeded) == 1


# ---------------------------------------------------------------------------
# headline — a real mixed-tool workflow clusters into one named proposal,
# where the bare-name version produced only ``auto-bash``.
# ---------------------------------------------------------------------------

def test_mixed_tool_workflow_clusters_into_named_proposal(tmp_path):
    mod = _load()
    db = tmp_path / "training_turns.db"
    for i in range(3):
        blocks = [
            _bash("curl -s http://127.0.0.1:8425/memory/retrieve"),
            _bash("python3 tools/stateless/notify/notify.py send --message hi"),
        ]
        _seed_blocks(db, session_id="s", turn_index=i, mind_id="skip",
                     blocks=blocks, captured_at=100 + i)
    seqs = mod.read_recent_sequences(str(db), "skip", lookback_turns=500)
    clusters = mod.cluster_sequences(seqs, min_frequency=3, min_sequence_length=2)
    assert len(clusters) == 1
    name = clusters[0].proposed_name
    assert name != "auto-bash"
    assert "memory" in name and "notify" in name
