"""The graph is one space per person, built by all of that person's minds.

Every test here drives the real routes with a real credential, because the
decision under test is made from the credential: which owner's graph a call
lands in is read off the row the token resolves to, and reading it off the
body is what this replaced.

Four holders stand in for the four shapes on this hive: two of one person's
minds, one of another person's, and the gateway's own service value. They
are minted per test with names of their own — `memory.credentials.holder` is
a primary key and `memory_nodes_owner_type_name` is a real unique index, so
two tests sharing a holder or an owner collide in setup where each used to
get its own empty in-memory database.

The app is driven over ASGI on this test's own event loop. `TestClient`
would run the routes in a portal thread with a loop of its own, and the
pool's connections belong to the loop that opened them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio

import hive_data
from hive_data import graph as hd_graph

from lucent_api import holder_access as ha
from lucent_api.tests.conftest import unique

ADMIN = "lucent-admin-credential"  # secret-guard: allow


@dataclass
class Hive:
    """One test's holders, owners and client."""

    client: httpx.AsyncClient
    mine: str          # the owner two of the minds answer for
    theirs: str        # another person
    skippy: str        # that owner's mind, keyed by a uuid
    skippy_uuid: str
    mordecai: str      # that owner's other mind, keyed by its holder name
    zach: str          # the other person's mind
    zach_uuid: str
    comms: str         # the gateway's service credential
    admin: str = ADMIN
    tag: str = field(default_factory=lambda: unique("t"))

    def h(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def name(self, stem: str) -> str:
        """A node name no other test in this run also writes."""
        return f"{stem}-{self.tag}"


@pytest_asyncio.fixture
async def hive(store, registered_types, monkeypatch):
    mine = unique("daniel")
    theirs = unique("aiden")
    skippy_uuid = unique("skippy-uuid")
    zach_uuid = unique("zach-uuid")
    holders = {
        "skippy": unique("skippy"),
        "mordecai": unique("mordecai"),
        "zach": unique("zach"),
        "comms": unique("hive-comms"),
    }
    tokens = {k: unique(f"{k}-token") for k in holders}

    # `memory.edges.mind_id` is a real foreign key into `core.minds`, so a
    # mind that writes edges has to be registered with the gateway first.
    for mind_id in (skippy_uuid, holders["mordecai"], zach_uuid):
        await hive_data.core.upsert_mind(
            mind_id=mind_id, name=mind_id,
            gateway_url="http://127.0.0.1:8426", model="claude-opus-5",
            harness="claude", registered_at=1_800_000_000.0,
            last_seen=1_800_000_000.0)

    await ha.put_credential(holders["skippy"], mine, tokens["skippy"],
                            role=ha.MIND, mind_id=skippy_uuid)
    await ha.put_credential(holders["mordecai"], mine, tokens["mordecai"],
                            role=ha.MIND)
    await ha.put_credential(holders["zach"], theirs, tokens["zach"],
                            role=ha.MIND, mind_id=zach_uuid)
    await ha.put_credential(holders["comms"], mine, tokens["comms"],
                            role=ha.SERVICE)
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN)

    from lucent_api.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://lucent"
    ) as client:
        yield Hive(
            client=client, mine=mine, theirs=theirs,
            skippy=tokens["skippy"], skippy_uuid=skippy_uuid,
            mordecai=tokens["mordecai"],
            zach=tokens["zach"], zach_uuid=zach_uuid,
            comms=tokens["comms"],
        )


# ---- helpers --------------------------------------------------------------


async def _seed(hive: Hive, name: str, owner: str | None, *,
                mind_id: str = "seed", entity_type: str = "Person",
                properties: dict | None = None) -> int:
    row = await hd_graph.node_write(
        owner=owner, type=entity_type, name=name, mind_id=mind_id,
        properties=properties or {}, data_class="person", tier="contextual",
        created_at=1.0, updated_at=1.0)
    return int(row["id"])


async def _create(hive: Hive, token: str, name: str,
                  entity_type: str = "Person") -> httpx.Response:
    return await hive.client.post(
        "/graph/nodes", headers=hive.h(token),
        json={"entity_type": entity_type, "name": name,
              "data_class": "person", "mind_id": ""})


