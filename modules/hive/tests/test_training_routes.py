"""Route tests for the training panel and its Hive Tools proxy."""

import os
import re
import sys
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import main as main_mod
import accounts


def _authed_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


@pytest.fixture()
def tools_call():
    """Patch the Hive Tools client and expose the recorded call."""
    with patch.object(main_mod, "_hive_tools_request", new_callable=AsyncMock) as mock:
        mock.return_value = {}
        yield mock


def test_training_page_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    response = TestClient(main_mod.app).get("/training", follow_redirects=False)
    assert response.status_code in (302, 303, 307)


def test_training_page_renders_for_an_authenticated_owner(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.get("/training")
    assert response.status_code == 200
    assert "Keep per duplicate cluster" in response.text


def test_training_appears_in_the_navigation(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    assert 'href="/training"' in client.get("/overview").text


def test_status_proxies_to_hive_tools(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"total": 8809, "by_flag": {"keep": 3024}}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/training/status")

    assert response.status_code == 200
    assert response.json()["total"] == 8809
    tools_call.assert_awaited_once_with("GET", "/training/status")


def test_curate_forwards_the_policy_body_with_a_long_timeout(
    tmp_path, monkeypatch, tools_call
):
    """Curation walks the whole corpus; the default 30s client would abort it."""
    tools_call.return_value = {"kept": 3024, "total": 8809}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post("/api/training/curate", json={"keep_per_cluster": 5})

    assert response.status_code == 200
    kwargs = tools_call.await_args.kwargs
    assert kwargs["json_body"] == {"keep_per_cluster": 5}
    assert kwargs["timeout"] > 30.0


def test_export_forwards_its_body(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"train_examples": 2840}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/training/export", json={"mode": "reasoning", "name": "v1"}
    )

    assert response.status_code == 200
    assert tools_call.await_args.kwargs["json_body"]["mode"] == "reasoning"


def test_train_requires_a_training_file(tmp_path, monkeypatch, tools_call):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post("/api/training/train", json={"name": "x"})

    assert response.status_code == 400
    tools_call.assert_not_awaited()


def test_train_forwards_a_valid_request(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"can_run": False, "blockers": ["gpu busy"]}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/training/train", json={"train_file": "/data/train.jsonl"}
    )

    assert response.status_code == 200
    assert response.json()["blockers"] == ["gpu busy"]


def test_runs_rejects_an_unknown_kind(tmp_path, monkeypatch, tools_call):
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/training/runs?kind=nonsense")

    assert response.status_code == 400
    tools_call.assert_not_awaited()


def test_runs_clamps_the_limit(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"runs": []}
    client = _authed_client(tmp_path, monkeypatch)

    client.get("/api/training/runs?limit=9999")

    assert tools_call.await_args.kwargs["params"]["limit"] == 200


def test_training_endpoints_require_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    for method, path in (
        ("get", "/api/training/status"),
        ("get", "/api/training/runs"),
        ("post", "/api/training/curate"),
        ("post", "/api/training/export"),
        ("post", "/api/training/train"),
    ):
        kwargs = {"follow_redirects": False}
        if method == "post":
            kwargs["json"] = {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code in (302, 303, 307, 401, 403), path


def test_base_models_proxies_to_hive_tools(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"default": "Qwen/Qwen3-8B", "models": []}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.get("/api/training/base-models")

    assert response.status_code == 200
    assert response.json()["default"] == "Qwen/Qwen3-8B"
    tools_call.assert_awaited_once_with("GET", "/training/base-models")


def test_base_models_requires_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    response = TestClient(main_mod.app).get(
        "/api/training/base-models", follow_redirects=False
    )
    assert response.status_code in (302, 303, 307, 401)


def test_deploy_proxies_the_body_to_hive_tools(tmp_path, monkeypatch, tools_call):
    tools_call.return_value = {"deployed": True, "model_name": "skippy-lora"}
    client = _authed_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/training/deploy",
        json={"name": "skippy-lora", "adapter_dir": "/data/v1/adapter"},
    )

    assert response.status_code == 200
    assert response.json()["deployed"] is True
    args, kwargs = tools_call.await_args
    assert args == ("POST", "/training/deploy")
    assert kwargs["json_body"]["adapter_dir"] == "/data/v1/adapter"


