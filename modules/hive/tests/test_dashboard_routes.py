"""The dashboard's routes: what they guard, and what they assemble.

`test_dashboard_data.py` covers the logic. These cover the two things only
a route can be wrong about — that the JSON behind the page is guarded as
tightly as the page (it carries context counts, host inventory and
subscription spend, so guarding the HTML alone leaves all of it open), and
that the assembled payload actually joins the three sources rather than
returning a shape that merely looks right.
"""

from __future__ import annotations

import asyncio
import os
import sys
import copy
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import dashboard_data
import main as main_mod


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


def _anonymous_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    return TestClient(main_mod.app)


LIVE = {
    "sessions": [
        {
            "session_id": "s1",
            "mind_id": "skippy",
            "model": "claude-opus-5",
            "started_at": 1.0,
            "quiet": False,
            "context": {
                "tokens": 150_000,
                "threshold": 300_000,
                "window": None,
                "observed_at": 100.0,
                "age_seconds": 12.0,
            },
        }
    ]
}
HOSTS = {"hosts": [{"host_id": "boot-1", "hostname": "workstation", "status": "reporting"}]}
PROVIDERS = {
    "providers": [
        {"name": "anthropic", "reporting": True,
         "windows": [{"label": "5h", "used_percent": 7.0}]},
        {"name": "openai", "reporting": False, "windows": []},
    ]
}
CATALOG = {"catalog": {"claude-opus-5": 1_000_000}}


@pytest.fixture()
def wired(monkeypatch):
    """The three upstreams stubbed, and every cache cleared between tests."""
    for snapshot in (
        main_mod._host_snapshot,
        main_mod._provider_snapshot,
        main_mod._catalog_snapshot,
    ):
        snapshot.invalidate()
    # A fresh copy per call: the route writes the resolved context back onto
    # the payload, which is correct against a real fetch and would otherwise
    # let one test's resolved window leak into the next.
    monkeypatch.setattr(
        main_mod, "_live_sessions", AsyncMock(side_effect=lambda: copy.deepcopy(LIVE))
    )
    monkeypatch.setattr(main_mod, "_fetch_hosts", AsyncMock(return_value=HOSTS))
    monkeypatch.setattr(
        main_mod, "_fetch_provider_limits", AsyncMock(return_value=PROVIDERS)
    )
    monkeypatch.setattr(main_mod, "_fetch_model_catalog", AsyncMock(return_value=CATALOG))
    main_mod._host_snapshot._fetch = main_mod._fetch_hosts
    main_mod._provider_snapshot._fetch = main_mod._fetch_provider_limits
    main_mod._catalog_snapshot._fetch = main_mod._fetch_model_catalog
    yield
    for snapshot in (
        main_mod._host_snapshot,
        main_mod._provider_snapshot,
        main_mod._catalog_snapshot,
    ):
        snapshot.invalidate()


# --- Requirement 1: the page and the data behind it are both guarded -------


def test_the_page_sends_an_anonymous_visitor_to_log_in(tmp_path, monkeypatch):
    client = _anonymous_client(tmp_path, monkeypatch)

    response = client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_the_data_route_refuses_an_anonymous_caller(tmp_path, monkeypatch):
    """The real gap. The JSON carries context counts, an inventory of every
    machine and what the hive is spending — guarding only the HTML leaves
    all of it on an open route."""
    client = _anonymous_client(tmp_path, monkeypatch)

    response = client.get("/api/dashboard", follow_redirects=False)

    assert response.status_code in (303, 401, 403)


def test_the_text_route_refuses_an_anonymous_caller(tmp_path, monkeypatch):
    """This one relays conversation prose from every mind in the hive."""
    client = _anonymous_client(tmp_path, monkeypatch)

    response = client.get("/api/dashboard/text?session_id=s1", follow_redirects=False)

    assert response.status_code in (303, 401, 403)


