"""Put src/ on the import path for every test module, not just the ones that
happen to insert it themselves. Without this, a single test file can only be
collected when another module has already run its own sys.path insert.

Also point HIVE_DB_PATH at a per-test temporary file for every test in the
suite. The default resolves to the repo's own data/hive.db, which on a
developer's machine is the live console database holding the real board and
the real logins — a test that forgets to redirect it does not fail, it
quietly edits production and reports green.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(autouse=True)
def _isolated_hive_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
