"""The roles tool's commands, against a console the test serves itself.

The tool is imported and its commands are run for real; only the console is
a stand-in, a loopback server that keeps a small list of roles and records
every request it is sent, with the bearer that came with it. What is asserted
is what the tool sent and what it printed.
"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[2]
           / "tools/stateless/roles/roles.py")
_spec = importlib.util.spec_from_file_location("roles_tool", _SCRIPT)
roles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(roles)

_TOKEN = "an-operator-token"
_ROLES = [
    {"id": 1, "slug": "general", "name": "General", "skills": []},
    {"id": 7, "slug": "developer", "name": "Developer", "skills": ["code"]},
    {"id": 2024, "slug": "2024", "name": "Year", "skills": []},
    {"id": None, "slug": "repo-only", "name": "Repo only"},
]


class _Console(BaseHTTPRequestHandler):
    def log_message(self, *args):
        del args

    def _answer(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.seen.append(
            (self.command, self.path, body,
             self.headers.get("Authorization")))
        if self.headers.get("Authorization") != f"Bearer {_TOKEN}":
            return self._answer({"detail": "Authentication required"}, 401)
        if self.command == "GET" and self.path == "/api/roles":
            return self._answer({"roles": _ROLES, "tools": []})
        if self.command == "DELETE" and self.path == "/api/roles/1":
            return self._answer({"detail": "The general role cannot be deleted"}, 409)
        return self._answer({"ok": True})

    do_GET = do_POST = do_PUT = do_DELETE = _handle


@pytest.fixture
def console(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Console)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("HIVE_SITE_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("ROLES_PUSH_TOKEN", _TOKEN)
    yield server
    server.shutdown()
    server.server_close()


def _writes(server):
    return [(m, p, b) for m, p, b, _ in server.seen if m != "GET"]


def _run(capsys, *argv):
    code = roles.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_every_request_carries_the_operators_bearer_and_the_token_is_never_printed(
        console, capsys):
    code = roles.main(["list"])
    printed = capsys.readouterr()

    assert code == 0
    assert {auth for *_, auth in console.seen} == {f"Bearer {_TOKEN}"}
    assert _TOKEN not in printed.out + printed.err


def test_a_refused_credential_is_reported_as_such_without_printing_the_token(
        console, capsys, monkeypatch):
    monkeypatch.setenv("ROLES_PUSH_TOKEN", "a-wrong-one")

    code, answer = _run(capsys, "list")

    assert code == 1
    assert answer["kind"] == "Refused"
    assert "refused the operator's roles credential" in answer["error"]
    assert "a-wrong-one" not in json.dumps(answer)


def test_an_unset_token_makes_no_request_and_says_what_is_missing(
        console, capsys, monkeypatch):
    monkeypatch.delenv("ROLES_PUSH_TOKEN")

    code, answer = _run(capsys, "list")

    assert code == 1
    assert "ROLES_PUSH_TOKEN" in answer["error"]
    assert console.seen == []


def test_a_role_is_created_with_the_grants_the_command_line_names(
        console, capsys):
    code, _ = _run(capsys, "create", "Reviewer", "--slug", "rev",
                   "--model", "a-model", "--skills", "read, write",
                   "--tools", "shell,files", "--hooks", "guard")

    assert code == 0
    assert _writes(console) == [("POST", "/api/roles", {
        "name": "Reviewer", "slug": "rev", "model": "a-model",
        "skills": ["read", "write"], "tools": ["shell", "files"],
        "hooks": ["guard"]})]


def test_a_role_made_with_only_a_name_sends_no_grants(console, capsys):
    code, _ = _run(capsys, "create", "Plain")

    assert code == 0
    assert _writes(console) == [("POST", "/api/roles", {"name": "Plain"})]


def test_an_update_sends_only_the_fields_it_was_given_to_the_role_by_slug(
        console, capsys):
    code, _ = _run(capsys, "update", "developer", "--name", "Dev",
                   "--skills", "code,plan")

    assert code == 0
    assert _writes(console) == [(
        "PUT", "/api/roles/7", {"name": "Dev", "skills": ["code", "plan"]})]


def test_an_update_can_clear_a_roles_model_and_an_empty_list_clears_a_list(
        console, capsys):
    code, _ = _run(capsys, "update", "7", "--clear-model", "--hooks", "")

    assert code == 0
    assert _writes(console) == [(
        "PUT", "/api/roles/7", {"model": None, "hooks": []})]


def test_an_update_that_changes_nothing_sends_nothing(console, capsys):
    code, answer = _run(capsys, "update", "developer")

    assert code == 1
    assert answer["kind"] == "Refused"
    assert _writes(console) == []


def test_a_skill_is_added_and_removed_one_at_a_time_never_by_replacing_the_list(
        console, capsys):
    added, _ = _run(capsys, "add-skill", "developer", "plan")
    removed, _ = _run(capsys, "remove-skill", "developer", "code")

    assert (added, removed) == (0, 0)
    assert _writes(console) == [
        ("POST", "/api/roles/7/skills", {"skill": "plan"}),
        ("DELETE", "/api/roles/7/skills/code", None)]


def test_a_numeric_reference_that_is_also_a_slug_finds_the_role_with_that_id_first(
        console, capsys):
    code, _ = _run(capsys, "delete", "2024")

    assert code == 0
    assert _writes(console) == [("DELETE", "/api/roles/2024", None)]


def test_a_role_that_does_not_exist_changes_nothing(console, capsys):
    code, answer = _run(capsys, "add-skill", "nope", "plan")

    assert code == 1
    assert "nope" in answer["error"]
    assert _writes(console) == []


def test_a_role_only_in_the_repo_is_not_written_to_until_it_is_taken(
        console, capsys):
    code, answer = _run(capsys, "add-skill", "repo-only", "plan")

    assert code == 1
    assert "take repo-only" in answer["error"]
    assert _writes(console) == []

    code, _ = _run(capsys, "take", "repo-only")

    assert code == 0
    assert _writes(console) == [("POST", "/api/roles/repo/repo-only/take", None)]


def test_keep_and_promote_go_to_the_roles_own_routes(console, capsys):
    _run(capsys, "keep", "developer")
    _run(capsys, "promote", "developer")

    assert _writes(console) == [
        ("POST", "/api/roles/7/keep", None),
        ("POST", "/api/roles/7/promote", None)]


def test_a_delete_the_console_refuses_is_reported_with_its_reason(console, capsys):
    code, answer = _run(capsys, "delete", "general")

    assert code == 1
    assert answer["kind"] == "Refused"
    assert "general role cannot be deleted" in answer["error"]


def test_show_and_catalog_read_through_the_console(console, capsys):
    code, shown = _run(capsys, "show", "developer")
    assert (code, shown["id"]) == (0, 7)

    code, _ = _run(capsys, "catalog")
    assert code == 0
    assert ("GET", "/api/roles/catalog") in [(m, p) for m, p, *_ in console.seen]


def test_a_malformed_command_line_exits_2_with_a_json_error(capsys):
    with pytest.raises(SystemExit) as stop:
        roles.main(["add-skill", "developer"])

    assert stop.value.code == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "UsageError"
