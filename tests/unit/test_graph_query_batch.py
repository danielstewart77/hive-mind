"""A graph identity lookup carries its names in the body, all of them at once.

``GET /graph/query?entity_name=X`` put a person's name in a URL, where it
landed verbatim in Zeek's ``http.log``, in uvicorn's access log and in Loki.
It also answered one name per request, so a turn mentioning a dozen people
cost a dozen serial round trips on the hot path of every prompt.

Requirements under test:
  R1   the names travel in the body and every one of them is answered
  R3   each name's answer carries the fields the single-name route carried
  R5   a request past the name ceiling is refused, not silently trimmed
  R6   a request carrying no names answers empty, not as an error
  R10  a name past the length ceiling is refused on its own entry,
       not by refusing the request
  R12  ``depth`` still reaches the traversal

The store is the suite's scratch Postgres and the app is driven over ASGI on
the test's own event loop: ``TestClient`` would run the routes in a portal
thread with a loop of its own, and the pool's connections belong to the loop
that opened them. Every seeded name is minted with ``unique()`` because that
database is shared by the whole run and ``memory_nodes_owner_type_name`` is a
real unique index.
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest
import pytest_asyncio

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

pytest.importorskip("hive_data")

from hive_data import graph as hd_graph  # noqa: E402

from lucent_api.tests.conftest import unique  # noqa: E402

#: These tests are about batch-query semantics, not about scoping, so they
#: present the admin credential — the one value that still reaches every
#: owner's graph. A holder credential would work too and would add an owner
#: filter that has nothing to do with what is being asserted here.
ADMIN_TOKEN = "lucent-admin-for-tests"  # secret-guard: allow


class Seeded:
    """Two people, one organisation and the edges between them.

    Names are per-fixture rather than per-module: the scratch database is
    shared by the run, so a fixed ``('daniel', 'Person', 'Maurice')`` would
    make "how many nodes answer to Maurice" depend on which other test had
    already written one.
    """

    def __init__(self, client: httpx.AsyncClient, owner: str) -> None:
        self.client = client
        self.owner = owner
        self.maurice = unique("Maurice")
        self.surname = unique("Westerdale")
        self.corey = unique("Corey")
        self.church = unique("Anchor Bend Church")
        self.diocese = unique("The Diocese")

    async def query(self, **body) -> dict:
        answer = await self.client.post("/graph/query", json=body)
        assert answer.status_code == 200, answer.text
        return answer.json()


@pytest_asyncio.fixture
async def client(store, monkeypatch):
    """The real app over ASGI, on this test's own event loop."""
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)

    from lucent_api.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://lucent",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    ) as opened:
        yield opened


@pytest_asyncio.fixture
async def seeded(client, registered_types, a_mind) -> Seeded:
    """The rows every test below names.

    Deliberately not a generator: `hive_data` refuses to open a unit of work
    inside one, because a generator holding a pooled connection across its
    yield is a transaction that unwinds whenever the loop's asyncgen
    finalizer gets to it. A `node_write` is a unit, so the seeding is a
    coroutine and the client is the fixture that yields.
    """
    owner = unique("daniel")
    state = Seeded(client, owner)
    maurice = await hd_graph.node_write(
        owner=owner, type="Person", name=state.maurice,
        first_name="Maurice", last_name=state.surname, mind_id=a_mind,
        properties={"relationship": "church friend"},
        data_class="current-state", tier="contextual",
        created_at=1.0, updated_at=1.0)
    await hd_graph.node_write(
        owner=owner, type="Person", name=state.corey,
        first_name="Corey", mind_id=a_mind, properties={},
        data_class="current-state", tier="contextual",
        created_at=1.0, updated_at=1.0)
    church = await hd_graph.node_write(
        owner=owner, type="Organization", name=state.church,
        mind_id=a_mind, properties={}, data_class="current-state",
        tier="contextual", created_at=1.0, updated_at=1.0)
    diocese = await hd_graph.node_write(
        owner=owner, type="Organization", name=state.diocese,
        mind_id=a_mind, properties={}, data_class="current-state",
        tier="contextual", created_at=1.0, updated_at=1.0)
    await hd_graph.edge_write(
        mind_id=a_mind, source_id=int(maurice["id"]),
        target_id=int(church["id"]), type="ATTENDS", owner=owner,
        created_at=1.0)
    # The second hop, which only a depth above one reaches.
    await hd_graph.edge_write(
        mind_id=a_mind, source_id=int(church["id"]),
        target_id=int(diocese["id"]), type="MEMBER_OF", owner=owner,
        created_at=1.0)
    return state


# ---- R1: the names travel in the body, and all of them are answered ----


@pytest.mark.asyncio
async def test_r1_every_name_in_the_body_is_answered_in_one_response(seeded):
    missing = unique("Nobody At All")

    results = (await seeded.query(
        names=[seeded.maurice, seeded.corey, missing]))["results"]

    assert [r["entity"] for r in results] == [
        seeded.maurice, seeded.corey, missing]
    assert [r["found"] for r in results] == [True, True, False]
    assert [r["count"] for r in results] == [1, 1, 0]


