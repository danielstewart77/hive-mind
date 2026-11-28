"""The key-migration command asks the console and prints what it is told.

The command is imported and run for real; only the console is a stand-in, a
loopback server that records every request. What is asserted is what the
command sent (the operator's bearer, to the console's routes only) and what it
printed, including a refused retirement and its warning.
"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "tools/stateless/key_migration/key_migration.py"
_spec = importlib.util.spec_from_file_location("key_migration_tool", _SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

OPERATOR = "operator-bearer-0123456789"  # secret-guard: allow - invented fixture


@pytest.fixture
def console(monkeypatch):
    seen: list[tuple[str, str, str]] = []
    answers: dict[tuple[str, str], tuple[int, dict]] = {}

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method):
            seen.append((method, self.path, self.headers.get("Authorization", "")))
            status, body = answers.get((method, self.path), (404, {"detail": "no route"}))
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._answer("GET")

        def do_POST(self):
            self._answer("POST")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("HIVE_SITE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("HIVE_OPERATOR_KEY", OPERATOR)
    yield seen, answers
    server.shutdown()


def test_a_move_asks_the_consoles_route_with_the_operators_bearer_and_prints_its_answer(console, capsys):
    seen, answers = console
    answers[("POST", "/api/settings/key-migration/ada/move")] = (200, {"mind": "ada", "next_step": "Recreate"})

    assert tool.main(["move", "Ada"]) == 0

    assert seen == [("POST", "/api/settings/key-migration/ada/move", f"Bearer {OPERATOR}")]
    assert json.loads(capsys.readouterr().out)["mind"] == "ada"


def test_a_refused_retirement_exits_1_lists_the_minds_not_moved_and_says_what_retiring_ends(console, capsys):
    seen, answers = console
    answers[("POST", "/api/settings/key-migration/retire")] = (
        409, {"retired": False, "blocking": ["bob is not moved: another machine"]})

    assert tool.main(["retire"]) == 1

    printed = json.loads(capsys.readouterr().out)
    assert printed["blocking"] == ["bob is not moved: another machine"]
    assert "going back a release" in printed["notice"]


def test_a_successful_retirement_prints_the_going_back_warning(console, capsys):
    seen, answers = console
    answers[("POST", "/api/settings/key-migration/retire")] = (200, {"retired": True})

    assert tool.main(["retire"]) == 0

    assert "going back a release" in json.loads(capsys.readouterr().out)["notice"]


def test_without_the_operators_credential_nothing_is_sent(console, monkeypatch, capsys):
    seen, answers = console
    monkeypatch.delenv("HIVE_OPERATOR_KEY")

    assert tool.main(["report"]) == 1

    assert seen == []
    assert "HIVE_OPERATOR_KEY" in json.loads(capsys.readouterr().out)["error"]


def test_an_undo_asks_the_undo_route_and_a_tools_credential_the_tools_credential_route(console, capsys):
    seen, answers = console
    answers[("POST", "/api/settings/key-migration/ada/undo")] = (200, {"mind": "ada"})
    answers[("POST", "/api/settings/key-migration/tools-credential/telegram-bot")] = (200, {"holder": "telegram-bot"})

    assert tool.main(["undo", "ada"]) == 0
    assert tool.main(["tools-credential", "Telegram-Bot"]) == 0

    assert [(m, p) for m, p, _ in seen] == [
        ("POST", "/api/settings/key-migration/ada/undo"),
        ("POST", "/api/settings/key-migration/tools-credential/telegram-bot")]


def test_a_401_from_the_console_is_reported_as_a_refused_credential_not_a_crash(console, capsys):
    seen, answers = console
    answers[("GET", "/api/settings/key-migration")] = (401, {"detail": "Authentication required"})

    assert tool.main(["report"]) == 1

    assert "refused" in json.loads(capsys.readouterr().out)["error"]
