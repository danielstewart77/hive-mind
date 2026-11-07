"""The disambiguation scan, and the owner clause on it.

New file, and narrow on purpose: `kg_guards.check_disambiguation` is the one
statement in that module, it moved off SQLite onto the shared access layer,
and its owner predicate changed spelling in the move — `owner IS ?` became
`owner IS NOT DISTINCT FROM $4`, because `= NULL` matches nothing in
Postgres and a NULL owner is a real row here. Nothing drove that predicate
before. The scan's result is handed back to the caller as `similar_nodes`
and pushed to a chat, so a scan that ignores the owner tells one mind which
names exist in somebody else's graph.
"""

from __future__ import annotations

import uuid

import pytest
from hive_data import graph

from .conftest import unique


async def _node(*, owner, name, node_type="Person"):
    return await graph.node_write(
        owner=owner, type=node_type, name=name, mind_id=str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_an_exact_name_in_the_same_graph_is_reported_as_a_merge(
        store, registered_types):
    from lucent_api.kg_guards import check_disambiguation

    owner = unique("owner")
    name = unique("Hive Mind")
    written = await _node(owner=owner, name=name)

    result = await check_disambiguation(
        name.lower(), "Project", "some-mind-id", owner)

    assert result.action == "merge"
    assert [n["id"] for n in result.existing_nodes] == [written["id"]]
    # Cross-type on purpose: a Person and a Project sharing a name are the
    # duplicate this guard exists to catch.
    assert result.existing_nodes[0]["labels"] == ["Person"]


@pytest.mark.asyncio
async def test_another_persons_node_is_not_reported(store, registered_types):
    """The scan is cross-mind within one owner and never cross-owner."""
    from lucent_api.kg_guards import check_disambiguation

    name = unique("Maurice")
    await _node(owner=unique("someone-else"), name=name)

    scoped = await check_disambiguation(name, "Person", "some-mind-id",
                                        unique("owner"))
    assert scoped.action == "proceed"
    assert scoped.existing_nodes == []

    # `None` is every owner, which is a service or an admin — and it has to
    # keep finding the row the scoped call must not.
    unscoped = await check_disambiguation(name, "Person", "some-mind-id", None)
    assert unscoped.action == "merge"


@pytest.mark.asyncio
async def test_a_name_contained_by_an_existing_one_needs_disambiguation(
        store, registered_types):
    """Similar but not equal is a question, not a merge."""
    from lucent_api.kg_guards import check_disambiguation

    owner = unique("owner")
    stem = unique("Stewart")
    await _node(owner=owner, name=f"{stem} Senior")

    result = await check_disambiguation(stem, "Person", "some-mind-id", owner)

    assert result.action == "disambiguate"
    assert [n["name"] for n in result.existing_nodes] == [f"{stem} Senior"]
    assert stem in result.message
