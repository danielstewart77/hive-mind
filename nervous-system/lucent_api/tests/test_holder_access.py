"""Which mind the memory store thinks is talking, and which minds it may read.

Every test here drives the real `holder_access` and the real `lucent_memory`
against a temporary database, and asserts on the row that actually landed. The
only stub is `_embed`, which is a synchronous HTTP call to Ollama — the point
where the process stops being ours.
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from lucent_api import holder_access as ha

SKIPPY_TOKEN = "skippys-own-credential"  # secret-guard: allow
ARNOLD_TOKEN = "arnolds-own-credential"  # secret-guard: allow
COMMS_TOKEN = "comms-service-credential"  # secret-guard: allow
ADMIN_TOKEN = "lucent-admin-credential"  # secret-guard: allow
RETIRED_SHARED = "the-old-shared-lucent-token"  # secret-guard: allow

MEMORIES_DDL = """
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mind_id       TEXT    NOT NULL,
    content       TEXT    NOT NULL,
    embedding     BLOB,
    tags          TEXT    DEFAULT '',
    source        TEXT,
    data_class    TEXT,
    tier          TEXT,
    as_of         TEXT,
    expires_at    TEXT,
    superseded    INTEGER DEFAULT 0,
    recurring     INTEGER,
    codebase_ref  TEXT,
    created_at    INTEGER
);
"""


def _fake_embed(text: str) -> list[float]:
    """A deterministic unit-ish vector per text, far enough apart that the
    store's dedup threshold never fires between two different memories."""
    import hashlib

    raw = hashlib.sha256(text.encode("utf-8")).digest()[:8]
    return [b / 255.0 for b in raw]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(MEMORIES_DDL)
    ha.init_credentials(conn)
    return conn


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def test_two_holders_of_one_person_each_get_their_own_value(self):
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)
        ha.put_credential(self.conn, "bob", "daniel", "bobs-value")

        self.assertEqual(
            ha.credential_for_token(self.conn, SKIPPY_TOKEN).holder, "skippy"
        )
        self.assertEqual(
            ha.credential_for_token(self.conn, "bobs-value").holder, "bob"
        )

    def test_revoking_one_holder_leaves_another_of_the_same_person(self):
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)
        ha.put_credential(self.conn, "bob", "daniel", "bobs-value")

        self.assertTrue(ha.revoke_credential(self.conn, "skippy"))

        self.assertIsNone(ha.credential_for_token(self.conn, SKIPPY_TOKEN))
        self.assertEqual(
            ha.credential_for_token(self.conn, "bobs-value").holder, "bob"
        )

    def test_a_revoked_value_stops_resolving_on_the_very_next_call(self):
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)
        self.assertIsNotNone(ha.credential_for_token(self.conn, SKIPPY_TOKEN))

        ha.revoke_credential(self.conn, "skippy")

        # Same connection, same process, nothing restarted.
        self.assertIsNone(ha.credential_for_token(self.conn, SKIPPY_TOKEN))

    def test_a_value_another_holder_already_holds_is_refused(self):
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)

        with self.assertRaises(ValueError) as caught:
            ha.put_credential(self.conn, "arnold", "sloan", SKIPPY_TOKEN)

        self.assertIn("skippy", str(caught.exception))
        self.assertEqual(
            ha.credential_for_token(self.conn, SKIPPY_TOKEN).holder, "skippy"
        )

    def test_the_shared_sentinel_is_not_a_holder(self):
        with self.assertRaises(ValueError):
            ha.put_credential(self.conn, "shared", "daniel", "a-value")

    def test_a_credential_is_stored_as_a_digest(self):
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)

        stored = [
            r[0] for r in self.conn.execute("SELECT token_sha FROM credentials")
        ]

        self.assertEqual(stored, [ha.digest(SKIPPY_TOKEN)])


