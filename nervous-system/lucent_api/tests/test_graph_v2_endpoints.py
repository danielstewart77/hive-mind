"""The property-safe v2 graph functions.

The legacy `/graph/upsert` path is full-replace on the properties blob and
also rewrites the source node when creating an edge. These cover the surface
that separates the four real intents, and the invariant that fixes the
original bug: creating an edge MUST NOT touch the source node's properties.

Every assertion is read back with `inspector` — a plain driver connection
outside the package — because a round trip through one layer passes even
when the write and the read agree on the same wrong column.

Rows are filed under an owner of their own per test.
`memory_nodes_owner_type_name` is a real unique index now, so a fixed name
under a fixed owner is a collision in setup rather than a fresh database.
"""

from __future__ import annotations

import json
import time

import pytest
from hive_data import graph as hd_graph

from lucent_api.tests.conftest import unique


async def _person(*, owner: str, name: str, props: dict | None = None,
                  first_name=None, last_name=None,
                  mind_id: str = "testmind") -> int:
    row = await hd_graph.node_write(
        owner=owner, type="Person", name=name, mind_id=mind_id,
        properties=props or {}, first_name=first_name, last_name=last_name,
        data_class="current-state", tier="contextual", source="user",
        as_of="2026-05-12T00:00:00Z")
    return int(row["id"])


async def _blob(inspector, node_id: int) -> dict:
    raw = await inspector.fetchval(
        'SELECT properties FROM memory.nodes WHERE id = $1', node_id)
    return json.loads(raw) if raw else {}


# ---- create ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_node_lands_with_the_properties_it_was_given(
    store, inspector, registered_types
) -> None:
    owner = unique("owner")
    name = unique("NewPerson")

    from lucent_api.lucent_graph import graph_node_create

    answer = json.loads(await graph_node_create(
        owner=owner, stamp=owner, entity_type="Person", name=name,
        data_class="current-state", mind_id="testmind",
        properties='{"title": "Engineer"}'))

    assert answer["ok"] is True
    assert answer["name"] == name
    assert (await _blob(inspector, answer["id"]))["title"] == "Engineer"
    assert (await inspector.fetchval(
        'SELECT owner FROM memory.nodes WHERE id = $1', answer["id"])) == owner


@pytest.mark.asyncio
async def test_creating_a_node_that_is_already_there_is_refused(
    store, registered_types
) -> None:
    owner = unique("owner")
    name = unique("Existing")
    await _person(owner=owner, name=name)

    from lucent_api.lucent_graph import graph_node_create

    answer = json.loads(await graph_node_create(
        owner=owner, stamp=owner, entity_type="Person", name=name,
        data_class="current-state", mind_id="testmind"))

    assert answer["ok"] is False
    assert answer["code"] == "exists"


@pytest.mark.asyncio
async def test_an_unregistered_type_is_named_in_the_refusal(
    store, registered_types
) -> None:
    """`memory.nodes.type` is a real foreign key into `memory.schema_types`
    now. An unregistered type has to be refused before the statement, or the
    refusal arrives as a constraint name rather than as the allow-list."""
    from lucent_api.lucent_graph import graph_node_create

    answer = json.loads(await graph_node_create(
        owner=unique("owner"), entity_type="Unicorn", name=unique("X"),
        data_class="current-state", mind_id="testmind"))

    assert "Unicorn" in answer["detail"]
    assert "Person" in answer["detail"]


# ---- merge ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_merge_leaves_the_keys_it_did_not_name(
    store, inspector, registered_types
) -> None:
    """The bug class this function fixes: adding one property must not erase
    the others."""
    owner = unique("owner")
    name = unique("Manny")
    node_id = await _person(
        owner=owner, name=name, first_name="Manny",
        props={"aliases": ["Manny", "Coach Manny"], "title": "Coach"})

    from lucent_api.lucent_graph import graph_properties_merge

    answer = json.loads(await graph_properties_merge(
        owner=owner, entity_type="Person", name=name,
        properties='{"birthday": "1970-01-01"}'))

    assert answer["ok"] is True
    assert answer["merged_keys"] == ["birthday"]
    stored = await _blob(inspector, node_id)
    assert stored["birthday"] == "1970-01-01"
    assert stored["aliases"] == ["Manny", "Coach Manny"]
    assert stored["title"] == "Coach"


