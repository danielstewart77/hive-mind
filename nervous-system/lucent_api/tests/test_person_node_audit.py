"""`audit_person_nodes` and `update_person_names`, against the real store.

The audit is scoped to one owner's graph rather than to one mind's writes:
keyed on the writer it answered "nothing to fix" over every Person node one
of the owner's *other* minds had written, which reads exactly like a tidy
graph.

Every test files its rows under an owner of its own. The owner partition is
what isolates them — `memory_nodes_owner_type_name` is a real unique index
now, so a fixed name under a fixed owner is a collision in setup rather than
a fresh in-memory database.
"""

from __future__ import annotations

import json

import pytest
from hive_data import graph as hd_graph

from lucent_api.tests.conftest import unique


async def _person(
    *, owner: str, name: str, first_name=None, last_name=None,
    properties: dict | None = None, mind_id: str = "ada",
) -> int:
    row = await hd_graph.node_write(
        owner=owner, type="Person", name=name, mind_id=mind_id,
        properties=properties or {},
        first_name=first_name, last_name=last_name,
        data_class="current-state", tier="contextual")
    return int(row["id"])


# ---- the audit ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_person_with_no_first_name_is_reported(
    store, registered_types
) -> None:
    owner = unique("owner")
    await _person(owner=owner, name="David Stewart", last_name="Stewart")

    from lucent_api.lucent_graph import audit_person_nodes

    answer = json.loads(await audit_person_nodes(owner=owner))

    assert answer["found"] is True
    assert answer["count"] == 1
    assert answer["nodes"][0]["name"] == "David Stewart"
    assert answer["nodes"][0]["first_name"] is None


@pytest.mark.asyncio
async def test_a_person_with_no_last_name_is_reported(
    store, registered_types
) -> None:
    owner = unique("owner")
    await _person(owner=owner, name="Jane", first_name="Jane")

    from lucent_api.lucent_graph import audit_person_nodes

    answer = json.loads(await audit_person_nodes(owner=owner))

    assert answer["count"] == 1
    assert answer["nodes"][0]["name"] == "Jane"
    assert answer["nodes"][0]["last_name"] is None


@pytest.mark.asyncio
async def test_a_person_with_both_names_is_not_reported(
    store, registered_types
) -> None:
    owner = unique("owner")
    await _person(owner=owner, name="Daniel Stewart",
                  first_name="Daniel", last_name="Stewart")
    await _person(owner=owner, name="Sloan")

    from lucent_api.lucent_graph import audit_person_nodes

    answer = json.loads(await audit_person_nodes(owner=owner))

    assert answer["count"] == 1
    assert answer["nodes"][0]["name"] == "Sloan"


@pytest.mark.asyncio
async def test_a_graph_with_nothing_to_fix_answers_not_found(
    store, registered_types
) -> None:
    owner = unique("owner")
    await _person(owner=owner, name="Daniel Stewart",
                  first_name="Daniel", last_name="Stewart")

    from lucent_api.lucent_graph import audit_person_nodes

    answer = json.loads(await audit_person_nodes(owner=owner))

    assert answer == {"found": False, "count": 0, "nodes": []}


@pytest.mark.asyncio
async def test_the_audit_does_not_report_another_owners_person(
    store, registered_types
) -> None:
    mine = unique("owner")
    theirs = unique("owner")
    await _person(owner=mine, name="Ada Person", last_name="Person")
    await _person(owner=theirs, name="Bob Person", last_name="Person",
                  mind_id="zach")

    from lucent_api.lucent_graph import audit_person_nodes

    answer = json.loads(await audit_person_nodes(owner=mine))

    assert answer["count"] == 1
    assert answer["nodes"][0]["name"] == "Ada Person"


@pytest.mark.asyncio
async def test_the_audit_carries_the_properties_blob(
    store, registered_types
) -> None:
    owner = unique("owner")
    await _person(owner=owner, name="Maurice", last_name="X",
                  properties={"church": "Anchor Bend"})

    from lucent_api.lucent_graph import audit_person_nodes

    answer = json.loads(await audit_person_nodes(owner=owner))

    assert answer["nodes"][0]["properties"]["church"] == "Anchor Bend"


