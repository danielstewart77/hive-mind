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

    # Every route that reaches a properties blob, not the three that were
    # convenient. The guard is called from six places; a mutation pass
    # showed that neutering three of them left this file entirely green,
    # and that driving the August payload through /graph/upsert-direct with
    # the service token wrote the line for real.
    SOUL_WRITE_ROUTES = [
        ("/graph/upsert", {"data_class": "current-state",
                           "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f"}),
        ("/graph/upsert-backup", {"data_class": "current-state",
                                  "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f"}),
        ("/graph/upsert-direct", {"data_class": "current-state",
                                  "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f"}),
        ("/graph/nodes", {"data_class": "current-state",
                          "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f"}),
        ("/graph/properties/merge", {}),
    ]

    def test_every_route_that_writes_properties_refuses_the_soul_key(self):
        """Test 1, across the whole surface rather than a sample."""
        for route, extra in self.SOUL_WRITE_ROUTES:
            with self.subTest(route=route):
                resp = self.client.post(
                    route,
                    headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                    json={
                        "entity_type": "Mind",
                        "name": "Skippy",
                        "properties": json.dumps({"soul_values": [THE_LINE]}),
                        **extra,
                    },
                )
                self.assertEqual(resp.status_code, 403, f"{route}: {resp.text}")
                self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

    def test_removing_the_soul_key_is_refused_too(self):
        """Deleting a soul is a soul change, and the louder one."""
        removal = self.client.request(
            "POST",
            "/graph/properties/remove",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={"entity_type": "Mind", "name": "Skippy", "keys": ["soul_values"]},
        )
        self.assertEqual(removal.status_code, 403, removal.text)
        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

    def test_a_full_replace_that_omits_the_soul_does_not_erase_it(self):
        """The boundary has to cover absence, not only presence.

        Every upsert route is full-replace on the properties blob. The
        refusal fires on the key being *there*, so a payload that simply
        leaves it out sailed through and returned `upserted: true` over an
        emptied identity — with no provenance row, because nothing on this
        path writes one. That is a worse outcome than the write it was
        built to stop.
        """
        for route in ("/graph/upsert-direct", "/graph/upsert-backup"):
            with self.subTest(route=route):
                resp = self.client.post(
                    route,
                    headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
                    json={
                        "entity_type": "Mind",
                        "name": "Skippy",
                        "data_class": "current-state",
                        "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f",
                        "properties": json.dumps({"role": "operator"}),
                    },
                )
                self.assertLess(resp.status_code, 500, resp.text)
                self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

    def test_a_mind_holding_a_soul_cannot_be_deleted_out_from_under_it(self):
        """An erased node leaves nothing behind to explain the erasure.

        `soul_changes` records changes to a node; delete the node and the
        record describes something that no longer exists. Emptying the soul
        first makes the loss visible, and then the node may go.
        """
        resp = self.client.request(
            "DELETE",
            "/graph/nodes",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={"entity_type": "Mind", "name": "Skippy"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)
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


class SoulRecordTests(SoulBoundaryTests):
    """What the record has to survive: the mutation pass, mostly.

    Every test below was written because neutering the code it covers left
    the original file green — an actor recorded as a literal `"mind"`, an
    unvalidated actor, a `soul_read` returning `[]` unconditionally, history
    ordered oldest-first. The provenance table's whole value is being
    trustworthy about who did what, and none of that was pinned.
    """

    def _write(self, **over):
        body = {
            "name": "Skippy",
            "soul_values": ORIGINAL_SOUL + ["I handle indignity with grace."],
            "actor": "mind",
            "reason": "a stated reason",
        }
        body.update(over)
        return self.client.post(
            "/graph/soul",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            json=body,
        )

    def _history(self):
        return self.client.get(
            "/graph/soul/history",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            params={"name": "Skippy"},
        ).json()["changes"]

    def test_a_console_edit_is_stored_as_the_console(self):
        """Requirement 16, on the far side of the write.

        The console asserts what it *sends*. Nothing asserted what lucent
        *stores*, so recording every change as `"mind"` — the exact lie the
        column exists to prevent — passed the whole suite.
        """
        self._write(actor="console", reason="Daniel removed a line by hand.")

        entry = self._history()[0]
        self.assertEqual(entry["actor"], "console")
        self.assertEqual(entry["reason"], "Daniel removed a line by hand.")

    def test_an_actor_that_is_neither_is_refused(self):
        """`actor` is the whole record. An unrecognised one is not a label."""
        resp = self._write(actor="ollama")

        self.assertFalse(resp.json()["ok"], resp.text)
        self.assertEqual(resp.json()["code"], "invalid_actor")
        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

    def test_reading_a_soul_returns_what_the_node_holds(self):
        """`GET /graph/soul` had no coverage; returning `[]` always passed.

        This is the read the skill builds its whole-list write from, so a
        read that silently returns nothing is how a soul gets erased by a
        caller doing exactly as instructed.
        """
        body = self.client.get(
            "/graph/soul",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            params={"name": "Skippy"},
        ).json()

        self.assertEqual(body["soul_values"], ORIGINAL_SOUL)
        self.assertEqual(body["change_count"], 0)

    def test_history_is_newest_first_and_honours_its_limit(self):
        for n in range(3):
            self._write(soul_values=ORIGINAL_SOUL + [f"line {n}"], reason=f"edit {n}")

        newest_first = self._history()
        self.assertEqual([c["reason"] for c in newest_first],
                         ["edit 2", "edit 1", "edit 0"])

        capped = self.client.get(
            "/graph/soul/history",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            params={"name": "Skippy", "limit": 1},
        ).json()["changes"]
        self.assertEqual([c["reason"] for c in capped], ["edit 2"])

    def test_a_change_written_under_another_casing_still_appears(self):
        """The node is found case-insensitively; the record was not.

        `_find_mind` matches `LOWER(name)`, so `name="skippy"` edits the same
        node — while the history filtered on `mind_name = ?` exactly and
        reported no changes at all. The console then renders "No recorded
        changes yet" for an edit made seconds earlier.
        """
        self._write(name="skippy", reason="written under the lowercase id")

        self.assertEqual([c["reason"] for c in self._history()],
                         ["written under the lowercase id"])

    def test_a_write_that_raced_another_is_refused_rather_than_applied(self):
        """Whole-list replacement is how two writers erase each other.

        Both read `[a, b]`; one writes `[a, b, mine]`, the other
        `[a, b, theirs]`. Last wins, both get `ok: true`, and both report to
        the operator that their line landed. One pane and the console is
        enough, and the console page exists to be used while the mind runs.
        """
        seen = self.client.get(
            "/graph/soul",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            params={"name": "Skippy"},
        ).json()["change_count"]

        first = self._write(soul_values=ORIGINAL_SOUL + ["from pane A"],
                            reason="pane A", expected_change_count=seen)
        self.assertTrue(first.json()["ok"], first.text)

        second = self._write(soul_values=ORIGINAL_SOUL + ["from pane B"],
                             reason="pane B", expected_change_count=seen)

        self.assertFalse(second.json()["ok"], second.text)
        self.assertEqual(second.json()["code"], "conflict")
        self.assertIn("from pane A", _soul_of(self.conn))

    def test_a_write_that_would_drop_most_of_a_soul_is_refused(self):
        """A failed read produces an empty list that means "delete me".

        Every caller sends the whole list, built from a read. Today that read
        can return 404, or 200 with `ok: false`. A caller that does not check
        builds from nothing and sends nothing, and the write is a wipe that
        reports success.
        """
        wipe = self._write(soul_values=[], reason="built from a read I did not check")

        self.assertFalse(wipe.json()["ok"], wipe.text)
        self.assertEqual(wipe.json()["code"], "shrink_refused")
        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL)

        deliberate = self._write(soul_values=[], reason="retiring this mind",
                                 allow_shrink=True)
        self.assertTrue(deliberate.json()["ok"], deliberate.text)
        self.assertEqual(_soul_of(self.conn), [])

    def test_removing_one_line_of_several_is_not_mistaken_for_a_wipe(self):
        """The guard has to leave ordinary editing alone.

        Requirement 9 is removal. A shrink guard that refused it would make
        the daily review — the only pass that can notice a stale line —
        unable to act on what it found.
        """
        self._write(soul_values=ORIGINAL_SOUL[:1], reason="dropped a stale line")

        self.assertEqual(_soul_of(self.conn), ORIGINAL_SOUL[:1])

    def test_a_soul_stored_as_a_string_is_reported_not_shredded(self):
        """`list("I am Skippy")` is a list of characters.

        Read back that way it becomes one soul line per letter, with
        `ok: true` on it — and saving the console page then persists the
        shredded form through the audited route, permanently.
        """
        self.conn.execute(
            "UPDATE nodes SET properties = ? WHERE name = 'Skippy'",
            (json.dumps({"soul_values": "I am Skippy the Magnificent."}),),
        )
        self.conn.commit()

        body = self.client.get(
            "/graph/soul",
            headers={"Authorization": f"Bearer {SOUL_TOKEN}"},
            params={"name": "Skippy"},
        ).json()

        self.assertFalse(body["ok"], body)
        self.assertEqual(body["code"], "malformed_soul")
