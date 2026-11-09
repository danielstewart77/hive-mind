"""A site with no database named refuses to open, rather than guessing one.

The store is the hive's Postgres and nothing else. A site that fell back to a
file of its own would come up healthy on an empty database nothing else
reads, answer every page with no board, no minds and no accounts, and report
nothing wrong.
"""

from __future__ import annotations

import pytest

import store
from hive_site.testing import run


def test_opening_with_no_database_named_refuses_and_creates_nothing(
        tmp_path, monkeypatch):
    empty = tmp_path / "nothing-named"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setenv(store.DSN_VAR, "")

    with pytest.raises(RuntimeError) as refused:
        run(store.open())

    assert store.DSN_VAR in str(refused.value)
    assert list(empty.iterdir()) == []