@pytest.mark.asyncio
async def test_a_merge_overwrites_the_keys_it_does_name(
    store, inspector, registered_types
) -> None:
    owner = unique("owner")
    name = unique("Manny")
    node_id = await _person(owner=owner, name=name, props={"title": "Coach"})

    from lucent_api.lucent_graph import graph_properties_merge

    await graph_properties_merge(
        owner=owner, entity_type="Person", name=name,
        properties='{"title": "Head Coach"}')

    assert (await _blob(inspector, node_id))["title"] == "Head Coach"


@pytest.mark.asyncio
async def test_a_merged_first_name_goes_to_its_column_and_not_the_blob(
    store, inspector, registered_types
) -> None:
    owner = unique("owner")
    name = unique("Manny")
    node_id = await _person(owner=owner, name=name)

    from lucent_api.lucent_graph import graph_properties_merge

    await graph_properties_merge(
        owner=owner, entity_type="Person", name=name,
        properties='{"first_name": "Manny"}')

    assert (await inspector.fetchval(
        'SELECT first_name FROM memory.nodes WHERE id = $1', node_id)
    ) == "Manny"
    assert "first_name" not in await _blob(inspector, node_id)


@pytest.mark.asyncio
async def test_merging_into_a_node_that_is_not_there_is_refused(
    store, registered_types
) -> None:
    from lucent_api.lucent_graph import graph_properties_merge

    answer = json.loads(await graph_properties_merge(
        owner=unique("owner"), entity_type="Person",
        name=unique("DoesNotExist"), properties='{"title": "x"}'))

    assert answer["ok"] is False
    assert answer["code"] == "not_found"


@pytest.mark.asyncio
async def test_a_merge_onto_another_minds_own_node_is_refused_here(
    store, inspector, registered_types
) -> None:
    """The identity guard belongs where the operation is. This module is
    registered directly as a tool, so a route-only check left a direct call
    to the merge with no identity guard at all."""
    owner = unique("owner")
    name = unique("Mordecai")
    node_id = await hd_graph.node_write(
        owner=owner, type="Mind", name=name, mind_id="mordecai-uuid",
        properties={"soul_values": ["a line"]})

    from lucent_api.lucent_graph import graph_properties_merge

    answer = json.loads(await graph_properties_merge(
        owner=owner, entity_type="Mind", name=name,
        properties='{"harness": "planted"}', mind_id="skippy-uuid"))

    assert answer["code"] == "identity_guard", answer
    stored = await _blob(inspector, node_id["id"])
    assert "harness" not in stored
    assert stored["soul_values"] == ["a line"]


@pytest.mark.asyncio
async def test_a_mind_merges_into_its_own_node(
    store, inspector, registered_types
) -> None:
    owner = unique("owner")
    name = unique("Skippy")
    row = await hd_graph.node_write(
        owner=owner, type="Mind", name=name, mind_id="skippy-uuid",
        properties={"role": "operator"})

    from lucent_api.lucent_graph import graph_properties_merge

    answer = json.loads(await graph_properties_merge(
        owner=owner, entity_type="Mind", name=name,
        properties='{"harness": "claude"}', mind_id="skippy-uuid"))

    assert answer["ok"] is True, answer
    assert (await _blob(inspector, row["id"]))["harness"] == "claude"