@pytest.mark.asyncio
async def test_r1_a_name_repeated_in_one_request_is_answered_once(seeded):
    results = (await seeded.query(
        names=[seeded.maurice, seeded.maurice]))["results"]

    assert [r["entity"] for r in results] == [seeded.maurice]


# ---- R3: the fields each answer carries ----


@pytest.mark.asyncio
async def test_r3_an_answer_carries_the_nodes_properties_and_connections(
    seeded
):
    results = (await seeded.query(names=[seeded.maurice]))["results"]

    match = results[0]["matches"][0]
    assert match["properties"] == {
        "relationship": "church friend",
        "name": seeded.maurice,
        "type": "Person",
        "first_name": "Maurice",
        "last_name": seeded.surname,
        "data_class": "current-state",
        "tier": "contextual",
    }
    assert [c["node"]["name"] for c in match["connections"]] == [seeded.church]
    assert [v["type"] for c in match["connections"]
            for v in c["via"]] == ["ATTENDS"]


@pytest.mark.asyncio
async def test_r3_a_miss_carries_the_name_that_missed(seeded):
    missing = unique("Nobody")

    results = (await seeded.query(names=[missing]))["results"]

    assert results[0] == {
        "entity": missing, "found": False, "count": 0, "matches": []}


# ---- R5: the ceiling ----


@pytest.mark.asyncio
async def test_r5_a_request_at_the_name_ceiling_is_answered_entire(seeded):
    from lucent_api.routers.graph import MAX_QUERY_NAMES

    names = [f"{seeded.maurice}-{i}" for i in range(MAX_QUERY_NAMES)]

    results = (await seeded.query(names=names))["results"]

    assert len(results) == MAX_QUERY_NAMES


@pytest.mark.asyncio
async def test_r5_one_name_past_the_ceiling_is_refused(seeded):
    from lucent_api.routers.graph import MAX_QUERY_NAMES

    names = [f"{seeded.maurice}-{i}" for i in range(MAX_QUERY_NAMES + 1)]

    answer = await seeded.client.post("/graph/query", json={"names": names})

    assert answer.status_code == 413, answer.text


@pytest.mark.asyncio
async def test_r10_a_name_past_the_length_ceiling_is_refused_on_its_own(
    seeded
):
    from lucent_api.routers.graph import MAX_NAME_CHARS

    too_long = "x" * (MAX_NAME_CHARS + 1)

    results = (await seeded.query(
        names=[seeded.maurice, too_long]))["results"]

    by_name = {r["entity"]: r for r in results}
    assert by_name[seeded.maurice]["count"] == 1
    assert str(MAX_NAME_CHARS) in by_name[too_long]["error"]
    assert by_name[too_long]["found"] is False
    # The last length that is looked up at all, rather than refused unread.
    at_the_line = (await seeded.query(
        names=["x" * MAX_NAME_CHARS]))["results"][0]
    assert "error" not in at_the_line


# ---- R6: no names ----


@pytest.mark.asyncio
async def test_r6_no_names_answers_empty_and_successfully(seeded):
    answer = await seeded.client.post("/graph/query", json={"names": []})

    assert answer.status_code == 200
    assert answer.json() == {"results": []}


# ---- R12: depth reaches the traversal ----


@pytest.mark.asyncio
async def test_r12_depth_reaches_the_traversal(seeded):
    """One hop from Maurice is his church; two is the church's diocese. The
    depth the body asks for is the number of hops the walk takes."""
    one = (await seeded.query(names=[seeded.maurice], depth=1))["results"][0]
    two = (await seeded.query(names=[seeded.maurice], depth=2))["results"][0]

    def reached(result: dict) -> set[str]:
        return {c["node"]["name"]
                for match in result["matches"]
                for c in match["connections"]}

    assert reached(one) == {seeded.church}
    assert seeded.diocese in reached(two)


@pytest.mark.asyncio
async def test_a_name_the_store_refuses_does_not_cost_its_siblings(seeded):
    """One token the database cannot read must not empty the whole batch.

    This route is on the hot path of every prompt: the contextual hook
    resolves every candidate name in the turn in one request. A refusal
    about one of them that fails the request is a turn with no identities
    resolved at all, which is the cost the route's own docstring promises
    not to pay.

    A NUL byte is the refusal driven here because Postgres genuinely cannot
    hold one — `text` is UTF-8 and `0x00` is not encodable — so the store
    raises `ValueRefused` for that name and only that name. Nothing is
    stubbed: the refusal comes from the database.
    """
    results = (await seeded.query(
        names=[seeded.maurice, "bad\x00name"]))["results"]

    by_name = {r["entity"]: r for r in results}
    assert by_name[seeded.maurice]["count"] == 1
    assert by_name["bad\x00name"]["found"] is False
    assert by_name["bad\x00name"]["error"]
