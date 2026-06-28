"""Wiring tests for the per-mind skill-telemetry Stop-hook adapters.

Asserts the four new hook files exist, that each mind's Stop hook array gains a
``skill_telemetry_capture.sh`` entry **without** clobbering the existing
``training_capture.sh`` / ``auto_remember.sh`` entries, and that each ``.py``
adapter imports the correct per-harness detector and the shared ``bump_skills``
entry point — proving the fork is wired the right way round (Claude detector on
Ada, Codex detector on Nagatha).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ADA = _ROOT / "minds" / "ada" / ".claude"
_NAGATHA = _ROOT / "minds" / "nagatha" / ".codex"


def _stop_commands_json(settings: dict) -> list[str]:
    cmds: list[str] = []
    for group in settings.get("hooks", {}).get("Stop", []):
        for h in group.get("hooks", []):
            cmd = h.get("command")
            if isinstance(cmd, str):
                cmds.append(cmd)
    return cmds


def _stop_commands_toml(config: dict) -> list[str]:
    cmds: list[str] = []
    for group in config.get("hooks", {}).get("Stop", []):
        for h in group.get("hooks", []):
            cmd = h.get("command")
            if isinstance(cmd, str):
                cmds.append(cmd)
    return cmds


def test_ada_stop_hook_includes_telemetry_capture():
    settings = json.loads((_ADA / "settings.json").read_text())
    cmds = _stop_commands_json(settings)
    blob = "\n".join(cmds)
    assert "skill_telemetry_capture.sh" in blob
    # existing Stop entries must survive (no clobber).
    assert "training_capture.sh" in blob
    assert "auto_remember.sh" in blob


def test_nagatha_stop_hook_includes_telemetry_capture():
    config = tomllib.loads((_NAGATHA / "config.toml").read_text())
    cmds = _stop_commands_toml(config)
    blob = "\n".join(cmds)
    assert "skill_telemetry_capture.sh" in blob
    assert "training_capture.sh" in blob


def test_hook_files_exist_for_both_minds():
    ada_py = _ADA / "hooks" / "skill_telemetry_capture.py"
    ada_sh = _ADA / "hooks" / "skill_telemetry_capture.sh"
    nag_py = _NAGATHA / "hooks" / "skill_telemetry_capture.py"
    nag_sh = _NAGATHA / "hooks" / "skill_telemetry_capture.sh"
    for f in (ada_py, ada_sh, nag_py, nag_sh):
        assert f.is_file(), f"missing hook file: {f}"

    ada_sh_text = ada_sh.read_text()
    assert "skill_telemetry_capture.py" in ada_sh_text
    assert "/opt/venv/bin/python3" in ada_sh_text

    nag_sh_text = nag_sh.read_text()
    assert "skill_telemetry_capture.py" in nag_sh_text
    assert "/opt/venv/bin/python3" in nag_sh_text


def test_ada_adapter_imports_claude_detector():
    text = (_ADA / "hooks" / "skill_telemetry_capture.py").read_text()
    assert "detect_claude_skills" in text
    assert "bump_skills" in text
    assert "detect_codex_skills" not in text


def test_nagatha_adapter_imports_codex_detector():
    text = (_NAGATHA / "hooks" / "skill_telemetry_capture.py").read_text()
    assert "detect_codex_skills" in text
    assert "bump_skills" in text
    assert "detect_claude_skills" not in text
