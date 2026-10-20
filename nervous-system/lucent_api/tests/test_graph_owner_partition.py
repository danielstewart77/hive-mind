"""The graph is one space per person, built by all of that person's minds.

Every test here drives the real routes with a real credential, because the
decision under test is made from the credential: which owner's graph a call
lands in is read off the row the token resolves to, and reading it off the
body is what this replaced.

Three holders stand in for the three shapes on this hive: two of Daniel's
minds, one of another person's, and the gateway's own service value.
"""
from __future__ import annotations

import json
import os
import sqlite3
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from lucent_api import holder_access as ha
from lucent_api.lucent import _init_schema

SKIPPY = "skippys-own-credential"  # secret-guard: allow
MORDECAI = "mordecais-own-credential"  # secret-guard: allow
ZACH = "zachs-own-credential"  # secret-guard: allow
COMMS = "comms-service-credential"  # secret-guard: allow
ADMIN = "lucent-admin-credential"  # secret-guard: allow

SKIPPY_UUID = "14cb820b-4a42-4f04-a593-54f532fd1d2f"
ZACH_UUID = "82cd16ef-f33e-4a90-9fbc-d24427b96680"


class GraphOwnerPartitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        _init_schema(self.conn)
        ha.init_credentials(self.conn)
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY,
                          role=ha.MIND, mind_id=SKIPPY_UUID)
        ha.put_credential(self.conn, "mordecai", "daniel", MORDECAI, role=ha.MIND)
        ha.put_credential(self.conn, "zach", "aiden", ZACH,
                          role=ha.MIND, mind_id=ZACH_UUID)
        ha.put_credential(self.conn, "hive-comms", "daniel", COMMS, role=ha.SERVICE)
        os.environ["LUCENT_ADMIN_BEARER_TOKEN"] = ADMIN
        self._patches = [
            mock.patch("lucent_api.holder_access.connection", return_value=self.conn),
            mock.patch("lucent_api.lucent_graph._get_conn", return_value=self.conn),
            mock.patch("lucent_api.lucent._get_connection", return_value=self.conn),
        ]
        for p in self._patches:
            p.start()
        from lucent_api.server import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        os.environ.pop("LUCENT_ADMIN_BEARER_TOKEN", None)
        self.conn.close()

    # -- helpers ----------------------------------------------------------
    def _h(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _create(self, token: str, name: str, entity_type: str = "Person") -> object:
        return self.client.post(
            "/graph/nodes",
            headers=self._h(token),
            json={"entity_type": entity_type, "name": name,
                  "data_class": "person", "mind_id": ""},
        )

    def _seed(self, name: str, owner: str, mind_id: str = "seed",
              entity_type: str = "Person", properties: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO nodes (mind_id, owner, type, name, properties, "
            "data_class, tier, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'person', 'contextual', 1.0)",
            (mind_id, owner, entity_type, name, json.dumps(properties or {})),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _query(self, token: str, name: str) -> dict:
        answer = self.client.post(
            "/graph/query", headers=self._h(token), json={"names": [name]}
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()["results"][0]

    def _row(self, name: str, owner: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM nodes WHERE name = ? AND owner IS ?", (name, owner)
        ).fetchone()

    # -- the hive half ----------------------------------------------------
    def test_a_mind_reads_a_node_its_owners_other_mind_wrote(self):
        self._create(MORDECAI, "Maurice")

        self.assertTrue(self._query(SKIPPY, "Maurice")["found"])

    def test_a_node_records_the_owner_of_the_mind_that_wrote_it(self):
        self.assertEqual(self._create(ZACH, "Clay").status_code, 200)

        row = self._row("Clay", "aiden")
        self.assertIsNotNone(row)
        self.assertEqual(row["mind_id"], ZACH_UUID)

    # -- the partition ----------------------------------------------------
    def test_another_owners_node_reads_as_absent(self):
        self._seed("Maurice", owner="daniel")

        self.assertFalse(self._query(ZACH, "Maurice")["found"])

    def test_a_write_onto_another_owners_node_reads_as_absent(self):
        self._seed("Maurice", owner="daniel", properties={"runs": "homebridge"})

        answer = self.client.post(
            "/graph/properties/merge",
            headers=self._h(ZACH),
            json={"entity_type": "Person", "name": "Maurice",
                  "properties": json.dumps({"planted": True})},
        )

        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json().get("code"), "not_found", answer.text)
        stored = json.loads(self._row("Maurice", "daniel")["properties"])
        self.assertNotIn("planted", stored)

    def test_a_delete_of_another_owners_node_leaves_it_standing(self):
        self._seed("Maurice", owner="daniel")

        answer = self.client.request(
            "DELETE", "/graph/nodes", headers=self._h(ZACH),
            json={"entity_type": "Person", "name": "Maurice"},
        )

        self.assertEqual(answer.json().get("code"), "not_found", answer.text)
        self.assertIsNotNone(self._row("Maurice", "daniel"))

    def test_text_search_and_person_search_answer_only_for_the_caller(self):
        self._seed("Maurice Westerdale", owner="daniel",
                   properties={"notes": "runs homebridge"})
        self._seed("Clay Homebridge", owner="aiden",
                   properties={"notes": "runs homebridge"})

        # `/graph/search` answers with node ids, so the assertion is on the
        # id of the other owner's row rather than on its name.
        theirs = self._row("Clay Homebridge", "aiden")["id"]
        mine = self._row("Maurice Westerdale", "daniel")["id"]
        hits = self.client.get(
            "/graph/search", headers=self._h(SKIPPY), params={"text": "homebridge"}
        ).json()
        reached = {h["node_id"] for h in hits}
        self.assertIn(mine, reached)
        self.assertNotIn(theirs, reached)

        persons = self.client.get(
            "/graph/person/search", headers=self._h(ZACH),
            params={"last_name": "Westerdale"},
        ).json()
        self.assertFalse(persons.get("found"), persons)

    def test_the_whole_graph_export_answers_only_for_the_caller(self):
        self._seed("Maurice", owner="daniel")
        self._seed("Clay", owner="aiden")

        mine = self.client.get("/graph/data", headers=self._h(SKIPPY)).json()

        labels = {n["label"] for n in mine["nodes"]}
        self.assertIn("Maurice", labels)
        self.assertNotIn("Clay", labels)

    def test_the_raw_properties_blob_answers_only_for_the_caller(self):
        self._seed("Maurice", owner="daniel", properties={"private": "yes"})

        answer = self.client.get(
            "/graph/raw-properties", headers=self._h(ZACH), params={"name": "Maurice"}
        ).json()

        self.assertFalse(answer.get("found"), answer)

    # -- the one carve-out ------------------------------------------------
    def test_a_mind_cannot_change_another_minds_own_node(self):
        self._seed("Mordecai", owner="daniel", mind_id="mordecai-uuid",
                   entity_type="Mind", properties={"soul_values": ["a line"]})

        # An ordinary property, deliberately: writing `soul_values` is
        # refused by the soul boundary whatever this guard does, so a test
        # using one passes while the carve-out is absent.
        answer = self.client.post(
            "/graph/properties/merge",
            headers=self._h(SKIPPY),
            json={"entity_type": "Mind", "name": "Mordecai",
                  "properties": json.dumps({"harness": "planted"})},
        )

        self.assertEqual(answer.status_code, 403, answer.text)
        stored = json.loads(self._row("Mordecai", "daniel")["properties"])
        self.assertNotIn("harness", stored)
        self.assertEqual(stored["soul_values"], ["a line"])

    def test_a_mind_cannot_delete_another_minds_own_node(self):
        self._seed("Mordecai", owner="daniel", mind_id="mordecai-uuid",
                   entity_type="Mind")

        answer = self.client.request(
            "DELETE", "/graph/nodes", headers=self._h(SKIPPY),
            json={"entity_type": "Mind", "name": "Mordecai"},
        )

        self.assertEqual(answer.json().get("code"), "identity_guard", answer.text)
        self.assertIsNotNone(self._row("Mordecai", "daniel"))

    def test_a_mind_changes_its_own_node(self):
        self._seed("Skippy", owner="daniel", mind_id=SKIPPY_UUID,
                   entity_type="Mind", properties={"role": "operator"})

        answer = self.client.post(
            "/graph/properties/merge",
            headers=self._h(SKIPPY),
            json={"entity_type": "Mind", "name": "Skippy",
                  "properties": json.dumps({"harness": "claude"})},
        )

        self.assertEqual(answer.status_code, 200, answer.text)
        stored = json.loads(self._row("Skippy", "daniel")["properties"])
        self.assertEqual(stored["harness"], "claude")

    def test_a_mind_reads_another_minds_own_node(self):
        self._seed("Mordecai", owner="daniel", mind_id="mordecai-uuid",
                   entity_type="Mind")

        self.assertTrue(self._query(SKIPPY, "Mordecai")["found"])

    # -- the machinery ----------------------------------------------------
    def test_a_service_credential_reaches_every_owners_graph(self):
        self._seed("Maurice", owner="daniel")
        self._seed("Clay", owner="aiden")

        for name in ("Maurice", "Clay"):
            with self.subTest(name=name):
                self.assertTrue(self._query(COMMS, name)["found"])

    def test_the_admin_credential_reaches_every_owners_graph(self):
        self._seed("Clay", owner="aiden")

        self.assertTrue(self._query(ADMIN, "Clay")["found"])


if __name__ == "__main__":
    unittest.main()
