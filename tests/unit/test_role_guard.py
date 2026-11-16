"""A DSH mind held to its roles, per tool call and per hook (feature 10.18).

Protected behaviours 1 and 2 of Role Management on the DSH harness: a mind
holding several roles can do exactly what their union grants and nothing
else, and changing a role changes what it can do on the very next call.

The guard and the gate are run as dsh runs them — `python3 role_guard.py
...` with the event on stdin — against a real HTTP server standing in for
the gateway, so what is asserted is the exit code and the reason dsh's hook
bridge would act on.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from minds.harness import role_guard

GUARD = Path(role_guard.__file__).resolve()


class Gateway:
    """`GET /minds/{id}/grants`, answering whatever `grants` holds now."""

    def __init__(self):
        self.grants: dict | None = {"managed": False}
        self.status = 200
        self.asked: list[str] = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                gateway.asked.append(self.path)
                body = json.dumps(gateway.grants).encode()
                self.send_response(gateway.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                del args

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def gateway():
    server = Gateway()
    yield server
    server.close()


def _managed(**grants):
    return {"managed": True, "roles": ["general", "writer"], "skills": [], "hooks": [],
            "models": [], "tools": [], **grants}


def _env(gateway, tmp_path, grants_file=None):
    return {"PATH": "/usr/bin:/bin", "MIND_ID": "mind-1", "COMMS_URL": gateway.url,
            "COMMS_BEARER_TOKEN": "t", "DSH_HOME": str(tmp_path / ".dsh"),
            "HIVE_ROLE_GRANTS_FILE": str(grants_file or tmp_path / "role-grants.json")}


def _tool(env, name, arguments=None):
    event = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": arguments or {}}
    return subprocess.run([sys.executable, str(GUARD), "guard"], input=json.dumps(event),
                          env=env, capture_output=True, text=True, timeout=30)


def test_a_tool_outside_the_union_of_the_roles_is_refused_and_one_inside_runs(gateway, tmp_path):
    gateway.grants = _managed(tools=["files", "web"], skills=["remember"])
    env = _env(gateway, tmp_path)

    shell = _tool(env, "bash", {"command": "ls"})
    read = _tool(env, "read", {"path": "/tmp/x"})
    search = _tool(env, "web_search", {"query": "x"})
    delegate = _tool(env, "subagent", {"task": "x"})

    assert shell.returncode == role_guard.DENY
    assert "shell" in shell.stderr and "not allowed" in shell.stderr
    assert read.returncode == 0 and search.returncode == 0
    assert delegate.returncode == role_guard.DENY


def test_a_skill_no_role_grants_is_refused_and_a_granted_one_runs(gateway, tmp_path):
    gateway.grants = _managed(skills=["remember", "code"])
    env = _env(gateway, tmp_path)

    assert _tool(env, "skill", {"name": "code"}).returncode == 0
    refused = _tool(env, "skill", {"name": "send-sms"})
    assert refused.returncode == role_guard.DENY
    assert "send-sms" in refused.stderr


def test_a_role_changed_between_two_calls_is_obeyed_on_the_second(gateway, tmp_path):
    env = _env(gateway, tmp_path)
    gateway.grants = _managed(tools=["files"])
    assert _tool(env, "bash").returncode == role_guard.DENY

    gateway.grants = _managed(tools=["files", "shell"])
    assert _tool(env, "bash").returncode == 0

    gateway.grants = _managed(tools=["files"])
    assert _tool(env, "bash").returncode == role_guard.DENY


def test_a_delegate_tool_counts_as_sub_minds(gateway, tmp_path):
    grants_file = tmp_path / "role-grants.json"
    role_guard.write_grants(grants_file, {"delegates": ["researcher"]})
    gateway.grants = _managed(tools=["files"])
    env = _env(gateway, tmp_path, grants_file)

    assert _tool(env, "researcher", {"task": "x"}).returncode == role_guard.DENY
    gateway.grants = _managed(tools=["files", "sub-minds"])
    assert _tool(env, "researcher", {"task": "x"}).returncode == 0


def test_with_the_gateway_down_the_spawn_grants_stand_in(gateway, tmp_path):
    grants_file = tmp_path / "role-grants.json"
    role_guard.write_grants(grants_file, _managed(tools=["files"]))
    gateway.status = 500
    env = _env(gateway, tmp_path, grants_file)

    assert _tool(env, "bash").returncode == role_guard.DENY
    assert _tool(env, "read").returncode == 0


def test_a_guard_with_nothing_to_judge_by_refuses_the_tool(gateway, tmp_path):
    gateway.status = 500
    env = _env(gateway, tmp_path, tmp_path / "missing.json")

    result = _tool(env, "read")

    assert result.returncode == role_guard.DENY


def test_an_unmanaged_mind_runs_every_tool(gateway, tmp_path):
    gateway.grants = {"managed": False}
    env = _env(gateway, tmp_path)

    assert _tool(env, "bash").returncode == 0
    assert _tool(env, "skill", {"name": "anything"}).returncode == 0


def _gate(env, name, marker):
    return subprocess.run(
        [sys.executable, str(GUARD), "gate", name, "--", "sh", "-c", f"cat > {marker}"],
        input="payload", env=env, capture_output=True, text=True, timeout=30)


def test_a_hook_no_role_grants_does_not_run_and_a_granted_one_does(gateway, tmp_path):
    env = _env(gateway, tmp_path)
    gateway.grants = _managed(hooks=["time_inject"])

    _gate(env, "training_capture", tmp_path / "training")
    _gate(env, "time_inject", tmp_path / "time")

    assert not (tmp_path / "training").exists()
    assert (tmp_path / "time").read_text() == "payload"


def test_the_hooks_a_mind_cannot_run_without_are_never_gated(tmp_path):
    source = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "bash /m/hooks/auto_remember.sh"},
        {"type": "command", "command": "python3 /m/hooks/rotation_check.py"},
        {"type": "command", "command": "bash /m/hooks/training_capture.sh"},
    ]}]}}

    written = role_guard.effective_hooks(source)

    commands = [h["command"] for h in written["hooks"]["Stop"][0]["hooks"]]
    assert commands[0] == "bash /m/hooks/auto_remember.sh"
    assert commands[1] == "python3 /m/hooks/rotation_check.py"
    assert " gate training_capture -- " in commands[2]
    guard = written["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert guard.endswith(" guard")


def test_a_mind_with_no_hooks_file_of_its_own_is_still_guarded(tmp_path, monkeypatch):
    monkeypatch.setattr(role_guard, "fetch", lambda *a, **k: {"managed": True, "tools": []})
    home = tmp_path / ".dsh"
    home.mkdir()

    env = role_guard.prepare(home, "", "mind-1", "http://gateway", "t")

    written = json.loads(Path(env["DSH_HOOKS_CONFIG"]).read_text())
    assert written["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith(" guard")
    assert json.loads(Path(env["HIVE_ROLE_GRANTS_FILE"]).read_text())["managed"] is True


def test_a_spawn_the_gateway_cannot_answer_for_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(role_guard, "fetch", lambda *a, **k: None)
    home = tmp_path / ".dsh"
    home.mkdir()

    with pytest.raises(RuntimeError):
        role_guard.prepare(home, "", "mind-1", "http://gateway", "t")


def test_with_spawn_grants_on_disk_a_role_change_is_still_obeyed_on_the_next_call(gateway, tmp_path):
    grants_file = tmp_path / "role-grants.json"
    role_guard.write_grants(grants_file, _managed(tools=["files"]))
    env = _env(gateway, tmp_path, grants_file)

    gateway.grants = _managed(tools=["files", "shell"])
    assert _tool(env, "bash").returncode == 0
    gateway.grants = _managed(tools=["files"])
    assert _tool(env, "bash").returncode == role_guard.DENY


def test_with_the_gateway_down_a_managed_minds_ungranted_hook_does_not_run(gateway, tmp_path):
    grants_file = tmp_path / "role-grants.json"
    role_guard.write_grants(grants_file, _managed(hooks=["time_inject"]))
    gateway.status = 500
    env = _env(gateway, tmp_path, grants_file)

    _gate(env, "training_capture", tmp_path / "training")
    _gate(env, "time_inject", tmp_path / "time")

    assert not (tmp_path / "training").exists()
    assert (tmp_path / "time").read_text() == "payload"


def test_a_persistent_terminal_is_a_shell_to_the_roles(gateway, tmp_path):
    gateway.grants = _managed(tools=["files"])
    env = _env(gateway, tmp_path)

    assert _tool(env, "terminal_open", {}).returncode == role_guard.DENY
    gateway.grants = _managed(tools=["files", "shell"])
    assert _tool(env, "terminal_open", {}).returncode == 0


def _prompt(env, text):
    event = {"hook_event_name": "UserPromptSubmit", "prompt": text}
    return subprocess.run([sys.executable, str(GUARD), "prompt"], input=json.dumps(event),
                          env=env, capture_output=True, text=True, timeout=30)


def test_a_typed_skill_name_no_role_grants_is_refused(gateway, tmp_path):
    (tmp_path / ".dsh" / "skills" / "send-sms").mkdir(parents=True)
    (tmp_path / ".dsh" / "skills" / "remember").mkdir(parents=True)
    gateway.grants = _managed(skills=["remember"])
    env = _env(gateway, tmp_path)

    refused = _prompt(env, "/send-sms tell Sloan dinner")
    assert refused.returncode == role_guard.DENY and "send-sms" in refused.stderr
    assert _prompt(env, "/remember this").returncode == 0
    assert _prompt(env, "/model opus").returncode == 0
