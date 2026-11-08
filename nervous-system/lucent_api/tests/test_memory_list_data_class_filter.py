"""The `data_class` filter on `memory_list`, against the real store.

Bug this guards: `memory_list` silently ignored any `data_class` argument,
so a caller using it as a delete scope — list the filtered rows, DELETE each
id — deleted the entire table. The filter is pinned here as the thing that
narrows the answer, and as the thing that answers nothing when nothing
matches.

The rows are seeded with a plain driver connection and plain SQL rather than
through the module under test, so a filter and a write agreeing on the same
wrong column cannot pass.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from lucent_api import lucent_memory
from lucent_api.tests.conftest import unique


@pytest_asyncio.fixture
async def three_classes(store, inspector):
    """One mind holding one row in each of three data classes.

    Named with `unique()` because the scratch database is shared by every
    test in the run: a fixed data class would make "how many rows are in
    this class" depend on which other tests had already written.
    """
    mind = unique("mind")
    classes = {
        "ephemeral": unique("ephemeral"),
        "feedback": unique("feedback"),
        "person": unique("person"),
    }
    now = 1_700_000_000
    for offset, name in enumerate(classes.values(), start=1):
        await inspector.execute(
            'INSERT INTO memory.memories '
            '("mind_id", "content", "source", "data_class", "tier", '
            ' "created_at") VALUES ($1, $2, $3, $4, $5, $6)',
            mind, f"{name} row", "session", name, "contextual", now + offset)
    return mind, classes


@pytest.mark.asyncio
async def test_a_data_class_filter_returns_only_that_class(three_classes):
    mind, classes = three_classes

    answer = json.loads(await lucent_memory.memory_list(
        mind_ids=[mind], data_class=classes["ephemeral"]))

    assert answer["total"] == 1, answer
    assert [e["data_class"] for e in answer["entries"]] == [
        classes["ephemeral"]]


@pytest.mark.asyncio
async def test_an_unfiltered_read_returns_every_class_the_mind_holds(
        three_classes):
    mind, classes = three_classes

    answer = json.loads(await lucent_memory.memory_list(mind_ids=[mind]))

    assert answer["total"] == 3, answer
    assert sorted(e["data_class"] for e in answer["entries"]) == sorted(
        classes.values())


@pytest.mark.asyncio
async def test_a_data_class_nothing_matches_answers_zero(three_classes):
    mind, _ = three_classes

    answer = json.loads(await lucent_memory.memory_list(
        mind_ids=[mind], data_class=unique("nope")))

    assert answer["total"] == 0
    assert answer["entries"] == []
