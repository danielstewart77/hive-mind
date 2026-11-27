"""The board tool's moves, conversions and deletes, against a console the
test serves itself.

The tool is imported and its commands are run for real; only the console is
a stand-in, a loopback server that keeps a small board and records every
request it is sent. What is asserted is what the tool sent and what it
printed.
"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[2]
           / "tools/stateless/board/board.py")
_spec = importlib.util.spec_from_file_location("board_tool", _SCRIPT)
board = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(board)

_BOARD = {
    "epics": [{"id": "e1", "title": "Epic one"},
              {"id": "e2", "title": "Epic two"}],
    "features": [{"id": "f1", "title": "Feature one", "epic_id": "e1"},
                 {"id": "f2", "title": "Feature two", "epic_id": "e1"}],
    "stories": [{"id": "s1", "title": "Story", "feature_id": "f1"},
                {"id": "s2", "title": "Under a story", "story_id": "s1"}],
    "tasks": [{"id": "t1", "title": "Task", "parent_id": "s1"}],
}
#: The rows that have something under them, which the console will not
#: delete when asked to spare them.
_PARENTS = {"f1": "This item has 2 stories, 1 task under it"}
_NOTHING = {"features": 0, "stories": 0, "tasks": 0, "attachments": 0}


class _Console(BaseHTTPRequestHandler):
    def log_message(self, *args):
        del args

    def _answer(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.seen.append((self.command, self.path, body))
        return body

    def do_GET(self):
        self._record()
        if self.path == "/api/kanban/data":
            return self._answer(_BOARD)
        if self.path == "/api/kanban/numbers":
            return self._answer({})
        return self._answer(None)

    def do_PATCH(self):
        body = self._record()
        self._answer({"id": self.path.rsplit("/", 1)[-1], **(body or {})})

    def do_POST(self):
        body = self._record()
        self._answer({"id": "new", **(body or {})})

    def do_DELETE(self):
        self._record()
        path, _, query = self.path.partition("?")
        item = path.rsplit("/", 1)[-1]
        if query == "only_if_empty=true" and item in _PARENTS:
            data = json.dumps({"detail": _PARENTS[item]}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._answer({"id": item, "removed": _NOTHING})


@pytest.fixture
def console(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Console)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("HIVE_SITE_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("MIND_KEY", "a-board-token")
    yield server
    server.shutdown()
    server.server_close()


def _writes(server):
    return [call for call in server.seen if call[0] != "GET"]


def _run(capsys, *argv):
    code = board.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_a_story_moved_under_another_feature_is_given_that_feature_as_its_parent(
        console, capsys):
    code, _ = _run(capsys, "move", "s1", "--under", "f2")

    assert code == 0
    assert _writes(console) == [
        ("PATCH", "/api/kanban/stories/s1", {"parent_id": "f2"})]


def test_a_feature_moved_under_another_epic_is_given_that_epic_as_its_parent(
        console, capsys):
    code, _ = _run(capsys, "move", "f1", "--under", "e2")

    assert code == 0
    assert _writes(console) == [
        ("PATCH", "/api/kanban/features/f1", {"parent_id": "e2"})]


def test_a_move_under_a_row_the_board_does_not_have_changes_nothing(
        console, capsys):
    code, answer = _run(capsys, "move", "s1", "--under", "nope")

    assert code == 1
    assert "nope" in answer["error"]
    assert _writes(console) == []


def test_a_task_is_converted_to_a_story_in_place(console, capsys):
    code, _ = _run(capsys, "convert", "t1", "--to", "story")

    assert code == 0
    assert _writes(console) == [
        ("PATCH", "/api/kanban/tasks/t1", {"type": "story"})]


def test_a_story_is_converted_to_a_task_in_place(console, capsys):
    code, _ = _run(capsys, "convert", "s1", "--to", "task")

    assert code == 0
    assert _writes(console) == [
        ("PATCH", "/api/kanban/stories/s1", {"type": "task"})]


def test_a_delete_asks_the_console_to_spare_a_row_with_rows_under_it(
        console, capsys):
    code, answer = _run(capsys, "delete", "t1")

    assert code == 0
    assert answer["id"] == "t1"
    assert _writes(console) == [
        ("DELETE", "/api/kanban/tasks/t1?only_if_empty=true", None)]


def test_a_delete_the_console_spared_is_reported_as_refused_with_its_reason(
        console, capsys):
    code, answer = _run(capsys, "delete", "f1")

    assert code == 1
    assert answer["kind"] == "Refused"
    assert "2 stories, 1 task under it" in answer["error"]


def test_a_delete_told_to_cascade_does_not_ask_for_the_row_to_be_spared(
        console, capsys):
    code, answer = _run(capsys, "delete", "f1", "--cascade")

    assert code == 0
    assert answer["id"] == "f1"
    assert _writes(console) == [("DELETE", "/api/kanban/features/f1", None)]


def test_the_rows_under_a_story_are_listed_whatever_kind_they_are(
        console, capsys):
    code, answer = _run(capsys, "list", "--parent", "s1")

    assert code == 0
    assert sorted((row["id"], row["kind"]) for row in answer["items"]) == [
        ("s2", "story"), ("t1", "task")]


def test_a_task_is_created_under_the_parent_it_names(console, capsys):
    code, _ = _run(capsys, "create", "task", "--title", "New",
                   "--parent", "s1")

    assert code == 0
    assert _writes(console) == [
        ("POST", "/api/kanban/tasks", {"title": "New", "parent_id": "s1"})]
