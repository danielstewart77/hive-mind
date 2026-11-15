"""hive-tools' model calls go to the inference proxy, with its key.

`/ollama/structured`, `/ollama/chat` and `/ollama/models` keep their paths
because every mind's capture hook calls them; what answers is the proxy named
by `INFERENCE_PROXY_URL`, authenticated with `INFERENCE_PROXY_KEY`. The only
stub is the HTTP transport — the point where the process stops being ours —
and it answers in the proxy's OpenAI shape.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.scratch import run

PROXY = "http://proxy.invalid:8899"
KEY = "hive-tools-proxy-key"


@pytest.fixture()
def app() -> FastAPI:
    from server import create_app

    return create_app()


@pytest.fixture()
def client(app: FastAPI) -> Generator[TestClient, None, None]:
    # Entered, not merely constructed: the store opens in the app's
    # lifespan, and a client that never enters it is an app whose database
    # was never opened.
    with TestClient(app) as entered:
        yield entered


@pytest.fixture()
def auth_headers(app: FastAPI) -> dict[str, str]:
    from auth_api import generate_token

    raw = run(generate_token("ada"))
    return {"Authorization": f"Bearer {raw}"}


class _Proxy:
    """The wire, answering each proxy route the way the proxy does."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.content: Any = "{}"
        self.status = 200
        self.listing: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "refused"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": self.listing})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": self.content}}]})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.sent if r.content]


@pytest.fixture()
def proxy(monkeypatch: pytest.MonkeyPatch) -> _Proxy:
    import tools.ollama as module

    monkeypatch.setenv("INFERENCE_PROXY_URL", PROXY + "/v1")
    monkeypatch.setenv("INFERENCE_PROXY_KEY", KEY)
    monkeypatch.setenv("HIVE_TOOLS_DEFAULT_MODEL", "model-of-record")
    wire = _Proxy()
    real = httpx.AsyncClient

    def client_on_the_wire(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=httpx.MockTransport(wire), **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", client_on_the_wire)
    return wire


def test_a_structured_call_is_answered_by_the_proxy_with_the_key_and_schema(
    client: TestClient, auth_headers: dict[str, str], proxy: _Proxy
) -> None:
    schema = {"type": "object", "properties": {"yes": {"type": "boolean"}}}
    proxy.content = '{"yes": true}'

    resp = client.post("/ollama/structured", headers=auth_headers,
                       json={"prompt": "is it?", "schema": schema, "system": "be brief"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"yes": True}
    (sent,) = proxy.sent
    assert str(sent.url) == f"{PROXY}/v1/chat/completions"
    assert sent.headers["Authorization"] == f"Bearer {KEY}"
    (body,) = proxy.bodies()
    assert body["model"] == "model-of-record"
    assert body["messages"] == [{"role": "system", "content": "be brief"},
                                {"role": "user", "content": "is it?"}]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == schema


def test_a_chat_call_returns_the_proxys_text_for_the_model_the_caller_named(
    client: TestClient, auth_headers: dict[str, str], proxy: _Proxy
) -> None:
    proxy.content = "hello from the proxy"

    resp = client.post("/ollama/chat", headers=auth_headers,
                       json={"prompt": "hi", "model": "caller-model"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"response": "hello from the proxy"}
    (body,) = proxy.bodies()
    assert body["model"] == "caller-model"
    assert "response_format" not in body
    assert proxy.sent[0].headers["Authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("content", ["not json at all", None, "[1, 2]"])
def test_a_structured_answer_that_is_not_an_object_comes_back_marked_as_an_error(
    client: TestClient, auth_headers: dict[str, str], proxy: _Proxy, content: Any
) -> None:
    proxy.content = content

    resp = client.post("/ollama/structured", headers=auth_headers,
                       json={"prompt": "p", "schema": {"type": "object"}})

    assert resp.status_code == 200, resp.text
    answer = resp.json()
    assert answer["_error"]
    assert answer["_raw"] == (content if isinstance(content, str) else "")


def test_the_model_list_is_the_proxys_chat_listing(
    client: TestClient, auth_headers: dict[str, str], proxy: _Proxy
) -> None:
    proxy.listing = [{"id": "gpt-oss:20b-32k", "label": "gpt-oss", "context_window": 32768,
                      "purpose": "chat", "wires": ["chat_completions"]}]

    resp = client.get("/ollama/models", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "proxy_url": PROXY,
        "default_model": "model-of-record",
        "models": [{"name": "gpt-oss:20b-32k", "label": "gpt-oss", "context_window": 32768}],
    }
    (sent,) = proxy.sent
    assert sent.url.path == "/v1/models"
    assert sent.url.params["purpose"] == "chat"
    assert sent.headers["Authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize("unset", ["INFERENCE_PROXY_URL", "INFERENCE_PROXY_KEY",
                                   "HIVE_TOOLS_DEFAULT_MODEL"])
def test_an_unconfigured_proxy_is_named_and_nothing_is_sent(
    client: TestClient, auth_headers: dict[str, str], proxy: _Proxy,
    monkeypatch: pytest.MonkeyPatch, unset: str
) -> None:
    monkeypatch.delenv(unset)

    resp = client.post("/ollama/chat", headers=auth_headers, json={"prompt": "hi"})

    assert resp.status_code == 503
    assert unset in resp.json()["detail"]
    assert proxy.sent == []


def test_a_key_the_proxy_refuses_is_reported_as_configuration_not_failure(
    client: TestClient, auth_headers: dict[str, str], proxy: _Proxy
) -> None:
    proxy.status = 401

    resp = client.post("/ollama/chat", headers=auth_headers, json={"prompt": "hi"})

    assert resp.status_code == 503
    assert "key" in resp.json()["detail"]


def test_an_unreachable_proxy_is_a_502(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import tools.ollama as module

    monkeypatch.setenv("INFERENCE_PROXY_URL", PROXY)
    monkeypatch.setenv("INFERENCE_PROXY_KEY", KEY)
    real = httpx.AsyncClient

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(module.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, transport=httpx.MockTransport(refused), **kw))

    resp = client.get("/ollama/models", headers=auth_headers)

    assert resp.status_code == 502
    assert "unreachable" in resp.json()["detail"]


def test_models_requires_auth(client: TestClient) -> None:
    resp = client.get("/ollama/models")
    assert resp.status_code == 401
