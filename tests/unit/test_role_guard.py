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

#: The real call to the gateway. The suite's conftest answers it as
#: "unmanaged" for every test; the tests here run a gateway of their own.
REAL_FETCH = role_guard.fetch


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
def gateway(monkeypatch):
    monkeypatch.setattr(role_guard, "fetch", REAL_FETCH)
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


@pytest.mark.parametrize("tool, arguments", [("skill", {"name": "send-sms"}),
                                             ("Skill", {"skill": "send-sms"})])
def test_a_managed_mind_loads_a_skill_none_of_its_roles_list(gateway, tmp_path, tool, arguments):
    gateway.grants = _managed(tools=["files"], skills=["remember"])
    env = _env(gateway, tmp_path)

    assert _tool(env, tool, arguments).returncode == 0
    assert _tool(env, "bash", {"command": "ls"}).returncode == role_guard.DENY


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


def test_a_typed_skill_name_on_a_pane_that_still_asks_the_guard_goes_through(gateway, tmp_path):
    (tmp_path / ".dsh" / "skills" / "send-sms").mkdir(parents=True)
    gateway.grants = _managed(skills=["remember"])
    event = {"hook_event_name": "UserPromptSubmit", "prompt": "/send-sms " + "x" * 200_000}

    typed = subprocess.run([sys.executable, str(GUARD), "prompt"], input=json.dumps(event),
                           env=_env(gateway, tmp_path), capture_output=True, text=True, timeout=30)

    assert typed.returncode == 0 and typed.stderr == ""


# ── the same guard on Claude and Codex (stories 10.18.6, 10.18.7) ─────────

#: One tool per family as each harness names it: dsh, Claude, and Codex
#: (whose shell reaches its hooks as Bash, verified against a live codex).
SAME_TOOL = {
    "shell": ("bash", "Bash", "exec_command"),
    "files": ("write", "Edit", "apply_patch"),
    "web": ("web_fetch", "WebFetch", "mcp__codex_apps__search_service__web_run"),
    "sub-minds": ("subagent", "Task", "collaborationspawn_agent"),
}


@pytest.mark.parametrize("family", sorted(SAME_TOOL))
def test_one_role_grants_a_family_the_same_on_dsh_claude_and_codex(gateway, tmp_path, family):
    env = _env(gateway, tmp_path)

    gateway.grants = _managed(tools=[f for f in SAME_TOOL if f != family])
    refused = [_tool(env, name).returncode for name in SAME_TOOL[family]]
    gateway.grants = _managed(tools=[family])
    allowed = [_tool(env, name).returncode for name in SAME_TOOL[family]]

    assert refused == [role_guard.DENY] * 3
    assert allowed == [0, 0, 0]


def _run_hook(command, event, env):
    return subprocess.run(command, shell=True, input=json.dumps(event), env=env,
                          capture_output=True, text=True, timeout=30)


def test_the_settings_a_claude_process_is_given_put_the_guard_on_every_tool(gateway, tmp_path):
    gateway.grants = _managed(tools=["files"])
    args, extra = role_guard.prepare_claude(tmp_path / "cfg", "mind-1", gateway.url, "t", python=sys.executable)
    assert args[0] == "--settings"
    settings = json.loads(Path(args[1]).read_text())
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    env = {**_env(gateway, tmp_path), **extra}

    bash = _run_hook(command, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, env)
    read = _run_hook(command, {"tool_name": "Read", "tool_input": {"file_path": "/x"}}, env)

    assert bash.returncode == role_guard.DENY and read.returncode == 0
    assert "matcher" not in settings["hooks"]["PreToolUse"][0]


def _codex_overrides(args):
    import tomllib
    pairs = [args[i + 1] for i in range(0, len(args), 2) if args[i] == "-c"]
    return {key: tomllib.loads(f"v = {value}")["v"]
            for key, value in (p.split("=", 1) for p in pairs)}


