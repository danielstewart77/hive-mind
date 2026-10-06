"""The console configures every Mind through its HTTP API, not a bind mount.

One code path for a container in this stack, a bare-metal Mind on this host,
and a Mind on another machine — the last of which no mount can reach.
"""

import asyncio
import os
import sys

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import mind_runtime


class _Client:
    """Stand-in for httpx.AsyncClient that answers with a fixed response."""

    def __init__(self, response=None, error=None, calls=None):
        self._response = response
        self._error = error
        self.calls = calls if calls is not None else []

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def _respond(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self._error is not None:
            raise self._error
        return self._response

    async def get(self, url, **kwargs):
        return await self._respond("GET", url, **kwargs)

    async def patch(self, url, **kwargs):
        return await self._respond("PATCH", url, **kwargs)

    async def put(self, url, **kwargs):
        return await self._respond("PUT", url, **kwargs)


def _response(status: int, json_body=None, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=json_body if json_body is not None else None,
        text=None if json_body is not None else text,
        request=httpx.Request("GET", "http://mind/runtime"),
    )


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")


def _run(coro):
    return asyncio.run(coro)


def test_fetch_reads_the_minds_runtime_route(monkeypatch):
    async def scenario():
        client = _Client(_response(200, {"configuration": {"default_model": "opus"}}))
        monkeypatch.setattr(httpx, "AsyncClient", client)

        configuration = await mind_runtime.fetch_configuration("http://bob:8420/")

        assert configuration["default_model"] == "opus"
        assert client.calls[0][1] == "http://bob:8420/runtime"

    _run(scenario())


def test_push_sends_the_admin_bearer(monkeypatch):
    async def scenario():
        client = _Client(_response(200, {"configuration": {"default_model": "opus"}}))
        monkeypatch.setattr(httpx, "AsyncClient", client)

        await mind_runtime.push_default_model("http://bob:8420", "opus")

        method, url, kwargs = client.calls[0]
        assert (method, url) == ("PATCH", "http://bob:8420/runtime")
        assert kwargs["json"] == {"default_model": "opus"}
        assert kwargs["headers"]["Authorization"] == "Bearer admin-token"

    _run(scenario())


def test_a_mind_that_is_down_is_a_bad_gateway(monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(error=httpx.ConnectError("refused"))
        )

        with pytest.raises(HTTPException) as exc:
            await mind_runtime.fetch_configuration("http://bob:8420")

        assert exc.value.status_code == 502

    _run(scenario())


def test_a_mind_without_the_route_says_so(monkeypatch):
    async def scenario():
        monkeypatch.setattr(httpx, "AsyncClient", _Client(_response(404, text="nope")))

        with pytest.raises(HTTPException) as exc:
            await mind_runtime.fetch_configuration("http://bob:8420")

        assert exc.value.status_code == 501

    _run(scenario())


def test_a_rejected_write_keeps_its_status(monkeypatch):
    async def scenario():
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(_response(401, text="unauthorized"))
        )

        with pytest.raises(HTTPException) as exc:
            await mind_runtime.push_default_model("http://bob:8420", "opus")

        assert exc.value.status_code == 401

    _run(scenario())


def test_a_mind_with_no_gateway_url_is_a_bad_request():
    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await mind_runtime.fetch_configuration("")

        assert exc.value.status_code == 400

    _run(scenario())


def test_no_admin_token_refuses_the_write(monkeypatch):
    async def scenario():
        monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(_response(200, {"configuration": {}}))
        )

        with pytest.raises(HTTPException) as exc:
            await mind_runtime.push_default_model("http://bob:8420", "opus")

        assert exc.value.status_code == 503

    _run(scenario())

# ---------------------------------------------------------------- model picker


CATALOG = [
    {"name": "claude-opus-5", "label": "Opus 5", "provider": "anthropic",
     "provider_label": "Anthropic"},
    {"name": "qwen35-131k", "label": "Qwen 3.5 9B (local, 131k)",
     "provider": "ollama", "provider_label": "Ollama"},
]


