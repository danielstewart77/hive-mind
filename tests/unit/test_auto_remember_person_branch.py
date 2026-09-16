"""The Stop hook's person branch reads the batched answer correctly.

`auto_remember.sh` decides whether to attach an observation to a person by
reading a count and a node type out of lucent's reply with jq. It is bash, so
there is no function to import — but the two jq filters are the contract
between the script and the route, and both ends of that contract are live
here: the filters are read out of the script file, and the JSON is whatever
the real route returns.

A wrong filter is silent. The branch takes its else arm, the observation is
discarded, and the log blames the name rather than the shape.

Requirements under test:
  R7  the Stop hook's person branch still resolves a name through the body route
  R3  an ambiguous name is still seen as ambiguous, so an observation is never
      attached to whichever person happened to sort first
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))

HOOK = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "..", "minds", "ada", ".claude", "hooks", "auto_remember.sh",
    )
)

# Hooks are per-host installs and ``minds/*/`` is gitignored: this runs on
# every machine with a mind on it and skips on a bare checkout.
pytestmark = [
    pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed"),
    pytest.mark.skipif(
        not os.path.isfile(HOOK), reason="no mind installed in this checkout"
    ),
]


def _filter(var: str) -> str:
    """Read the jq filter the script actually runs for PERSON_COUNT / PERSON_TYPE."""
    src = open(HOOK, encoding="utf-8").read()
    m = re.search(rf"""{var}=\$\(printf '%s' "\$PERSON_RESP" \| jq -r '([^']+)'""", src)
    assert m, f"{var} filter not found in {HOOK}"
    return m.group(1)


def _jq(filt: str, payload: dict) -> str:
    out = subprocess.run(
        ["jq", "-r", filt], input=json.dumps(payload), capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _seeded_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mind_id TEXT NOT NULL,
            type TEXT NOT NULL, name TEXT NOT NULL, first_name TEXT,
            last_name TEXT, properties TEXT DEFAULT '{}', data_class TEXT,
            tier TEXT, source TEXT, as_of TEXT, created_at REAL, updated_at REAL
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, mind_id TEXT NOT NULL,
            source_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            type TEXT NOT NULL, as_of TEXT, source TEXT, data_class TEXT,
            tier TEXT, created_at REAL, properties TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    now = time.time()
    for name, first, last in [
        ("Maurice Westerdale", "Maurice", "Westerdale"),
        ("Daniel Stewart", "Daniel", "Stewart"),
        ("Sloan Stewart", "Sloan", "Stewart"),
    ]:
        conn.execute(
            "INSERT INTO nodes (mind_id, type, name, first_name, last_name,"
            " properties, data_class, tier, created_at)"
            " VALUES (?, 'Person', ?, ?, ?, '{}', 'current-state', 'contextual', ?)",
            ("seed-uuid", name, first, last, now),
        )
    conn.commit()
    return conn


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.delenv("LUCENT_BEARER_TOKEN", raising=False)
    from lucent_api import lucent_graph
    from lucent_api.server import create_app

    conn = _seeded_conn()
    monkeypatch.setattr(lucent_graph, "_get_conn", lambda: conn)
    return TestClient(create_app())


def test_r7_the_person_branch_resolves_a_single_person(client):
    payload = client.post("/graph/query", json={"names": ["Maurice"], "depth": 1}).json()
    assert _jq(_filter("PERSON_COUNT"), payload) == "1"
    assert _jq(_filter("PERSON_TYPE"), payload) == "Person"


def test_r3_the_person_branch_still_sees_an_ambiguous_name_as_ambiguous(client):
    payload = client.post("/graph/query", json={"names": ["Stewart"], "depth": 1}).json()
    assert _jq(_filter("PERSON_COUNT"), payload) == "2"
