"""The setup page answers only a request carrying this run's code."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from installer.server import CODE_HEADER, SetupServer

from tests.unit.installer_helpers import answers, installer_at


def _call(server: SetupServer, method: str, path: str, code: str | None) -> int:
    data = b"{}" if method == "POST" else None
    headers = {"Content-Type": "application/json"}
    if code is not None:
        headers[CODE_HEADER] = code
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture
def pages(tmp_path):
    inst = installer_at(tmp_path, answers())
    previous = SetupServer(inst, host="127.0.0.1")
    previous.start()
    previous.stop()
    current = SetupServer(inst, host="127.0.0.1")
    current.start()
    yield previous, current
    try:
        current.stop()
    except Exception:
        pass


def test_every_route_turns_away_no_code_a_wrong_code_and_a_previous_runs_code(pages):
    previous, server = pages
    routes = [*SetupServer.ROUTES, ("GET", "/no-such-route")]
    for method, path in routes:
        for code in (None, "not-the-code", previous.code):
            assert _call(server, method, path, code) == 403, (method, path, code)


def test_every_route_answers_a_request_carrying_this_runs_code(pages):
    _, server = pages
    # Closing stops the page, so it is asked last.
    routes = sorted(SetupServer.ROUTES, key=lambda r: r[1] == "/api/close")
    for method, path in routes:
        assert _call(server, method, path, server.code) != 403, (method, path)