async def _query(hive: Hive, token: str, name: str) -> dict:
    answer = await hive.client.post(
        "/graph/query", headers=hive.h(token), json={"names": [name]})
    assert answer.status_code == 200, answer.text
    return answer.json()["results"][0]


async def _row(inspector, name: str, owner: str | None):
    return await inspector.fetchrow(
        'SELECT * FROM memory.nodes WHERE name = $1 '
        'AND owner IS NOT DISTINCT FROM $2', name, owner)


async def _blob(inspector, name: str, owner: str | None) -> dict:
    row = await _row(inspector, name, owner)
    assert row is not None, f"{name!r} is not in {owner!r}'s graph"
    return json.loads(row["properties"]) if row["properties"] else {}


# ---- the hive half --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mind_reads_a_node_its_owners_other_mind_wrote(hive) -> None:
    name = hive.name("Maurice")
    assert (await _create(hive, hive.mordecai, name)).status_code == 200

    assert (await _query(hive, hive.skippy, name))["found"] is True


@pytest.mark.asyncio
async def test_a_node_records_the_owner_of_the_mind_that_wrote_it(
    hive, inspector
) -> None:
    name = hive.name("Clay")
    assert (await _create(hive, hive.zach, name)).status_code == 200

    row = await _row(inspector, name, hive.theirs)
    assert row is not None
    assert row["mind_id"] == hive.zach_uuid


# ---- the partition --------------------------------------------------------


@pytest.mark.asyncio
async def test_another_owners_node_reads_as_absent(hive) -> None:
    name = hive.name("Maurice")
    await _seed(hive, name, owner=hive.mine)

    assert (await _query(hive, hive.zach, name))["found"] is False


@pytest.mark.asyncio
async def test_a_write_onto_another_owners_node_reads_as_absent(
    hive, inspector
) -> None:
    name = hive.name("Maurice")
    await _seed(hive, name, owner=hive.mine, properties={"runs": "homebridge"})

    answer = await hive.client.post(
        "/graph/properties/merge", headers=hive.h(hive.zach),
        json={"entity_type": "Person", "name": name,
              "properties": json.dumps({"planted": True})})

    assert answer.status_code == 200, answer.text
    assert answer.json().get("code") == "not_found", answer.text
    assert "planted" not in await _blob(inspector, name, hive.mine)


@pytest.mark.asyncio
async def test_a_delete_of_another_owners_node_leaves_it_standing(
    hive, inspector
) -> None:
    name = hive.name("Maurice")
    await _seed(hive, name, owner=hive.mine)

    answer = await hive.client.request(
        "DELETE", "/graph/nodes", headers=hive.h(hive.zach),
        json={"entity_type": "Person", "name": name})

    assert answer.json().get("code") == "not_found", answer.text
    assert await _row(inspector, name, hive.mine) is not None


@pytest.mark.asyncio
async def test_mention_search_answers_only_for_the_caller(
    hive, inspector
) -> None:
    mine = hive.name("Maurice Westerdale")
    theirs = hive.name("Clay Homebridge")
    # Stored capitalised and searched in lower case, deliberately. SQLite's
    # `LIKE` folds case and Postgres' does not, so an unwrapped pattern
    # answers "nothing matched" over the row it is looking at.
    await _seed(hive, mine, owner=hive.mine,
                properties={"notes": "Runs Homebridge"})
    await _seed(hive, theirs, owner=hive.theirs,
                properties={"notes": "Runs Homebridge"})

    # `/graph/search` answers with node ids, so the assertion is on the id of
    # the other owner's row rather than on its name.
    my_id = (await _row(inspector, mine, hive.mine))["id"]
    their_id = (await _row(inspector, theirs, hive.theirs))["id"]
    hits = (await hive.client.get(
        "/graph/search", headers=hive.h(hive.skippy),
        params={"text": "homebridge", "limit": 200})).json()

    reached = {h["node_id"] for h in hits}
    assert my_id in reached
    assert their_id not in reached