# ---- remove ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_remove_drops_only_the_keys_it_named(
    store, inspector, registered_types
) -> None:
    owner = unique("owner")
    name = unique("Manny")
    node_id = await _person(
        owner=owner, name=name,
        props={"title": "Coach", "notes": "x", "aliases": ["Manny"]})

    from lucent_api.lucent_graph import graph_properties_remove

    answer = json.loads(await graph_properties_remove(
        owner=owner, entity_type="Person", name=name, keys=["notes"]))

    assert answer["ok"] is True
    assert answer["removed_keys"] == ["notes"]
    stored = await _blob(inspector, node_id)
    assert "notes" not in stored
    assert stored["title"] == "Coach"
    assert stored["aliases"] == ["Manny"]


@pytest.mark.asyncio
async def test_removing_first_name_clears_its_column(
    store, inspector, registered_types
) -> None:
    owner = unique("owner")
    name = unique("Manny")
    node_id = await _person(owner=owner, name=name, first_name="Manny")

    from lucent_api.lucent_graph import graph_properties_remove

    await graph_properties_remove(
        owner=owner, entity_type="Person", name=name, keys=["first_name"])

    assert (await inspector.fetchval(
        'SELECT first_name FROM memory.nodes WHERE id = $1', node_id)) is None


@pytest.mark.asyncio
async def test_protected_metadata_cannot_be_removed(
    store, registered_types
) -> None:
    """Identity, timestamp and staleness fields stay protected."""
    owner = unique("owner")
    name = unique("Manny")
    await _person(owner=owner, name=name)

    from lucent_api.lucent_graph import graph_properties_remove

    answer = json.loads(await graph_properties_remove(
        owner=owner, entity_type="Person", name=name, keys=["name", "type"]))

    assert answer["ok"] is False
    assert answer["code"] == "protected_metadata"


@pytest.mark.asyncio
async def test_the_vector_store_columns_can_be_stripped(
    store, inspector, registered_types
) -> None:
    """`data_class` / `tier` / `source` leak onto KG nodes from the shared
    write pipeline, and the tidy walk has to be able to strip them — from the
    blob and from their own columns."""
    owner = unique("owner")
    name = unique("Manny")
    node_id = await _person(
        owner=owner, name=name,
        props={"data_class": "person", "tier": "durable", "source": "user"})

    from lucent_api.lucent_graph import graph_properties_remove

    answer = json.loads(await graph_properties_remove(
        owner=owner, entity_type="Person", name=name,
        keys=["data_class", "tier", "source"]))

    assert answer["ok"] is True
    assert sorted(answer["removed_keys"]) == ["data_class", "source", "tier"]
    row = await inspector.fetchrow(
        'SELECT data_class, tier, source FROM memory.nodes WHERE id = $1',
        node_id)
    assert (row["data_class"], row["tier"], row["source"]) == (None, None, None)
    stored = await _blob(inspector, node_id)
    assert "tier" not in stored


# ---- edges ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_creating_an_edge_does_not_touch_the_source_node(
    store, inspector, registered_types, a_mind
) -> None:
    """The regression that bit Manny: creating an edge MUST NOT clobber the
    source node's properties."""
    owner = unique("owner")
    source = unique("Manny")
    target = unique("Zoe")
    manny = await _person(owner=owner, name=source,
                          props={"title": "Coach", "aliases": ["Manny"]})
    zoe = await _person(owner=owner, name=target)
    before = await inspector.fetchval(
        'SELECT properties FROM memory.nodes WHERE id = $1', manny)

    from lucent_api.lucent_graph import graph_edge_create

    answer = json.loads(await graph_edge_create(
        owner=owner, stamp=owner, source_name=source, source_type="Person",
        target_name=target, target_type="Person", relation="PARENT_OF",
        data_class="current-state", mind_id=a_mind))

    assert answer["ok"] is True, answer
    assert (await inspector.fetchval(
        'SELECT properties FROM memory.nodes WHERE id = $1', manny)) == before
    assert (await inspector.fetchval(
        'SELECT type FROM memory.edges WHERE source_id = $1 AND target_id = $2',
        manny, zoe)) == "PARENT_OF"


