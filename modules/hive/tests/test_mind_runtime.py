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
    {"name": "opus", "provider": "anthropic"},
    {"name": "claude-opus-5", "provider": "anthropic"},
    {"name": "gpt-5.4", "provider": "openai"},
    {"name": "gpt-5.6", "provider": "openai"},
    {"name": "gemma4-131k", "provider": "ollama"},
]


def _picker(monkeypatch, *, harness, provider):
    """The model names this Mind's editor would offer."""
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
    result = asyncio.run(main_mod.api_mind_configuration("m", user={"sub": "t"}))
    return set(result["models"])


def test_a_codex_mind_is_offered_the_openai_shaped_models(monkeypatch):
    """Requirement 4: Nagatha runs gpt-5.4 and her picker listed nothing.

    A Mind reaching the proxy declares `provider: anthropic` whatever it
    actually runs, so keying the picker off the provider alone matched no
    bucket for a codex harness and produced an empty list.
    """
    assert _picker(monkeypatch, harness="codex_cli", provider="anthropic") == {
        "gpt-5.4",
        "gpt-5.6",
    }


def test_a_claude_mind_on_ollama_is_offered_both_the_local_and_hosted_models(
    monkeypatch,
):
    """Bob runs a local tag through the proxy and could run a hosted one."""
    assert _picker(monkeypatch, harness="claude_cli", provider="ollama") == {
        "opus",
        "claude-opus-5",
        "gemma4-131k",
    }
