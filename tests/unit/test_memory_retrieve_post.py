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

What each body field did used to be asserted by wrapping the search in a
recording spy and reading its call log. The search is ours, so the spy was a
stub on the code under test and the assertion was on the stub rather than on
anything the store did. Each field is now asserted on the memories that come
back: the rows are seeded through the access layer, the filtering and the
ranking are the database's, and the only stub left is the embedder — a
synchronous HTTP call to Ollama, which is the point where the process stops
being ours.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import pytest_asyncio

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

pytest.importorskip("hive_data")

from hive_data import memory as hd_memory  # noqa: E402

from lucent_api import lucent_memory  # noqa: E402
from lucent_api.tests.conftest import unique  # noqa: E402

#: About how the query travels in the body, not about scoping, so these
#: present the admin credential — the one value that still reaches every mind.
ADMIN_TOKEN = "lucent-admin-for-tests"  # secret-guard: allow

#: `memory.memories.embedding` is `vector(4096)`, so a stub returning
#: anything shorter is refused by the database rather than by the test.
DIMS = 4096


def _fake_embed(text: str) -> list[float]:
    """A unit vector per text, orthogonal to every other text's.

    So a query identical to a stored memory scores 1.0 against it and 0.0
    against everything else, which is what makes `k` and `min_score`
    assertable on the answer rather than on a call log.
    """
    slot = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4],
                          "big") % DIMS
    values = [0.0] * DIMS
    values[slot] = 1.0
    return values


@pytest_asyncio.fixture
async def client(store, monkeypatch):
    """The real app over ASGI, on this test's own event loop.

    `TestClient` would run the routes in a portal thread with a loop of its
    own, and the pool's connections belong to the loop that opened them.
    """
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(lucent_memory, "_embed", _fake_embed)

    from lucent_api.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://lucent",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as opened:
        yield opened


async def _remember(mind: str, content: str, *, tags: str = "",
                    data_class: str | None = None) -> None:
    """One memory in the store, embedded exactly as the route would embed it.

    Written through the access layer rather than through `/memory/store` so
    a filter and a write agreeing on the same wrong column cannot pass, and
    so save-time dedup — which compares across every mind in a data class —
    is not in the way of a fixture.
    """
    await hd_memory.store(
        mind_id=mind, content=content, embedding=_fake_embed(content),
        tags=tags, source="session",
        data_class=data_class or unique("class"), tier="contextual",
        created_at=1_700_000_000)


async def _retrieve(client: httpx.AsyncClient, **body) -> dict:
    answer = await client.post("/memory/retrieve", json=body)
    assert answer.status_code == 200, answer.text
    return answer.json()


# ---- R1: the body's fields reach the search ----


@pytest.mark.asyncio
async def test_r1_the_bodys_named_mind_is_the_only_one_read(client):
    """The mind a read may see is a *set* — a mind reads its own plus the
    `shared` sentinel, which one value could never express — and an admin
    naming one mind resolves to exactly that mind."""
    mine, theirs = unique("mind"), unique("mind")
    ours = unique("who is maurice")
    await _remember(mine, ours)
    await _remember(theirs, unique("who is maurice to somebody else"))

    answer = await _retrieve(client, query=ours, k=10, mind_id=mine)

    contents = {m["content"] for m in answer["memories"]}
    assert ours in contents
    assert {m["mind_id"] for m in answer["memories"]} == {mine}


@pytest.mark.asyncio
async def test_r1_the_bodys_class_and_tag_filters_narrow_the_answer(client):
    """The two filters only the vector search takes. They are arguments to
    the ranking rather than a pass over its results, so a read that names
    both has to still find a row that satisfies both."""
    mind = unique("mind")
    wanted = unique("cooking ingredients")
    await _remember(mind, wanted, tags="recipe", data_class="feedback")
    await _remember(mind, unique("cooking ingredients untagged"),
                    data_class="feedback")
    await _remember(mind, unique("cooking ingredients other class"),
                    tags="recipe", data_class="current-state")

    answer = await _retrieve(
        client, query=wanted, k=10, mind_id=mind,
        data_class="feedback", tag_filter="recipe")

    assert [m["content"] for m in answer["memories"]] == [wanted]


@pytest.mark.asyncio
async def test_r1_the_bodys_k_bounds_how_many_come_back(client):
    mind = unique("mind")
    nearest = unique("the nearest memory")
    await _remember(mind, nearest)
    await _remember(mind, unique("another memory"))
    await _remember(mind, unique("a third memory"))

    one = await _retrieve(client, query=nearest, k=1, mind_id=mind)
    three = await _retrieve(client, query=nearest, k=3, mind_id=mind)

    assert [m["content"] for m in one["memories"]] == [nearest]
    assert one["count"] == 1
    assert three["count"] == 3


@pytest.mark.asyncio
async def test_r1_the_bodys_min_score_drops_what_ranks_below_it(client):
    mind = unique("mind")
    exact = unique("who is maurice")
    await _remember(mind, exact)
    await _remember(mind, unique("nothing to do with it"))

    answer = await _retrieve(
        client, query=exact, k=10, mind_id=mind, min_score=0.5)

    assert [m["content"] for m in answer["memories"]] == [exact]
    assert answer["memories"][0]["score"] >= 0.5


