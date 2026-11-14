# ruff: noqa: F811
"""A saved rotation preference reaches the next safe conversation boundary."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from comms_pg_fixture import comms_db, scratch_dsn  # noqa: F401 - scratch database fixtures
from fastapi import FastAPI
from fastapi.testclient import TestClient

from minds import runtime_api
from minds.harness import rotation_hooks
from tests.test_context_window import (
    DONE,
    GRILL_ADMIN,
    MIND,
    OVERFLOW,
    Mind,
    _measured,
    _register,
    _run,
    _run_in,
    _seed,
    _soul,
    execute,
    fetchrow,
)

MIND_ADMIN = "mind-admin-for-rotation-tests"
APP_ROOT = Path(__file__).resolve().parents[2]


class _PausedLines:
    def __init__(self, lines: list[bytes], started: threading.Event, release: asyncio.Event):
        self._lines = list(lines)
        self._started = started
        self._release = release
        self._paused = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._paused:
            self._paused = True
            self._started.set()
            await self._release.wait()
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _ObservedResponse:
    """Observe a real row at the response boundary without replacing Mind I/O."""

    def __init__(self, response, observe):
        self._response = response
        self._observe = observe

    async def __aenter__(self):
        await self._response.__aenter__()
        await self._observe()
        return self

    async def __aexit__(self, *exc):
        return await self._response.__aexit__(*exc)

    def __getattr__(self, name):
        return getattr(self._response, name)


class _TransitionMind(Mind):
    """External Mind transport with one held turn and observed successor spawn."""

    def __init__(self, manager, release: asyncio.Event, *, hold_first: bool):
        super().__init__(turns=[
            [
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "The current turn finished."},
                ]}},
                DONE,
            ],
            [
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "The successor answered."},
                ]}},
                DONE,
            ],
        ])
        self.manager = manager
        self.release = release
        self.hold_first = hold_first
        self.message_started = threading.Event()
        self.spawn_rows: list[dict] = []
        self.rotate_results: list[tuple[int, dict]] = []

    def _answer(self, method, url, params=None, json_body=None):
        response = super()._answer(method, url, params, json_body)
        path = urlsplit(url).path
        if method == "POST" and path.endswith("/message") and self.hold_first:
            self.hold_first = False
            response.content = _PausedLines(
                response.content._lines, self.message_started, self.release
            )
        if (method == "POST" and path == "/sessions"
                and (json_body or {}).get("resume_sid") != "conv-1"):
            async def observe():
                self.spawn_rows.append(await self.manager._get_row("sess-1"))

            return _ObservedResponse(response, observe)
        if method == "POST" and path.endswith("/rotate-pty") and self.rotate_results:
            response.status, response._body = self.rotate_results.pop(0)
        return response


@contextmanager
def _forward_comms_to_test_client(client: TestClient):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            headers = {"Content-Type": "application/json"}
            authorization = self.headers.get("Authorization")
            if authorization:
                headers["Authorization"] = authorization
            try:
                response = client.post(self.path, content=body, headers=headers)
                payload = response.content
                self.send_response(response.status_code)
            except Exception as exc:  # noqa: BLE001 — surface bridge errors to the real hook process
                payload = json.dumps({"error": type(exc).__name__}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@contextmanager
def _rotation_system(tmp_path: Path, monkeypatch, *, hold_first: bool = False):
    """Use the real comms routes/DB and runtime settings route around transport edges."""
    import importlib

    from comms import server as server_module

    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", GRILL_ADMIN)
    monkeypatch.setenv("MIND_ADMIN_TOKEN", MIND_ADMIN)
    for key in (
        "INFERENCE_PROXY_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
        "MIND_PROXY_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    importlib.reload(server_module)
    runtime_path = tmp_path / "runtime.yaml"
    runtime_path.write_text(
        "name: ada\nmind_id: rotation-test\ngateway_url: http://mind.test:8420\n"
        "harness: claude_cli\nprovider: anthropic\ndefault_model: claude-opus-5\n"
        "model_context_window: 200000\nrotation_threshold_percent: 75\n"
    )
    mind_app = FastAPI()
    runtime_api.install_runtime_routes(
        mind_app, path=runtime_path, mind_id="rotation-test",
        log=logging.getLogger("rotation-preference-test"),
    )

    with (
        TestClient(server_module.app,
                   headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as gateway,
        TestClient(mind_app) as mind_client,
    ):
        manager = server_module.session_mgr
        _run(_register())
        _run(_seed(manager))
        _run(_measured(manager, tokens=30_000, window=200_000, threshold=150_000))
        _run_in(gateway, manager.set_session_name(
            "sess-1", name="Rotation continuity", color="#3481cc"
        ))
        release = gateway.portal.call(asyncio.Event)
        mind = _TransitionMind(manager, release, hold_first=hold_first)
        with mind.wired(), _soul():
            try:
                yield {
                    "gateway": gateway,
                    "manager": manager,
                    "mind": mind,
                    "mind_client": mind_client,
                    "runtime_path": runtime_path,
                    "release": release,
                }
            finally:
                manager._procs.clear()


def _save_40(mind_client: TestClient):
    response = mind_client.patch(
        "/runtime/settings",
        json={"settings": {"rotation_threshold_percent": 40}},
        headers={"Authorization": f"Bearer {MIND_ADMIN}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rotation_threshold_percent"] == 40


def _save_100(mind_client: TestClient):
    response = mind_client.patch(
        "/runtime/settings",
        json={"settings": {"rotation_threshold_percent": 100}},
        headers={"Authorization": f"Bearer {MIND_ADMIN}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["rotation_threshold_percent"] == 100


def _generated_claude_commands(system, tmp_path: Path):
    home = tmp_path / ".claude"
    home.mkdir(exist_ok=True)
    installed = rotation_hooks.write_harness_hooks(
        "claude", home, runtime_path=system["runtime_path"],
        python=sys.executable, app_root=APP_ROOT,
    )
    assert installed["installed"] is True
    config = json.loads((home / "settings.json").read_text())
    return {
        event: shlex.split(config["hooks"][event][0]["hooks"][0]["command"])
        for event in ("Stop", "UserPromptSubmit")
    }


def _hook_environment(
    system, comms_url: str, *, hive_surface="telegram", surface="telegram",
    client_ref="123", state_dir: Path | None = None,
):
    environment = {
        **os.environ,
        "COMMS_URL": comms_url,
        "COMMS_BEARER_TOKEN": GRILL_ADMIN,
        "SURFACE": surface,
        "CLIENT_REF": client_ref,
        "HIVE_SURFACE": hive_surface,
        "HIVE_HARNESS": "claude",
        "HIVE_MODEL": "claude-opus-5",
        "HIVE_MODEL_CONTEXT_WINDOW": "200000",
        # This is the process's stale 75% spawn-time threshold. The generated
        # check must read the newly saved 40% preference on disk instead.
        "HIVE_ROTATION_THRESHOLD_TOKENS": "150000",
    }
    if state_dir is not None:
        environment["AUTO_REMEMBER_LOG_DIR"] = str(state_dir)
    return environment


def _run_generated_claude_check(
    system, tmp_path: Path, comms_url: str, *, environment=None,
):
    command = _generated_claude_commands(system, tmp_path)["Stop"]
    transcript = tmp_path / "conversation.jsonl"
    transcript.write_text("\n".join(json.dumps(row) for row in (
        {"type": "user", "message": {"content": "continue"}},
        {"type": "assistant", "message": {
            "model": "claude-opus-5",
            "usage": {"input_tokens": 119_000, "output_tokens": 1_000},
            "content": [],
        }},
    )) + "\n")
    event = {
        "session_id": "conv-1", "model": "claude-opus-5",
        "transcript_path": str(transcript),
    }
    environment = environment or _hook_environment(system, comms_url)
    result = subprocess.run(
        command, check=False, input=json.dumps(event), text=True,
        capture_output=True, cwd=APP_ROOT, env=environment, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result


def _run_generated_claude_prompt(
    system, tmp_path: Path, *, prompt: str, environment,
):
    command = _generated_claude_commands(system, tmp_path)["UserPromptSubmit"]
    result = subprocess.run(
        command,
        check=False,
        input=json.dumps({"session_id": "conv-1", "prompt": prompt}),
        text=True, capture_output=True, cwd=APP_ROOT, env=environment, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result


def _send(client: TestClient, content: str):
    return client.post("/sessions/sess-1/message", json={"content": content})


def test_saving_percentage_during_turn_does_not_interrupt_it(
    comms_db, tmp_path, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor

    with _rotation_system(tmp_path, monkeypatch, hold_first=True) as system:
        gateway = system["gateway"]
        mind = system["mind"]
        manager = system["manager"]
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(_send, gateway, "finish the current turn")
            assert mind.message_started.wait(timeout=5)
            try:
                _save_40(system["mind_client"])
                assert not pending.done()
                row = _run_in(gateway, manager._get_row("sess-1"))
                assert row["conversation_id"] == "conv-1"
                assert row["rotation_armed"] == 0
                assert mind.kills("sess-1") == []
            finally:
                gateway.portal.call(system["release"].set)
            response = pending.result(timeout=10)

        assert response.status_code == 200
        assert "The current turn finished." in response.text
        turns = _run_in(gateway, manager.get_session_turns("sess-1"))
        assert any(turn["content"] == "The current turn finished." for turn in turns)
        row = _run_in(gateway, manager._get_row("sess-1"))
        assert row["conversation_id"] == "conv-1"
        assert mind.kills("sess-1") == []


def test_threshold_check_arms_without_replacing_until_next_user_turn(
    comms_db, tmp_path, monkeypatch,
):
    with _rotation_system(tmp_path, monkeypatch) as system:
        gateway = system["gateway"]
        manager = system["manager"]
        mind = system["mind"]
        assert _send(gateway, "finish before checking").status_code == 200
        _save_40(system["mind_client"])
        with _forward_comms_to_test_client(gateway) as comms_url:
            _run_generated_claude_check(system, tmp_path, comms_url)

        row = _run_in(gateway, manager._get_row("sess-1"))
        assert row["context_tokens"] == 120_000
        assert (row["context_window"], row["context_threshold"]) == (200_000, 80_000)
        assert row["rotation_armed"] == 1
        assert row["conversation_id"] == "conv-1"
        assert mind.kills("sess-1") == []

        response = _send(gateway, "start the successor turn")
        row = _run_in(gateway, manager._get_row("sess-1"))
        assert response.status_code == 200
        assert row["conversation_id"] != "conv-1"
        assert row["rotation_armed"] == 0


def test_threshold_handover_preserves_ledger_and_successor_user_prompt(
    comms_db, tmp_path, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor

    with _rotation_system(tmp_path, monkeypatch, hold_first=True) as system:
        gateway = system["gateway"]
        manager = system["manager"]
        mind = system["mind"]
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(_send, gateway, "finish before handover")
            assert mind.message_started.wait(timeout=5)
            try:
                _save_40(system["mind_client"])
                assert not pending.done()
                assert _run_in(gateway, manager._get_row("sess-1"))["conversation_id"] == "conv-1"
            finally:
                gateway.portal.call(system["release"].set)
            assert pending.result(timeout=10).status_code == 200

        with _forward_comms_to_test_client(gateway) as comms_url:
            _run_generated_claude_check(system, tmp_path, comms_url)
        armed = _run_in(gateway, manager._get_row("sess-1"))
        assert armed["rotation_armed"] == 1
        assert armed["conversation_id"] == "conv-1"

        prompt = "continue exactly once"
        response = _send(gateway, prompt)
        row = _run_in(gateway, manager._get_row("sess-1"))
        turns = _run_in(gateway, manager.get_session_turns("sess-1"))
        owner = _run(fetchrow(
            "SELECT owner FROM core.minds WHERE mind_id = $1", MIND
        ))
        count = _run(fetchrow("SELECT COUNT(*) AS n FROM core.sessions"))
        successor_rows = [
            value for value in mind.spawn_rows
            if value and value["conversation_id"] != "conv-1"
        ]

        assert response.status_code == 200
        assert row["id"] == "sess-1"
        assert row["mind_id"] == MIND
        assert (row["name"], row["color"]) == ("Rotation continuity", "#3481cc")
        assert owner["owner"] == "daniel"
        assert count["n"] == 1
        assert row["conversation_id"] != "conv-1"
        assert row["rotation_armed"] == 0
        assert len(successor_rows) == 1
        successor = successor_rows[0]
        assert successor["id"] == "sess-1"
        assert successor["name"] == "Rotation continuity"
        assert successor["color"] == "#3481cc"
        assert successor["context_threshold"] == 80_000
        assert successor["carry_forward_sid"] == successor["conversation_id"]
        assert "HANDOVER: the old conversation" in successor["carry_forward"]
        prompt_requests = [
            request for request in mind.messages()
            if prompt in request["json"]["content"]
        ]
        assert len(prompt_requests) == 1
        assert prompt_requests[0]["json"]["content"].count(prompt) == 1
        assert [turn["content"] for turn in turns].count(prompt) == 1
        assert any(turn["content"] == "The successor answered." for turn in turns)


def test_failed_terminal_fire_keeps_ready_marker_and_retry_hands_over(
    comms_db, tmp_path, monkeypatch,
):
    with _rotation_system(tmp_path, monkeypatch) as system:
        gateway = system["gateway"]
        manager = system["manager"]
        mind = system["mind"]
        state_dir = tmp_path / "hook-state"
        monkeypatch.setenv("AUTO_REMEMBER_LOG_DIR", str(state_dir))
        _run(execute(
            "UPDATE core.sessions SET terminal_held = true WHERE id = 'sess-1'"
        ))
        _save_40(system["mind_client"])

        environment = _hook_environment(
            system, "", hive_surface="terminal", state_dir=state_dir,
        )
        with _forward_comms_to_test_client(gateway) as comms_url:
            environment["COMMS_URL"] = comms_url
            _run_generated_claude_check(
                system, tmp_path, comms_url, environment=environment,
            )
            marker = state_dir / "rotation-staged-conv-1.json"
            assert json.loads(marker.read_text())["state"] == "ready"

            mind.rotate_results.extend([
                (500, {"error": "temporary Mind refusal"}),
                (200, {"rotated": True}),
            ])
            prompt = "retry the terminal handoff"
            _run_generated_claude_prompt(
                system, tmp_path, prompt=prompt, environment=environment,
            )
            failed_row = _run_in(gateway, manager._get_row("sess-1"))
            assert failed_row["conversation_id"] == "conv-1"
            assert failed_row["rotation_armed"] == 1
            assert json.loads(marker.read_text())["state"] == "ready"

            _run_generated_claude_prompt(
                system, tmp_path, prompt=prompt, environment=environment,
            )

        row = _run_in(gateway, manager._get_row("sess-1"))
        owner = _run(fetchrow(
            "SELECT owner FROM core.minds WHERE mind_id = $1", MIND
        ))
        turns = _run_in(gateway, manager.get_session_turns("sess-1"))
        calls = mind.sent("POST", "/sessions/sess-1/rotate-pty")

        assert not marker.exists()
        assert len(calls) == 2
        assert calls[-1]["json"]["user_prompt"].endswith(prompt)
        assert row["id"] == "sess-1"
        assert row["mind_id"] == MIND
        assert row["conversation_id"] != "conv-1"
        assert row["rotation_armed"] == 0
        assert row["carry_forward_sid"] == row["conversation_id"]
        assert row["carry_forward"].count(prompt) == 1
        assert "HANDOVER: the old conversation" in row["carry_forward"]
        assert (row["name"], row["color"]) == ("Rotation continuity", "#3481cc")
        assert owner["owner"] == "daniel"
        assert any(turn["content"] == "an earlier turn" for turn in turns)


def _report_a_default_full_threshold(system, tmp_path: Path, gateway: TestClient):
    """Use the generated check so the real mind API reports its effective100%."""
    _save_100(system["mind_client"])
    with _forward_comms_to_test_client(gateway) as comms_url:
        _run_generated_claude_check(system, tmp_path, comms_url)
    row = _run_in(gateway, system["manager"]._get_row("sess-1"))
    assert row["context_window"] == 200_000
    assert row["context_threshold"] == 200_000
    assert row["rotation_armed"] == 0


def test_full_percentage_overflow_rotates_and_resends_once(
    comms_db, tmp_path, monkeypatch,
):
    with _rotation_system(tmp_path, monkeypatch) as system:
        gateway = system["gateway"]
        manager = system["manager"]
        mind = system["mind"]
        _report_a_default_full_threshold(system, tmp_path, gateway)
        mind.turns = [[OVERFLOW], [DONE]]

        response = _send(gateway, "recover once")
        row = _run_in(gateway, manager._get_row("sess-1"))
        turns = _run_in(gateway, manager.get_session_turns("sess-1"))
        messages = mind.messages()

        assert response.status_code == 200
        assert len(mind.sent("POST", "/handover")) == 1
        assert len(messages) == 2
        assert messages[1]["json"]["content"].count("recover once") == 1
        assert row["id"] == "sess-1"
        assert row["conversation_id"] != "conv-1"
        assert [turn["content"] for turn in turns].count("recover once") == 1
        assert "ran out of room" in response.text


def test_repeated_overflow_at_full_percentage_reports_failure_without_second_rotation(
    comms_db, tmp_path, monkeypatch,
):
    with _rotation_system(tmp_path, monkeypatch) as system:
        gateway = system["gateway"]
        manager = system["manager"]
        mind = system["mind"]
        _report_a_default_full_threshold(system, tmp_path, gateway)
        mind.turns = [[OVERFLOW], [OVERFLOW]]

        response = _send(gateway, "cannot fit twice")
        row = _run_in(gateway, manager._get_row("sess-1"))

        assert response.status_code == 200
        assert len(mind.sent("POST", "/handover")) == 1
        assert len(mind.messages()) == 2
        assert row["id"] == "sess-1"
        assert row["conversation_id"] != "conv-1"
        assert '"is_error": true' in response.text
        assert "ran out of room again" in response.text