def _picker(monkeypatch, *, harness, provider):
    """The rows this Mind's editor would offer."""
    import main as main_mod

    async def fake_configuration(_gateway):
        return {"name": "m", "harness": harness, "provider": provider,
                "default_model": "x", "deployment": "container"}

    async def fake_gateway_url(_name):
        return "http://m:8420"

    async def fake_models(_gateway):
        return CATALOG

    monkeypatch.setattr(main_mod.mind_runtime, "fetch_configuration", fake_configuration)
    monkeypatch.setattr(main_mod, "_mind_gateway_url", fake_gateway_url)
    monkeypatch.setattr(main_mod.mind_runtime, "fetch_models", fake_models)
    return asyncio.run(main_mod.api_mind_configuration("m", user={"sub": "t"}))["models"]


def test_the_console_relays_what_the_mind_reported_without_re_sorting_it(monkeypatch):
    """The Mind's own key and harness already decided the list.

    The console used to bucket the listing itself, by a `provider` field that
    means something different on each side — a codex Mind declares `anthropic`
    whatever it runs — and every Mind it guessed wrong about got an empty
    picker. Grouping now follows the provider each row names.
    """
    rows = _picker(monkeypatch, harness="codex_cli", provider="anthropic")
    assert [row["name"] for row in rows] == ["claude-opus-5", "qwen35-131k"]


def test_each_offered_row_carries_its_label_and_provider(monkeypatch):
    """A picker shows the label, saves the name, groups by the provider."""
    rows = {row["name"]: row for row in _picker(
        monkeypatch, harness="claude_cli", provider="ollama"
    )}
    assert rows["claude-opus-5"]["label"] == "Opus 5"
    assert rows["qwen35-131k"]["provider"] == "ollama"
    assert rows["qwen35-131k"]["provider_label"] == "Ollama"


# ---------------------------------------------------------------------------
# The voice
# ---------------------------------------------------------------------------
def test_saving_a_voice_patches_the_mind_it_names(monkeypatch):
    """The voice is written on the Mind, over its own runtime route.

    A console that wrote a bind-mounted copy would reach a container in this
    stack and nothing else, and the Mind re-registers from its own file on
    every boot — so a write anywhere but the file is undone by a restart.
    """

    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(_response(200, {"configuration": {"voice": "bm_george"}}), calls=calls),
        )

        configuration = await mind_runtime.push_voice("http://cypher:8420", "bm_george")

        assert configuration["voice"] == "bm_george"
        method, url, kwargs = calls[0]
        assert (method, url) == ("PATCH", "http://cypher:8420/runtime")
        assert kwargs["json"] == {"voice": "bm_george"}
        assert kwargs["headers"]["Authorization"] == "Bearer admin-token"

    _run(scenario())


def test_saving_a_voice_sends_only_the_voice(monkeypatch):
    """Resending the model the page rendered would write a stale one back."""

    async def scenario():
        calls = []
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _Client(_response(200, {"configuration": {"voice": "af_heart"}}), calls=calls),
        )

        await mind_runtime.push_voice("http://cypher:8420", "af_heart")

        assert "default_model" not in calls[0][2]["json"]

    _run(scenario())


def test_a_mind_with_no_gateway_url_refuses_rather_than_guessing():
    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await mind_runtime.push_voice("", "af_heart")

        assert exc.value.status_code == 400

    _run(scenario())


def test_an_unreachable_voice_server_reports_unreadable_not_empty(monkeypatch):
    """Empty would say this engine has no voices, which is never true."""

    async def scenario():
        monkeypatch.setattr(
            httpx, "AsyncClient", _Client(error=httpx.ConnectError("refused"))
        )

        body = await mind_runtime.fetch_voice_catalogue("http://voice:8422")

        assert body["catalogue"]["readable"] is False
        assert body["catalogue"]["voices"] == []
        assert "could not be reached" in body["catalogue"]["detail"]

    _run(scenario())


def test_the_catalogue_is_relayed_as_the_voice_server_reported_it(monkeypatch):
    async def scenario():
        offered = {
            "engine": "kokoro",
            "default_voice": "af_heart",
            "catalogue": {
                "language_code": "b",
                "readable": True,
                "detail": "",
                "voices": [
                    {"name": "bm_george", "language": "British English",
                     "gender": "male", "label": "George — British English, male"}
                ],
            },
        }
        monkeypatch.setattr(httpx, "AsyncClient", _Client(_response(200, offered)))

        body = await mind_runtime.fetch_voice_catalogue("http://voice:8422")

        assert body["catalogue"]["voices"][0]["label"] == "George — British English, male"
        assert body["default_voice"] == "af_heart"

    _run(scenario())
