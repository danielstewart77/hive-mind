"""A graph identity lookup carries its names in the body, all of them at once.

``GET /graph/query?entity_name=X`` put a person's name in a URL, where it
landed verbatim in Zeek's ``http.log``, in uvicorn's access log and in Loki.
It also answered one name per request, so a turn mentioning a dozen people
cost a dozen serial round trips on the hot path of every prompt.

Requirements under test:
  R1   the names travel in the body and every one of them is answered
  R3   each name's answer carries the fields the single-name route carried
  R5   a request past the name ceiling is refused, not silently trimmed
  R6   a request carrying no names answers empty, not as an error
  R10  one unanswerable name costs that name and no other
  R12  ``depth`` still reaches the traversal
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))


def _seeded_conn() -> sqlite3.Connection:
    """Two people, one mind, one edge — the state the assertions name."""
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
    conn.execute(
        "INSERT INTO nodes (mind_id, type, name, first_name, last_name, properties,"
        " data_class, tier, created_at) VALUES"
        " (?, 'Person', 'Maurice Westerdale', 'Maurice', 'Westerdale',"
        " '{\"relationship\": \"church friend\"}', 'current-state', 'contextual', ?)",
        ("seed-uuid", now),
    )
    conn.execute(
        "INSERT INTO nodes (mind_id, type, name, first_name, properties,"
        " data_class, tier, created_at) VALUES"
        " (?, 'Person', 'Corey Means', 'Corey', '{}', 'current-state', 'contextual', ?)",
        ("seed-uuid", now),
    )
    conn.execute(
        "INSERT INTO nodes (mind_id, type, name, properties, data_class, tier, created_at)"
        " VALUES (?, 'Organization', 'Anchor Bend Church', '{}', 'current-state',"
        " 'contextual', ?)",
        ("seed-uuid", now),
    )
    conn.execute(
        "INSERT INTO edges (mind_id, source_id, target_id, type, created_at)"
        " VALUES (?, 1, 3, 'ATTENDS', ?)",
        ("seed-uuid", now),
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


# ---- R1: the names travel in the body, and all of them are answered ----


def test_r1_every_name_in_the_body_is_answered_in_one_response(client):
    resp = client.post(
        "/graph/query",
        json={"names": ["Maurice", "Corey Means", "Nobody At All"]},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["entity"] for r in results] == ["Maurice", "Corey Means", "Nobody At All"]
    assert [r["found"] for r in results] == [True, True, False]
    assert [r["count"] for r in results] == [1, 1, 0]


def test_r1_a_name_repeated_in_one_request_is_answered_once(client):
    results = client.post(
        "/graph/query", json={"names": ["Maurice", "Maurice"]}
    ).json()["results"]
    assert [r["entity"] for r in results] == ["Maurice"]


# ---- R3: the fields each answer carries ----


def test_r3_an_answer_carries_the_nodes_properties_and_connections(client):
    results = client.post("/graph/query", json={"names": ["Maurice"]}).json()["results"]
    match = results[0]["matches"][0]
    assert match["properties"] == {
        "relationship": "church friend",
        "name": "Maurice Westerdale",
        "type": "Person",
        "first_name": "Maurice",
        "last_name": "Westerdale",
        "data_class": "current-state",
        "tier": "contextual",
    }
    assert [c["node"]["name"] for c in match["connections"]] == ["Anchor Bend Church"]
    assert [v["type"] for c in match["connections"] for v in c["via"]] == ["ATTENDS"]


def test_r3_a_miss_carries_the_name_that_missed(client):
    results = client.post("/graph/query", json={"names": ["Nobody"]}).json()["results"]
    assert results[0] == {"entity": "Nobody", "found": False, "count": 0, "matches": []}


# ---- R5: the ceiling ----


def test_r5_two_hundred_and_fifty_six_names_are_answered(client):
    names = [f"Name{i}" for i in range(256)]
    resp = client.post("/graph/query", json={"names": names})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 256


def test_r5_two_hundred_and_fifty_seven_names_are_refused(client):
    names = [f"Name{i}" for i in range(257)]
    assert client.post("/graph/query", json={"names": names}).status_code == 413


def test_r10_a_name_longer_than_two_hundred_characters_is_refused_on_its_own(client):
    resp = client.post("/graph/query", json={"names": ["Maurice", "x" * 201]})
    assert resp.status_code == 200
    by_name = {r["entity"]: r for r in resp.json()["results"]}
    assert by_name["Maurice"]["count"] == 1
    assert "exceeds 200" in by_name["x" * 201]["error"]
    assert by_name["x" * 201]["found"] is False
    # 200 is the last length that is looked up at all.
    at_the_line = client.post("/graph/query", json={"names": ["x" * 200]})
    assert "error" not in at_the_line.json()["results"][0]


# ---- R6: no names ----


def test_r6_no_names_answers_empty_and_successfully(client):
    resp = client.post("/graph/query", json={"names": []})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


# ---- R10: one bad name costs only itself ----


def test_r10_a_name_the_store_cannot_answer_does_not_cost_its_siblings(
    client, monkeypatch
):
    from lucent_api import lucent_graph

    real = lucent_graph.graph_query

    def explode_on_one(entity_name: str, **kwargs):
        if entity_name == "Boom":
            raise sqlite3.OperationalError("LIKE or GLOB pattern too complex")
        return real(entity_name=entity_name, **kwargs)

    monkeypatch.setattr(lucent_graph, "graph_query", explode_on_one)

    results = client.post(
        "/graph/query", json={"names": ["Maurice", "Boom", "Corey Means"]}
    ).json()["results"]
    by_name = {r["entity"]: r for r in results}
    assert by_name["Maurice"]["count"] == 1
    assert by_name["Corey Means"]["count"] == 1
    assert by_name["Boom"]["found"] is False
    assert "pattern too complex" in by_name["Boom"]["error"]


# ---- R12: depth reaches the traversal ----


def test_r12_depth_reaches_the_traversal(client, monkeypatch):
    from lucent_api import lucent_graph

    seen: list[int] = []
    real = lucent_graph.graph_query

    def spy(entity_name: str, mind_id: str = "", depth: int = 1):
        seen.append(depth)
        return real(entity_name=entity_name, mind_id=mind_id, depth=depth)

    monkeypatch.setattr(lucent_graph, "graph_query", spy)

    client.post("/graph/query", json={"names": ["Maurice"], "depth": 3})
    client.post("/graph/query", json={"names": ["Maurice"]})
    assert seen == [3, 1]