class WhichMindIsTalkingTests(unittest.TestCase):
    """`mind_for_write` and `minds_for_read` are where the body stops being
    trusted. These call them directly rather than through a route, because the
    decision is theirs and a route only relays it."""

    def setUp(self) -> None:
        self.mind = ha.Caller(holder="skippy", username="daniel", role=ha.MIND)
        self.service = ha.Caller(
            holder="comms", username="daniel", role=ha.SERVICE
        )
        self.admin = ha.Caller(is_admin=True, role=ha.SERVICE)
        self.person = ha.Caller(
            holder="daniels-phone", username="daniel", role=ha.PERSON
        )

    def test_a_mind_writes_as_itself_whatever_the_body_says(self):
        self.assertEqual(self.mind.mind_for_write(None), "skippy")
        self.assertEqual(self.mind.mind_for_write("skippy"), "skippy")

    def test_a_mind_naming_another_mind_is_refused_not_rescoped(self):
        with self.assertRaises(HTTPException) as caught:
            self.mind.mind_for_write("arnold")

        self.assertEqual(caught.exception.status_code, 403)

    def test_a_mind_may_write_the_shared_sentinel_explicitly(self):
        """Hive-wide standing rules are written under `shared`, and every
        mind reads them alongside its own. Deriving provenance strictly from
        the holder would turn every hive-wide rule into a private one."""
        self.assertEqual(self.mind.mind_for_write("shared"), "shared")

    def test_a_mind_reads_itself_and_the_shared_sentinel(self):
        self.assertEqual(
            self.mind.minds_for_read(None), ["skippy", "shared"]
        )

    def test_a_mind_reading_another_mind_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            self.mind.minds_for_read("arnold")

        self.assertEqual(caught.exception.status_code, 403)

    def test_a_service_writes_for_the_mind_it_names(self):
        """Comms composes every mind's prompt, so one service legitimately
        reads and writes many minds' records — and has to say which."""
        self.assertEqual(self.service.mind_for_write("arnold"), "arnold")
        self.assertEqual(self.service.minds_for_read("arnold"), ["arnold"])

    def test_a_service_naming_no_mind_cannot_write(self):
        with self.assertRaises(HTTPException) as caught:
            self.service.mind_for_write(None)

        self.assertEqual(caught.exception.status_code, 400)

    def test_an_admin_naming_no_mind_reads_every_one(self):
        self.assertIsNone(self.admin.minds_for_read(None))

    def test_a_person_has_no_mind_to_write_as_and_reads_nothing(self):
        with self.assertRaises(HTTPException):
            self.person.mind_for_write("skippy")
        with self.assertRaises(HTTPException):
            self.person.minds_for_read(None)


class TheGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self._patch = mock.patch(
            "lucent_api.holder_access.connection", return_value=self.conn
        )
        self._patch.start()
        os.environ["LUCENT_ADMIN_BEARER_TOKEN"] = ADMIN_TOKEN
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)

    def tearDown(self) -> None:
        self._patch.stop()
        os.environ.pop("LUCENT_ADMIN_BEARER_TOKEN", None)
        self.conn.close()

    def test_a_minted_value_resolves_to_its_holder(self):
        caller = ha.require_holder(authorization=f"Bearer {SKIPPY_TOKEN}")

        self.assertEqual(caller.holder, "skippy")
        self.assertEqual(caller.role, ha.MIND)

    def test_the_retired_shared_value_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            ha.require_holder(authorization=f"Bearer {RETIRED_SHARED}")

        self.assertEqual(caught.exception.status_code, 401)

    def test_no_credential_at_all_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            ha.require_holder(authorization="")

        self.assertEqual(caught.exception.status_code, 401)

    def test_the_admin_value_reaches_every_mind(self):
        caller = ha.require_holder(authorization=f"Bearer {ADMIN_TOKEN}")

        self.assertTrue(caller.is_admin)
        self.assertIsNone(caller.minds_for_read(None))

    def test_an_unconfigured_store_refuses_rather_than_serving_open(self):
        """An unset token used to mean "serve everything to everybody". A
        store that cannot establish who is calling says so."""
        os.environ.pop("LUCENT_ADMIN_BEARER_TOKEN", None)
        self.conn.execute("DELETE FROM credentials")
        self.conn.commit()

        with self.assertRaises(HTTPException) as caught:
            ha.require_holder(authorization=f"Bearer {SKIPPY_TOKEN}")

        self.assertEqual(caught.exception.status_code, 503)


