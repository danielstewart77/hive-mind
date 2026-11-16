"""Generated harness hook commands reload the mind-owned rotation policy."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from minds import failed_turn
from minds.harness import dsh_hooks, rotation_hooks


class _HookService(ThreadingHTTPServer):
    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                self._reply({"data": [{"id": "model-a", "context_window": 100_000}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, body))
                if self.path.endswith("/arm-rotation"):
                    answer = {"ok": True}
                elif self.path.endswith("/fire-rotation"):
                    answer = {"ok": True, "rotated": True}
                else:
                    answer = {"ok": True}
                self._reply(answer)

            def log_message(self, *_args):
                pass

        super().__init__(("127.0.0.1", 0), Handler)


@pytest.fixture
def service():
    server = _HookService()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _runtime(path: Path, percentage: object = 75) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "name": "probe", "mind_id": "mind-probe",
        "rotation_threshold_percent": percentage,
    }))


def _transcript(tmp_path: Path, harness: str, tokens: int = 60_000) -> Path:
    path = tmp_path / ("transcript.jsonl" if harness == "claude" else "rollout.jsonl")
    if harness == "claude":
        entries = [{"type": "assistant", "message": {
            "model": "model-a", "usage": {"input_tokens": tokens, "output_tokens": 0},
            "content": [{"type": "text", "text": "done"}],
        }}]
    elif harness == "codex":
        entries = [
            {"type": "turn_context", "payload": {"model": "model-a"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": tokens, "output_tokens": 0,
                                     "total_tokens": tokens},
                "total_token_usage": {"total_tokens": 999_000},
            }}},
        ]
    else:
        entries = []
    path.write_text("".join(json.dumps(item) + "\n" for item in entries))
    return path


def _unmeasured_transcript(tmp_path: Path, harness: str) -> Path:
    path = tmp_path / ("unmeasured.jsonl" if harness == "claude" else "unmeasured-rollout.jsonl")
    entries = [] if harness == "claude" else [
        {"type": "turn_context", "payload": {"model": "model-a"}},
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in entries))
    return path


def _write_config(harness: str, mind_dir: Path, runtime_path: Path, app_root: Path) -> Path:
    if harness == "dsh":
        result = dsh_hooks.write_hooks(
            "probe", mind_dir.parent, container_minds_dir=str(mind_dir.parent),
            python=sys.executable,
        )
        return Path(result["hooks_config"])
    home = mind_dir / (".claude" if harness == "claude" else ".codex")
    home.mkdir(parents=True, exist_ok=True)
    rotation_hooks.write_harness_hooks(
        harness, home, runtime_path=runtime_path, python=sys.executable,
        app_root=app_root,
    )
    return home / ("settings.json" if harness == "claude" else "config.toml")


def _commands(harness: str, config_path: Path) -> dict[str, list[str]]:
    if harness == "codex":
        config = tomllib.loads(config_path.read_text())
    else:
        config = json.loads(config_path.read_text())
    events = {}
    for event, groups in (config.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks") or []:
                command = hook.get("command") if isinstance(hook, dict) else None
                if command:
                    events.setdefault(event, []).append(command)
    return events


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_native_hook_install_preserves_user_settings_and_replaces_only_managed_entries(
    harness, tmp_path,
):
    mind_dir = tmp_path / "minds" / "probe"
    runtime_path = mind_dir / "runtime.yaml"
    _runtime(runtime_path, 60)
    home = mind_dir / (".claude" if harness == "claude" else ".codex")
    home.mkdir(parents=True)
    app_root = Path(__file__).resolve().parents[2]
    previous_runtime_path = mind_dir / "previous-runtime.yaml"
    managed_command = rotation_hooks._command(
        sys.executable, app_root, "check", harness, previous_runtime_path,
    )
    if harness == "claude":
        config_path = home / "settings.json"
        config_path.write_text(json.dumps({
            "custom_key": {"keep": True},
            "hooks": {"Stop": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "custom-stop"},
                {"type": "command", "command": managed_command},
            ]}]},
        }))
    else:
        config_path = home / "config.toml"
        config_path.write_text(
            'model_provider = "local"\n'
            '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n'
            'type = "command"\n'
            f'command = {json.dumps(managed_command)}\n\n'
            '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\n'
            'type = "command"\ncommand = "custom-stop"\n'
        )

    rotation_hooks.write_harness_hooks(
        harness, home, runtime_path=runtime_path, python=sys.executable, app_root=app_root,
    )
    rotation_hooks.write_harness_hooks(
        harness, home, runtime_path=runtime_path, python=sys.executable, app_root=app_root,
    )
    events = _commands(harness, config_path)

    stop_commands = events["Stop"]
    assert sum("rotation_hooks.py check" in command for command in stop_commands) == 1
    assert "custom-stop" in stop_commands
    assert managed_command not in stop_commands
    if harness == "claude":
        saved = json.loads(config_path.read_text())
        assert saved["custom_key"] == {"keep": True}
    else:
        saved = tomllib.loads(config_path.read_text())
        assert saved["model_provider"] == "local"
        assert failed_turn.rotation_check_command(saved["hooks"]["Stop"]) is not None


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
@pytest.mark.parametrize("custom_kind", ["same_name", "python_wrapper", "direct_wrapper"])
def test_rotation_hook_refresh_preserves_custom_rotation_named_command_in_mixed_group(
    harness, custom_kind, tmp_path,
):
    mind_dir = tmp_path / "minds" / "probe"
    runtime_path = mind_dir / "runtime.yaml"
    _runtime(runtime_path, 60)
    app_root = Path(__file__).resolve().parents[2]
    config_path = _write_config(harness, mind_dir, runtime_path, app_root)
    managed_script = (
        mind_dir.parent / "probe" / ".dsh" / "hooks" / "rotation_check.py"
        if harness == "dsh"
        else app_root / "minds" / "harness" / "rotation_hooks.py"
    )
    generated_commands = _commands(harness, config_path)
    generated_check = generated_commands["Stop"][-1]
    generated_fire = generated_commands["UserPromptSubmit"][-1]
    custom_command = {
        "same_name": "python /projects/custom/rotation_check.py --report",
        "python_wrapper": shlex.join([
            "python", "/projects/custom/audit.py", str(managed_script), "check",
        ]),
        "direct_wrapper": shlex.join([
            "/projects/custom/audit", str(managed_script), "check",
        ]),
    }[custom_kind]
    sibling_command = "custom-stop"

    if harness in ("claude", "dsh"):
        saved = json.loads(config_path.read_text())
        saved["hooks"]["Stop"][0]["hooks"].extend([
            {"type": "command", "command": sibling_command},
            {"type": "command", "command": custom_command},
        ])
        config_path.write_text(json.dumps(saved))
    else:
        text = config_path.read_text()
        boundary = "[[hooks.UserPromptSubmit]]"
        assert boundary in text
        text = text.replace(
            boundary,
            '[[hooks.Stop.hooks]]\n'
            'type = "command"\n'
            f'command = {json.dumps(sibling_command)}\n\n'
            '[[hooks.Stop.hooks]]\n'
            'type = "command"\n'
            f'command = {json.dumps(custom_command)}\n\n'
            + boundary,
            1,
        )
        config_path.write_text(text)

    if harness == "dsh":
        dsh_hooks.write_hooks(
            "probe", mind_dir.parent, container_minds_dir=str(mind_dir.parent),
            python=sys.executable,
        )
    else:
        rotation_hooks.write_harness_hooks(
            harness, mind_dir / (".claude" if harness == "claude" else ".codex"),
            runtime_path=runtime_path, python=sys.executable, app_root=app_root,
        )

    commands = _commands(harness, config_path)["Stop"]
    assert commands.count(custom_command) == 1
    assert commands.count(sibling_command) == 1
    assert commands.count(generated_check) == 1
    assert _commands(harness, config_path)["UserPromptSubmit"].count(generated_fire) == 1


def test_native_hook_install_does_not_create_an_undeclared_home(tmp_path):
    home = tmp_path / "not-provisioned"

    result = rotation_hooks.write_harness_hooks(
        "claude", home, runtime_path=tmp_path / "runtime.yaml",
    )

    assert result["installed"] is False
    assert not home.exists()


def _event(harness: str, transcript: Path) -> dict:
    common = {"session_id": "conversation-1", "hook_event_name": "Stop",
              "model": "model-a", "transcript_path": str(transcript)}
    if harness == "dsh":
        return {**common, "context": {"tokens": 60_000, "kind": "usage",
                                      "model": "model-a", "window": 100_000},
                "goal": True}
    return common


def _run(command: str, event: dict, env: dict[str, str], app_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(shlex.split(command), check=False,
                          input=json.dumps(event), text=True,
                          capture_output=True, env=env, cwd=app_root, timeout=20)


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_generated_safe_check_reloads_saved_percentage_for_each_harness(
    harness, tmp_path, service,
):
    service_url, requests = service
    mind_dir = tmp_path / "minds" / "probe"
    runtime_path = mind_dir / "runtime.yaml"
    _runtime(runtime_path, 75)
    app_root = Path(__file__).resolve().parents[2]
    config_path = _write_config(harness, mind_dir, runtime_path, app_root)
    stop_command = next(command for command in _commands(harness, config_path)["Stop"]
                        if "rotation_check" in command or "rotation_hooks.py" in command)
    transcript = _transcript(tmp_path, harness)
    env = os.environ.copy()
    env.update({
        "HIVE_MIND_APP_ROOT": str(app_root), "HIVE_RUNTIME_PATH": str(runtime_path),
        "HIVE_HARNESS": harness, "HIVE_MODEL": "model-a",
        "HIVE_MODEL_CONTEXT_WINDOW": "100000", "CLIENT_REF": "client-1",
        "SURFACE": "terminal", "HIVE_SURFACE": "terminal",
        "COMMS_URL": service_url, "COMMS_BEARER_TOKEN": "test-comms",
        "INFERENCE_PROXY_URL": service_url, "MIND_PROXY_KEY": "test-proxy",
        "HIVE_GOAL_RUN": "1" if harness == "dsh" else "0",
        "AUTO_REMEMBER_LOG_DIR": str(mind_dir / "data" / "auto-remember"),
    })

    below = _run(stop_command, _event(harness, transcript), env, app_root)
    assert below.returncode == 0, below.stderr
    reports = [body for method, path, body in requests if path.endswith("/sessions/context")]
    assert reports[-1]["threshold"] == 75_000
    assert not [request for request in requests if request[1].endswith("/arm-rotation")]

    _runtime(runtime_path, 40)
    crossed = _run(stop_command, _event(harness, transcript), env, app_root)
    assert crossed.returncode == 0, crossed.stderr
    reports = [body for method, path, body in requests if path.endswith("/sessions/context")]
    assert reports[-1]["threshold"] == 40_000
    if harness == "dsh":
        assert json.loads(crossed.stdout) == {"rotate": True}
    else:
        assert len([request for request in requests
                    if request[1].endswith("/arm-rotation")]) == 1
        marker = mind_dir / "data" / "auto-remember" / (
            "rotation-staged-conversation-1.json")
        assert json.loads(marker.read_text())["state"] == "ready"
        fire_command = next(command for command in _commands(harness, config_path)[
            "UserPromptSubmit"] if "rotation_hooks.py fire" in command)
        fired = _run(fire_command, {"session_id": "conversation-1", "prompt": "continue"},
                     env, app_root)
        assert fired.returncode == 0, fired.stderr
        assert not marker.exists()
        assert [body for method, path, body in requests
                if path.endswith("/sessions/fire-rotation")][-1]["prompt"] == "continue"


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_generated_safe_check_relays_an_unavailable_measurement_as_unknown(
    harness, tmp_path, service,
):
    """The generated native command must not turn absent usage into zero."""
    service_url, requests = service
    mind_dir = tmp_path / "minds" / "probe"
    runtime_path = mind_dir / "runtime.yaml"
    _runtime(runtime_path, 40)
    app_root = Path(__file__).resolve().parents[2]
    config_path = _write_config(harness, mind_dir, runtime_path, app_root)
    stop_command = next(command for command in _commands(harness, config_path)["Stop"]
                        if "rotation_check" in command or "rotation_hooks.py" in command)
    event = _event(harness, _unmeasured_transcript(tmp_path, harness))
    if harness == "dsh":
        event["context"] = {"model": "model-a", "window": 100_000}
    env = os.environ.copy()
    env.update({
        "HIVE_MIND_APP_ROOT": str(app_root), "HIVE_RUNTIME_PATH": str(runtime_path),
        "HIVE_HARNESS": harness, "HIVE_MODEL": "model-a",
        "HIVE_MODEL_CONTEXT_WINDOW": "100000", "CLIENT_REF": "client-1",
        "SURFACE": "terminal", "HIVE_SURFACE": "terminal",
        "COMMS_URL": service_url, "COMMS_BEARER_TOKEN": "test-comms",
        "INFERENCE_PROXY_URL": service_url, "MIND_PROXY_KEY": "test-proxy",
        "HIVE_GOAL_RUN": "1" if harness == "dsh" else "0",
        "AUTO_REMEMBER_LOG_DIR": str(mind_dir / "data" / "auto-remember"),
    })

    completed = _run(stop_command, event, env, app_root)

    assert completed.returncode == 0, completed.stderr
    report = [body for method, path, body in requests if path.endswith("/sessions/context")][-1]
    assert (report["kind"], report["tokens"]) == ("none", None)
    assert not [request for request in requests if request[1].endswith("/arm-rotation")]


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_generated_safe_check_suppresses_invalid_policy_and_recovers(harness, tmp_path, service):
    service_url, requests = service
    mind_dir = tmp_path / "minds" / "probe"
    runtime_path = mind_dir / "runtime.yaml"
    runtime_path.parent.mkdir(parents=True)
    runtime_path.write_text("name: [malformed\n")
    app_root = Path(__file__).resolve().parents[2]
    config_path = _write_config(harness, mind_dir, runtime_path, app_root)
    stop_command = next(command for command in _commands(harness, config_path)["Stop"]
                        if "rotation_check" in command or "rotation_hooks.py" in command)
    transcript = _transcript(tmp_path, harness, tokens=100_000)
    event = _event(harness, transcript)
    if harness == "dsh":
        event["context"]["tokens"] = 100_000
    env = os.environ.copy()
    env.update({
        "HIVE_MIND_APP_ROOT": str(app_root), "HIVE_RUNTIME_PATH": str(runtime_path),
        "HIVE_HARNESS": harness, "HIVE_MODEL": "model-a",
        "HIVE_MODEL_CONTEXT_WINDOW": "100000", "CLIENT_REF": "client-1",
        "SURFACE": "terminal", "HIVE_SURFACE": "terminal",
        "COMMS_URL": service_url, "COMMS_BEARER_TOKEN": "test-comms",
        "INFERENCE_PROXY_URL": service_url, "MIND_PROXY_KEY": "test-proxy",
        "HIVE_GOAL_RUN": "1" if harness == "dsh" else "0",
        "AUTO_REMEMBER_LOG_DIR": str(mind_dir / "data" / "auto-remember"),
    })

    refused = _run(stop_command, event, env, app_root)
    assert refused.returncode == 0, refused.stderr
    assert not [request for request in requests if request[1].endswith("/arm-rotation")]
    assert runtime_path.read_text() == "name: [malformed\n"

    _runtime(runtime_path, 40)
    transcript = _transcript(tmp_path, harness, tokens=60_000)
    restored = _run(stop_command, _event(harness, transcript), env, app_root)
    assert restored.returncode == 0, restored.stderr
    reports = [body for method, path, body in requests if path.endswith("/sessions/context")]
    assert reports[-1]["threshold"] == 40_000


@pytest.mark.parametrize("harness", ["claude", "codex", "dsh"])
def test_generated_safe_check_recovers_when_runtime_file_is_missing(harness, tmp_path, service):
    service_url, requests = service
    mind_dir = tmp_path / "minds" / "probe"
    runtime_path = mind_dir / "runtime.yaml"
    app_root = Path(__file__).resolve().parents[2]
    config_path = _write_config(harness, mind_dir, runtime_path, app_root)
    stop_command = next(command for command in _commands(harness, config_path)["Stop"]
                        if "rotation_check" in command or "rotation_hooks.py" in command)
    transcript = _transcript(tmp_path, harness, tokens=100_000)
    event = _event(harness, transcript)
    if harness == "dsh":
        event["context"]["tokens"] = 100_000
    env = os.environ.copy()
    env.update({
        "HIVE_MIND_APP_ROOT": str(app_root), "HIVE_RUNTIME_PATH": str(runtime_path),
        "HIVE_HARNESS": harness, "HIVE_MODEL": "model-a",
        "HIVE_MODEL_CONTEXT_WINDOW": "100000", "CLIENT_REF": "client-1",
        "SURFACE": "terminal", "HIVE_SURFACE": "terminal",
        "COMMS_URL": service_url, "COMMS_BEARER_TOKEN": "test-comms",
        "INFERENCE_PROXY_URL": service_url, "MIND_PROXY_KEY": "test-proxy",
        "HIVE_GOAL_RUN": "1" if harness == "dsh" else "0",
        "AUTO_REMEMBER_LOG_DIR": str(mind_dir / "data" / "auto-remember"),
    })

    missing = _run(stop_command, event, env, app_root)
    assert missing.returncode == 0, missing.stderr
    assert not runtime_path.exists()
    assert not [request for request in requests if request[1].endswith("/arm-rotation")]
    log_path = mind_dir / "data" / "auto-remember" / "hooks.log"
    assert "config_unreadable" in log_path.read_text()
    assert "test-comms" not in log_path.read_text()

    _runtime(runtime_path, 40)
    transcript = _transcript(tmp_path, harness, tokens=60_000)
    restored = _run(stop_command, _event(harness, transcript), env, app_root)
    assert restored.returncode == 0, restored.stderr
    reports = [body for method, path, body in requests if path.endswith("/sessions/context")]
    assert reports[-1]["threshold"] == 40_000