def test_a_codex_process_gets_the_guard_ahead_of_its_own_pre_tool_hooks(gateway, tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(
        '[[hooks.PreToolUse]]\n[[hooks.PreToolUse.hooks]]\ntype = "command"\n'
        'command = "bash /m/hooks/own \\"quoted\\".sh"\n')
    gateway.grants = _managed(tools=["shell"])

    args, extra = role_guard.prepare_codex(home, "mind-1", gateway.url, "t", python=sys.executable)
    groups = _codex_overrides(args)["hooks.PreToolUse"]
    env = {**_env(gateway, tmp_path), **extra}

    assert groups[1]["hooks"][0]["command"] == 'bash /m/hooks/own "quoted".sh'
    guard = groups[0]["hooks"][0]["command"]
    assert _run_hook(guard, {"tool_name": "apply_patch", "tool_input": {}}, env).returncode == role_guard.DENY
    assert _run_hook(guard, {"tool_name": "Bash", "tool_input": {"command": "ls"}}, env).returncode == 0


def test_codexs_own_web_search_is_off_for_a_managed_mind_without_web(gateway, tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()

    gateway.grants = _managed(tools=["shell"])
    without = _codex_overrides(role_guard.prepare_codex(home, "m", gateway.url, "t")[0])
    gateway.grants = _managed(tools=["web"])
    granted = _codex_overrides(role_guard.prepare_codex(home, "m", gateway.url, "t")[0])
    gateway.grants = {"managed": False}
    unmanaged = _codex_overrides(role_guard.prepare_codex(home, "m", gateway.url, "t")[0])

    assert without.get("web_search") == "disabled"
    assert "web_search" not in granted and "web_search" not in unmanaged


@pytest.mark.parametrize("prepare", ["claude", "codex"])
def test_a_claude_or_codex_spawn_the_gateway_cannot_answer_for_is_refused(gateway, tmp_path, prepare):
    gateway.status = 500
    with pytest.raises(RuntimeError, match="not started"):
        if prepare == "claude":
            role_guard.prepare_claude(tmp_path, "m", gateway.url, "t")
        else:
            role_guard.prepare_codex(tmp_path, "m", gateway.url, "t")


#: Every Claude and Codex tool name a family owns, written out here from the
#: harnesses themselves, not read back from the guard's own table.
EVERY_NAME = {
    "shell": ["Bash", "BashOutput", "KillShell", "Monitor", "PowerShell", "TaskOutput",
              "exec_command", "write_stdin", "shell", "shell_command", "local_shell",
              "unified_exec", "js_repl"],
    "files": ["Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS",
              "apply_patch", "view_image"],
    "web": ["WebFetch", "WebSearch", "mcp__codex_apps__search_service__web_run"],
    "sub-minds": ["Task", "Agent", "SendMessage", "TaskStop", "Workflow",
                  "spawn_agent", "send_input", "wait_agent", "close_agent", "resume_agent",
                  "collaborationspawn_agent", "collaborationwait_agent"],
}


@pytest.mark.parametrize("family,name", [(f, n) for f, names in EVERY_NAME.items() for n in names])
def test_every_claude_and_codex_tool_is_held_to_its_family(family, name):
    others = [f for f in EVERY_NAME if f != family]

    assert role_guard.tool_refusal(_managed(tools=others), name, {}) is not None
    assert role_guard.tool_refusal(_managed(tools=[family]), name, {}) is None


def test_a_gateway_that_no_longer_knows_a_managed_mind_does_not_unmanage_it(gateway, tmp_path):
    grants_file = tmp_path / "role-grants.json"
    role_guard.write_grants(grants_file, _managed(tools=["files"]))
    gateway.status = 404
    env = _env(gateway, tmp_path, grants_file)

    assert _tool(env, "Bash").returncode == role_guard.DENY
    assert _tool(env, "Read").returncode == 0


def test_a_claude_guard_asks_about_its_own_mind_whatever_the_pane_inherited(gateway, tmp_path):
    gateway.grants = _managed(tools=["files"])
    args, extra = role_guard.prepare_claude(tmp_path / "cfg", "mind-1", gateway.url, "t", python=sys.executable)
    settings = json.loads(Path(args[1]).read_text())
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    # A pane's environment: nothing of the mind's but what the spawn handed it.
    env = {"PATH": "/usr/bin:/bin", "COMMS_BEARER_TOKEN": "t", "MIND_ID": "somebody-else",
           **extra}

    before = _run_hook(command, {"tool_name": "Bash", "tool_input": {}}, env).returncode
    gateway.grants = _managed(tools=["files", "shell"])
    after = _run_hook(command, {"tool_name": "Bash", "tool_input": {}}, env).returncode

    assert (before, after) == (role_guard.DENY, 0)
    assert gateway.asked[-1] == "/minds/mind-1/grants"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_a_guard_whose_interpreter_has_gone_refuses_the_tool(gateway, tmp_path, harness):
    gone = str(tmp_path / "venv" / "bin" / "python")
    if harness == "claude":
        args, extra = role_guard.prepare_claude(tmp_path, "m", gateway.url, "t", python=gone)
        command = json.loads(Path(args[1]).read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    else:
        args, extra = role_guard.prepare_codex(tmp_path, "m", gateway.url, "t", python=gone)
        command = _codex_overrides(args)["hooks.PreToolUse"][0]["hooks"][0]["command"]

    done = _run_hook(command, {"tool_name": "Read", "tool_input": {}},
                     {**_env(gateway, tmp_path), **extra})

    assert done.returncode == role_guard.DENY and "cannot run" in done.stderr


def test_a_codex_hook_of_the_minds_own_survives_being_carried_over_whatever_it_says(gateway, tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    own = 'bash -c "echo \\U0001F600 caf\\u00e9 \'x\'"'
    (home / "config.toml").write_text(
        '[[hooks.PreToolUse]]\nmatcher = "Bash"\n[[hooks.PreToolUse.hooks]]\n'
        f'type = "command"\ntimeout = 5\ncommand = {json.dumps(own)}\n', encoding="utf-8")
    expected = __import__("tomllib").loads((home / "config.toml").read_text(encoding="utf-8"))

    args, _ = role_guard.prepare_codex(home, "m", gateway.url, "t")

    assert _codex_overrides(args)["hooks.PreToolUse"][1:] == expected["hooks"]["PreToolUse"]


def test_a_codex_config_that_does_not_parse_refuses_the_spawn_and_says_so(gateway, tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text("[[hooks.PreToolUse\n")

    with pytest.raises(role_guard.NotStarted, match="config.toml"):
        role_guard.prepare_codex(home, "m", gateway.url, "t")
