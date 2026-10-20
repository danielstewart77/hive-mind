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

    def _seed(self, name: str, owner: str | None, mind_id: str = "seed",
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

    # -- the machinery, on the write side ---------------------------------
    def test_a_service_credential_edits_another_owners_node(self):
        """The console opens whichever mind's page the operator asked for."""
        self._seed("Clay", owner="aiden")

        answer = self.client.post(
            "/graph/properties/merge",
            headers=self._h(COMMS),
            json={"entity_type": "Person", "name": "Clay",
                  "properties": json.dumps({"checked": True})},
        )

        self.assertEqual(answer.status_code, 200, answer.text)
        stored = json.loads(self._row("Clay", "aiden")["properties"])
        self.assertTrue(stored.get("checked"))

    def test_the_admin_credential_edits_another_owners_node(self):
        self._seed("Clay", owner="aiden")

        answer = self.client.post(
            "/graph/properties/merge",
            headers=self._h(ADMIN),
            json={"entity_type": "Person", "name": "Clay",
                  "properties": json.dumps({"checked": True})},
        )

        self.assertEqual(answer.status_code, 200, answer.text)
        stored = json.loads(self._row("Clay", "aiden")["properties"])
        self.assertTrue(stored.get("checked"))

    def test_a_node_the_admin_creates_is_readable_by_a_mind(self):
        """The admin value answers for nobody, so a row it creates takes the
        primary user — never the empty string, which no mind can read."""
        with mock.patch.dict(os.environ, {"LUCENT_PRIMARY_USER": "daniel"}):
            self.assertEqual(self._create(ADMIN, "NewGuy").status_code, 200)

            self.assertTrue(self._query(SKIPPY, "NewGuy")["found"])

    def test_the_console_deletes_a_mind_node_with_no_soul_left(self):
        """Emptying a soul and then removing the node is the sanctioned
        order, and the console is the only thing that can do it."""
        self._seed("Dragoman", owner="daniel", mind_id="dragoman-uuid",
                   entity_type="Mind")

        answer = self.client.request(
            "DELETE", "/graph/nodes", headers=self._h(COMMS),
            json={"entity_type": "Mind", "name": "Dragoman"},
        )

        self.assertTrue(answer.json().get("ok"), answer.text)
        self.assertIsNone(self._row("Dragoman", "daniel"))

    # -- the carve-out, on the paths that are not the merge route ----------
    def test_a_mind_cannot_reach_another_minds_own_node_as_an_edge_target(self):
        self._seed("Mordecai", owner="daniel", mind_id="mordecai-uuid",
                   entity_type="Mind",
                   properties={"tier": "standing", "data_class": "identity"})

        answer = self.client.post(
            "/graph/upsert-direct",
            headers=self._h(SKIPPY),
            json={"entity_type": "Person", "name": "Maurice",
                  "data_class": "person", "mind_id": "",
                  "relation": "KNOWS_ABOUT", "target_name": "Mordecai",
                  "target_type": "Mind"},
        )

        self.assertFalse(answer.json().get("upserted"), answer.text)
        stored = json.loads(self._row("Mordecai", "daniel")["properties"])
        self.assertEqual(stored["tier"], "standing")
        self.assertEqual(stored["data_class"], "identity")

    def test_an_ordinary_node_named_after_a_mind_is_still_a_mind_to_delete(self):
        """The carve-out is about the node that *is* a mind. Asked about a
        Person of the same name it used to answer for the Mind node, which
        made every node named after a mind undeletable by anybody."""
        self._seed("Hex", owner="daniel", mind_id="hex-uuid", entity_type="Mind")
        self._seed("Hex", owner="daniel", mind_id="skippy", entity_type="Person")

        answer = self.client.request(
            "DELETE", "/graph/nodes", headers=self._h(SKIPPY),
            json={"entity_type": "Person", "name": "Hex"},
        )

        self.assertTrue(answer.json().get("ok"), answer.text)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM nodes WHERE name = 'Hex' AND type = 'Mind'"
        ).fetchone())

    # -- every write path files the row, not just POST /graph/nodes --------
    def test_each_write_route_records_the_owner(self):
        """`/graph/upsert` is the memory pipeline's own path. A row written
        ownerless there is invisible to every mind afterwards, with a 200."""
        routes = {
            # `/graph/upsert` refuses an orphan, so this one carries the
            # edge it demands; the owner of both ends is what is asserted.
            "/graph/upsert": {"entity_type": "Person", "name": "Upserted",
                              "data_class": "person", "mind_id": "",
                              "relation": "MEMBER_OF", "target_name": "Propel",
                              "target_type": "Organization"},
            "/graph/upsert-direct": {"entity_type": "Person", "name": "Directed",
                                     "data_class": "person", "mind_id": ""},
            "/graph/nodes": {"entity_type": "Person", "name": "Created",
                             "data_class": "person", "mind_id": ""},
        }
        for route, body in routes.items():
            with self.subTest(route=route):
                answer = self.client.post(route, headers=self._h(SKIPPY), json=body)
                self.assertEqual(answer.status_code, 200, answer.text)
                self.assertIsNotNone(self._row(body["name"], "daniel"), answer.text)

    def test_an_edge_records_the_owner(self):
        self._seed("Maurice", owner="daniel")
        self._seed("Propel", owner="daniel", entity_type="Organization")

        answer = self.client.post(
            "/graph/edges", headers=self._h(SKIPPY),
            json={"source_name": "Maurice", "source_type": "Person",
                  "target_name": "Propel", "target_type": "Organization",
                  "relation": "MEMBER_OF", "data_class": "person",
                  "mind_id": ""},
        )

        self.assertTrue(answer.json().get("ok"), answer.text)
        owners = [r[0] for r in self.conn.execute("SELECT owner FROM edges")]
        self.assertEqual(owners, ["daniel"])

    def test_a_mind_naming_another_owner_on_the_request_is_refused(self):
        """The body carries an `owner` field. A mind may name its own and
        nothing else, or the partition is one request away from nothing."""
        answer = self.client.post(
            "/graph/nodes", headers=self._h(ZACH),
            json={"entity_type": "Person", "name": "Maurice",
                  "data_class": "person", "mind_id": "", "owner": "daniel"},
        )

        self.assertEqual(answer.status_code, 403, answer.text)
        self.assertIsNone(self._row("Maurice", "daniel"))

    # -- the filters on the paths that had none ---------------------------
    def test_removing_a_key_from_another_owners_node_reads_as_absent(self):
        self._seed("Maurice", owner="daniel", properties={"private": "yes"})

        answer = self.client.post(
            "/graph/properties/remove", headers=self._h(ZACH),
            json={"entity_type": "Person", "name": "Maurice",
                  "keys": ["private"]},
        )

        self.assertEqual(answer.json().get("code"), "not_found", answer.text)
        stored = json.loads(self._row("Maurice", "daniel")["properties"])
        self.assertEqual(stored.get("private"), "yes")

    def test_an_edge_cannot_be_wired_or_unwired_across_owners(self):
        self._seed("Maurice", owner="daniel")
        self._seed("Clay", owner="aiden")

        wired = self.client.post(
            "/graph/edges", headers=self._h(ZACH),
            json={"source_name": "Clay", "source_type": "Person",
                  "target_name": "Maurice", "target_type": "Person",
                  "relation": "FRIEND_OF", "data_class": "person",
                  "mind_id": ""},
        )

        self.assertEqual(wired.json().get("code"), "target_not_found", wired.text)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 0
        )

    def test_creating_a_name_another_owner_holds_does_not_report_their_row(self):
        """`code: exists` with their row id is a cross-owner oracle, and it
        also leaves a mind unable to create its own node of that name."""
        self._seed("Maurice", owner="daniel")

        answer = self.client.post(
            "/graph/nodes", headers=self._h(ZACH),
            json={"entity_type": "Person", "name": "Maurice",
                  "data_class": "person", "mind_id": ""},
        )

        self.assertTrue(answer.json().get("ok"), answer.text)
        self.assertIsNotNone(self._row("Maurice", "aiden"))

    def test_the_connection_walk_does_not_step_into_another_owners_node(self):
        mine = self._seed("Maurice", owner="daniel")
        theirs = self._seed("Clay", owner="aiden")
        self.conn.execute(
            "INSERT INTO edges (mind_id, owner, source_id, target_id, type, "
            "created_at) VALUES ('seed', 'daniel', ?, ?, 'FRIEND_OF', 1.0)",
            (mine, theirs),
        )
        self.conn.commit()

        answer = self._query(SKIPPY, "Maurice")

        names = {
            (c.get("node") or {}).get("name")
            for match in answer["matches"]
            for c in match.get("connections", [])
        }
        self.assertNotIn("Clay", names)

    def test_disambiguation_does_not_report_another_owners_similar_names(self):
        """The similar-node list is handed back to the caller and pushed to a
        chat, so an unscoped scan tells a mind which names exist elsewhere."""
        self._seed("Maurice Westerdale", owner="daniel")

        answer = self.client.post(
            "/graph/upsert", headers=self._h(ZACH),
            json={"entity_type": "Person", "name": "Maurice",
                  "data_class": "person", "mind_id": "",
                  "relation": "MEMBER_OF", "target_name": "Propel",
                  "target_type": "Organization"},
        )

        reported = json.dumps(answer.json())
        self.assertNotIn("Westerdale", reported)

    def test_a_full_replace_on_a_minds_own_node_keeps_its_soul(self):
        """`preserve_soul` carries the soul across a blob that omits it. Its
        lookup is owner-scoped now, and a lookup that misses erases a soul
        with a 200."""
        self._seed("Skippy", owner="daniel", mind_id=SKIPPY_UUID,
                   entity_type="Mind", properties={"soul_values": ["a line"]})

        answer = self.client.post(
            "/graph/upsert-direct", headers=self._h(SKIPPY),
            json={"entity_type": "Mind", "name": "Skippy",
                  "data_class": "identity", "mind_id": "",
                  "properties": json.dumps({"role": "operator"})},
        )

        self.assertEqual(answer.status_code, 200, answer.text)
        stored = json.loads(self._row("Skippy", "daniel")["properties"])
        self.assertEqual(stored.get("soul_values"), ["a line"])

    def test_a_minds_own_node_is_found_even_while_it_has_no_owner(self):
        """A mind whose node this store cannot resolve to an owner holds it
        under a null owner — both of the boys' minds do until their
        credentials are minted here. An owner-scoped refusal misses that row,
        so the mind mints a second node of its own name and the empty
        newcomer shadows the soul-bearing original on every read."""
        self._seed("Zach", owner=None, mind_id=ZACH_UUID, entity_type="Mind",
                   properties={"soul_values": ["a line"]})

        answer = self._create(ZACH, "Zach", entity_type="Mind")

        # Refused as already there, which it is. The mind owns this node and
        # may change it — what it may not do is end up with two.
        self.assertEqual(answer.json().get("code"), "exists", answer.text)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE name = 'Zach' AND type = 'Mind'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)


if __name__ == "__main__":
    unittest.main()