@pytest.mark.asyncio
async def test_r1_hybrid_mode_answers_out_of_the_store(client):
    """`mode` picks a different search entirely. Pinned on an answer rather
    than on which function was called: a hybrid read that reaches the
    keyword half and the vector half still has to return the row."""
    mind = unique("mind")
    wanted = unique("the proxy sits in front of the gateway")
    await _remember(mind, wanted)

    answer = await _retrieve(
        client, query=wanted, k=5, mind_id=mind, mode="hybrid", debug=False)

    assert "error" not in answer, answer
    # The hybrid search labels its own answer, which is how a dispatch that
    # quietly ran the vector search instead is caught.
    assert answer["mode"] == "hybrid", answer
    assert wanted in {m["content"] for m in answer["memories"]}


# ---- R2: a query longer than a URL can carry ----


@pytest.mark.asyncio
async def test_r2_query_longer_than_a_url_reaches_the_search(client):
    # Nginx caps a request line at 8k and Zeek truncates a logged URI well
    # before that; this is the size a real soul-plus-recent-memory prompt hit.
    mind = unique("mind")
    long_query = "sentinel triage context " * 400 + unique("tail")
    assert len(long_query) > 8000
    await _remember(mind, long_query)

    # `min_score` deliberately: without a threshold the one row this mind
    # holds comes back whatever the query embedded to, so an embedder handed
    # only the head of the prompt would pass. Scored, it has to be the whole
    # string that was embedded.
    answer = await _retrieve(client, query=long_query, k=3, mind_id=mind,
                             min_score=0.5)

    # A 200 alone is not enough. The search used to catch its own exceptions
    # and return {"error": ...} with a 200, which every caller reads as "no
    # relevant memory" rather than as a failure — so an embedder that gives
    # up on a long input is invisible unless the body is checked. The memory
    # whose text *is* the query is what proves it got through.
    assert "error" not in answer, answer
    assert [m["content"] for m in answer["memories"]] == [long_query]


# ---- R3: no query is a refusal, not an empty search ----


@pytest.mark.asyncio
async def test_r3_retrieve_with_no_query_is_refused(client):
    answer = await client.post("/memory/retrieve",
                               json={"k": 3, "mode": "hybrid"})

    assert answer.status_code == 422, answer.text


@pytest.mark.asyncio
async def test_r3_an_empty_query_is_refused_rather_than_searched(client):
    # Distinct from the missing-field case: the live route used to accept ""
    # and answer 200 with {"error": "list index out of range"}, which every
    # caller reads as "no relevant memory" rather than as a refusal.
    answer = await client.post(
        "/memory/retrieve", json={"query": "", "k": 3, "mode": "hybrid"})

    assert answer.status_code == 422, answer.text


# ---- R2's ceiling: refused loudly rather than held in a shared embedder ----


@pytest.mark.asyncio
async def test_r2_a_query_past_the_ceiling_is_refused(client):
    from lucent_api.routers.memory import MAX_QUERY_BYTES

    answer = await client.post(
        "/memory/retrieve",
        json={"query": "x" * (MAX_QUERY_BYTES + 1), "k": 3, "mode": "hybrid"})

    assert answer.status_code == 413, answer.text


@pytest.mark.asyncio
async def test_r2_the_ceiling_is_counted_in_bytes_not_characters(client):
    from lucent_api.routers.memory import MAX_QUERY_BYTES

    # Box-drawing from a quoted TUI transcript is 3 bytes per character, so a
    # string comfortably under the limit in characters is over it in bytes.
    query = "─" * (MAX_QUERY_BYTES // 2)
    assert len(query) < MAX_QUERY_BYTES < len(query.encode("utf-8"))

    answer = await client.post(
        "/memory/retrieve", json={"query": query, "k": 3, "mode": "hybrid"})

    assert answer.status_code == 413, answer.text


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
    out = contextual_retrieval.format_injection(prompt, mind_id="skippy")

    assert captured, "the gateway never called lucent"
    req = captured[-1]
    assert req["method"] == "POST"
    assert "?" not in req["path"], f"the prompt went into the address: {req['path'][:120]}"
    body = json.loads(req["body"])
    assert body["query"] == prompt
    # Comms holds a service credential, so an unnamed mind would retrieve
    # across every mind's feedback memories.
    assert body["mind_id"] == "skippy"
    assert "never guess a pronoun" in out


def test_r5_gateway_retrieval_naming_no_mind_reads_nothing(lucent_stub, monkeypatch):
    """This is the one call in the hive that fires on every single turn, and
    comms' own credential is a service — so an unnamed mind reaches every
    mind's behavioural rules. It returns nothing instead: injecting another
    person's rules into a reply is worse than injecting none."""
    url, captured = lucent_stub
    monkeypatch.setenv("LUCENT_URL", url)
    sys.modules.pop("contextual_retrieval", None)
    sys.path.insert(0, os.path.abspath(os.path.join(NS, "comms")))
    import contextual_retrieval

    monkeypatch.setattr(contextual_retrieval, "LUCENT_URL", url)

    out = contextual_retrieval.format_injection("Sloan is my son")

    assert out == ""
    assert captured == []