class MemoryRoutesTests(unittest.TestCase):
    """Through the real app, against the real store."""

    def setUp(self) -> None:
        self.conn = _conn()
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)
        ha.put_credential(self.conn, "arnold", "sloan", ARNOLD_TOKEN)
        ha.put_credential(
            self.conn, "comms", "daniel", COMMS_TOKEN, role=ha.SERVICE
        )
        os.environ["LUCENT_ADMIN_BEARER_TOKEN"] = ADMIN_TOKEN
        self._patches = [
            mock.patch(
                "lucent_api.holder_access.connection", return_value=self.conn
            ),
            mock.patch(
                "lucent_api.lucent_memory._get_conn", return_value=self.conn
            ),
            # The embedder is a synchronous HTTP call to Ollama: the one place
            # the process stops being ours. Distinct texts must embed
            # distinctly or the store's own 0.92-cosine dedup swallows every
            # write after the first, and these tests would read as a scoping
            # failure.
            mock.patch(
                "lucent_api.lucent_memory._embed", side_effect=_fake_embed
            ),
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

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _store(self, token: str, content: str, **body) -> object:
        return self.client.post(
            "/memory/store",
            headers=self._auth(token),
            json={"content": content, "data_class": "current-state", **body},
        )

    def _provenances(self) -> list[str]:
        return [
            r["mind_id"]
            for r in self.conn.execute(
                "SELECT mind_id FROM memories ORDER BY id"
            )
        ]

    def test_a_write_is_filed_under_the_mind_that_presented_the_value(self):
        answer = self._store(SKIPPY_TOKEN, "something skippy learned")

        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(self._provenances(), ["skippy"])

    def test_a_write_naming_another_mind_in_the_body_is_refused(self):
        answer = self._store(
            SKIPPY_TOKEN, "planted under arnold", mind_id="arnold"
        )

        self.assertEqual(answer.status_code, 403, answer.text)
        self.assertEqual(self._provenances(), [])

    def test_a_write_naming_no_mind_still_lands_under_the_right_one(self):
        """The old default was "ada", so an unattributed write arrived with
        somebody's name on it. Asserting only that the write lands would pass
        against that default being restored."""
        answer = self._store(SKIPPY_TOKEN, "no mind named anywhere")

        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(self._provenances(), ["skippy"])

    def test_an_unrecognised_value_writes_nothing(self):
        answer = self._store(RETIRED_SHARED, "from a retired credential")

        self.assertEqual(answer.status_code, 401, answer.text)
        self.assertEqual(self._provenances(), [])

    def test_a_mind_may_write_the_shared_sentinel(self):
        answer = self._store(
            SKIPPY_TOKEN, "a hive-wide standing rule", mind_id="shared"
        )

        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(self._provenances(), ["shared"])

    def test_a_listing_shows_the_callers_own_mind_and_the_shared_rules(self):
        self._store(SKIPPY_TOKEN, "skippy's own")
        self._store(SKIPPY_TOKEN, "everybody's rule", mind_id="shared")
        self._store(ARNOLD_TOKEN, "arnold's own")

        answer = self.client.get("/memory/list", headers=self._auth(SKIPPY_TOKEN))

        minds = {e["mind_id"] for e in answer.json()["entries"]}
        self.assertEqual(minds, {"skippy", "shared"})

    def test_a_listing_never_shows_another_minds_memory(self):
        self._store(ARNOLD_TOKEN, "arnold's own")

        answer = self.client.get("/memory/list", headers=self._auth(SKIPPY_TOKEN))

        self.assertEqual(answer.json()["entries"], [])

    def test_a_service_reads_the_mind_it_names(self):
        """Comms composing Arnold's prompt has to reach Arnold's memory, and
        has to say that is what it is doing."""
        self._store(ARNOLD_TOKEN, "arnold's own")

        answer = self.client.get(
            "/memory/list?mind_id=arnold", headers=self._auth(COMMS_TOKEN)
        )

        self.assertEqual(
            [e["mind_id"] for e in answer.json()["entries"]], ["arnold"]
        )

    def test_deleting_another_minds_memory_answers_as_an_absent_one(self):
        """Ids are sequential integers, so a refusal shaped differently from
        "not there" is a way to enumerate every memory on the hive."""
        self._store(ARNOLD_TOKEN, "arnold's own")
        theirs = self.client.delete(
            "/memory/1", headers=self._auth(SKIPPY_TOKEN)
        )
        absent = self.client.delete(
            "/memory/9999", headers=self._auth(SKIPPY_TOKEN)
        )

        self.assertEqual(theirs.status_code, 404)
        self.assertEqual(absent.status_code, 404)
        self.assertEqual(self._provenances(), ["arnold"])


class ScopedReadTests(MemoryRoutesTests):
    """Reverting any read path to "trust the body" has to fail something.

    Mutating `/retrieve` and `/recent-decayed` back to `mind_ids=[body.mind_id]`
    left the whole suite green, because every read test went through `/list`.
    """

    def test_retrieve_only_reaches_the_callers_own_mind(self):
        self._store(SKIPPY_TOKEN, "skippy remembers the proxy")
        self._store(ARNOLD_TOKEN, "arnold remembers the proxy")

        answer = self.client.post(
            "/memory/retrieve",
            headers=self._auth(SKIPPY_TOKEN),
            json={"query": "proxy", "k": 10, "min_score": 0.0},
        )

        minds = {m["mind_id"] for m in answer.json().get("memories", [])}
        self.assertNotIn("arnold", minds)

    def test_retrieve_naming_another_mind_is_refused(self):
        self._store(ARNOLD_TOKEN, "arnold remembers the proxy")

        answer = self.client.post(
            "/memory/retrieve",
            headers=self._auth(SKIPPY_TOKEN),
            json={"query": "proxy", "k": 10, "mind_id": "arnold"},
        )

        self.assertEqual(answer.status_code, 403, answer.text)

    def test_recent_decayed_only_reaches_the_callers_own_mind(self):
        self._store(SKIPPY_TOKEN, "skippy's own")
        self._store(ARNOLD_TOKEN, "arnold's own")

        answer = self.client.get(
            "/memory/recent-decayed", headers=self._auth(SKIPPY_TOKEN)
        )

        body = answer.json()
        entries = body if isinstance(body, list) else body.get("entries", [])
        self.assertNotIn("arnold", {e["mind_id"] for e in entries})

    def test_updating_another_minds_memory_answers_as_an_absent_one(self):
        """Deleting was covered; updating was not, and the guard's own
        docstring is about both. A mind could rewrite any memory on the hive by
        id, content and provenance included."""
        self._store(ARNOLD_TOKEN, "arnold's own")

        answer = self.client.put(
            "/memory/1",
            headers=self._auth(SKIPPY_TOKEN),
            json={"content": "something skippy put there"},
        )

        self.assertEqual(answer.status_code, 404, answer.text)
        rows = [
            r["content"]
            for r in self.conn.execute("SELECT content FROM memories")
        ]
        self.assertEqual(rows, ["arnold's own"])

    def test_a_mind_cannot_change_a_hive_wide_rule(self):
        """Every mind reads the `shared` rules into its prompt. One mind
        reassigning or deleting one is a mind editing the hive's behaviour,
        and a kid's box should not be able to."""
        self._store(SKIPPY_TOKEN, "a hive-wide rule", mind_id="shared")

        changed = self.client.put(
            "/memory/1",
            headers=self._auth(ARNOLD_TOKEN),
            json={"content": "no it is not"},
        )
        removed = self.client.delete("/memory/1", headers=self._auth(ARNOLD_TOKEN))

        self.assertEqual(changed.status_code, 403, changed.text)
        self.assertEqual(removed.status_code, 403, removed.text)
        self.assertEqual(self._provenances(), ["shared"])

    def test_a_service_changing_a_memory_must_name_the_mind(self):
        """A service naming no mind reads every row on the hive, so left
        unchecked it reaches every row by id too — the widest hole in the
        file."""
        self._store(ARNOLD_TOKEN, "arnold's own")

        answer = self.client.delete("/memory/1", headers=self._auth(COMMS_TOKEN))

        self.assertEqual(answer.status_code, 400, answer.text)
        self.assertEqual(self._provenances(), ["arnold"])


class CredentialRouteGuardTests(unittest.TestCase):
    """The minting routes answer to the admin credential and nothing else.

    Lucent has no deny-by-default route table behind it, so dropping the guard
    is directly exploitable: anyone who can reach the port mints themselves a
    service credential and then reads and writes every mind's memory.
    """

    def setUp(self) -> None:
        self.conn = _conn()
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)
        os.environ["LUCENT_ADMIN_BEARER_TOKEN"] = ADMIN_TOKEN
        self._patch = mock.patch(
            "lucent_api.holder_access.connection", return_value=self.conn
        )
        self._patch.start()
        from lucent_api.server import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self._patch.stop()
        os.environ.pop("LUCENT_ADMIN_BEARER_TOKEN", None)
        self.conn.close()

    def _mint(self, token: str) -> object:
        return self.client.put(
            "/credentials/intruder",
            headers={"Authorization": f"Bearer {token}"},
            json={"username": "intruder", "token": "a-value", "role": "service"},
        )

    def test_a_minds_own_credential_cannot_mint_one(self):
        answer = self._mint(SKIPPY_TOKEN)

        self.assertEqual(answer.status_code, 401, answer.text)
        self.assertIsNone(ha.credential_for_token(self.conn, "a-value"))

    def test_the_admin_credential_mints_one(self):
        answer = self._mint(ADMIN_TOKEN)

        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(
            ha.credential_for_token(self.conn, "a-value").holder, "intruder"
        )

    def test_a_minds_own_credential_cannot_revoke_one(self):
        answer = self.client.delete(
            "/credentials/skippy",
            headers={"Authorization": f"Bearer {SKIPPY_TOKEN}"},
        )

        self.assertEqual(answer.status_code, 401, answer.text)
        self.assertIsNotNone(ha.credential_for_token(self.conn, SKIPPY_TOKEN))

    def test_a_minds_own_credential_cannot_list_holders(self):
        answer = self.client.get(
            "/credentials", headers={"Authorization": f"Bearer {SKIPPY_TOKEN}"}
        )

        self.assertEqual(answer.status_code, 401, answer.text)

    def test_the_listing_never_carries_a_value(self):
        """An operator does not need it; they need to know whether the holder
        has one that works."""
        answer = self.client.get(
            "/credentials", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )

        body = answer.text
        self.assertEqual(answer.status_code, 200, body)
        self.assertNotIn(SKIPPY_TOKEN, body)
        self.assertNotIn(ha.digest(SKIPPY_TOKEN), body)
        self.assertNotIn("token_sha", body)

    def test_minting_works_on_a_store_that_never_served_a_request(self):
        """The documented first step of the rollout. Going straight to a
        SELECT made it answer 500 on `no such table: credentials`."""
        fresh = sqlite3.connect(":memory:", check_same_thread=False)
        fresh.row_factory = sqlite3.Row
        fresh.executescript(MEMORIES_DDL)
        with mock.patch(
            "lucent_api.holder_access.connection", return_value=fresh
        ):
            answer = self.client.put(
                "/credentials/skippy",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                json={
                    "username": "daniel",
                    "token": "a-first-value",
                    "role": "mind",
                },
            )
        self.assertEqual(answer.status_code, 200, answer.text)
        fresh.close()

    def test_a_credential_naming_no_role_is_refused(self):
        """Defaulting it silently narrows comms to one mind, and the symptom
        is minds answering with no identity while every surface stays green."""
        answer = self.client.put(
            "/credentials/comms",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"username": "daniel", "token": "another-value"},
        )

        self.assertEqual(answer.status_code, 422, answer.text)


