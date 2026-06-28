"""Step 3 — proposer DB reader + sequence clustering.

Builds a faithful fixture training_turns.db via ``core.training_capture`` (the
authoritative schema) in tmp_path — NEVER reads the live data/training_turns.db.
Asserts the reader extracts ordered tool-name tuples per mind, and clustering
counts/filters identical sequences deterministically.
"""

import importlib.util
import sys
from pathlib import Path

from core import training_capture as tc

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_proposer/skill_proposer.py")


def _load():
    name = "skill_proposer_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the @dataclass decorator can resolve the module.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _tool_blocks(*names):
    return [{"type": "tool_use", "name": n, "input": {}} for n in names]


def _seed_turn(db_path, *, session_id, turn_index, mind_id, tools,
               harness=tc.HARNESS_CLAUDE_CODE, captured_at=None):
    turn = tc.TrainingTurn.from_blocks(
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
        mind_id=mind_id,
        user_content="hi",
        assistant_blocks=_tool_blocks(*tools),
        captured_at=captured_at,
    )
    tc.upsert_turns(str(db_path), [turn])


def test_read_recent_sequences_extracts_tool_names(tmp_path):
    mod = _load()
    db = tmp_path / "training_turns.db"
    _seed_turn(db, session_id="s1", turn_index=0, mind_id="ada",
               tools=["Read", "Grep", "Edit"], captured_at=100)
    _seed_turn(db, session_id="s1", turn_index=1, mind_id="ada",
               tools=["Bash"], captured_at=101)
    seqs = mod.read_recent_sequences(str(db), "ada", lookback_turns=500)
    assert ("Read", "Grep", "Edit") in seqs
    assert ("Bash",) in seqs


def test_read_filters_by_mind_id(tmp_path):
    mod = _load()
    db = tmp_path / "training_turns.db"
    _seed_turn(db, session_id="s1", turn_index=0, mind_id="ada",
               tools=["Read", "Grep"], captured_at=100)
    _seed_turn(db, session_id="s2", turn_index=0, mind_id="nagatha",
               tools=["Bash", "Bash"], captured_at=101)
    ada_seqs = mod.read_recent_sequences(str(db), "ada", lookback_turns=500)
    assert ada_seqs == [("Read", "Grep")]
    nag_seqs = mod.read_recent_sequences(str(db), "nagatha", lookback_turns=500)
    assert nag_seqs == [("Bash", "Bash")]


def test_read_respects_lookback_limit(tmp_path):
    mod = _load()
    db = tmp_path / "training_turns.db"
    for i in range(5):
        _seed_turn(db, session_id="s1", turn_index=i, mind_id="ada",
                   tools=[f"T{i}", "X"], captured_at=100 + i)
    seqs = mod.read_recent_sequences(str(db), "ada", lookback_turns=2)
    assert len(seqs) == 2
    # Most-recent first: captured_at 104 then 103.
    assert seqs[0] == ("T4", "X")
    assert seqs[1] == ("T3", "X")


def test_cluster_groups_identical_sequences(tmp_path):
    mod = _load()
    seqs = [("Read", "Grep", "Edit")] * 3 + [("Bash",)]
    clusters = mod.cluster_sequences(seqs, min_frequency=3, min_sequence_length=2)
    assert len(clusters) == 1
    assert clusters[0].signature == ("Read", "Grep", "Edit")
    assert clusters[0].count == 3


def test_cluster_drops_below_frequency_threshold(tmp_path):
    mod = _load()
    seqs = [("Read", "Grep"), ("Read", "Grep")]
    clusters = mod.cluster_sequences(seqs, min_frequency=3, min_sequence_length=2)
    assert clusters == []


def test_cluster_ignores_short_sequences(tmp_path):
    mod = _load()
    seqs = [("Bash",)] * 5
    clusters = mod.cluster_sequences(seqs, min_frequency=3, min_sequence_length=2)
    assert clusters == []


def test_proposed_name_is_valid_kebab(tmp_path):
    mod = _load()
    seqs = [("Read", "Grep", "Edit")] * 3
    clusters = mod.cluster_sequences(seqs, min_frequency=3, min_sequence_length=2)
    name = clusters[0].proposed_name
    import re
    assert re.match(r"^[a-z0-9][a-z0-9._-]*$", name), name
    assert len(name) <= 64
