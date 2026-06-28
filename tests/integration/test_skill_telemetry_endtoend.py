"""End-to-end acceptance test for the skill-telemetry pipeline (backlog 1.3).

Drives the full detect -> seed -> bump path against fixture transcripts in a
tmp ``config_dir``, then reads ``<config_dir>/skills/.usage.json`` off disk and
asserts ``use_count`` incremented — the backlog's done-when signal — without a
live container. The Claude and Codex flows are exercised against independent
config dirs to prove the copy-don't-share guarantee (no cross-mind bleed).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from core.skill_telemetry_detect import detect_claude_skills, detect_codex_skills

_ROOT = Path(__file__).resolve().parents[2]
_SIDECAR = _ROOT / "tools" / "stateless" / "skill_telemetry" / "skill_telemetry.py"


def _load_sidecar():
    spec = importlib.util.spec_from_file_location("skill_telemetry_e2e", str(_SIDECAR))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_skill_dir(config_dir: Path, name: str) -> None:
    skill_dir = config_dir / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\n")


def _read_usage(config_dir: Path) -> dict:
    return json.loads((config_dir / "skills" / ".usage.json").read_text())


# ---------------------------------------------------------------------------
# Claude transcript fixture (mirror test_training_capture_claude.py shapes)
# ---------------------------------------------------------------------------

def _write_claude_sitrep(tmp_path: Path) -> Path:
    lines = [
        json.dumps({
            "type": "user",
            "isSidechain": False,
            "message": {"role": "user", "content": "/sitrep"},
        }),
        json.dumps({
            "type": "assistant",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Skill",
                     "input": {"skill": "sitrep"}},
                ],
            },
        }),
    ]
    p = tmp_path / "claude-session.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def _write_codex_git(tmp_path: Path) -> Path:
    def _item(payload):
        return json.dumps({"type": "response_item", "payload": payload})

    lines = [
        _item({"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": "commit the work"}]}),
        _item({"type": "function_call", "name": "exec_command",
               "arguments": json.dumps(
                   {"command": ["cat", "/usr/src/app/.codex/skills/git/SKILL.md"]}),
               "call_id": "c1"}),
    ]
    p = tmp_path / "codex-rollout.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------

def test_claude_turn_increments_use_count_in_sidecar(tmp_path):
    sidecar = _load_sidecar()
    cfg = tmp_path / "ada" / ".claude"
    _make_skill_dir(cfg, "sitrep")
    transcript = _write_claude_sitrep(tmp_path)

    sidecar.seed_existing_skills(cfg)
    bumped = sidecar.bump_skills(cfg, detect_claude_skills(transcript))

    assert bumped == ["sitrep"]
    usage = _read_usage(cfg)
    assert usage["sitrep"]["use_count"] == 1


def test_codex_turn_increments_use_count_in_sidecar(tmp_path):
    sidecar = _load_sidecar()
    cfg = tmp_path / "nagatha" / ".codex"
    _make_skill_dir(cfg, "git")
    transcript = _write_codex_git(tmp_path)

    sidecar.seed_existing_skills(cfg)
    bumped = sidecar.bump_skills(cfg, detect_codex_skills(transcript))

    assert bumped == ["git"]
    usage = _read_usage(cfg)
    assert usage["git"]["use_count"] == 1


def test_two_minds_have_independent_sidecars(tmp_path):
    sidecar = _load_sidecar()
    cfg_ada = tmp_path / "ada" / ".claude"
    cfg_nag = tmp_path / "nagatha" / ".codex"
    _make_skill_dir(cfg_ada, "sitrep")
    _make_skill_dir(cfg_nag, "git")

    claude_t = _write_claude_sitrep(tmp_path)
    codex_t = _write_codex_git(tmp_path)

    sidecar.seed_existing_skills(cfg_ada)
    sidecar.bump_skills(cfg_ada, detect_claude_skills(claude_t))
    sidecar.seed_existing_skills(cfg_nag)
    sidecar.bump_skills(cfg_nag, detect_codex_skills(codex_t))

    ada_usage = _read_usage(cfg_ada)
    nag_usage = _read_usage(cfg_nag)

    # Each sidecar carries only its own mind's bump — no cross-mind bleed.
    assert ada_usage["sitrep"]["use_count"] == 1
    assert "git" not in ada_usage
    assert nag_usage["git"]["use_count"] == 1
    assert "sitrep" not in nag_usage