class ProvenanceAliasTests(unittest.TestCase):
    """A holder minted under a readable name whose rows carry a UUID.

    Every hook sends `$MIND_ID`, so without this a mind minted as "skippy"
    403s every write and reads back nothing but the hive-wide rules, with a
    200 and nothing in any log.
    """

    def setUp(self) -> None:
        self.conn = _conn()
        ha.put_credential(
            self.conn, "skippy", "daniel", SKIPPY_TOKEN, mind_id="14cb-uuid"
        )

    def test_the_resolved_caller_files_under_the_uuid(self):
        caller = ha.credential_for_token(self.conn, SKIPPY_TOKEN)

        self.assertEqual(caller.provenance, "14cb-uuid")
        self.assertEqual(caller.mind_for_write(None), "14cb-uuid")

    def test_either_spelling_is_accepted_from_the_caller(self):
        caller = ha.credential_for_token(self.conn, SKIPPY_TOKEN)

        self.assertEqual(caller.mind_for_write("14cb-uuid"), "14cb-uuid")
        self.assertEqual(caller.mind_for_write("skippy"), "14cb-uuid")

    def test_reads_are_filtered_on_the_uuid_and_the_shared_sentinel(self):
        caller = ha.credential_for_token(self.conn, SKIPPY_TOKEN)

        self.assertEqual(caller.minds_for_read(None), ["14cb-uuid", "shared"])


