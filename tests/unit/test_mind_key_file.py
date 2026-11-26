"""A mind reads its one key from a file the console replaces, at each request.

Requirement 21. The mind's real `runtime_api._own_key` (what every request is
judged against and what registration presents) and `core.mind_key.refresh` are
driven against a real folder; the console's replacement is a temp file renamed
over the old one, as `modules/hive/src/mind_key_files.write` does it. Nothing is
restarted between the two keys.
"""
from __future__ import annotations

import logging
import os

import pytest

from core import mind_key
from minds import runtime_api

FIRST = "first-key-0123456789abcdef"  # secret-guard: allow - invented fixture
SECOND = "second-key-0123456789abcdef"  # secret-guard: allow - invented fixture
ENV_KEY = "env-key-0123456789abcdef"  # secret-guard: allow - invented fixture


def _place(path, text: str) -> None:
    """What the console does: a temp file in the folder, renamed over the key."""
    temporary = path.with_name(".key.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


@pytest.fixture
def folder(tmp_path):
    """A key file path, with the process environment put back afterwards:
    `refresh` sets the key and every name derived from it in `os.environ`, which
    monkeypatch does not restore for a name that was absent."""
    from unittest import mock

    with mock.patch.dict(os.environ):
        for name in ("MIND_KEY", "COMMS_BEARER_TOKEN", "HIVEMIND_BROKER_TOKEN",
                     "LUCENT_BEARER_TOKEN", "HIVE_TOOLS_TOKEN", "OPENAI_API_KEY",
                     "DSH_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MIND_PROXY_KEY"):
            os.environ.pop(name, None)
        os.environ["MIND_KEY_FILE"] = str(tmp_path / "key")
        yield tmp_path / "key"


def test_a_replaced_key_is_the_mind_s_key_on_its_next_request_with_no_restart(folder):
    _place(folder, FIRST + "\n")
    assert runtime_api._own_key() == FIRST
    assert os.environ["COMMS_BEARER_TOKEN"] == FIRST

    _place(folder, SECOND + "\n")

    assert runtime_api._own_key() == SECOND
    # Every name a harness, hook or skill reads follows it, so a turn spawned
    # now carries the new key too.
    for name in mind_key.GENERIC_NAMES:
        assert os.environ[name] == SECOND, name


def test_a_mind_whose_file_is_not_there_at_start_runs_on_the_key_in_its_environment(folder):
    os.environ["MIND_KEY"] = ENV_KEY

    assert runtime_api._own_key() == ENV_KEY

    # And takes the file the moment it appears.
    _place(folder, FIRST)
    assert runtime_api._own_key() == FIRST


@pytest.mark.parametrize("bad", ["", "   \n", "not a key at all", "short-k", "a" * 5000,
                                 "two\nlines-0123456789"])
def test_after_a_good_key_an_empty_or_garbled_file_keeps_the_last_good_key_and_logs_once(
        folder, caplog, bad):
    _place(folder, FIRST)
    assert runtime_api._own_key() == FIRST

    with caplog.at_level(logging.WARNING, logger="hive-mind.mind_key"):
        _place(folder, bad)
        # What the process holds is dropped, so only the file reader's own
        # memory of the last good key can supply it.
        for name in ("MIND_KEY", *mind_key.GENERIC_NAMES):
            os.environ.pop(name, None)
        for _ in range(5):
            assert runtime_api._own_key() == FIRST

    assert len(caplog.records) == 1
    assert FIRST not in caplog.text
    assert not bad.strip() or bad.strip() not in caplog.text


def test_after_a_good_key_a_file_that_goes_missing_or_unreadable_keeps_the_last_good_key(
        folder, caplog):
    _place(folder, FIRST)
    assert runtime_api._own_key() == FIRST

    with caplog.at_level(logging.WARNING, logger="hive-mind.mind_key"):
        folder.unlink()
        for name in ("MIND_KEY", *mind_key.GENERIC_NAMES):
            os.environ.pop(name, None)
        assert runtime_api._own_key() == FIRST
        assert runtime_api._own_key() == FIRST
        folder.write_bytes(b"\xff\xfe\x00bad")
        assert runtime_api._own_key() == FIRST

    assert FIRST not in caplog.text
    # A good key written after the fault is taken again.
    _place(folder, SECOND)
    assert runtime_api._own_key() == SECOND


def test_a_good_key_after_a_fault_is_logged_again_if_it_faults_again(folder, caplog):
    _place(folder, FIRST)
    runtime_api._own_key()
    with caplog.at_level(logging.WARNING, logger="hive-mind.mind_key"):
        _place(folder, "garbage here")
        runtime_api._own_key()
        _place(folder, SECOND)
        assert runtime_api._own_key() == SECOND
        _place(folder, "garbage again")
        assert runtime_api._own_key() == SECOND

    assert len(caplog.records) == 2


def test_a_mind_with_a_key_file_never_answers_its_bot_with_the_old_gateway_value(folder):
    # Told where its key file is but not yet able to read it: still not the
    # open route, and never the value it started with.
    token = mind_key.proactive_token({"MIND_KEY_FILE": str(folder)}, "old-gateway-value-0123456789")

    assert token and token != "old-gateway-value-0123456789"


# --- the console's half: how the file is written, and how the mind sees it -----

import importlib.util
import threading
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[2] / "modules" / "hive" / "src" / "mind_key_files.py"
_spec = importlib.util.spec_from_file_location("console_mind_key_files", _SOURCE)
mind_key_files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mind_key_files)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(mind_key_files, "KEY_ROOT", tmp_path)
    monkeypatch.delenv("HIVE_MIND_UID", raising=False)
    return tmp_path