@pytest.mark.asyncio
async def test_person_search_answers_only_for_the_caller(hive) -> None:
    surname = hive.name("Westerdale")
    await _seed(hive, hive.name("Maurice"), owner=hive.mine)
    await hd_graph.node_write(
        owner=hive.mine, type="Person", name=hive.name("Maurice W"),
        mind_id="seed", last_name=surname, properties={})

    found = (await hive.client.get(
        "/graph/person/search", headers=hive.h(hive.skippy),
        params={"last_name": surname})).json()
    assert found.get("found") is True, found

    denied = (await hive.client.get(
        "/graph/person/search", headers=hive.h(hive.zach),
        params={"last_name": surname})).json()
    assert denied.get("found") is False, denied


@pytest.mark.asyncio
async def test_the_whole_graph_export_answers_only_for_the_caller(
    hive
) -> None:
    mine = hive.name("Maurice")
    theirs = hive.name("Clay")
    await _seed(hive, mine, owner=hive.mine)
    await _seed(hive, theirs, owner=hive.theirs)

    export = (await hive.client.get(
        "/graph/data", headers=hive.h(hive.skippy),
        params={"limit": 2000})).json()

    labels = {n["label"] for n in export["nodes"]}
    assert mine in labels
    assert theirs not in labels


@pytest.mark.asyncio
async def test_the_raw_properties_blob_answers_only_for_the_caller(
    hive
) -> None:
    name = hive.name("Maurice")
    await _seed(hive, name, owner=hive.mine, properties={"private": "yes"})

    denied = (await hive.client.get(
        "/graph/raw-properties", headers=hive.h(hive.zach),
        params={"name": name})).json()
    assert denied.get("found") is False, denied

    mine = (await hive.client.get(
        "/graph/raw-properties", headers=hive.h(hive.skippy),
        params={"name": name})).json()
    assert mine["properties"]["private"] == "yes"


@pytest.mark.asyncio
async def test_a_holder_whose_credential_names_no_user_reaches_nothing(
    hive, inspector
) -> None:
    """The read-side fail-closed branch. A recognised holder answering for
    nobody reaches no owner's graph rather than everyone's — which is what
    the clause SQLite spelled `AND 0` was for, and `AND 0` is not even a
    boolean here, so a mechanical port turns the security branch into an
    error and the tempting repair turns it into a total bypass."""
    name = hive.name("Maurice")
    await _seed(hive, name, owner=hive.mine)
    nobodys = unique("nobodys-token")
    # Minted by hand: `put_credential` refuses a credential naming no user,
    # and the row is the state this guard exists for.
    await inspector.execute(
        'INSERT INTO memory.credentials '
        '(holder, username, role, token_sha, created_at) '
        'VALUES ($1, $2, $3, $4, $5)',
        unique("nobody"), "", ha.MIND, ha.digest(nobodys), 1.0)

    answer = await _query(hive, nobodys, name)

    assert answer["found"] is False, answer


# ---- the one carve-out ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_mind_cannot_change_another_minds_own_node(
    hive, inspector
) -> None:
    name = hive.name("Mordecai")
    await _seed(hive, name, owner=hive.mine, mind_id="mordecai-uuid",
                entity_type="Mind", properties={"soul_values": ["a line"]})

    # An ordinary property, deliberately: writing `soul_values` is refused by
    # the soul boundary whatever this guard does, so a test using one passes
    # while the carve-out is absent.
    answer = await hive.client.post(
        "/graph/properties/merge", headers=hive.h(hive.skippy),
        json={"entity_type": "Mind", "name": name,
              "properties": json.dumps({"harness": "planted"})})

    assert answer.status_code == 403, answer.text
    stored = await _blob(inspector, name, hive.mine)
    assert "harness" not in stored
    assert stored["soul_values"] == ["a line"]