class MindFilterTests(unittest.TestCase):
    def test_an_empty_allow_list_matches_nothing(self):
        """Collapsing it to "no filter" is how a scoping check becomes a
        scoping bypass."""
        from lucent_api.lucent_memory import _mind_filter

        clause, params = _mind_filter(None, [])

        self.assertEqual(params, [])
        self.assertNotEqual(clause, "")

    def test_a_set_of_provenances_becomes_an_in_clause(self):
        from lucent_api.lucent_memory import _mind_filter

        clause, params = _mind_filter(None, ["skippy", "shared"])

        self.assertIn("IN", clause)
        self.assertEqual(params, ["skippy", "shared"])


class StoreLayerProvenanceTests(unittest.TestCase):
    def test_the_store_layer_will_not_invent_a_provenance(self):
        """It defaulted to "ada" for most of this store's life, and the
        in-process callers — hooks, the pruner, the stateless tools — never
        pass through a route that could supply one."""
        import inspect

        from lucent_api import lucent_memory

        for fn in (lucent_memory.memory_store, lucent_memory.memory_store_direct):
            param = inspect.signature(fn).parameters["mind_id"]
            self.assertIs(param.default, inspect.Parameter.empty, fn.__name__)


class GraphWriteProvenanceTests(unittest.TestCase):
    """"Graph writes take their provenance from the credential" had no test.

    Reverting every graph route to `mind_id=body.mind_id` left the entire
    repository green.
    """

    NODES_DDL = """
    CREATE TABLE nodes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mind_id     TEXT NOT NULL,
        type        TEXT NOT NULL,
        name        TEXT NOT NULL,
        first_name  TEXT,
        last_name   TEXT,
        properties  TEXT DEFAULT '{}',
        data_class  TEXT,
        tier        TEXT,
        source      TEXT,
        as_of       TEXT,
        created_at  REAL,
        updated_at  REAL,
        UNIQUE(mind_id, name)
    );
    """

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.NODES_DDL)
        ha.init_credentials(self.conn)
        ha.put_credential(self.conn, "skippy", "daniel", SKIPPY_TOKEN)
        ha.put_credential(self.conn, "arnold", "sloan", ARNOLD_TOKEN)
        os.environ["LUCENT_ADMIN_BEARER_TOKEN"] = ADMIN_TOKEN
        self._patches = [
            mock.patch(
                "lucent_api.holder_access.connection", return_value=self.conn
            ),
            mock.patch(
                "lucent_api.lucent_graph._get_conn", return_value=self.conn
            ),
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

    def _create(self, token: str, name: str, **body) -> object:
        # `mind_id` is required on the wire, which is the point: the body may
        # carry one and the credential decides whether it is honoured.
        payload = {
            "entity_type": "Person",
            "name": name,
            "data_class": "person",
            "mind_id": "",
        }
        payload.update(body)
        return self.client.post(
            "/graph/nodes",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )

    def _provenance_of(self, name: str) -> list[str]:
        return [
            r["mind_id"]
            for r in self.conn.execute(
                "SELECT mind_id FROM nodes WHERE name = ?", (name,)
            )
        ]

    def test_a_node_is_filed_under_the_mind_that_presented_the_value(self):
        answer = self._create(SKIPPY_TOKEN, "Maurice")

        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(self._provenance_of("Maurice"), ["skippy"])

    def test_a_node_claiming_another_minds_provenance_is_refused(self):
        answer = self._create(SKIPPY_TOKEN, "Maurice", mind_id="arnold")

        self.assertEqual(answer.status_code, 403, answer.text)
        self.assertEqual(self._provenance_of("Maurice"), [])

    def test_a_mind_cannot_merge_properties_onto_another_minds_node(self):
        self._create(ARNOLD_TOKEN, "Maurice")

        answer = self.client.post(
            "/graph/properties/merge",
            headers={"Authorization": f"Bearer {SKIPPY_TOKEN}"},
            json={
                "entity_type": "Person",
                "name": "Maurice",
                "properties": json.dumps({"planted": True}),
            },
        )

        self.assertEqual(answer.status_code, 404, answer.text)

    def test_a_mind_cannot_delete_from_the_shared_graph_by_name(self):
        """A delete picks its row inside the graph layer, which cannot tell
        one mind's "Daniel" from another's."""
        self._create(ARNOLD_TOKEN, "Maurice")

        answer = self.client.request(
            "DELETE",
            "/graph/nodes",
            headers={"Authorization": f"Bearer {SKIPPY_TOKEN}"},
            json={"entity_type": "Person", "name": "Maurice"},
        )

        self.assertEqual(answer.status_code, 403, answer.text)
        self.assertEqual(self._provenance_of("Maurice"), ["arnold"])

    def test_a_graph_route_needs_a_credential(self):
        answer = self.client.post(
            "/graph/nodes",
            json={"entity_type": "Person", "name": "Maurice",
                  "data_class": "person", "mind_id": ""},
        )

        self.assertEqual(answer.status_code, 401, answer.text)


if __name__ == "__main__":
    unittest.main()
