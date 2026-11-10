"""Lucent's model calls go to the inference proxy, with the one key.

The embedding for a memory and the prune's shipped-or-not verdict used to be
fetched from Ollama and hive-tools' `/ollama/structured` directly. Both now
go to the proxy named by `INFERENCE_PROXY_URL`, authenticated with
`INFERENCE_PROXY_KEY`, for the model the install configured. The only stub
is `requests.post` — the point where the process stops being ours — and it
answers in the proxy's OpenAI shape.
"""

from __future__ import annotations

import json
import random

import pytest
import requests

from hive_data import memory as hd_memory
from lucent_api import lucent_memory, prune_memory
from lucent_api.tests.conftest import unique

DIMS = 4096
PROXY = "http://proxy.invalid:8888"
KEY = "the-one-proxy-key"


class _Reply:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


class _Transport:
    """`requests.post`, answering each proxy route the way the proxy does."""

    def __init__(self, verdict: dict | None = None):
        self.sent: list[dict] = []
        self.verdict = verdict or {}

    def __call__(self, url, **kw):
        body = kw.get("json") or {}
        self.sent.append({"url": url, "body": body, "headers": kw.get("headers") or {}})
        if url.endswith("/v1/embeddings"):
            # A direction of its own, so save-time dedup against other
            # tests' rows in the shared scratch database never fires.
            rng = random.Random(body.get("input"))
            vector = [rng.gauss(0.0, 1.0) for _ in range(DIMS)]
            return _Reply({"data": [{"embedding": vector, "index": 0}],
                           "model": body["model"]})
        content = json.dumps(self.verdict)
        return _Reply({"choices": [{"message": {"role": "assistant", "content": content}}]})


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setenv("INFERENCE_PROXY_URL", PROXY)
    monkeypatch.setenv("INFERENCE_PROXY_KEY", KEY)
    monkeypatch.setenv("LUCENT_EMBEDDING_MODEL", "embedder-of-record")
    monkeypatch.setenv("LUCENT_CLASSIFIER_MODEL", "classifier-of-record")
    transport = _Transport({"shipped": True, "reason": "it is live"})
    monkeypatch.setattr(requests, "post", transport)
    return transport


@pytest.mark.asyncio
async def test_a_stored_memory_is_embedded_by_the_proxy_with_the_key_and_configured_model(
        store, proxy, inspector):
    answer = json.loads(await lucent_memory.memory_store_direct(
        content=f"a memory to embed {unique('e')}", data_class="current-state",
        source="session", mind_id="proxy-embed-test"))

    assert answer.get("stored") is True, answer
    embeds = [r for r in proxy.sent if r["url"] == f"{PROXY}/v1/embeddings"]
    assert embeds, proxy.sent
    assert all(r["headers"]["Authorization"] == f"Bearer {KEY}" for r in embeds)
    assert {r["body"]["model"] for r in embeds} == {"embedder-of-record"}
    landed = await inspector.fetchrow(
        'SELECT "embedding" IS NOT NULL AS "has" FROM memory.memories WHERE "id" = $1',
        int(answer["id"]))
    assert landed["has"] is True


@pytest.mark.asyncio
async def test_the_prune_classifies_through_the_proxy_chat_route_and_acts_on_the_verdict(
        store, proxy, inspector):
    written = await hd_memory.store(
        mind_id="proxy-prune-test", content=f"a plan that has shipped {unique('p')}",
        source="session", data_class="future-state", tier="contextual",
        created_at=1_700_000_000)
    planned = int(written["row"]["id"])

    run = await prune_memory.prune_future_state()

    chats = [r for r in proxy.sent if r["url"] == f"{PROXY}/v1/chat/completions"]
    assert chats, proxy.sent
    assert all(r["headers"]["Authorization"] == f"Bearer {KEY}" for r in chats)
    assert {r["body"]["model"] for r in chats} == {"classifier-of-record"}
    assert {r["body"]["response_format"]["type"] for r in chats} == {"json_schema"}
    assert planned in run["deleted"], run
    gone = await inspector.fetchrow(
        'SELECT "id" FROM memory.memories WHERE "id" = $1', planned)
    assert gone is None