@pytest.mark.asyncio
async def test_a_mind_cannot_delete_another_minds_own_node(
    hive, inspector
) -> None:
    name = hive.name("Mordecai")
    await _seed(hive, name, owner=hive.mine, mind_id="mordecai-uuid",
                entity_type="Mind")

    answer = await hive.client.request(
        "DELETE", "/graph/nodes", headers=hive.h(hive.skippy),
        json={"entity_type": "Mind", "name": name})

    # 403, not a 200 with a code in the body. Every caller on this path is
    # fire-and-forget shell — curl piped into jq, no status check — so a
    # refusal that answers 200 is a refusal nobody notices, and the delete
    # route had no router-level guard to catch it the way merge and remove
    # do.
    assert answer.status_code == 403, answer.text
    assert name in answer.json()["detail"]
    assert hive.skippy_uuid in answer.json()["detail"]
    assert await _row(inspector, name, hive.mine) is not None


@pytest.mark.asyncio
async def test_a_mind_changes_its_own_node(hive, inspector) -> None:
    name = hive.name("Skippy")
    await _seed(hive, name, owner=hive.mine, mind_id=hive.skippy_uuid,
                entity_type="Mind", properties={"role": "operator"})

    answer = await hive.client.post(
        "/graph/properties/merge", headers=hive.h(hive.skippy),
        json={"entity_type": "Mind", "name": name,
              "properties": json.dumps({"harness": "claude"})})

    assert answer.status_code == 200, answer.text
    assert (await _blob(inspector, name, hive.mine))["harness"] == "claude"


@pytest.mark.asyncio
async def test_an_ordinary_node_named_after_a_mind_is_still_its_own_type(
    hive, inspector
) -> None:
    """The carve-out is about the node that *is* a mind. Asked about a Person
    of the same name it used to answer for the Mind node, which made every
    node named after a mind undeletable by anybody."""
    name = hive.name("Hex")
    await _seed(hive, name, owner=hive.mine, mind_id="hex-uuid",
                entity_type="Mind")
    await _seed(hive, name, owner=hive.mine, mind_id=hive.skippy_uuid,
                entity_type="Person")

    answer = await hive.client.request(
        "DELETE", "/graph/nodes", headers=hive.h(hive.skippy),
        json={"entity_type": "Person", "name": name})

    assert answer.json().get("ok") is True, answer.text
    assert (await inspector.fetchval(
        "SELECT COUNT(*) FROM memory.nodes "
        "WHERE name = $1 AND type = 'Mind'", name)) == 1


@pytest.mark.asyncio
async def test_a_mind_cannot_reach_another_minds_own_node_as_an_edge_target(
    hive, inspector
) -> None:
    name = hive.name("Mordecai")
    await _seed(hive, name, owner=hive.mine, mind_id="mordecai-uuid",
                entity_type="Mind",
                properties={"tier": "standing", "data_class": "identity"})

    answer = await hive.client.post(
        "/graph/upsert-direct", headers=hive.h(hive.skippy),
        json={"entity_type": "Person", "name": hive.name("Maurice"),
              "data_class": "person", "mind_id": "",
              "relation": "KNOWS_ABOUT", "target_name": name,
              "target_type": "Mind"})

    assert answer.json().get("upserted") is not True, answer.text
    stored = await _blob(inspector, name, hive.mine)
    assert stored["tier"] == "standing"
    assert stored["data_class"] == "identity"


@pytest.mark.asyncio
async def test_a_minds_own_node_is_found_even_while_it_has_no_owner(
    hive, inspector
) -> None:
    """A mind whose node this store cannot resolve to an owner holds it under
    a null owner — both of the boys' minds do until their credentials are
    minted here. An owner-scoped refusal misses that row, so the mind mints a
    second node of its own name and the empty newcomer shadows the
    soul-bearing original on every read. `owner IS NULL` is also where the
    unique index stops helping: a plain btree does not collide on NULL."""
    name = hive.name("Zach")
    await _seed(hive, name, owner=None, mind_id=hive.zach_uuid,
                entity_type="Mind", properties={"soul_values": ["a line"]})

    answer = await _create(hive, hive.zach, name, entity_type="Mind")

    # Refused as already there, which it is. The mind owns this node and may
    # change it — what it may not do is end up with two.
    assert answer.json().get("code") == "exists", answer.text
    assert (await inspector.fetchval(
        "SELECT COUNT(*) FROM memory.nodes "
        "WHERE name = $1 AND type = 'Mind'", name)) == 1


