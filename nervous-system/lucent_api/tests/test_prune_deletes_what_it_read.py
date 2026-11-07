"""The prune deletes the rows it names, in the store it read them from.

The pruner used to open its own `sqlite3` file on a path that was not even
the one lucent used, and delete over HTTP. Both halves could be wrong
independently and the report would look identical: a read against an absent
file answers `{"deleted": [], "kept": 0, "errors": []}`, which is a clean
nightly success for a pass that read nothing, and a delete that 404s is
logged and then dropped.

So this holds the pass to three claims at once — it read the rows that are
there, every id it reported deleting is gone from the store, and the row it
reported keeping is still there — and it checks the store with a plain
driver connection rather than by asking the pruner again.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from hive_data import memory as hd_memory
from lucent_api import prune_memory

#: Old enough for the feedback decay to put it under `delete_below`:
#: `exp(-age / (90 * 86400)) < 0.02` needs an age past about 352 days.
STALE_DAYS = 500
SEEDED_AT = 1_700_000_000


@pytest_asyncio.fixture
async def feedback_rows(store):
    """Two feedback memories old enough to prune, and one that is not."""
    stale = []
    for n in range(2):
        written = await hd_memory.store(
            mind_id="prune-test", content=f"a stale feedback memory {n}",
            source="session", data_class="feedback", tier="contextual",
            created_at=SEEDED_AT)
        stale.append(int(written["row"]["id"]))
    written = await hd_memory.store(
        mind_id="prune-test", content="a feedback memory written just now",
        source="session", data_class="feedback", tier="contextual",
        created_at=SEEDED_AT + STALE_DAYS * 86400)
    return stale, int(written["row"]["id"])


@pytest.mark.asyncio
async def test_the_prune_deletes_the_stale_rows_it_read(feedback_rows,
                                                        inspector):
    stale, fresh = feedback_rows

    run = await prune_memory.prune_feedback(
        now=SEEDED_AT + STALE_DAYS * 86400)

    assert run["read"] >= 3, run
    assert set(stale) <= set(run["deleted"]), run
    assert fresh not in run["deleted"], run
    assert run["kept"] >= 1, run
    assert run["errors"] == [], run

    # Every id the pass named, checked in the store rather than in its own
    # report: a pruner that deletes nothing and reports ids is the exact
    # failure the HTTP delete had.
    gone = await inspector.fetch(
        'SELECT "id" FROM memory.memories WHERE "id" = ANY($1::bigint[])',
        list(run["deleted"]))
    assert gone == []
    kept = await inspector.fetchrow(
        'SELECT "id" FROM memory.memories WHERE "id" = $1', fresh)
    assert kept is not None


@pytest.mark.asyncio
async def test_the_run_reports_how_many_rows_each_pruner_read(feedback_rows):
    """A pass that read nothing and a quiet night are the same four dicts
    without this, which is what the scheduler's own error check reads."""
    runs = await prune_memory.run_all()

    assert [r["data_class"] for r in runs] == [
        "ephemeral", "current-state", "future-state", "feedback"]
    assert all("read" in r for r in runs), runs
    assert any(r["read"] for r in runs), runs