@pytest.fixture
def clean_environment():
    from unittest import mock

    with mock.patch.dict(os.environ):
        yield


def test_a_reader_of_the_key_file_never_sees_half_a_key_while_the_console_replaces_it(root):
    a, b = "A" * 200 + "-first", "B" * 200 + "-second"
    mind_key_files.write("ada", a)
    stop, seen = threading.Event(), set()

    def reader():
        path = mind_key_files.path("ada")
        while not stop.is_set():
            seen.add(path.read_text().strip())

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(300):
            mind_key_files.write("ada", b if index % 2 == 0 else a)
    finally:
        stop.set()
        thread.join()

    assert seen <= {a, b}
    assert seen  # it did read


def test_a_write_that_fails_part_way_leaves_the_old_key_and_no_stray_file(root, monkeypatch):
    mind_key_files.write("ada", FIRST)

    def refuse(*args, **kwargs):
        raise OSError(5, "input/output error")

    monkeypatch.setattr(mind_key_files.os, "replace", refuse)
    with pytest.raises(mind_key_files.KeyFileError):
        mind_key_files.write("ada", SECOND)

    assert mind_key_files.read("ada") == FIRST
    assert sorted(p.name for p in (root / "ada").iterdir()) == ["key"]


@pytest.mark.parametrize("bad", ["", "short", "has a space-0123456789", "a\nb-0123456789"])
def test_a_value_that_is_not_the_shape_of_a_key_is_never_written(root, bad):
    mind_key_files.write("ada", FIRST)

    with pytest.raises(mind_key_files.KeyFileError):
        mind_key_files.write("ada", bad)

    assert mind_key_files.read("ada") == FIRST


def test_what_the_console_writes_a_mind_accepts_and_the_two_shapes_agree(root, clean_environment):
    assert mind_key_files.KEY_SHAPE.pattern == mind_key.KEY_SHAPE.pattern
    os.environ["MIND_KEY_FILE"] = str(mind_key_files.write("ada", FIRST))
    os.environ.pop("MIND_KEY", None)

    assert runtime_api._own_key() == FIRST

    mind_key_files.write("ada", SECOND)  # the console replaces it; no restart

    assert runtime_api._own_key() == SECOND
