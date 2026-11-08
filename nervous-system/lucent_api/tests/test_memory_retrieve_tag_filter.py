"""`tag_filter` matches a tag whatever its case, as it did under SQLite.

`LIKE` is case-insensitive in SQLite and case-sensitive in Postgres. Carried
across unwrapped, `tag_filter="Deploy"` stops matching a row tagged `deploy`
and `/memory/retrieve` answers `{"memories": [], "count": 0}` at two
hundred — and "no memories matched" is a normal-sounding sentence a model
will say out loud, so nothing about the failure looks like a failure.

Both directions are checked by one query, because wrapping only the pattern
or only the column fixes one of them and leaves the other silently broken.

The embedder is the only stub: a synchronous HTTP call to Ollama is where
this process stops being ours. The vectors are constructed, the ranking and
the filter are the store's.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from hive_data import memory as hd_memory
from lucent_api import lucent_memory
from lucent_api.tests.conftest import unique

#: The column is `vector(4096)`, so a stub returning anything shorter is
#: refused by the database rather than by the test.
DIMS = 4096


def _vector(lead: float) -> list[float]:
    values = [0.0] * DIMS
    values[0] = lead
    values[1] = 1.0
    return values


@pytest_asyncio.fixture
async def tagged_two_ways(store, monkeypatch):
    """One mind with the same tag written in two cases, plus an untagged row."""
    mind = unique("mind")
    monkeypatch.setattr(lucent_memory, "_embed",
                        lambda text: _vector(1.0))
    for tags, lead in (("deploy,infra", 1.0), ("Deploy", 0.9), ("", 0.8)):
        await hd_memory.store(
            mind_id=mind, content=f"a memory tagged {tags!r}",
            embedding=_vector(lead), tags=tags, source="session",
            data_class=unique("class"), tier="contextual",
            created_at=1_700_000_000)
    return mind


@pytest.mark.asyncio
async def test_a_mixed_case_tag_filter_matches_both_spellings(tagged_two_ways):
    answer = json.loads(await lucent_memory.memory_retrieve(
        query="anything", k=10, mind_ids=[tagged_two_ways],
        tag_filter="Deploy"))

    assert answer["count"] == 2, answer
    assert sorted(m["tags"] for m in answer["memories"]) == [
        "Deploy", "deploy,infra"]


@pytest.mark.asyncio
async def test_a_tag_filter_still_excludes_rows_without_the_tag(
        tagged_two_ways):
    """The fix must not be "match everything", which would also pass above."""
    answer = json.loads(await lucent_memory.memory_retrieve(
        query="anything", k=10, mind_ids=[tagged_two_ways],
        tag_filter="deploy"))

    assert "" not in [m["tags"] for m in answer["memories"]], answer