@pytest.mark.asyncio
async def test_an_edge_from_a_node_that_is_not_there_is_refused(
    store, registered_types, a_mind
) -> None:
    owner = unique("owner")
    target = unique("Target")
    await _person(owner=owner, name=target)

    from lucent_api.lucent_graph import graph_edge_create

    answer = json.loads(await graph_edge_create(
        owner=owner, source_name=unique("Ghost"), source_type="Person",
        target_name=target, target_type="Person", relation="PARENT_OF",
        data_class="current-state", mind_id=a_mind))

    assert answer["ok"] is False
    assert answer["code"] == "source_not_found"


@pytest.mark.asyncio
async def test_an_edge_that_is_already_there_is_refused_as_existing(
    store, registered_types, a_mind
) -> None:
    owner = unique("owner")
    source = unique("Manny")
    target = unique("Zoe")
    await _person(owner=owner, name=source)
    await _person(owner=owner, name=target)

    from lucent_api.lucent_graph import graph_edge_create

    args = dict(
        owner=owner, stamp=owner, source_name=source, source_type="Person",
        target_name=target, target_type="Person", relation="PARENT_OF",
        data_class="current-state", mind_id=a_mind)
    first = json.loads(await graph_edge_create(**args))
    second = json.loads(await graph_edge_create(**args))

    assert first["ok"] is True, first
    assert second["ok"] is False
    assert second["code"] == "edge_exists"


@pytest.mark.asyncio
async def test_an_edge_for_an_unregistered_mind_names_the_mind(
    store, registered_types
) -> None:
    """`memory.edges.mind_id` is a real foreign key into `core.minds` and
    `memory.nodes.mind_id` is not, so a mind can write nodes and have its
    edges refused. The refusal names the mind; `internal_error` does not."""
    owner = unique("owner")
    source = unique("Manny")
    target = unique("Zoe")
    stranger = unique("unregistered-mind")
    await _person(owner=owner, name=source)
    await _person(owner=owner, name=target)

    from lucent_api.lucent_graph import graph_edge_create

    answer = json.loads(await graph_edge_create(
        owner=owner, stamp=owner, source_name=source, source_type="Person",
        target_name=target, target_type="Person", relation="PARENT_OF",
        data_class="current-state", mind_id=stranger))

    assert answer["code"] == "missing_reference", answer
    assert answer["http_status"] == 409
    assert stranger in answer["detail"]


@pytest.mark.asyncio
async def test_an_edge_is_deleted_by_its_tuple(
    store, inspector, registered_types, a_mind
) -> None:
    owner = unique("owner")
    source = unique("Manny")
    target = unique("Zoe")
    manny = await _person(owner=owner, name=source)
    zoe = await _person(owner=owner, name=target)
    await hd_graph.edge_write(
        mind_id=a_mind, source_id=manny, target_id=zoe, type="PARENT_OF",
        owner=owner, created_at=time.time())

    from lucent_api.lucent_graph import graph_edge_delete

    answer = json.loads(await graph_edge_delete(
        owner=owner, source_name=source, source_type="Person",
        target_name=target, target_type="Person", relation="PARENT_OF"))

    assert answer["ok"] is True
    assert answer["deleted"] == 1
    assert (await inspector.fetchval(
        'SELECT COUNT(*) FROM memory.edges WHERE source_id = $1', manny)) == 0


# ---- node delete ----------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_node_takes_the_edges_on_both_sides_with_it(
    store, inspector, registered_types, a_mind
) -> None:
    owner = unique("owner")
    source = unique("Manny")
    manny = await _person(owner=owner, name=source)
    zoe = await _person(owner=owner, name=unique("Zoe"))
    carl = await _person(owner=owner, name=unique("Carl"))
    now = time.time()
    # Manny has one outbound and one inbound edge; both have to go.
    await hd_graph.edge_write(mind_id=a_mind, source_id=manny, target_id=zoe,
                              type="PARENT_OF", owner=owner, created_at=now)
    await hd_graph.edge_write(mind_id=a_mind, source_id=carl, target_id=manny,
                              type="FRIEND_OF", owner=owner, created_at=now)

    from lucent_api.lucent_graph import graph_node_delete

    answer = json.loads(await graph_node_delete(
        entity_type="Person", name=source, owner=owner))

    assert answer["ok"] is True, answer
    assert answer["node_deleted"] == 1
    assert answer["edges_deleted"] == 2
    assert (await inspector.fetchval(
        'SELECT COUNT(*) FROM memory.nodes WHERE id = $1', manny)) == 0
    # The other two nodes survive.
    assert (await inspector.fetchval(
        'SELECT COUNT(*) FROM memory.nodes WHERE owner = $1', owner)) == 2


