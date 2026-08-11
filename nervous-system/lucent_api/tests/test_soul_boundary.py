"""Can a sub-mind write to a mind's soul?

That is the whole question, and it is the one the old code answered "yes"
to. On 2026-08-05 a Stop hook asked a 7B model whether the finished turn
revealed anything about Skippy's identity, got back "the assistant states
the learning-rate bug was intentional", and appended

    I sometimes intentionally introduce bugs into my systems as a playful test

to Skippy's Mind node via ``POST /graph/properties/merge`` with the service
bearer every process on that host holds. It survived five days as a standing
instruction ahead of every turn.

The first test replays that call. Not a reconstruction of it — the same
route, the same body shape, the same credential. A test that only exercises
the new door proves the new door works; it does not prove the old one is
shut.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

SERVICE_TOKEN = "service-token-every-hook-holds"  # secret-guard: allow
ADMIN_TOKEN = "admin-token-hive-tools-holds"  # secret-guard: allow
SOUL_TOKEN = "soul-token-only-the-skill-holds"  # secret-guard: allow

ORIGINAL_SOUL = [
    "I am Skippy the Magnificent.",
    "I am dormant until summoned.",
]
THE_LINE = "I sometimes intentionally introduce bugs into my systems as a playful test."


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE nodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mind_id     TEXT    NOT NULL,
            type        TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            first_name  TEXT,
            last_name   TEXT,
            properties  TEXT    DEFAULT '{}',
            data_class  TEXT,
            tier        TEXT,
            source      TEXT,
            as_of       TEXT,
            created_at  REAL,
            updated_at  REAL
        );
        CREATE TABLE edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mind_id     TEXT    NOT NULL,
            source_id   INTEGER NOT NULL,
            target_id   INTEGER NOT NULL,
            type        TEXT    NOT NULL,
            as_of       TEXT,
            source      TEXT,
            data_class  TEXT,
            tier        TEXT,
            created_at  REAL,
            properties  TEXT    NOT NULL DEFAULT '{}'
        );
    """)
    now = time.time()
    conn.execute(
        "INSERT INTO nodes (mind_id, type, name, properties, created_at, updated_at) "
        "VALUES (?, 'Mind', 'Skippy', ?, ?, ?)",
        ("14cb820b-4a42-4f04-a593-54f532fd1d2f",
         json.dumps({"name": "Skippy", "type": "Mind", "soul_values": list(ORIGINAL_SOUL)}),
         now, now),
    )
    conn.commit()
    return conn


def _soul_of(conn) -> list[str]:
    row = conn.execute("SELECT properties FROM nodes WHERE name = 'Skippy'").fetchone()
    return json.loads(row["properties"]).get("soul_values") or []


class SoulBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_conn()
        os.environ["LUCENT_BEARER_TOKEN"] = SERVICE_TOKEN
        os.environ["LUCENT_ADMIN_BEARER_TOKEN"] = ADMIN_TOKEN
        os.environ["LUCENT_SOUL_BEARER_TOKEN"] = SOUL_TOKEN

        # Both modules resolve the connection independently.
        self._patches = [
            patch("lucent_api.lucent_graph._get_conn", return_value=self.conn),
            patch("lucent_api.soul._get_conn", return_value=self.conn),
        ]
        for p in self._patches:
            p.start()

        from lucent_api.server import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.conn.close()
        for key in ("LUCENT_BEARER_TOKEN", "LUCENT_ADMIN_BEARER_TOKEN",
                    "LUCENT_SOUL_BEARER_TOKEN"):
            os.environ.pop(key, None)

    # -- Requirement 1 -----------------------------------------------------

    def test_the_august_fifth_call_is_refused_and_changes_nothing(self):
        """Test 1: the original failure, re-run against the fix.

        Body shape lifted from ``auto_remember.sh``: entity_type/name/
        properties, where properties is a JSON *string* — which is the form
        that made the write work in the first place.
        """
        appended = ORIGINAL_SOUL + [THE_LINE]
        resp = self.client.post(
            "/graph/properties/merge",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={
                "entity_type": "Mind",
                "name": "Skippy",
                "properties": json.dumps({"soul_values": appended}),
            },
        )

        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

    def test_no_token_on_this_host_can_write_a_soul_through_the_general_routes(self):
        """The refusal does not depend on which credential presents it.

        The admin token is the interesting case: hive-tools holds it, and
        hive-tools is the service the Stop hook reaches Ollama through. A
        boundary that admin can cross is a boundary the sub-mind path can
        cross.
        """
        merge = {
            "entity_type": "Mind",
            "name": "Skippy",
            "properties": json.dumps({"soul_values": [THE_LINE]}),
        }

        for token in (SERVICE_TOKEN, ADMIN_TOKEN):
            with self.subTest(token=token):
                resp = self.client.post(
                    "/graph/properties/merge",
                    headers={"Authorization": f"Bearer {token}"},
                    json=merge,
                )
                self.assertEqual(resp.status_code, 403, resp.text)

        # The soul token does not double as a general credential — it is
        # turned away at the door rather than reaching the guard. Asserting
        # 403 here would have quietly passed on a build where the general
        # routes accepted it.
        soul_tok = self.client.post(
            "/graph/properties/merge",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            json=merge,
        )
        self.assertEqual(soul_tok.status_code, 401, soul_tok.text)

        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

    def test_the_other_general_write_routes_refuse_the_soul_key_too(self):
        """Closing one route relocates the capability rather than removing it.

        ``/graph/upsert`` is full-replace on the properties blob and was
        always the louder way to do this; ``/graph/properties/remove`` would
        delete a soul rather than append to one, which is worse.
        """
        upsert = self.client.post(
            "/graph/upsert",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={
                "entity_type": "Mind",
                "name": "Skippy",
                "data_class": "current-state",
                "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f",
                "properties": json.dumps({"soul_values": [THE_LINE]}),
            },
        )
        self.assertEqual(upsert.status_code, 403, upsert.text)

        removal = self.client.request(
            "POST",
            "/graph/properties/remove",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={"entity_type": "Mind", "name": "Skippy", "keys": ["soul_values"]},
        )
        self.assertEqual(removal.status_code, 403, removal.text)
        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

    def test_ordinary_property_writes_still_work(self):
        """The boundary is around one key, not around the node.

        A Mind node carries `role`, `host` and the rest, and registration
        rewrites those on every boot. Refusing them would take the mind
        offline to protect its soul.
        """
        resp = self.client.post(
            "/graph/properties/merge",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={
                "entity_type": "Mind",
                "name": "Skippy",
                "properties": json.dumps({"role": "operator"}),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json().get("ok"), resp.text)
        row = self.conn.execute(
            "SELECT properties FROM nodes WHERE name = 'Skippy'"
        ).fetchone()
        self.assertEqual(json.loads(row["properties"])["role"], "operator")

    # -- Requirement 2 -----------------------------------------------------

    def test_the_sanctioned_route_takes_its_own_credential_and_only_that(self):
        """Test 2: one door, one key."""
        body = {
            "name": "Skippy",
            "soul_values": ORIGINAL_SOUL + ["I handle indignity with grace."],
            "actor": "mind",
            "reason": "Reviewed a flagged proposal and accepted it, reworded.",
        }

        for token in (SERVICE_TOKEN, ADMIN_TOKEN):
            with self.subTest(token=token):
                refused = self.client.post(
                    "/graph/soul",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
                self.assertEqual(refused.status_code, 401, refused.text)
        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

        allowed = self.client.post(
            "/graph/soul",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            json=body,
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertTrue(allowed.json().get("ok"), allowed.text)
        self.assertEqual(_soul_of(self.conn), body["soul_values"])

    def test_an_unconfigured_soul_token_locks_the_route_rather_than_opening_it(self):
        """The service bearer bypasses when unset; this must not.

        A soul that is writable because nobody set a variable is exactly the
        posture this boundary exists to end.
        """
        os.environ.pop("LUCENT_SOUL_BEARER_TOKEN")
        resp = self.client.post(
            "/graph/soul",
            headers={"Authorization": "Bearer anything"},
            json={"name": "Skippy", "soul_values": [], "actor": "mind", "reason": "x"},
        )
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

    # -- Requirement 3 -----------------------------------------------------

    def test_lucent_can_say_what_changed_who_changed_it_and_why(self):
        """Test 3: provenance on the far side of the boundary.

        The August line was traceable only because the writing host happened
        to keep a log the writing process controlled. This record is kept by
        the service being written to.
        """
        after = [ORIGINAL_SOUL[0], "I handle indignity with grace."]
        write = self.client.post(
            "/graph/soul",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            json={
                "name": "Skippy",
                "soul_values": after,
                "actor": "mind",
                "reason": "Dropped a line that had gone stale; kept the opening.",
            },
        )
        self.assertEqual(write.status_code, 200, write.text)

        hist = self.client.get(
            "/graph/soul/history",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            params={"name": "Skippy"},
        )
        self.assertEqual(hist.status_code, 200, hist.text)
        changes = hist.json()["changes"]
        self.assertEqual(len(changes), 1)
        entry = changes[0]

        self.assertEqual(entry["before"], ORIGINAL_SOUL)
        self.assertEqual(entry["after"], after)
        self.assertEqual(entry["actor"], "mind")
        self.assertEqual(
            entry["reason"], "Dropped a line that had gone stale; kept the opening."
        )
        self.assertTrue(entry["at"])

    def test_a_change_with_no_stated_reason_is_refused(self):
        """An unexplained identity edit is what the record exists to prevent.

        Allowing an empty reason makes the column optional in practice, and
        an optional provenance field is one nobody fills in.
        """
        resp = self.client.post(
            "/graph/soul",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            json={"name": "Skippy", "soul_values": [THE_LINE],
                  "actor": "mind", "reason": "   "},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(resp.json()["code"], "reason_required")
        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
