"""Failed-turn recovery runs only a directly registered rotation check."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from minds import failed_turn
from minds.harness import dsh_hooks, rotation_hooks


def _stop_commands(config_path: Path) -> list[str]:
    config = __import__("tomllib").loads(config_path.read_text())
    return [
        hook["command"]
        for group in config["hooks"]["Stop"]
        for hook in group.get("hooks", [])
    ]


def test_codex_failed_turn_prefers_generated_check_over_preserved_custom_hook(
    tmp_path, monkeypatch,
):
    """FR-01 / AC-06,08: a custom same-name Stop hook is never recovery."""
    from minds.harness import codex_cli

    app_root = Path(__file__).resolve().parents[2]
    mind_dir = tmp_path / "minds" / "probe"
    home = mind_dir / ".codex"
    runtime_path = mind_dir / "runtime.yaml"
    home.mkdir(parents=True)
    runtime_path.write_text("name: probe\nmind_id: probe\nrotation_threshold_percent: 75\n")
    rotation_hooks.write_harness_hooks(
        "codex", home, runtime_path=runtime_path, python=sys.executable, app_root=app_root,
    )
    custom = "python /projects/custom/rotation_check.py --report"
    config_path = home / "config.toml"
    text = config_path.read_text()
    boundary = "[[hooks.UserPromptSubmit]]"
    config_path.write_text(text.replace(
        boundary,
        "[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(custom)}\n\n"
        + boundary,
        1,
    ))
    rotation_hooks.write_harness_hooks(
        "codex", home, runtime_path=runtime_path, python=sys.executable, app_root=app_root,
    )

    monkeypatch.setattr(codex_cli, "CODEX_HOME", home)
    monkeypatch.setattr(codex_cli, "MIND_DIR", mind_dir)
    selected = codex_cli._rotation_check_command()

    commands = _stop_commands(config_path)
    generated = next(command for command in commands if command != custom)
    assert commands.index(custom) < commands.index(generated)
    assert selected == __import__("shlex").split(generated)


def test_failed_turn_ignores_custom_wrappers_fire_and_malformed_commands(tmp_path):
    """FR-01 / AC-06,08: only direct owned checks authorize recovery."""
    native = tmp_path / "rotation_hooks.py"
    copied = tmp_path / "rotation_check.py"
    stop_groups = [{"hooks": [
        {"command": "python 'unterminated"},
        {"command": f"python /custom/audit.py {native} check"},
        {"command": f"python {native} fire"},
        {"command": f"python {copied}"},
        {"command": f"python {native} check --harness codex"},
    ]}]

    assert failed_turn.rotation_check_command(
        stop_groups, native_script=native, copied_scripts=(copied,),
    ) == ["python", str(native), "check", "--harness", "codex"]


@pytest.mark.parametrize("legacy_at", ["declared", "canonical"])
def test_codex_adapter_selects_each_documented_owned_legacy_copy(
    legacy_at, tmp_path, monkeypatch,
):
    """FR-01 IT-05g-L: each documented Codex copy remains selectable."""
    from minds.harness import codex_cli

    mind_dir = tmp_path / "minds" / "probe"
    declared_home = tmp_path / "declared home"
    canonical_home = mind_dir / ".codex"
    declared_home.mkdir(parents=True)
    legacy = (declared_home if legacy_at == "declared" else canonical_home)
    legacy_check = legacy / "hooks" / "rotation_check.py"
    command = shlex.join(["python3", str(legacy_check)])
    (declared_home / "config.toml").write_text(
        "[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(command)}\n"
    )
    monkeypatch.setattr(codex_cli, "CODEX_HOME", declared_home)
    monkeypatch.setattr(codex_cli, "MIND_DIR", mind_dir)

    assert codex_cli._rotation_check_command() == ["python3", str(legacy_check)]


def test_codex_adapter_prefers_generated_native_check_over_earlier_legacy_copy(
    tmp_path, monkeypatch,
):
    """FR-01 IT-05g-L: a generated native check beats an owned old copy."""
    from minds.harness import codex_cli

    app_root = Path(__file__).resolve().parents[2]
    mind_dir = tmp_path / "minds" / "probe"
    home = mind_dir / ".codex"
    runtime_path = mind_dir / "runtime.yaml"
    home.mkdir(parents=True)
    runtime_path.write_text("name: probe\nmind_id: probe\nrotation_threshold_percent: 75\n")
    legacy = shlex.join(["python3", str(home / "hooks" / "rotation_check.py")])
    (home / "config.toml").write_text(
        "[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(legacy)}\n"
    )
    rotation_hooks.write_harness_hooks(
        "codex", home, runtime_path=runtime_path, python=sys.executable, app_root=app_root,
    )
    monkeypatch.setattr(codex_cli, "CODEX_HOME", home)
    monkeypatch.setattr(codex_cli, "MIND_DIR", mind_dir)

    generated = next(
        command for command in _stop_commands(home / "config.toml")
        if "rotation_hooks.py" in command
    )
    assert codex_cli._rotation_check_command() == shlex.split(generated)


def test_dsh_adapter_selects_configured_and_declared_owned_copies(tmp_path, monkeypatch):
    """FR-01 IT-05g-L: dsh honors the selected config and declared home."""
    from minds.harness import dsh_cli

    mind_dir = tmp_path / "minds" / "probe"
    declared_home = tmp_path / "declared"
    selected_home = tmp_path / "selected config"
    declared_home.mkdir()
    selected = dsh_hooks.write_hooks(
        "probe", mind_dir.parent, container_minds_dir=str(mind_dir.parent),
        home=selected_home, container_hooks_dir=str(selected_home / "hooks"),
        python=sys.executable,
    )
    monkeypatch.setattr(dsh_cli, "DSH_HOME", declared_home)
    monkeypatch.setattr(dsh_cli, "MIND_DIR", mind_dir)
    monkeypatch.setenv("DSH_HOOKS_CONFIG", selected["hooks_config"])
    assert dsh_cli._rotation_check_command() == [
        sys.executable, str(selected_home / "hooks" / "rotation_check.py"),
    ]

    generated = dsh_hooks.hooks_config(str(declared_home / "hooks"), python=sys.executable)
    selected_config = selected_home / "effective-hooks.json"
    selected_config.write_text(json.dumps(generated))
    monkeypatch.setenv("DSH_HOOKS_CONFIG", str(selected_config))
    assert dsh_cli._rotation_check_command() == [
        sys.executable, str(declared_home / "hooks" / "rotation_check.py"),
    ]


def test_adapter_selectors_reject_custom_or_fire_and_skip_malformed_entries(
    tmp_path, monkeypatch,
):
    """FR-01 IT-05g-N: recovery requires an owned check and keeps scanning."""
    from minds.harness import codex_cli, dsh_cli

    app_root = Path(__file__).resolve().parents[2]
    mind_dir = tmp_path / "minds" / "probe"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    fire = rotation_hooks._command(
        sys.executable, app_root, "fire", "codex", mind_dir / "runtime.yaml",
    )
    custom = "python /projects/custom/rotation_check.py --report"
    (codex_home / "config.toml").write_text(
        "[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(custom)}\n"
        "[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(fire)}\n"
    )
    initial_codex = (codex_home / "config.toml").read_bytes()
    monkeypatch.setattr(codex_cli, "CODEX_HOME", codex_home)
    monkeypatch.setattr(codex_cli, "MIND_DIR", mind_dir)
    assert codex_cli._rotation_check_command() is None
    assert (codex_home / "config.toml").read_bytes() == initial_codex

    check = rotation_hooks._command(
        sys.executable, app_root, "check", "codex", mind_dir / "runtime.yaml",
    )
    (codex_home / "config.toml").write_text(
        "[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n"
        'type = "command"\ncommand = "python \'unterminated"\n'
        "[[hooks.Stop.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(check)}\n"
    )
    assert codex_cli._rotation_check_command() == shlex.split(check)

    dsh_home = tmp_path / "dsh"
    generated = dsh_hooks.hooks_config(str(dsh_home / "hooks"), python=sys.executable)
    dsh_config = tmp_path / "dsh-hooks.json"
    fire_command = generated["hooks"]["UserPromptSubmit"][-1]["hooks"][0]["command"]
    generated["hooks"]["Stop"] = [{"hooks": [
        {"type": "command", "command": custom},
        {"type": "command", "command": fire_command},
    ]}]
    dsh_config.write_text(json.dumps(generated))
    initial_dsh = dsh_config.read_bytes()
    monkeypatch.setattr(dsh_cli, "DSH_HOME", dsh_home)
    monkeypatch.setattr(dsh_cli, "MIND_DIR", mind_dir)
    monkeypatch.setenv("DSH_HOOKS_CONFIG", str(dsh_config))
    assert dsh_cli._rotation_check_command() is None
    assert dsh_config.read_bytes() == initial_dsh

    generated = dsh_hooks.hooks_config(str(dsh_home / "hooks"), python=sys.executable)
    generated["hooks"]["Stop"][0]["hooks"].insert(
        0, {"type": "command", "command": "python 'unterminated"},
    )
    dsh_config.write_text(json.dumps(generated))
    assert dsh_cli._rotation_check_command() == [
        sys.executable, str(dsh_home / "hooks" / "rotation_check.py"),
    ]
