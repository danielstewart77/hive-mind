"""A dsh mind's generated hooks: the file the mind server checks, and the
scripts it names, run from where the generator put them."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from minds import failed_turn
from minds.harness import dsh_hooks

_SCRIPT_MODULES = ("hive_mind_env", "dsh_transcript", "auto_remember", "rotation_check",
                   "rotation_fire", "contextual_retrieval")


@pytest.fixture
def mind(tmp_path, monkeypatch):
    """A generated mind folder, its hook scripts importable as the copies they are."""
    report = dsh_hooks.write_hooks(
        "probe", tmp_path / "minds", container_minds_dir=str(tmp_path / "minds"),
        python=sys.executable,
    )
    hooks_dir = report["hooks_dir"]
    for name in _SCRIPT_MODULES:
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(hooks_dir)
    for name in ("AUTO_REMEMBER_LOG_DIR", "HIVE_GOAL_RUN", "SPECS_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield Path(hooks_dir).parent  # the mind's dsh home, minds/probe/.dsh
    for name in _SCRIPT_MODULES:
        sys.modules.pop(name, None)


@pytest.fixture
def service(monkeypatch):
    """One HTTP server standing in for every hive service; records each request."""
    seen: list[dict] = []
    replies: dict[str, dict] = {}

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("content-length") or 0)
            seen.append({"method": self.command, "path": self.path.split("?")[0],
                         "auth": self.headers.get("authorization"),
                         "body": json.loads(self.rfile.read(length)) if length else None})
            out = json.dumps(replies.get(self.path.split("?")[0], {})).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    for name, value in (("COMMS_URL", url), ("COMMS_BEARER_TOKEN", "comms-key"),
                        ("LUCENT_URL_SELF", url), ("LUCENT_BEARER_TOKEN", "lucent-key"),
                        ("INFERENCE_PROXY_URL", url), ("MIND_PROXY_KEY", "proxy-key"),
                        ("HIVE_CLASSIFIER_MODEL", "classifier-of-record"),
                        ("MIND_ID", "mind-uuid"), ("SURFACE", "telegram"),
                        ("CLIENT_REF", "chat-1")):
        monkeypatch.setenv(name, value)
    # A hive without the tools service: the capture must not need it.
    for name in ("HIVE_TOOLS_URL", "HIVE_TOOLS_TOKEN", "DSH_PROXY_BASE_URL", "DSH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield seen, replies
    server.shutdown()


def _transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def _save_rotation_policy(mind: Path, percentage: int) -> Path:
    runtime_path = mind.parent / "runtime.yaml"
    runtime_path.write_text(
        f"name: probe\nrotation_threshold_percent: {percentage}\n"
    )
    return runtime_path


# --- the generator ---------------------------------------------------------


def test_generated_hook_file_passes_the_mind_servers_hook_check(mind, monkeypatch):
    from minds import mind_server

    monkeypatch.setattr(mind_server.ADAPTERS["dsh"], "hooks_config",
                        lambda: str(mind / "hooks.json"))

    assert mind_server._hooks_check("dsh") is None


def test_failed_turn_path_finds_the_generated_rotation_check_in_the_container():
    config = dsh_hooks.hooks_config("/usr/src/app/minds/probe/.dsh/hooks")

    assert failed_turn.rotation_check_command(config["hooks"]["Stop"]) == [
        "/opt/venv/bin/python", "/usr/src/app/minds/probe/.dsh/hooks/rotation_check.py",
    ]


def test_second_run_rewrites_scripts_and_merges_only_managed_rotation_hooks(tmp_path):
    dsh_hooks.write_hooks("probe", tmp_path)
    home = tmp_path / "probe/.dsh"
    custom = {"custom": {"keep": True}, "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "custom-stop"}]}],
        "UserPromptSubmit": [],
    }}
    (home / "hooks.json").write_text(json.dumps(custom) + "\n")
    (home / "hooks/rotation_check.py").write_text("stale\n")

    report = dsh_hooks.write_hooks("probe", tmp_path)

    installed = json.loads((home / "hooks.json").read_text())
    assert installed["custom"] == {"keep": True}
    stop_commands = [hook["command"] for group in installed["hooks"]["Stop"]
                     for hook in group.get("hooks", [])]
    assert "custom-stop" in stop_commands
    assert sum("rotation_check.py" in command for command in stop_commands) == 1
    assert (home / "hooks/rotation_check.py").read_text() == (
        dsh_hooks.SCRIPTS_DIR / "rotation_check.py").read_text()
    assert report["hooks_config_written"] is True


# --- rotation_check --------------------------------------------------------


def test_a_turn_over_the_threshold_is_reported_and_armed(mind, service, monkeypatch):
    seen, replies = service
    replies["/sessions/arm-rotation"] = {"ok": True, "session_id": "row-1"}
    monkeypatch.setenv("HIVE_ROTATION_THRESHOLD_TOKENS", "100")
    _save_rotation_policy(mind, 10)
    rotation_check = importlib.import_module("rotation_check")
    event = {"session_id": "conv-1",
             "context": {"tokens": 150, "kind": "usage", "model": "m", "window": 1000}}

    assessed = rotation_check.assess(event)
    rotation_check.arm(assessed)

    report, arm = seen
    assert (report["path"], report["auth"]) == ("/sessions/context", "Bearer comms-key")
    assert report["body"]["tokens"] == 150 and report["body"]["threshold"] == 100
    assert arm["path"] == "/sessions/arm-rotation"
    assert arm["body"]["conversation_id"] == "conv-1"
    assert arm["body"]["client_ref"] == "chat-1"


def test_generated_dsh_goal_check_uses_the_latest_saved_percentage(
    mind, service, monkeypatch, tmp_path,
):
    """A long-lived harness env cannot pin the percentage saved at spawn."""
    from minds import failed_turn

    runtime_path = _save_rotation_policy(mind, 40)
    monkeypatch.setenv("HIVE_RUNTIME_PATH", str(runtime_path))
    monkeypatch.setenv("HIVE_HARNESS", "dsh")
    monkeypatch.setenv("HIVE_MODEL", "model-a")
    monkeypatch.setenv("HIVE_MODEL_CONTEXT_WINDOW", "100000")
    monkeypatch.setenv("HIVE_ROTATION_THRESHOLD_TOKENS", "75000")
    monkeypatch.setenv("HIVE_GOAL_RUN", "1")
    monkeypatch.setenv(
        "HIVE_MIND_APP_ROOT", str(Path(__file__).resolve().parents[2])
    )
    config = json.loads((mind / "hooks.json").read_text())
    command = failed_turn.rotation_check_command(config["hooks"]["Stop"])
    assert command is not None
    command[0] = sys.executable
    result = subprocess.run(
        command,
        input=json.dumps({"session_id": "conv-live", "goal": True,
                          "context": {"tokens": 60_000, "kind": "usage",
                                      "model": "model-a", "window": 100_000}}),
        check=False, text=True, capture_output=True, env=os.environ.copy(), timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"rotate": True}


def test_a_turn_refused_for_size_is_reported_and_never_armed(mind, service, monkeypatch):
    seen, _ = service
    monkeypatch.setenv("HIVE_ROTATION_THRESHOLD_TOKENS", "100")
    _save_rotation_policy(mind, 10)
    rotation_check = importlib.import_module("rotation_check")

    assessed = rotation_check.assess({
        "session_id": "conv-1", "stop_reason": "error",
        "error_code": "CONTEXT_WINDOW_EXCEEDED",
        "context": {"tokens": 900, "kind": "usage", "model": "m"}})

    assert assessed["rotate"] is False
    assert [r["path"] for r in seen if r["path"] == "/sessions/context"] == [
        "/sessions/context"
    ]
    assert not [r for r in seen if r["path"] == "/sessions/arm-rotation"]


def test_a_failed_turn_is_sized_from_the_last_count_plus_what_it_added(mind, tmp_path):
    rotation_check = importlib.import_module("rotation_check")
    added = {"type": "user", "message": {"content": "x" * 400}}
    entries = [{"type": "context", "context": {"tokens": 1000, "kind": "usage"}},
               {"type": "turn_start"}, added]

    measured = rotation_check.measure({"stop_reason": "error"}, entries)

    assert measured["kind"] == "estimated"
    assert measured["tokens"] == 1000 + len(json.dumps(added["message"]["content"])) // 4


def test_a_terminal_arm_is_staged_where_the_gateway_drops_it(mind, service, monkeypatch):
    from minds import rotation_marker_api

    _, replies = service
    replies["/sessions/arm-rotation"] = {"ok": True}
    monkeypatch.setenv("HIVE_ROTATION_THRESHOLD_TOKENS", "100")
    _save_rotation_policy(mind, 10)
    monkeypatch.setenv("HIVE_SURFACE", "terminal")
    rotation_check = importlib.import_module("rotation_check")
    rotation_check.arm(rotation_check.assess(
        {"session_id": "conv-1", "context": {"tokens": 150, "kind": "usage"}}))
    staged = mind.parent / "data/auto-remember/rotation-staged-conv-1.json"
    assert json.loads(staged.read_text())["state"] == "ready"

    rotation_marker_api.drop_markers(mind.parent, "conv-1")

    assert not staged.exists()


# --- rotation_fire ---------------------------------------------------------


def test_a_typed_prompt_fires_the_staged_rotation_with_that_prompt(mind, service):
    seen, replies = service
    replies["/sessions/fire-rotation"] = {"ok": True, "rotated": True}
    env = importlib.import_module("hive_mind_env")
    rotation_fire = importlib.import_module("rotation_fire")
    env.write_marker("conv-1", state="ready", staged_at=__import__("time").time(),
                     client_type="terminal", client_ref="pane-1")

    marker = rotation_fire.claim("conv-1")
    rotation_fire.fire(marker, "conv-1", "carry on")

    assert seen[-1]["path"] == "/sessions/fire-rotation"
    assert seen[-1]["body"] == {"client_type": "terminal", "client_ref": "pane-1",
                                "conversation_id": "conv-1", "prompt": "carry on"}
    assert env.read_marker("conv-1") is None


def test_a_claimed_rotation_cannot_be_claimed_by_a_second_prompt(mind):
    env = importlib.import_module("hive_mind_env")
    rotation_fire = importlib.import_module("rotation_fire")
    env.write_marker("conv-1", state="ready", staged_at=__import__("time").time())

    assert rotation_fire.claim("conv-1") is not None
    assert rotation_fire.claim("conv-1") is None


def test_a_prompt_the_harness_submitted_does_not_fire(mind):
    rotation_fire = importlib.import_module("rotation_fire")

    assert rotation_fire.typed({"prompt": "hi", "prompt_sources": ["user"]}) is True
    assert rotation_fire.typed({"prompt": "hi", "prompt_sources": ["user", "hook"]}) is False


# --- auto_remember ---------------------------------------------------------


def test_a_turn_is_classified_by_the_proxy_with_the_one_key_and_stored_as_this_minds_memory(
        mind, service, tmp_path):
    seen, replies = service
    verdict = {
        "feedback": {"present": True, "content": "Answer tersely.", "reason": ""},
        "person": {"present": False}, "tech": {"present": True, "data_class": "future-state",
                                               "prose": "The hive moves to v2.",
                                               "fits_schema": False},
        "self": {"present": True}}
    replies["/v1/chat/completions"] = {
        "choices": [{"message": {"role": "assistant", "content": json.dumps(verdict)}}]}
    replies["/memory/store"] = {"id": "mem-1"}
    auto_remember = importlib.import_module("auto_remember")
    transcript = _transcript(tmp_path / "t.jsonl", [
        {"type": "user", "message": {"content": [{"type": "text", "text": "be terse"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}])

    stored = auto_remember.capture({"session_id": "conv-1", "transcript_path": str(transcript)})

    asked = [r for r in seen if r["path"] == "/v1/chat/completions"]
    assert len(asked) == 1
    assert asked[0]["auth"] == "Bearer proxy-key"
    assert asked[0]["body"]["model"] == "classifier-of-record"
    assert asked[0]["body"]["response_format"]["type"] == "json_schema"
    assert not [r for r in seen if r["path"].startswith("/ollama")]
    writes = [r["body"] for r in seen if r["path"] == "/memory/store"]
    assert stored == ["mem-1", "mem-1"]
    assert [(w["data_class"], w["content"]) for w in writes] == [
        ("feedback", "Answer tersely."), ("future-state", "The hive moves to v2.")]
    assert all(w["mind_id"] == "mind-uuid" and w["tier"] == "contextual" for w in writes)


def test_a_person_is_remembered_only_when_the_name_resolves(mind):
    auto_remember = importlib.import_module("auto_remember")
    verdict = {"person": {"present": True, "name": "Gary", "prose": "Gary carves wood.",
                          "fits_schema": False}}

    assert auto_remember.chunks(verdict, lambda name: True) == [
        ("person", "current-state", "[About Gary] Gary carves wood.")]
    assert auto_remember.chunks(verdict, lambda name: False) == []


# --- contextual_retrieval --------------------------------------------------


def test_standing_rules_come_from_the_minds_own_memory_service(mind, service):
    seen, replies = service
    replies["/memory/list"] = {"entries": [{"content": "Never guess.", "mind_id": "mind-uuid"},
                                           {"content": "Not mine.", "mind_id": "other"}]}
    contextual_retrieval = importlib.import_module("contextual_retrieval")

    injection = contextual_retrieval.format_injection("what now?")

    assert "<standing-rules>\n- Never guess.\n</standing-rules>" in injection
    assert "Not mine." not in injection
    assert seen[0]["auth"] == "Bearer lucent-key"