@pytest.mark.asyncio
async def test_a_full_replace_on_a_minds_own_node_keeps_its_soul(
    hive, inspector
) -> None:
    """`preserve_soul` carries the soul across a blob that omits it. Its
    lookup is owner-scoped, and a lookup that misses erases a soul with a
    200."""
    name = hive.name("Skippy")
    await _seed(hive, name, owner=hive.mine, mind_id=hive.skippy_uuid,
                entity_type="Mind", properties={"soul_values": ["a line"]})

    answer = await hive.client.post(
        "/graph/upsert-direct", headers=hive.h(hive.skippy),
        json={"entity_type": "Mind", "name": name, "data_class": "identity",
              "mind_id": "", "properties": json.dumps({"role": "operator"})})

    assert answer.status_code == 200, answer.text
    stored = await _blob(inspector, name, hive.mine)
    assert stored.get("soul_values") == ["a line"]
    assert stored.get("role") == "operator"


# ---- the machinery --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_service_credential_reaches_every_owners_graph(hive) -> None:
    mine = hive.name("Maurice")
    theirs = hive.name("Clay")
    await _seed(hive, mine, owner=hive.mine)
    await _seed(hive, theirs, owner=hive.theirs)

    for name in (mine, theirs):
        assert (await _query(hive, hive.comms, name))["found"] is True, name


@pytest.mark.asyncio
async def test_the_admin_credential_reaches_every_owners_graph(hive) -> None:
    theirs = hive.name("Clay")
    await _seed(hive, theirs, owner=hive.theirs)

    assert (await _query(hive, hive.admin, theirs))["found"] is True


@pytest.mark.asyncio
async def test_a_service_credential_edits_another_owners_node(
    hive, inspector
) -> None:
    """The console opens whichever mind's page the operator asked for."""
    name = hive.name("Clay")
    await _seed(hive, name, owner=hive.theirs)

    answer = await hive.client.post(
        "/graph/properties/merge", headers=hive.h(hive.comms),
        json={"entity_type": "Person", "name": name, "owner": hive.theirs,
              "properties": json.dumps({"checked": True})})

    assert answer.status_code == 200, answer.text
    assert (await _blob(inspector, name, hive.theirs))["checked"] is True


@pytest.mark.asyncio
async def test_the_admin_credential_edits_another_owners_node(
    hive, inspector
) -> None:
    name = hive.name("Clay")
    await _seed(hive, name, owner=hive.theirs)

    answer = await hive.client.post(
        "/graph/properties/merge", headers=hive.h(hive.admin),
        json={"entity_type": "Person", "name": name, "owner": hive.theirs,
              "properties": json.dumps({"checked": True})})

    assert answer.status_code == 200, answer.text
    assert (await _blob(inspector, name, hive.theirs))["checked"] is True


@pytest.mark.asyncio
async def test_a_node_the_admin_creates_is_readable_by_a_mind(
    hive, monkeypatch
) -> None:
    """The admin value answers for nobody, so a row it creates takes the
    primary user — never the empty string, which no mind can read."""
    monkeypatch.setenv("LUCENT_PRIMARY_USER", hive.mine)
    name = hive.name("NewGuy")

    assert (await _create(hive, hive.admin, name)).status_code == 200

    assert (await _query(hive, hive.skippy, name))["found"] is True


@pytest.mark.asyncio
async def test_the_console_deletes_a_mind_node_with_no_soul_left(
    hive, inspector
) -> None:
    """Emptying a soul and then removing the node is the sanctioned order,
    and the console is the only thing that can do it."""
    name = hive.name("Dragoman")
    await _seed(hive, name, owner=hive.mine, mind_id="dragoman-uuid",
                entity_type="Mind")

    answer = await hive.client.request(
        "DELETE", "/graph/nodes", headers=hive.h(hive.comms),
        json={"entity_type": "Mind", "name": name, "owner": hive.mine})

    assert answer.json().get("ok") is True, answer.text
    assert await _row(inspector, name, hive.mine) is None


# ---- every write path files the row ---------------------------------------


