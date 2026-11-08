"""A fresh database can take a graph write, because the service seeds it.

`memory.nodes.type` is a real foreign key into `memory.schema_types`, and
the schema load creates that table empty. SQLite never enforced the key —
lucent's connection never set the pragma — so for years the registry was
decoration and an unseeded one cost nothing.

It costs everything now. Unseeded, `_validate_label` refuses every single
graph write with literally `Must be one of: []` and `GET /graph/schema`
answers an empty registry. That is not an edge: it is a fresh install, CI, a
dev box, and a revert. Only the one hand-loaded database on this host has
rows in it, and nothing in the service was putting them there — the seed had
been called by the schema init that this story replaced, and died with it.

So this test gets a database of its own, loaded and not seeded, which is the
one condition the suite's shared fixtures deliberately never leave it in.
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

import hive_data
from hive_data.testing import claimed_database

from .conftest import ADMIN_DSN, NO_SERVER, _SCHEMA_DIR


@pytest_asyncio.fixture
async def unseeded_dsn():
    """A database carrying the schema and nothing else."""
    if not ADMIN_DSN:
        pytest.skip(NO_SERVER)
    async with claimed_database(ADMIN_DSN, "lucent_test_") as dsn:
        target = await asyncpg.connect(dsn)
        try:
            await target.execute((_SCHEMA_DIR / "core.sql").read_text())
            await target.execute((_SCHEMA_DIR / "memory.sql").read_text())
        finally:
            await target.close()
        yield dsn


@pytest.mark.asyncio
async def test_opening_the_store_seeds_the_type_registry(
        unseeded_dsn, monkeypatch):
    from lucent_api import lucent

    monkeypatch.setenv(hive_data.config.DSN_VAR, unseeded_dsn)
    inspector = await asyncpg.connect(unseeded_dsn)
    try:
        assert await inspector.fetchval(
            "SELECT count(*) FROM memory.schema_types") == 0

        await lucent.open_store()
        try:
            seeded = await inspector.fetchval(
                "SELECT count(*) FROM memory.schema_types")
            edges = await inspector.fetchval(
                "SELECT count(*) FROM memory.schema_edges")
        finally:
            await lucent.close_store()
    finally:
        await inspector.close()

    # The declared registry, not a number written here: the test asserts the
    # store holds what `schema_registry` declares, so adding a type to the
    # declaration does not make this fail.
    from lucent_api.schema_registry import SCHEMA_EDGES, SCHEMA_TYPES

    assert seeded == len(SCHEMA_TYPES)
    assert edges == len(SCHEMA_EDGES)


@pytest.mark.asyncio
async def test_a_graph_write_lands_on_a_store_nobody_seeded_by_hand(
        unseeded_dsn, monkeypatch):
    """The symptom, not the mechanism.

    `Must be one of: []` is what a mind actually sees, on every write, on
    any database but the one hand-loaded one.
    """
    from lucent_api import lucent, lucent_graph

    monkeypatch.setenv(hive_data.config.DSN_VAR, unseeded_dsn)
    await lucent.open_store()
    try:
        answer = await lucent_graph.graph_node_create(
            entity_type="Person", name="Maurice", data_class="person",
            properties="{}", mind_id="test-mind", owner="daniel",
            stamp="daniel")
    finally:
        await lucent.close_store()

    import json
    body = json.loads(answer)
    assert body.get("ok") is True, answer
