"""The SQLite half opens the file it was told to, or none at all.

`store.py` used to live in this repository's own `src/` and could fall back
to the checkout's `data/hive.db` one directory up. It is installed into two
images now, where that relative guess resolves to a directory inside
site-packages — so an unnamed file is a refusal rather than a default.

This is the failure the whole Postgres migration is about, arrived at from
the other direction: a site that invents a path comes up healthy on an empty
database nothing else reads, answers every page with no board, no minds and
no accounts, and reports nothing wrong.
"""

from __future__ import annotations

import asyncio

import pytest

import store


def test_an_unnamed_file_is_refused_rather_than_guessed(monkeypatch):
    monkeypatch.delenv(store.PATH_VAR, raising=False)
    monkeypatch.setenv(store.DSN_VAR, "")

    with pytest.raises(RuntimeError) as refused:
        store.sqlite_path()

    assert store.PATH_VAR in str(refused.value)


def test_opening_without_a_named_file_creates_nothing(tmp_path, monkeypatch):
    """The refusal has to happen before anything is written.

    A path invented and *then* refused would already have left an empty
    database behind for somebody to find later and believe.
    """
    # A directory of its own: the suite's own fixture has already opened a
    # store on a file in `tmp_path` itself, and what is asked here is
    # whether *this* open writes anything.
    empty = tmp_path / "nothing-named"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.delenv(store.PATH_VAR, raising=False)
    monkeypatch.setenv(store.DSN_VAR, "")

    with pytest.raises(RuntimeError):
        asyncio.run(store.open())

    assert list(empty.iterdir()) == []


def test_the_named_file_is_the_one_opened(tmp_path, monkeypatch):
    named = tmp_path / "somewhere" / "hive.db"
    monkeypatch.setenv(store.PATH_VAR, str(named))
    monkeypatch.setenv(store.DSN_VAR, "")

    asyncio.run(store.open())
    try:
        assert store.sqlite_path() == named
        assert named.is_file()
    finally:
        asyncio.run(store.close())


def test_a_relative_file_is_refused(monkeypatch):
    """A relative path opens a different database per working directory."""
    monkeypatch.setenv(store.PATH_VAR, "data/hive.db")
    monkeypatch.setenv(store.DSN_VAR, "")

    with pytest.raises(RuntimeError) as refused:
        store.sqlite_path()

    assert "relative" in str(refused.value)
