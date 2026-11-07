"""Save-time dedup compares a write against its own data class, and only it.

`hive_data.memory.similar` takes a limit and a provenance and has no
`data_class`, and both of the obvious ways to fit dedup onto it lose a
memory. `similar(limit=1)` returns the nearest row in the whole store, which
can score over `DEDUP_THRESHOLD` while belonging to a different class
entirely — and lucent then answers `{"deduped": true}` and never writes the
turn's memory, while the hook that asked records a success. Widening the
limit instead makes dedup stop firing, and the store accumulates one
duplicate per turn.

So both directions are pinned: a near-identical memory in another class does
not stop a write, and a near-identical memory in the same class does.

The embedder is the only stub — the point where the process stops being
ours. Both texts embed to the same vector, so cosine is 1.0 and the
threshold is not what either test turns on.
"""

from __future__ import annotations

import itertools
import json

import pytest
import pytest_asyncio

from hive_data import memory as hd_memory
from lucent_api import lucent_memory
from lucent_api.tests.conftest import unique

DIMS = 4096
MIND = "dedup-test"

#: A direction of its own per test. The scratch database is shared across
#: the run and these two tests use real data classes, so two tests writing
#: the same vector would leave the nearest-neighbour query with a tie
#: between them and no defined winner.
_slots = itertools.count()


def _vector(slot: int) -> list[float]:
    values = [0.0] * DIMS
    values[slot % DIMS] = 1.0
    return values


@pytest_asyncio.fixture
async def existing_current_state(store, monkeypatch):
    """One `current-state` memory, and an embedder that matches it exactly."""
    vector = _vector(next(_slots))
    monkeypatch.setattr(lucent_memory, "_embed", lambda text: vector)
    written = await hd_memory.store(
        mind_id=MIND, content=f"an existing memory {unique('n')}",
        embedding=vector, source="session", data_class="current-state",
        tier="contextual", created_at=1_700_000_000)
    return int(written["row"]["id"])


@pytest.mark.asyncio
async def test_a_duplicate_in_another_class_does_not_stop_the_write(
        existing_current_state, inspector):
    answer = json.loads(await lucent_memory.memory_store_direct(
        content="the same thing said as a plan", data_class="future-state",
        source="session", mind_id=MIND))

    assert answer.get("stored") is True, answer
    landed = await inspector.fetchrow(
        'SELECT "data_class" FROM memory.memories WHERE "id" = $1',
        int(answer["id"]))
    assert landed is not None, "the write was reported and is not in the store"
    assert landed["data_class"] == "future-state"


@pytest.mark.asyncio
async def test_a_duplicate_in_the_same_class_is_deduped(
        existing_current_state):
    answer = json.loads(await lucent_memory.memory_store_direct(
        content="the same thing said again", data_class="current-state",
        source="session", mind_id=MIND))

    assert answer.get("deduped") is True, answer
    assert answer["existing_id"] == existing_current_state, answer
