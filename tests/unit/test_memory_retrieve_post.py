"""The memory retrieve API takes its query in the body, never in a URL.

A retrieve query is a whole prompt — on a mind's turn it carries the soul
and the recent-memory block with it. Sent as a query string it landed
verbatim in Zeek's ``http.log``, in uvicorn's access log and in Loki, where
the sentinel read a mind's own context back as network traffic and filed it
under a class called ``hive_lucent_memory_retrieve_intrusion``.

Requirements under test:
  R1  the body's fields reach the search that runs
  R2  a query far longer than a URL can carry returns results
  R3  a retrieve with no query is refused, not answered as an empty search
  R5  the gateway's own retrieval sends the prompt in a body, and the
      address it calls carries no query string at all
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.delenv("LUCENT_BEARER_TOKEN", raising=False)
    from lucent_api.server import create_app

    return TestClient(create_app())


@pytest.fixture
def hybrid_calls(monkeypatch) -> list[dict]:
    """Record what the live hybrid search is handed, still running it."""
    from lucent_api import lucent_memory

    seen: list[dict] = []
    real = lucent_memory.memory_retrieve_hybrid

    def spy(**kwargs):
        seen.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(lucent_memory, "memory_retrieve_hybrid", spy)
    return seen


@pytest.fixture
def vector_calls(monkeypatch) -> list[dict]:
    from lucent_api import lucent_memory

    seen: list[dict] = []
    real = lucent_memory.memory_retrieve

    def spy(**kwargs):
        seen.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(lucent_memory, "memory_retrieve", spy)
    return seen


# ---- R1: the body's fields reach the search ----


def test_r1_hybrid_search_receives_the_body_fields(client, hybrid_calls):
    client.post(
        "/memory/retrieve",
        json={
            "query": "who is maurice",
            "k": 7,
            "min_score": 0.42,
            "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f",
            "mode": "hybrid",
            "debug": False,
        },
    )
    assert hybrid_calls, "the hybrid search was never reached"
    call = hybrid_calls[-1]
    assert call["query"] == "who is maurice"
    assert call["k"] == 7
    assert call["min_score"] == 0.42
    assert call["mind_id"] == "14cb820b-4a42-4f04-a593-54f532fd1d2f"
    assert call["debug"] is False


def test_r1_vector_search_receives_the_filters_only_it_takes(client, vector_calls):
    client.post(
        "/memory/retrieve",
        json={
            "query": "cooking ingredients",
            "k": 3,
            "data_class": "feedback",
            "tag_filter": "recipe",
        },
    )
    assert vector_calls, "the vector search was never reached"
    call = vector_calls[-1]
    assert call["query"] == "cooking ingredients"
    assert call["k"] == 3
    assert call["data_class"] == "feedback"
    assert call["tag_filter"] == "recipe"


# ---- R2: a query longer than a URL can carry ----


def test_r2_query_longer_than_a_url_reaches_the_search(client, hybrid_calls):
    # Nginx caps a request line at 8k and Zeek truncates a logged URI well
    # before that; this is the size a real soul-plus-recent-memory prompt hit.
    long_query = "sentinel triage context " * 400
    assert len(long_query) > 8000

    resp = client.post(
        "/memory/retrieve",
        json={"query": long_query, "k": 3, "mode": "hybrid"},
    )

    assert resp.status_code == 200
    assert hybrid_calls[-1]["query"] == long_query


# ---- R3: no query is a refusal, not an empty search ----


def test_r3_retrieve_with_no_query_is_refused(client, hybrid_calls, vector_calls):
    resp = client.post("/memory/retrieve", json={"k": 3, "mode": "hybrid"})

    assert resp.status_code == 422
    assert not hybrid_calls and not vector_calls, "a query-less search ran anyway"


# ---- R5: the gateway sends a body and an address with no query string ----


class _Capture(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode()
        type(self).requests.append({"method": "POST", "path": self.path, "body": raw})
        payload = json.dumps({"memories": [{"content": "never guess a pronoun"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        type(self).requests.append({"method": "GET", "path": self.path, "body": ""})
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def lucent_stub():
    _Capture.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _Capture.requests
    server.shutdown()


def test_r5_gateway_retrieval_puts_the_prompt_in_the_body(lucent_stub, monkeypatch):
    url, captured = lucent_stub
    monkeypatch.setenv("LUCENT_URL", url)
    sys.modules.pop("contextual_retrieval", None)
    sys.path.insert(0, os.path.abspath(os.path.join(NS, "comms")))
    import contextual_retrieval

    monkeypatch.setattr(contextual_retrieval, "LUCENT_URL", url)
    prompt = "Sloan is my son " * 600  # the size that broke the URL
    out = contextual_retrieval.format_injection(prompt)

    assert captured, "the gateway never called lucent"
    req = captured[-1]
    assert req["method"] == "POST"
    assert "?" not in req["path"], f"the prompt went into the address: {req['path'][:120]}"
    assert json.loads(req["body"])["query"] == prompt
    assert "never guess a pronoun" in out