def test_deploy_needs_an_adapter(tmp_path, monkeypatch, tools_call):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.post("/api/training/deploy", json={"name": "skippy-lora"})
    assert response.status_code == 400
    tools_call.assert_not_awaited()


def test_deploy_needs_a_name(tmp_path, monkeypatch, tools_call):
    client = _authed_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/training/deploy", json={"adapter_dir": "/data/v1/adapter"}
    )
    assert response.status_code == 400
    tools_call.assert_not_awaited()


def test_deploy_gets_the_long_timeout(tmp_path, monkeypatch, tools_call):
    """Uploading a merged model or waiting on a conversion is not a 30s call."""
    client = _authed_client(tmp_path, monkeypatch)
    client.post(
        "/api/training/deploy",
        json={"name": "skippy-lora", "adapter_dir": "/data/v1/adapter"},
    )
    _, kwargs = tools_call.await_args
    assert kwargs["timeout"] == main_mod._TRAINING_LONG_TIMEOUT_SECONDS


def test_the_page_explains_every_decision_it_asks_for(tmp_path, monkeypatch):
    """The hints are the feature. A control without one is a trap."""
    client = _authed_client(tmp_path, monkeypatch)
    body = client.get("/training").text

    for phrase in (
        "mixture-of-experts",
        "lora rank",
        "quantization",
        # Deploy is no longer a choice between two routes, so what the page
        # owes the reader is what the one route costs them.
        "full-precision weights",
        "hands that to ollama to compress",
    ):
        assert phrase in body.lower(), f"the page never mentions {phrase}"

    # Each of the four stages is present and numbered, so the order the
    # pipeline actually runs in is visible without reading the docs.
    for heading in ("1 · Curation", "2 · Export", "3 · Fine-tune", "4 · Deploy"):
        assert heading in body


def test_every_form_control_that_can_surprise_someone_has_a_hint(
    tmp_path, monkeypatch
):
    import re

    client = _authed_client(tmp_path, monkeypatch)
    body = client.get("/training").text

    hints = re.findall(r'data-hint="([^"]{40,})"', body)
    assert len(hints) >= 18, f"only {len(hints)} explained controls"


# ----------------------------------------- the run header's status tiles

TILES = ("step", "percent", "elapsed", "left", "card", "util", "free")

#: What each tile must reserve, in `ch`, to hold its widest real value.
#: A `ch` is the advance of a zero and a percent sign is wider than that,
#: so these are sized against rendered text, not digit counts.
RESERVED = {
    "step": 12,      # "10000/12000"
    "percent": 6,    # "100%"
    "elapsed": 8,    # "10h 05m"
    "left": 8,       # "unknown"
    "card": 12,      # constant per host
    "util": 6,       # "100%"
    "free": 17,      # "12.3 of 48.0 GB"
}