@pytest.mark.asyncio
async def test_each_write_route_records_the_owner(hive, inspector) -> None:
    """`/graph/upsert` is the memory pipeline's own path. A row written
    ownerless there is invisible to every mind afterwards, with a 200."""
    routes = {
        # `/graph/upsert` refuses an orphan, so this one carries the edge it
        # demands; the owner of both ends is what is asserted.
        "/graph/upsert": {
            "entity_type": "Person", "name": hive.name("Upserted"),
            "data_class": "person", "mind_id": "",
            "relation": "MEMBER_OF", "target_name": hive.name("Propel"),
            "target_type": "Organization"},
        "/graph/upsert-direct": {
            "entity_type": "Person", "name": hive.name("Directed"),
            "data_class": "person", "mind_id": ""},
        "/graph/nodes": {
            "entity_type": "Person", "name": hive.name("Created"),
            "data_class": "person", "mind_id": ""},
    }
    for route, body in routes.items():
        answer = await hive.client.post(
            route, headers=hive.h(hive.skippy), json=body)
        assert answer.status_code == 200, (route, answer.text)
        assert await _row(inspector, body["name"], hive.mine) is not None, (
            route, answer.text)


@pytest.mark.asyncio
async def test_an_edge_records_the_owner(hive, inspector) -> None:
    source = hive.name("Maurice")
    target = hive.name("Propel")
    await _seed(hive, source, owner=hive.mine)
    await _seed(hive, target, owner=hive.mine, entity_type="Organization")

    answer = await hive.client.post(
        "/graph/edges", headers=hive.h(hive.skippy),
        json={"source_name": source, "source_type": "Person",
              "target_name": target, "target_type": "Organization",
              "relation": "MEMBER_OF", "data_class": "person",
              "mind_id": ""})

    assert answer.json().get("ok") is True, answer.text
    assert (await inspector.fetchval(
        'SELECT owner FROM memory.edges WHERE id = $1',
        answer.json()["id"])) == hive.mine


@pytest.mark.asyncio
async def test_a_mind_naming_another_owner_on_the_request_is_refused(
    hive, inspector
) -> None:
    """The body carries an `owner` field. A mind may name its own and nothing
    else, or the partition is one request away from nothing."""
    name = hive.name("Maurice")
    answer = await hive.client.post(
        "/graph/nodes", headers=hive.h(hive.zach),
        json={"entity_type": "Person", "name": name, "data_class": "person",
              "mind_id": "", "owner": hive.mine})

    assert answer.status_code == 403, answer.text
    assert await _row(inspector, name, hive.mine) is None


# ---- the filters on the paths that had none -------------------------------


@pytest.mark.asyncio
async def test_removing_a_key_from_another_owners_node_reads_as_absent(
    hive, inspector
) -> None:
    name = hive.name("Maurice")
    await _seed(hive, name, owner=hive.mine, properties={"private": "yes"})

    answer = await hive.client.post(
        "/graph/properties/remove", headers=hive.h(hive.zach),
        json={"entity_type": "Person", "name": name, "keys": ["private"]})

    assert answer.json().get("code") == "not_found", answer.text
    assert (await _blob(inspector, name, hive.mine))["private"] == "yes"


@pytest.mark.asyncio
async def test_an_edge_cannot_be_wired_across_owners(hive, inspector) -> None:
    mine = hive.name("Maurice")
    theirs = hive.name("Clay")
    await _seed(hive, mine, owner=hive.mine)
    their_id = await _seed(hive, theirs, owner=hive.theirs)

    wired = await hive.client.post(
        "/graph/edges", headers=hive.h(hive.zach),
        json={"source_name": theirs, "source_type": "Person",
              "target_name": mine, "target_type": "Person",
              "relation": "FRIEND_OF", "data_class": "person",
              "mind_id": ""})

    assert wired.json().get("code") == "target_not_found", wired.text
    assert (await inspector.fetchval(
        'SELECT COUNT(*) FROM memory.edges '
        'WHERE source_id = $1 OR target_id = $1', their_id)) == 0


