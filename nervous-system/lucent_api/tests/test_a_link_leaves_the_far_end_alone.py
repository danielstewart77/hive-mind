"""Writing something linked to a node that is already there.

Daniel, 2026-11-18: making a new mind relabelled his own Person node,
because the store restamped any existing node that a write linked to with
the writer's data class, tier, source and dates.

Read back with `inspector`, a plain driver connection outside the package,
as the rest of this suite does.
"""

from __future__ import annotations

import json

import pytest
from hive_data import graph as hd_graph

from lucent_api.tests.conftest import unique

_LABELS = ('SELECT data_class, tier, source, as_of, updated_at, properties, '
           'first_name, last_name, mind_id FROM memory.nodes WHERE id = $1')


@pytest.mark.asyncio
async def test_a_write_linked_to_a_node_that_is_already_there_leaves_that_nodes_labels_as_they_were(
    store, inspector, registered_types, a_mind
) -> None:
    owner = unique("owner")
    person = unique("Owner")
    seeded = await hd_graph.node_write(
        owner=owner, type="Person", name=person, mind_id="an-earlier-writer",
        properties={"note": "kept"}, first_name="Ann", last_name="Lee",
        data_class="person", tier="standing", source="user",
        as_of="2026-05-12T23:24:38Z",
        created_at=1_776_000_000.0, updated_at=1_776_000_000.0)
    before = dict(await inspector.fetchrow(_LABELS, seeded["id"]))

    from lucent_api.lucent_graph import graph_upsert_direct

    child = unique("Child")
    answer = json.loads(await graph_upsert_direct(
        entity_type="Person", name=child, data_class="newcomer-identity",
        relation="PARENT_OF", target_name=person, target_type="Person",
        mind_id=a_mind, owner=owner, stamp=owner,
        as_of="2026-11-18T13:01:00+00:00", source="self"))

    assert answer["upserted"] is True and answer["relation_created"] is True, answer
    assert dict(await inspector.fetchrow(_LABELS, seeded["id"])) == before
    # And it is that node the new one hangs from, not a second of its name.
    assert await inspector.fetchval(
        'SELECT target_id FROM memory.edges WHERE source_id = $1 AND type = $2',
        answer["id"], "PARENT_OF") == seeded["id"]