def test_an_authenticated_visitor_gets_the_page(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "/api/dashboard" in response.text or "dashboard.js" in response.text


# --- The assembled payload --------------------------------------------------


def test_the_payload_splits_live_conversations_into_columns(tmp_path, monkeypatch, wired):
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/dashboard").json()

    assert [row["session_id"] for row in body["columns"]] == ["s1"]


def test_a_session_without_a_reported_window_takes_it_from_the_model_map(
    tmp_path, monkeypatch, wired
):
    """Requirement 9. The proxy owns the model map; the console relays it
    rather than keeping a second copy that would disagree the first time
    somebody edited a row."""
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/dashboard").json()

    assert body["columns"][0]["context"]["window"] == 1_000_000


def test_the_context_is_rendered_as_progress_toward_the_rotation_point(
    tmp_path, monkeypatch, wired
):
    client = _authed_client(tmp_path, monkeypatch)

    context = client.get("/api/dashboard").json()["columns"][0]["context"]

    assert context["fraction_of_threshold"] == 0.5
    assert context["threshold"] == 300_000


def test_each_provider_is_carried_separately(tmp_path, monkeypatch, wired):
    client = _authed_client(tmp_path, monkeypatch)

    providers = {
        row["name"]: row for row in client.get("/api/dashboard").json()["providers"]["providers"]
    }

    assert providers["anthropic"]["windows"][0]["used_percent"] == 7.0
    assert providers["openai"]["reporting"] is False


def test_a_gateway_that_is_down_does_not_take_the_page_with_it(
    tmp_path, monkeypatch, wired
):
    """Hosts and allowances still render when comms cannot be reached, and
    the failure is named rather than shown as a hive with nothing running."""
    monkeypatch.setattr(
        main_mod, "_live_sessions", AsyncMock(side_effect=RuntimeError("comms down"))
    )
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/dashboard").json()

    assert body["columns"] == []
    assert "comms down" in body["sessions_error"]
    assert body["hosts"]["hosts"][0]["hostname"] == "workstation"


def test_a_proxy_that_is_down_leaves_the_conversations_rendering(
    tmp_path, monkeypatch, wired
):
    main_mod._provider_snapshot.invalidate()
    main_mod._catalog_snapshot.invalidate()
    main_mod._provider_snapshot._fetch = AsyncMock(side_effect=RuntimeError("proxy down"))
    main_mod._catalog_snapshot._fetch = AsyncMock(side_effect=RuntimeError("proxy down"))
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/dashboard").json()

    assert body["columns"][0]["session_id"] == "s1"
    assert body["providers"]["stale"] is True


def test_an_unreachable_proxy_leaves_the_window_unknown_rather_than_guessed(
    tmp_path, monkeypatch, wired
):
    """Requirement 10c's live case. A guessed denominator draws a
    conversation at 95% as one at 40%."""
    main_mod._catalog_snapshot.invalidate()
    main_mod._catalog_snapshot._fetch = AsyncMock(side_effect=RuntimeError("proxy down"))
    client = _authed_client(tmp_path, monkeypatch)

    context = client.get("/api/dashboard").json()["columns"][0]["context"]

    assert context["window"] is None
    assert context["fraction_of_window"] is None


def test_repeated_page_loads_do_not_re_probe_the_machines(
    tmp_path, monkeypatch, wired
):
    """Requirement 26. Every tab reads one shared reading; without it, ten
    open tabs is ten `nvidia-smi` invocations per interval on a kid's PC."""
    probe = AsyncMock(return_value=HOSTS)
    main_mod._host_snapshot.invalidate()
    main_mod._host_snapshot._fetch = probe
    client = _authed_client(tmp_path, monkeypatch)

    client.get("/api/dashboard")
    client.get("/api/dashboard")
    client.get("/api/dashboard")

    assert probe.await_count == 1


def test_the_text_route_relays_the_sequenced_blocks(tmp_path, monkeypatch, wired):
    relay = AsyncMock(
        return_value={"session_id": "s1", "blocks": [{"seq": 4, "text": "hi"}],
                      "first_available_seq": 1}
    )
    monkeypatch.setattr(main_mod, "_gateway_json", relay)
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/dashboard/text?session_id=s1&since=3").json()

    assert body["blocks"][0]["seq"] == 4
    assert relay.await_args.args[1] == {"session_id": "s1", "since": 3}


# --- the probe itself, not just the pure function it calls ------------------


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """An httpx.AsyncClient stand-in that answers one way."""

    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _probe(monkeypatch, result, *, token="admin-token"):
    if token:
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", token)
    else:
        monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(main_mod.httpx, "AsyncClient", lambda **kw: _Client(result))
    import asyncio

    return asyncio.run(
        main_mod._probe_mind_host({"name": "skippy", "id": "m1",
                                   "gateway_url": "http://skippy:8420"})
    )


def test_a_mind_that_refuses_the_credential_is_not_called_unreachable(monkeypatch):
    """The only production caller of `host_status_for`. Proving the pure
    function keeps 401 and 'down' apart says nothing about the route that
    could still fold them — and folding them sends somebody to reboot a
    healthy box over a token."""
    report = _probe(monkeypatch, _Response(401, text="unauthorized"))

    assert report["status"] == "refused"


def test_a_mind_without_the_route_is_reported_as_unsupported(monkeypatch):
    report = _probe(monkeypatch, _Response(404, text="Not Found"))

    assert report["status"] == "unsupported"


def test_a_mind_that_does_not_answer_is_unreachable(monkeypatch):
    report = _probe(monkeypatch, OSError("connection refused"))

    assert report["status"] == "unreachable"
    assert report["reading"] is None


def test_a_reporting_mind_carries_its_reading_through(monkeypatch):
    report = _probe(monkeypatch, _Response(200, {"host": {"host_id": "boot-1"}}))

    assert report["status"] == "reporting"
    assert report["reading"]["host"]["host_id"] == "boot-1"


def test_an_unset_console_token_does_not_condemn_every_machine(monkeypatch):
    """`admin_headers()` raises when the console has no admin token. Called
    inside the request's try block that exception is caught by the
    unreachable arm, and one unset variable on this box names every machine
    in the hive as down."""
    report = _probe(monkeypatch, _Response(200, {"host": {}}), token="")

    assert report["status"] == "unconfigured"


def test_a_mind_with_no_gateway_url_is_not_called_unreachable(monkeypatch):
    import asyncio

    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
    report = asyncio.run(
        main_mod._probe_mind_host({"name": "ghost", "id": "m2", "gateway_url": ""})
    )

    assert report["status"] == "unconfigured"


def test_a_gateway_too_old_for_the_route_is_named_not_read_as_quiet(monkeypatch):
    """A 200 carrying the wrong body is a failure, not a hive with nothing
    running — a gateway predating this route answers the single-session
    route for the id "live", and reading that as an empty list renders a
    working hive as a silent one."""
    monkeypatch.setattr(
        main_mod,
        "_gateway_json",
        AsyncMock(return_value=[{"error": "Session not found"}, 404]),
    )

    with pytest.raises(RuntimeError, match="unexpected shape"):
        asyncio.run(main_mod._live_sessions())


def test_a_gateway_answering_the_route_is_passed_through(monkeypatch):
    """The well-formed answer reaches the page unaltered."""
    monkeypatch.setattr(
        main_mod, "_gateway_json", AsyncMock(return_value={"sessions": [{"id": "a"}]})
    )

    assert asyncio.run(main_mod._live_sessions()) == {"sessions": [{"id": "a"}]}


def test_the_dashboard_reads_the_gateway_as_admin(monkeypatch):
    """Both live routes carry assistant prose from every conversation in the
    hive, so the console must present the admin bearer and not the service
    token every surface bot also holds."""
    seen: list[dict] = []

    def fake_sync(path, params=None, admin=False):
        seen.append({"path": path, "admin": admin})
        return {"sessions": []} if path == "/sessions/live" else {"blocks": []}

    monkeypatch.setattr(main_mod, "_gateway_json_sync", fake_sync)

    asyncio.run(main_mod._live_sessions())
    asyncio.run(main_mod.api_dashboard_text(session_id="s1", since=0, user={}))

    assert [call["path"] for call in seen] == ["/sessions/live", "/sessions/live/text"]
    assert all(call["admin"] for call in seen)


def test_a_column_reaches_the_page_headed_by_its_mind_name(
    tmp_path, monkeypatch, wired
):
    """Requirement 3 at the layer it lands on: the registry is actually
    consulted, not just available to be consulted."""
    monkeypatch.setattr(
        main_mod,
        "_broker_minds_safe",
        AsyncMock(return_value=[{"id": "skippy", "name": "Skippy"}]),
    )
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/dashboard").json()

    assert body["columns"][0]["mind_name"] == "Skippy"


def test_an_allowance_window_reaches_the_page_carrying_its_band(
    tmp_path, monkeypatch, wired
):
    """Requirement 10 at the layer it lands on: the browser is handed the
    band rather than re-deriving the two thresholds from the number."""
    monkeypatch.setattr(
        main_mod,
        "_fetch_provider_limits",
        AsyncMock(
            return_value={
                "providers": [
                    {
                        "name": "anthropic",
                        "reporting": True,
                        "windows": [{"label": "5h", "used_percent": 91.0}],
                    }
                ]
            }
        ),
    )
    main_mod._provider_snapshot._fetch = main_mod._fetch_provider_limits
    main_mod._provider_snapshot.invalidate()
    client = _authed_client(tmp_path, monkeypatch)

    body = client.get("/api/dashboard").json()

    assert body["providers"]["providers"][0]["windows"][0]["band"] == "hot"
