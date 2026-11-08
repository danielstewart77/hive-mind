"""`mind_id` is surfaced only on Mind nodes.

`mind_id` is writer-provenance, not a node attribute. Returning it on every
node confuses readers into thinking it is part of the entity. The Mind node
is the exception — there `mind_id` identifies the mind itself.

Seeded through `hive_data.graph`, which is not the code under test, and
asserted on what `lucent_graph.graph_query` answered.
"""

from __future__ import annotations

import json

import pytest
from hive_data import graph as hd_graph

from lucent_api.tests.conftest import unique


@pytest.mark.asyncio
async def test_a_mind_node_carries_the_mind_it_is(
    store, registered_types
) -> None:
    owner = unique("owner")
    name = unique("Mind")
    await hd_graph.node_write(
        owner=owner, type="Mind", name=name, mind_id="testmind-uuid",
        properties={}, data_class="current-state", tier="contextual")

    from lucent_api.lucent_graph import graph_query

    answer = json.loads(await graph_query(name, owners=[owner]))

    assert answer["found"] is True
    assert answer["matches"][0]["properties"]["mind_id"] == "testmind-uuid"


@pytest.mark.asyncio
async def test_a_person_node_does_not_carry_the_mind_that_wrote_it(
    store, registered_types
) -> None:
    owner = unique("owner")
    name = unique("Person")
    await hd_graph.node_write(
        owner=owner, type="Person", name=name, mind_id="testmind-uuid",
        first_name="Test", properties={}, data_class="current-state",
        tier="contextual")

    from lucent_api.lucent_graph import graph_query

    answer = json.loads(await graph_query(name, owners=[owner]))

    assert answer["found"] is True
    assert "mind_id" not in answer["matches"][0]["properties"]


@pytest.mark.asyncio
async def test_an_alias_element_resolves_the_node_it_is_on(
    store, registered_types
) -> None:
    """The alias predicate was `json_extract(...) LIKE ?` over a column that
    is `text` here, so the cast raises on a blob written by something else
    where SQLite answered NULL — and `LIKE` folded case there and does not
    here."""
    owner = unique("owner")
    name = unique("Person")
    await hd_graph.node_write(
        owner=owner, type="Person", name=name, mind_id="testmind-uuid",
        properties={"aliases": ["Coach Manny", "Manny"]})

    from lucent_api.lucent_graph import graph_query

    answer = json.loads(await graph_query("manny", owners=[owner]))

    assert answer["found"] is True
    assert answer["matches"][0]["properties"]["name"] == name


@pytest.mark.asyncio
async def test_a_blob_that_is_not_json_does_not_take_the_read_down(
    store, inspector, registered_types
) -> None:
    """`properties` is a text column. A row written by something else is a
    row the alias cast would raise on, and the raise would be reported as
    "no node of that name" over a graph that has one."""
    owner = unique("owner")
    broken = unique("Person")
    wanted = unique("Person")
    await hd_graph.node_write(owner=owner, type="Person", name=broken,
                           mind_id="testmind-uuid")
    await inspector.execute(
        'UPDATE memory.nodes SET properties = $1 '
        'WHERE owner = $2 AND name = $3',
        "this is not json at all", owner, broken)
    await hd_graph.node_write(owner=owner, type="Person", name=wanted,
                           mind_id="testmind-uuid",
                           properties={"aliases": ["Findable"]})

    from lucent_api.lucent_graph import graph_query

    answer = json.loads(await graph_query("Findable", owners=[owner]))

    assert answer.get("found") is True, answer
    assert answer["matches"][0]["properties"]["name"] == wanted