@pytest.mark.asyncio
async def test_creating_a_name_another_owner_holds_does_not_report_their_row(
    hive, inspector
) -> None:
    """`code: exists` with their row id is a cross-owner oracle, and it also
    leaves a mind unable to create its own node of that name."""
    name = hive.name("Maurice")
    await _seed(hive, name, owner=hive.mine)

    answer = await _create(hive, hive.zach, name)

    assert answer.json().get("ok") is True, answer.text
    assert await _row(inspector, name, hive.theirs) is not None


@pytest.mark.asyncio
async def test_the_connection_walk_does_not_step_into_another_owners_node(
    hive
) -> None:
    mine = hive.name("Maurice")
    theirs = hive.name("Clay")
    my_id = await _seed(hive, mine, owner=hive.mine)
    their_id = await _seed(hive, theirs, owner=hive.theirs)
    await hd_graph.edge_write(
        mind_id=hive.skippy_uuid, source_id=my_id, target_id=their_id,
        type="FRIEND_OF", owner=hive.mine, created_at=1.0)

    answer = await _query(hive, hive.skippy, mine)

    reached = {
        (c.get("node") or {}).get("name")
        for match in answer["matches"]
        for c in match.get("connections", [])
    }
    assert theirs not in reached


@pytest.mark.asyncio
async def test_disambiguation_does_not_report_another_owners_similar_names(
    hive
) -> None:
    """The similar-node list is handed back to the caller and pushed to a
    chat, so an unscoped scan tells a mind which names exist elsewhere."""
    stem = hive.name("Maurice")
    await _seed(hive, f"{stem}-Westerdale", owner=hive.mine)

    answer = await hive.client.post(
        "/graph/upsert", headers=hive.h(hive.zach),
        json={"entity_type": "Person", "name": stem,
              "data_class": "person", "mind_id": "",
              "relation": "MEMBER_OF", "target_name": hive.name("Propel"),
              "target_type": "Organization"})

    assert "Westerdale" not in json.dumps(answer.json()), answer.text


# ---- the store's refusals reach the caller as refusals --------------------


@pytest.mark.asyncio
async def test_an_edge_for_an_unregistered_mind_is_not_a_two_hundred(
    hive
) -> None:
    """`memory.edges.mind_id` is a foreign key into `core.minds` and
    `memory.nodes.mind_id` is not, so an upsert's node half can land while
    its edge half is refused. A refusal that returns 200 is a refusal nobody
    notices."""
    source = hive.name("Maurice")
    target = hive.name("Propel")
    stranger = unique("unregistered")
    await _seed(hive, source, owner=hive.mine)
    await _seed(hive, target, owner=hive.mine, entity_type="Organization")

    answer = await hive.client.post(
        "/graph/edges", headers=hive.h(hive.comms),
        json={"source_name": source, "source_type": "Person",
              "target_name": target, "target_type": "Organization",
              "relation": "MEMBER_OF", "data_class": "person",
              "owner": hive.mine, "mind_id": stranger})

    assert answer.status_code == 409, answer.text
    assert stranger in answer.json()["detail"]


@pytest.mark.asyncio
async def test_an_upserts_node_half_landing_without_its_edge_says_so(
    hive, inspector
) -> None:
    """An upsert writes a node and then an edge, and only the edge's
    `mind_id` is a foreign key. So the two halves fail differently: the node
    lands and the edge does not, and the caller has to be told both."""
    stranger = unique("unregistered")
    node = hive.name("Maurice")
    target = hive.name("Propel")
    await _seed(hive, target, owner=hive.mine, entity_type="Organization")

    answer = await hive.client.post(
        "/graph/upsert-direct", headers=hive.h(hive.comms),
        json={"entity_type": "Person", "name": node, "data_class": "person",
              "owner": hive.mine, "mind_id": stranger,
              "relation": "MEMBER_OF", "target_name": target,
              "target_type": "Organization"})

    assert answer.status_code == 409, answer.text
    detail = answer.json()["detail"]
    assert node in detail and "MEMBER_OF" in detail, detail
    # The node half really did land, which is the fact the refusal reports.
    assert await _row(inspector, node, hive.mine) is not None