@pytest.mark.asyncio
async def test_an_owner_of_none_audits_every_owners_graph(
    store, registered_types
) -> None:
    """`None` is every owner — the console and the hive's own services —
    and is not the same thing as a caller entitled to nobody's graph."""
    mine = unique("owner")
    theirs = unique("owner")
    await _person(owner=mine, name=unique("Mine"))
    await _person(owner=theirs, name=unique("Theirs"), mind_id="zach")

    from lucent_api.lucent_graph import audit_person_nodes

    answer = json.loads(await audit_person_nodes(owner=None))

    owners = {n["name"].rsplit("-", 1)[0] for n in answer["nodes"]}
    assert {"Mine", "Theirs"} <= owners


# ---- the backfill ---------------------------------------------------------


@pytest.mark.asyncio
async def test_both_name_parts_land_on_the_row(
    store, inspector, registered_types
) -> None:
    owner = unique("owner")
    node_id = await _person(owner=owner, name="David Stewart")

    from lucent_api.lucent_graph import update_person_names

    answer = json.loads(await update_person_names(
        name="David Stewart", first_name="David", last_name="Stewart",
        owner=owner))

    assert answer["updated"] is True
    row = await inspector.fetchrow(
        'SELECT first_name, last_name FROM memory.nodes WHERE id = $1', node_id)
    assert row["first_name"] == "David"
    assert row["last_name"] == "Stewart"


@pytest.mark.asyncio
async def test_naming_only_the_first_name_leaves_the_last_one_alone(
    store, inspector, registered_types
) -> None:
    owner = unique("owner")
    node_id = await _person(owner=owner, name="Jane Doe", last_name="Doe")

    from lucent_api.lucent_graph import update_person_names

    answer = json.loads(await update_person_names(
        name="Jane Doe", first_name="Jane", owner=owner))

    assert answer["updated"] is True
    assert "last_name" not in answer
    row = await inspector.fetchrow(
        'SELECT first_name, last_name FROM memory.nodes WHERE id = $1', node_id)
    assert row["first_name"] == "Jane"
    assert row["last_name"] == "Doe"


@pytest.mark.asyncio
async def test_naming_neither_name_is_refused(store, registered_types) -> None:
    from lucent_api.lucent_graph import update_person_names

    answer = json.loads(await update_person_names(
        name="X", owner=unique("owner")))

    assert "first_name" in answer["error"]
    assert "last_name" in answer["error"]


@pytest.mark.asyncio
async def test_a_person_that_is_not_there_is_not_reported_as_updated(
    store, registered_types
) -> None:
    from lucent_api.lucent_graph import update_person_names

    answer = json.loads(await update_person_names(
        name=unique("Nobody"), first_name="X", last_name="Y",
        owner=unique("owner")))

    assert answer["updated"] is False
    assert "reason" in answer


@pytest.mark.asyncio
async def test_an_update_does_not_reach_another_owners_row_of_that_name(
    store, inspector, registered_types
) -> None:
    mine = unique("owner")
    theirs = unique("owner")
    name = unique("Twin")
    my_id = await _person(owner=mine, name=name)
    their_id = await _person(owner=theirs, name=name, mind_id="zach",
                             first_name="OriginalBob", last_name="Surname")

    from lucent_api.lucent_graph import update_person_names

    answer = json.loads(await update_person_names(
        name=name, first_name="UpdatedAda", last_name="Smith", owner=mine))

    assert answer["updated"] is True
    assert (await inspector.fetchval(
        'SELECT first_name FROM memory.nodes WHERE id = $1', my_id)
    ) == "UpdatedAda"
    assert (await inspector.fetchval(
        'SELECT first_name FROM memory.nodes WHERE id = $1', their_id)
    ) == "OriginalBob"
