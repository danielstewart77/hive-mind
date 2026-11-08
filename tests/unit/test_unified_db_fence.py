"""A write to a retired store fails, and the way back puts it exactly back.

Every assertion here is against a real SQLite writer on a real file, because
the whole question is what `sqlite3` does to a path, and the answer is not
the obvious one: a path with no file there is *created*, which means moving a
store aside is not a fence at all — it is a way to mint an empty second
store that reports success. What the tests drive is the thing that does hold.

`unified_db.schema_map` is pointed at a temporary checkout root rather than
reimplemented, so these run against the map the cutover uses.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from unified_db import fence, schema_map


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A tree shaped like the real one, with a store in every place."""
    for rel in list(schema_map.SOURCES.values()) + list(
            schema_map.QUIET_COPIES.values()):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE conversations (id TEXT, body TEXT)")
        connection.execute(
            "INSERT INTO conversations VALUES ('one', 'a real turn')")
        connection.commit()
        connection.close()
    monkeypatch.setenv(fence.JOURNAL_VAR, str(tmp_path / "fence.json"))
    return tmp_path


def _write(path: Path) -> None:
    """What a stray writer does: open the store and append a turn."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS conversations "
                           "(id TEXT, body TEXT)")
        connection.execute(
            "INSERT INTO conversations VALUES ('two', 'a second copy')")
        connection.commit()
    finally:
        connection.close()


def test_a_write_to_a_retired_store_is_refused(checkout):
    """The requirement, driven by the thing it is about.

    No mocking: the store is retired by the real fence and then opened by
    the real driver, which is the only way to know whether the fence holds.
    """
    store = checkout / schema_map.SOURCES["lucent"]
    fence.raise_fence(str(checkout))

    with pytest.raises(sqlite3.OperationalError) as refused:
        _write(store)

    assert "unable to open database file" in str(refused.value)


def test_moving_the_store_aside_without_the_placeholder_mints_a_second_one(
        checkout):
    """Why the fence is a directory, asserted rather than claimed.

    This is the defect the story is about, reproduced: a writer handed a
    path with nothing there gets a brand-new store, writes into it, and
    reports success. If this ever stops being true the placeholder is
    unnecessary — and if the placeholder is ever removed, this is what the
    hive goes back to.
    """
    store = checkout / schema_map.SOURCES["lucent"]
    store.rename(store.with_suffix(".db.moved"))

    _write(store)

    assert store.exists()
    rows = sqlite3.connect(store).execute(
        "SELECT body FROM conversations").fetchall()
    assert rows == [("a second copy",)]


def test_the_retired_store_keeps_its_bytes(checkout):
    """Retired, not deleted. The file is the way back."""
    store = checkout / schema_map.SOURCES["sessions"]
    before = store.read_bytes()

    report = fence.raise_fence(str(checkout))

    entry = next(e for e in report.fenced if e.source == "sessions")
    assert Path(entry.moved_to).read_bytes() == before


def test_a_quiet_copy_is_fenced_with_the_store_it_shadows(checkout):
    """`--only lucent` has to mean both of lucent's files.

    The copy in `hive_mind/data/` is the one a revert run from the host
    checkout finds, and leaving it open while fencing the live file is the
    exact pair this closes.
    """
    report = fence.raise_fence(str(checkout), sources={"lucent"})

    fenced = {e.source for e in report.fenced}
    assert fenced == {"lucent", "quiet:lucent"}
    assert (checkout / schema_map.SOURCES["sessions"]).is_file()


def test_the_revert_puts_every_store_back_byte_for_byte(checkout):
    """The way back is a command sequence by decision, so it has to work."""
    befores = {name: (checkout / rel).read_bytes()
               for name, rel in schema_map.SOURCES.items()}
    fence.raise_fence(str(checkout))

    report = fence.lower_fence()

    assert report.ok, report.moved
    for name, rel in schema_map.SOURCES.items():
        path = checkout / rel
        assert path.is_file(), f"{name} did not come back"
        assert path.read_bytes() == befores[name]
    # And a writer works again, which is what a revert is for.
    _write(checkout / schema_map.SOURCES["lucent"])


def test_the_revert_leaves_a_placeholder_somebody_has_been_using(checkout):
    """A directory that has grown contents is not ours to delete."""
    store = checkout / schema_map.SOURCES["tools"]
    fence.raise_fence(str(checkout))
    (store / "somebody-elses-file").write_text("mine")

    report = fence.lower_fence()

    assert not report.ok
    assert any("tools" in line and "somebody-elses-file" in line
               for line in report.moved), report.moved
    assert (store / "somebody-elses-file").read_text() == "mine"


def test_the_proof_reports_a_retired_store_that_grew(checkout):
    """What nothing asserted: the files are inspected, not assumed frozen."""
    fence.raise_fence(str(checkout))
    entry = fence.read_journal().fenced[0]
    Path(entry.moved_to).write_bytes(
        Path(entry.moved_to).read_bytes() + b"appended")

    report = fence.inspect()

    assert not report.ok
    assert any(entry.source in line and "wrote to a retired store" in line
               for line in report.moved), report.moved


def test_the_proof_reports_a_retired_store_opened_without_growing(checkout):
    """Same size is not the same file. The modification time is checked too."""
    fence.raise_fence(str(checkout))
    entry = fence.read_journal().fenced[0]
    moved = Path(entry.moved_to)
    moved.write_bytes(moved.read_bytes())          # same bytes, new mtime
    import os
    os.utime(moved, (entry.mtime + 60, entry.mtime + 60))

    report = fence.inspect()

    assert not report.ok
    assert any("modification time" in line for line in report.moved), \
        report.moved


def test_the_proof_reports_a_fence_that_has_been_taken_down(checkout):
    """A placeholder replaced by a file is a writer creating a second store."""
    fence.raise_fence(str(checkout))
    store = checkout / schema_map.SOURCES["console"]
    (store / fence.MARKER_NAME).unlink()
    store.rmdir()
    _write(store)

    report = fence.inspect()

    assert not report.ok
    assert any("console" in line and "not a directory" in line
               for line in report.moved), report.moved


def test_the_proof_passes_on_stores_nothing_touched(checkout):
    fence.raise_fence(str(checkout))

    report = fence.inspect()

    assert report.ok, report.moved
    assert len(report.fenced) == len(schema_map.SOURCES) + len(
        schema_map.QUIET_COPIES)


def test_a_second_run_does_not_bury_a_placeholder_inside_a_placeholder(
        checkout):
    first = fence.raise_fence(str(checkout))

    second = fence.raise_fence(str(checkout))

    assert second.fenced == []
    assert len(second.absent) == len(first.fenced)
    assert (checkout / schema_map.SOURCES["lucent"] / fence.MARKER_NAME
            ).is_file()


def test_the_way_back_is_written_where_it_can_be_read(checkout):
    """The sequence goes in the journal and inside each placeholder.

    A revert that is a command sequence only works if the sequence survives
    the terminal that printed it.
    """
    report = fence.raise_fence(str(checkout))
    entry = next(e for e in report.fenced if e.source == "broker")

    journal = json.loads((checkout / "fence.json").read_text())
    marker = (Path(entry.path) / fence.MARKER_NAME).read_text()

    assert any(entry.moved_to in line for line in journal["revert"])
    assert entry.moved_to in marker
    assert "unified_db.fence --revert" in marker
    assert any("--revert" in line for line in fence.revert_commands())


def test_inspecting_without_a_journal_says_so_rather_than_passing(checkout):
    """An absent journal must not read as a clean proof.

    The whole failure mode of this story is a check that is green because it
    looked at nothing.
    """
    with pytest.raises(FileNotFoundError) as refused:
        fence.inspect()

    assert fence.JOURNAL_VAR in str(refused.value)


# ---------------------------------------------------------------------------
# The proof the operator actually reads
# ---------------------------------------------------------------------------


def test_the_postflight_reports_the_retired_stores_unmoved(checkout):
    """The check belongs in the report somebody runs, not beside the fence.

    A proof split across two commands is a proof somebody runs half of.
    """
    import asyncio
    from unified_db import postflight

    fence.raise_fence(str(checkout))

    state, detail = asyncio.run(postflight._retired_stores(None))

    assert state == "ok", detail
    assert "unmoved" in detail


def test_the_postflight_fails_when_a_retired_store_was_written_to(checkout):
    import asyncio
    from unified_db import postflight

    fence.raise_fence(str(checkout))
    entry = fence.read_journal().fenced[0]
    _write_retired(Path(entry.moved_to))

    state, detail = asyncio.run(postflight._retired_stores(None))

    assert state == "fail"
    assert entry.source in detail


def test_the_postflight_will_not_pass_on_a_journal_naming_nothing(checkout):
    """Green because it looked at nothing is this story's whole failure mode."""
    import asyncio
    from unified_db import postflight

    (checkout / "fence.json").write_text('{"at": 0, "fenced": [], '
                                         '"absent": []}\n')

    state, detail = asyncio.run(postflight._retired_stores(None))

    assert state == "fail"
    assert "looked at nothing" in detail


def test_the_postflight_reports_an_unfenced_host_as_unchecked_not_passing(
        checkout):
    """No journal means the fence was never raised here. Not a pass."""
    import asyncio
    from unified_db import postflight

    with pytest.raises(postflight.Absent):
        asyncio.run(postflight._retired_stores(None))


def _write_retired(path: Path) -> None:
    """A stray writer appending to the file that was moved aside."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO conversations VALUES ('three', 'written after')")
        connection.commit()
    finally:
        connection.close()