def _page(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    return client.get("/training").text, client.get("/static/console.css").text


def test_each_run_value_gets_its_own_labelled_tile(tmp_path, monkeypatch):
    """Requirement 1: one labelled tile per value, each carrying its value.

    The header used to be a single line joined with separators, so seven
    unrelated numbers competed for one width and every one of them moved
    whenever any other changed.
    """
    body, _ = _page(tmp_path, monkeypatch)

    for tile in TILES:
        assert f'data-tile="{tile}"' in body, f"no tile for {tile}"
        # A tile nothing ever writes to is a permanent em-dash, which is
        # indistinguishable from a run whose numbers stopped arriving.
        assert f'setTile("{tile}"' in body, f"nothing ever fills {tile}"
    # And setTile writes what it is handed rather than being a stub.
    assert re.search(
        r"function setTile\(name, value\)\s*{[^}]*textContent = value", body
    ), "setTile does not write its value"
    for label in ("Step", "Done", "Elapsed", "Remaining", "Card", "Busy", "Free"):
        assert f">{label}<" in body, f"tile {label} is unlabelled"
    # And the filling runs on the poll, not merely in a function nobody
    # calls.
    assert "renderTiles(beat, result.progress" in body


def test_a_tile_cannot_be_resized_by_its_own_value(tmp_path, monkeypatch):
    """Requirement 2: ninety nine to one hundred moves nothing beside it.

    Two mechanisms, both needed. Tabular figures stop a 1 being narrower
    than a 0, and a reserved width stops the tile growing when a digit is
    added. There is no browser in this suite, so the declarations are what
    is checked — but the reserved widths are checked as numbers, since a
    selector that exists and reserves nothing is the pre-fix behaviour
    wearing the fix's clothes.
    """
    _, css = _page(tmp_path, monkeypatch)

    assert "tabular-nums" in css[css.index(".run-tile-value") :][:400]
    for tile, floor in RESERVED.items():
        match = re.search(
            r'\.run-tile-value\[data-tile="%s"\][^}]*min-width:\s*(\d+)ch' % tile, css
        )
        assert match, f"{tile} reserves no width"
        assert int(match.group(1)) >= floor, (
            f"{tile} reserves {match.group(1)}ch, needs {floor}ch"
        )


def test_the_pulsing_dot_stops_when_the_polling_stops(tmp_path, monkeypatch):
    """Requirement 3: a dot that proves the page is still polling.

    The spinner it replaces cycled four glyphs of four different widths —
    the largest single source of movement in this header — but it did
    testify, because it only advanced when a poll returned. An animation
    set to run forever keeps pulsing through a hung fetch, so this one
    fires once and is restarted by each completed poll.
    """
    body, css = _page(tmp_path, monkeypatch)

    # The dot lives in the strip, not merely somewhere in the document.
    strip = body[body.index("data-run-tiles") : body.index("</div>", body.index("data-run-tiles"))]
    dot = re.search(r"<span[^>]*class=\"run-pulse\"[^>]*>", strip)
    assert dot, "no pulsing dot inside the run strip"
    # A hidden dot proves nothing, and the strip's own hiding is the only
    # thing entitled to take it off screen.
    assert "hidden" not in dot.group(0)

    # Fires once per beat rather than looping, and something binds it.
    assert "@keyframes run-pulse" in css
    animation = re.search(r"\.run-pulse\.is-beating\s*{[^}]*animation:([^;]+);", css)
    assert animation, "nothing binds the keyframes"
    assert "infinite" not in animation.group(1)
    assert "beatThePulse" in body and "classList.remove(\"is-beating\")" in body


def test_the_tiles_are_absent_until_a_run_is_live(tmp_path, monkeypatch):
    """Requirement 4: an idle machine shows no run tiles.

    The strip ships hidden and is only revealed by a heartbeat reporting a
    live trainer, so a finished run puts it away again rather than leaving
    the last numbers on screen looking current.
    """
    body, css = _page(tmp_path, monkeypatch)

    strip = body[body.index("data-run-tiles") : body.index("data-run-tiles") + 120]
    assert "hidden" in strip
    # An author-origin display beats the UA stylesheet's [hidden] rule, so
    # without this the attribute is a flag nothing reads.
    assert re.search(r"\.run-tiles\[hidden\]\s*{[^}]*display:\s*none", css)
    # Revealed by the heartbeat rather than unconditionally.
    assert "tiles.hidden = !(live || pending)" in body
    assert "const live = !!(beat && beat.live)" in body
    # And blanked, since values outlive the hiding.
    assert 'TILE_NAMES.forEach(function (name) { setTile(name, "—"); })' in body