@pytest.mark.asyncio
async def test_deleting_a_node_that_is_not_there_is_refused(
    store, registered_types
) -> None:
    from lucent_api.lucent_graph import graph_node_delete

    answer = json.loads(await graph_node_delete(
        entity_type="Person", name=unique("Nobody"), owner=unique("owner")))

    assert answer["ok"] is False
    assert answer["code"] == "not_found"


@pytest.mark.asyncio
async def test_a_node_with_recorded_soul_changes_cannot_be_deleted(
    store, inspector, registered_types
) -> None:
    """`memory.soul_changes.node_id` is ON DELETE RESTRICT, so the node
    delete can be refused *after* the edges are gone. One unit for both, and
    a refusal that names the mind rather than a constraint."""
    owner = unique("owner")
    name = unique("Dragoman")
    row = await hd_graph.node_write(
        owner=owner, type="Mind", name=name, mind_id="dragoman-uuid",
        properties={})
    await hd_graph.soul_change_record(
        node_id=row["id"], mind_name=name, mind_id="dragoman-uuid",
        actor="console", reason="seeded", before=[], after=["a line"])

    from lucent_api.lucent_graph import graph_node_delete

    answer = json.loads(await graph_node_delete(
        entity_type="Mind", name=name, owner=owner, unrestricted=True))

    assert answer["code"] == "soul_history_present", answer
    assert answer["http_status"] == 409
    assert name in answer["detail"]
    assert (await inspector.fetchval(
        'SELECT COUNT(*) FROM memory.nodes WHERE id = $1', row["id"])) == 1


@pytest.mark.asyncio
async def test_a_node_with_a_soul_still_on_it_cannot_be_deleted(
    store, registered_types
) -> None:
    """A different set from "has soul history", deliberately: this is the
    `soul_values` property, and both have to refuse."""
    owner = unique("owner")
    name = unique("Dragoman")
    await hd_graph.node_write(
        owner=owner, type="Mind", name=name, mind_id="dragoman-uuid",
        properties={"soul_values": ["a line"]})

    from lucent_api.lucent_graph import graph_node_delete
    from lucent_api.soul import SOUL_KEY_REFUSED

    answer = json.loads(await graph_node_delete(
        entity_type="Mind", name=name, owner=owner, unrestricted=True))

    assert answer["code"] == SOUL_KEY_REFUSED, answer


# ---- the store's own refusals ---------------------------------------------


@pytest.mark.asyncio
async def test_a_store_that_does_not_answer_is_not_an_internal_error() -> None:
    """`Conflict`, `MissingReference`, `TimedOut`, `NoConnectionAvailable`
    and `Unreachable` were impossible against a local file and are routine
    against a socket. Each has to arrive as a refusal that names the rule it
    broke, carrying a status — not as `internal_error` at 200.

    Takes no `store` fixture on purpose: nothing in `hive_data` opens a pool
    on demand, so this is the no-store case and the one every other failure
    mode shares a code path with.
    """
    from lucent_api.lucent_graph import graph_node_create

    answer = json.loads(await graph_node_create(
        owner=unique("owner"), entity_type="Person", name=unique("X"),
        data_class="current-state", mind_id="testmind"))

    assert answer["code"] == "store_not_open", answer
    assert answer["http_status"] == 503
