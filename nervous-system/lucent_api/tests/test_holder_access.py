"""Which mind the memory store thinks is talking, and which minds it may read.

Every test here drives the real `holder_access`, the real `lucent_memory` and
the real routes against the suite's scratch Postgres, and asserts on the row
that actually landed. The only stub is `_embed`, which is a synchronous HTTP
call to Ollama — the point where the process stops being ours.

Two things the move off SQLite changed about how these are written.

Each class used to build its own empty in-memory database in `setUp`, so a
fixed holder, a fixed owner and a fixed content string were virgin every
time. One Postgres is shared by the whole run, `memory.credentials.holder` is
a primary key, and save-time dedup compares a write against every mind's rows
in the same data class — so every holder, owner, provenance and content string
here is minted with `unique()`, and no assertion counts a whole table.

The app is driven over ASGI on this test's own event loop. `TestClient` would
run the routes in a portal thread with a loop of its own, and the pool's
connections belong to the loop that opened them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException

from hive_data import memory as hd_memory

from lucent_api import holder_access as ha
from lucent_api import lucent_memory
from lucent_api.tests.conftest import unique

ADMIN_TOKEN = "lucent-admin-credential"  # secret-guard: allow

#: `memory.memories.embedding` is `vector(4096)`, so a stub returning
#: anything shorter is refused by the database rather than by the test.
DIMS = 4096


def _fake_embed(text: str) -> list[float]:
    """A unit vector per text, orthogonal to every other text's.

    Distinct texts must embed distinctly or the store's own 0.92-cosine
    dedup — which compares against every mind's rows in the same data
    class — swallows every write after the first, and these tests would
    read as a scoping failure. Identical text embeds identically, which is
    what lets a retrieval query rank its own memory first.
    """
    slot = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4],
                          "big") % DIMS
    values = [0.0] * DIMS
    values[slot] = 1.0
    return values


# ---- the hive these tests are run against ---------------------------------


@dataclass
class Hive:
    """One test's holders, owners and client."""

    client: httpx.AsyncClient
    mine: str           # the owner skippy and mordecai both answer for
    theirs: str         # another person
    skippy: str         # that owner's mind
    skippy_token: str
    mordecai: str       # that owner's other mind
    mordecai_token: str
    arnold: str         # the other person's mind
    arnold_token: str
    comms: str          # the gateway's service credential
    comms_token: str
    admin: str = ADMIN_TOKEN

    def h(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    async def store(self, token: str, content: str, **body) -> httpx.Response:
        return await self.client.post(
            "/memory/store", headers=self.h(token),
            json={"content": content, "data_class": "current-state", **body})

    async def stored(self, token: str, content: str, **body) -> int:
        """One memory that really landed, and its id."""
        answer = await self.store(token, content, **body)
        assert answer.status_code == 200, answer.text
        body_json = answer.json()
        assert body_json.get("stored") is True, body_json
        return int(body_json["id"])

    async def listing(self, token: str, **params) -> list[dict]:
        answer = await self.client.get(
            "/memory/list", headers=self.h(token),
            params={"limit": 100, **params})
        assert answer.status_code == 200, answer.text
        return answer.json()["entries"]


@pytest_asyncio.fixture
async def hive(store, registered_types, monkeypatch):
    mine = unique("daniel")
    theirs = unique("sloan")
    holders = {k: unique(k) for k in ("skippy", "mordecai", "arnold", "comms")}
    tokens = {k: unique(f"{k}-token") for k in holders}

    await ha.put_credential(holders["skippy"], mine, tokens["skippy"],
                            role=ha.MIND)
    await ha.put_credential(holders["mordecai"], mine, tokens["mordecai"],
                            role=ha.MIND)
    await ha.put_credential(holders["arnold"], theirs, tokens["arnold"],
                            role=ha.MIND)
    await ha.put_credential(holders["comms"], mine, tokens["comms"],
                            role=ha.SERVICE)
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(lucent_memory, "_embed", _fake_embed)

    from lucent_api.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://lucent"
    ) as client:
        yield Hive(
            client=client, mine=mine, theirs=theirs,
            skippy=holders["skippy"], skippy_token=tokens["skippy"],
            mordecai=holders["mordecai"], mordecai_token=tokens["mordecai"],
            arnold=holders["arnold"], arnold_token=tokens["arnold"],
            comms=holders["comms"], comms_token=tokens["comms"],
        )


# ---- the credential store -------------------------------------------------


@pytest.mark.asyncio
async def test_two_holders_of_one_person_each_get_their_own_value(store):
    owner = unique("daniel")
    first, second = unique("skippy"), unique("bob")
    first_value, second_value = unique("first-value"), unique("second-value")
    await ha.put_credential(first, owner, first_value)
    await ha.put_credential(second, owner, second_value)

    assert (await ha.credential_for_token(first_value)).holder == first
    assert (await ha.credential_for_token(second_value)).holder == second


@pytest.mark.asyncio
async def test_revoking_one_holder_leaves_another_of_the_same_person(store):
    owner = unique("daniel")
    first, second = unique("skippy"), unique("bob")
    first_value, second_value = unique("first-value"), unique("second-value")
    await ha.put_credential(first, owner, first_value)
    await ha.put_credential(second, owner, second_value)

    assert await ha.revoke_credential(first) is True

    assert await ha.credential_for_token(first_value) is None
    assert (await ha.credential_for_token(second_value)).holder == second


@pytest.mark.asyncio
async def test_a_revoked_value_stops_resolving_on_the_very_next_call(store):
    holder = unique("skippy")
    value = unique("value")
    await ha.put_credential(holder, unique("daniel"), value)
    assert await ha.credential_for_token(value) is not None

    await ha.revoke_credential(holder)

    # Same pool, same process, nothing restarted.
    assert await ha.credential_for_token(value) is None


@pytest.mark.asyncio
async def test_a_value_another_holder_already_holds_is_refused(store):
    holder = unique("skippy")
    value = unique("value")
    await ha.put_credential(holder, unique("daniel"), value)

    with pytest.raises(ValueError) as caught:
        await ha.put_credential(unique("arnold"), unique("sloan"), value)

    assert holder in str(caught.value)
    assert (await ha.credential_for_token(value)).holder == holder


@pytest.mark.asyncio
async def test_the_shared_sentinel_is_not_a_holder(store):
    with pytest.raises(ValueError):
        await ha.put_credential(ha.SHARED, unique("daniel"), unique("value"))


@pytest.mark.asyncio
async def test_a_credential_is_stored_as_a_digest(store, inspector):
    holder = unique("skippy")
    value = unique("value")
    await ha.put_credential(holder, unique("daniel"), value)

    row = await inspector.fetchrow(
        'SELECT * FROM memory.credentials WHERE "holder" = $1', holder)

    assert row["token_sha"] == ha.digest(value)
    assert value not in json.dumps({k: str(v) for k, v in dict(row).items()})


# ---- which mind is talking ------------------------------------------------
#
# `mind_for_write` and `minds_for_read` are where the body stops being
# trusted. These call them directly rather than through a route, because the
# decision is theirs and a route only relays it. `Caller` is pure and did not
# move with the store, so these are the tests the port left untouched.


class TestWhichMindIsTalking:
    mind = ha.Caller(holder="skippy", username="daniel", role=ha.MIND)
    service = ha.Caller(holder="comms", username="daniel", role=ha.SERVICE)
    admin = ha.Caller(is_admin=True, role=ha.SERVICE)
    person = ha.Caller(holder="daniels-phone", username="daniel",
                       role=ha.PERSON)

    def test_a_mind_writes_as_itself_whatever_the_body_says(self):
        assert self.mind.mind_for_write(None) == "skippy"
        assert self.mind.mind_for_write("skippy") == "skippy"

    def test_a_mind_naming_another_mind_is_refused_not_rescoped(self):
        with pytest.raises(HTTPException) as caught:
            self.mind.mind_for_write("arnold")

        assert caught.value.status_code == 403

    def test_a_mind_may_write_the_shared_sentinel_explicitly(self):
        """Hive-wide standing rules are written under `shared`, and every
        mind reads them alongside its own. Deriving provenance strictly from
        the holder would turn every hive-wide rule into a private one."""
        assert self.mind.mind_for_write("shared") == "shared"

    def test_a_mind_reads_itself_and_the_shared_sentinel(self):
        assert self.mind.minds_for_read(None) == ["skippy", "shared"]

    def test_a_mind_reading_another_mind_is_refused(self):
        with pytest.raises(HTTPException) as caught:
            self.mind.minds_for_read("arnold")

        assert caught.value.status_code == 403

    def test_a_service_writes_for_the_mind_it_names(self):
        """Comms composes every mind's prompt, so one service legitimately
        reads and writes many minds' records — and has to say which."""
        assert self.service.mind_for_write("arnold") == "arnold"
        assert self.service.minds_for_read("arnold") == ["arnold"]

    def test_a_service_naming_no_mind_cannot_write(self):
        with pytest.raises(HTTPException) as caught:
            self.service.mind_for_write(None)

        assert caught.value.status_code == 400

    def test_an_admin_naming_no_mind_reads_every_one(self):
        assert self.admin.minds_for_read(None) is None

    def test_a_person_has_no_mind_to_write_as_and_reads_nothing(self):
        with pytest.raises(HTTPException):
            self.person.mind_for_write("skippy")
        with pytest.raises(HTTPException):
            self.person.minds_for_read(None)


# ---- the gate -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_minted_value_resolves_to_its_holder(hive):
    caller = await ha.require_holder(
        authorization=f"Bearer {hive.skippy_token}")

    assert caller.holder == hive.skippy
    assert caller.role == ha.MIND


@pytest.mark.asyncio
async def test_a_value_no_holder_holds_is_refused(hive):
    """The retired shared `LUCENT_BEARER_TOKEN` is one of these now."""
    with pytest.raises(HTTPException) as caught:
        await ha.require_holder(authorization=f"Bearer {unique('retired')}")

    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_no_credential_at_all_is_refused(hive):
    with pytest.raises(HTTPException) as caught:
        await ha.require_holder(authorization="")

    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_the_admin_value_reaches_every_mind(hive):
    caller = await ha.require_holder(authorization=f"Bearer {hive.admin}")

    assert caller.is_admin is True
    assert caller.minds_for_read(None) is None


@pytest_asyncio.fixture
async def no_live_credentials(store, inspector):
    """Every credential in the scratch store parked, and then put back.

    `credentials_configured` asks the whole table and this database is
    shared by the run, so the only way to reach the unconfigured branch is
    to revoke what is there and restore it afterwards.
    """
    parked = [r["holder"] for r in await inspector.fetch(
        'UPDATE memory.credentials SET "revoked_at" = 1 '
        'WHERE "revoked_at" IS NULL RETURNING "holder"')]
    try:
        yield
    finally:
        if parked:
            await inspector.execute(
                'UPDATE memory.credentials SET "revoked_at" = NULL '
                'WHERE "holder" = ANY($1::text[])', parked)


@pytest.mark.asyncio
async def test_an_unconfigured_store_refuses_rather_than_serving_open(
    no_live_credentials, monkeypatch
):
    """An unset token used to mean "serve everything to everybody". A store
    that cannot establish who is calling says so."""
    monkeypatch.delenv("LUCENT_ADMIN_BEARER_TOKEN", raising=False)

    with pytest.raises(HTTPException) as caught:
        await ha.require_holder(authorization=f"Bearer {unique('anything')}")

    assert caught.value.status_code == 503


# ---- the memory routes ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_is_filed_under_the_mind_that_presented_the_value(
    hive, inspector
):
    content = unique("something skippy learned")

    memory_id = await hive.stored(hive.skippy_token, content,
                                  mind_id=hive.skippy)

    assert (await inspector.fetchval(
        'SELECT "mind_id" FROM memory.memories WHERE "id" = $1',
        memory_id)) == hive.skippy


@pytest.mark.asyncio
async def test_a_write_naming_another_mind_in_the_body_is_refused(
    hive, inspector
):
    content = unique("planted under arnold")

    answer = await hive.store(hive.skippy_token, content,
                              mind_id=hive.arnold)

    assert answer.status_code == 403, answer.text
    assert (await inspector.fetchval(
        'SELECT count(*) FROM memory.memories WHERE "content" = $1',
        content)) == 0


@pytest.mark.asyncio
async def test_a_write_naming_no_mind_still_lands_under_the_right_one(
    hive, inspector
):
    """The old default was "ada", so an unattributed write arrived with
    somebody's name on it. Asserting only that the write lands would pass
    against that default being restored."""
    content = unique("no mind named anywhere")

    memory_id = await hive.stored(hive.skippy_token, content)

    assert (await inspector.fetchval(
        'SELECT "mind_id" FROM memory.memories WHERE "id" = $1',
        memory_id)) == hive.skippy


@pytest.mark.asyncio
async def test_an_unrecognised_value_writes_nothing(hive, inspector):
    content = unique("from a retired credential")

    answer = await hive.store(unique("retired"), content)

    assert answer.status_code == 401, answer.text
    assert (await inspector.fetchval(
        'SELECT count(*) FROM memory.memories WHERE "content" = $1',
        content)) == 0


@pytest.mark.asyncio
async def test_a_mind_may_write_the_shared_sentinel(hive, inspector):
    content = unique("a hive-wide standing rule")

    memory_id = await hive.stored(hive.skippy_token, content,
                                  mind_id=ha.SHARED)

    assert (await inspector.fetchval(
        'SELECT "mind_id" FROM memory.memories WHERE "id" = $1',
        memory_id)) == ha.SHARED


@pytest.mark.asyncio
async def test_a_listing_shows_the_callers_own_mind_and_the_shared_rules(
    hive
):
    """Skippy's own provenance is unique to this test; `shared` cannot be,
    since being the one provenance every mind writes to is the whole point
    of it. So the listing is read a page of 100 deep — the page is ordered
    oldest first, and this test's own rows are the newest in it."""
    own = unique("skippys own")
    rule = unique("everybodys rule")
    theirs = unique("arnolds own")
    await hive.stored(hive.skippy_token, own)
    await hive.stored(hive.skippy_token, rule, mind_id=ha.SHARED)
    await hive.stored(hive.arnold_token, theirs)

    entries = await hive.listing(hive.skippy_token)

    contents = {e["content"] for e in entries}
    assert own in contents
    assert rule in contents
    assert theirs not in contents
    assert {e["mind_id"] for e in entries} == {hive.skippy, ha.SHARED}


@pytest.mark.asyncio
async def test_a_listing_never_shows_another_minds_memory(hive):
    theirs = unique("arnolds own")
    await hive.stored(hive.arnold_token, theirs)

    entries = await hive.listing(hive.skippy_token)

    assert theirs not in {e["content"] for e in entries}


@pytest.mark.asyncio
async def test_a_service_reads_the_mind_it_names(hive):
    """Comms composing Arnold's prompt has to reach Arnold's memory, and
    has to say that is what it is doing."""
    theirs = unique("arnolds own")
    await hive.stored(hive.arnold_token, theirs)

    entries = await hive.listing(hive.comms_token, mind_id=hive.arnold)

    assert [e["content"] for e in entries] == [theirs]
    assert {e["mind_id"] for e in entries} == {hive.arnold}


@pytest.mark.asyncio
async def test_deleting_another_minds_memory_answers_as_an_absent_one(
    hive, inspector
):
    """Ids are sequential integers, so a refusal shaped differently from
    "not there" is a way to enumerate every memory on the hive."""
    theirs = await hive.stored(hive.arnold_token, unique("arnolds own"))

    refused = await hive.client.delete(
        f"/memory/{theirs}", headers=hive.h(hive.skippy_token))
    absent = await hive.client.delete(
        "/memory/999999999", headers=hive.h(hive.skippy_token))

    assert refused.status_code == 404, refused.text
    assert absent.status_code == 404, absent.text
    assert (await inspector.fetchval(
        'SELECT count(*) FROM memory.memories WHERE "id" = $1', theirs)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vector", "hybrid"])
async def test_retrieve_only_reaches_the_callers_own_mind(hive, mode):
    """Both modes, because they are two searches: `mode=hybrid` goes to
    `recall` and `mode=vector` to `similar`, each with its own scope
    argument, and reverting one of them left the other's test green."""
    own = unique("skippy remembers the proxy")
    theirs = unique("arnold remembers the proxy")
    await hive.stored(hive.skippy_token, own)
    # Arnold's row is seeded with the *same* vector as Skippy's, so a read
    # that lost its scope ranks it just as highly and is caught. Through the
    # route with the same text it would dedup instead — dedup compares
    # across every mind in a data class — and the test would pass on the
    # write never happening.
    await hd_memory.store(
        mind_id=hive.arnold, content=theirs, embedding=_fake_embed(own),
        source="session", data_class="current-state", tier="contextual",
        created_at=1_700_000_000)

    answer = await hive.client.post(
        "/memory/retrieve", headers=hive.h(hive.skippy_token),
        json={"query": own, "k": 10, "min_score": 0.5, "mode": mode,
              "debug": False})

    assert answer.status_code == 200, answer.text
    contents = {m["content"] for m in answer.json()["memories"]}
    assert own in contents
    assert theirs not in contents


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vector", "hybrid"])
async def test_retrieve_naming_another_mind_is_refused(hive, mode):
    await hive.stored(hive.arnold_token, unique("arnold remembers the proxy"))

    answer = await hive.client.post(
        "/memory/retrieve", headers=hive.h(hive.skippy_token),
        json={"query": "proxy", "k": 10, "mind_id": hive.arnold,
              "mode": mode})

    assert answer.status_code == 403, answer.text


@pytest.mark.asyncio
async def test_recent_decayed_only_reaches_the_callers_own_mind(hive):
    own = unique("skippys own")
    theirs = unique("arnolds own")
    await hive.stored(hive.skippy_token, own)
    await hive.stored(hive.arnold_token, theirs)

    answer = await hive.client.get(
        "/memory/recent-decayed", headers=hive.h(hive.skippy_token),
        params={"limit": 100})

    assert answer.status_code == 200, answer.text
    body = answer.json()
    entries = body if isinstance(body, list) else body.get("entries", [])
    contents = {e["content"] for e in entries}
    assert own in contents
    assert theirs not in contents


@pytest.mark.asyncio
async def test_updating_another_minds_memory_answers_as_an_absent_one(
    hive, inspector
):
    """Deleting was covered; updating was not, and the guard's own docstring
    is about both. A mind could rewrite any memory on the hive by id,
    content and provenance included."""
    content = unique("arnolds own")
    theirs = await hive.stored(hive.arnold_token, content)

    answer = await hive.client.put(
        f"/memory/{theirs}", headers=hive.h(hive.skippy_token),
        json={"content": unique("something skippy put there")})

    assert answer.status_code == 404, answer.text
    assert (await inspector.fetchval(
        'SELECT "content" FROM memory.memories WHERE "id" = $1',
        theirs)) == content


@pytest.mark.asyncio
async def test_a_mind_cannot_change_a_hive_wide_rule(hive, inspector):
    """Every mind reads the `shared` rules into its prompt. One mind
    reassigning or deleting one is a mind editing the hive's behaviour, and
    a kid's box should not be able to."""
    content = unique("a hive-wide rule")
    rule = await hive.stored(hive.skippy_token, content, mind_id=ha.SHARED)

    changed = await hive.client.put(
        f"/memory/{rule}", headers=hive.h(hive.arnold_token),
        json={"content": unique("no it is not")})
    removed = await hive.client.delete(
        f"/memory/{rule}", headers=hive.h(hive.arnold_token))

    assert changed.status_code == 403, changed.text
    assert removed.status_code == 403, removed.text
    row = await inspector.fetchrow(
        'SELECT "content", "mind_id" FROM memory.memories WHERE "id" = $1',
        rule)
    assert row["content"] == content
    assert row["mind_id"] == ha.SHARED


@pytest.mark.asyncio
async def test_a_service_changing_a_memory_must_name_the_mind(
    hive, inspector
):
    """A service naming no mind reads every row on the hive, so left
    unchecked it reaches every row by id too — the widest hole in the
    file."""
    theirs = await hive.stored(hive.arnold_token, unique("arnolds own"))

    answer = await hive.client.delete(
        f"/memory/{theirs}", headers=hive.h(hive.comms_token))

    assert answer.status_code == 400, answer.text
    assert (await inspector.fetchval(
        'SELECT count(*) FROM memory.memories WHERE "id" = $1', theirs)) == 1


# ---- the minting routes ---------------------------------------------------
#
# They answer to the admin credential and nothing else. Lucent has no
# deny-by-default route table behind it, so dropping the guard is directly
# exploitable: anyone who can reach the port mints themselves a service
# credential and then reads and writes every mind's memory.


async def _mint(hive: Hive, token: str, holder: str, value: str,
                **body) -> httpx.Response:
    return await hive.client.put(
        f"/credentials/{holder}", headers=hive.h(token),
        json={"username": unique("intruder"), "token": value,
              "role": "service", **body})


@pytest.mark.asyncio
async def test_a_minds_own_credential_cannot_mint_one(hive):
    value = unique("a-value")

    answer = await _mint(hive, hive.skippy_token, unique("intruder"), value)

    assert answer.status_code == 401, answer.text
    assert await ha.credential_for_token(value) is None


@pytest.mark.asyncio
async def test_the_admin_credential_mints_one(hive):
    holder = unique("newcomer")
    value = unique("a-value")

    answer = await _mint(hive, hive.admin, holder, value)

    assert answer.status_code == 200, answer.text
    assert (await ha.credential_for_token(value)).holder == holder


@pytest.mark.asyncio
async def test_a_minds_own_credential_cannot_revoke_one(hive):
    answer = await hive.client.delete(
        f"/credentials/{hive.skippy}", headers=hive.h(hive.skippy_token))

    assert answer.status_code == 401, answer.text
    assert await ha.credential_for_token(hive.skippy_token) is not None


@pytest.mark.asyncio
async def test_a_minds_own_credential_cannot_list_holders(hive):
    answer = await hive.client.get(
        "/credentials", headers=hive.h(hive.skippy_token))

    assert answer.status_code == 401, answer.text


@pytest.mark.asyncio
async def test_the_listing_never_carries_a_value(hive):
    """An operator does not need it; they need to know whether the holder
    has one that works."""
    answer = await hive.client.get("/credentials", headers=hive.h(hive.admin))

    assert answer.status_code == 200, answer.text
    body = answer.text
    assert hive.skippy in body, "the holder under test is not even listed"
    assert hive.skippy_token not in body
    assert ha.digest(hive.skippy_token) not in body
    assert "token_sha" not in body


@pytest.mark.asyncio
async def test_a_credential_naming_no_role_is_refused(hive):
    """Defaulting it silently narrows comms to one mind, and the symptom is
    minds answering with no identity while every surface stays green."""
    answer = await hive.client.put(
        f"/credentials/{unique('comms')}", headers=hive.h(hive.admin),
        json={"username": unique("daniel"), "token": unique("another-value")})

    assert answer.status_code == 422, answer.text


# ---- the provenance alias -------------------------------------------------
#
# A holder minted under a readable name whose rows carry a UUID. Every hook
# sends `$MIND_ID`, so without this a mind minted as "skippy" 403s every
# write and reads back nothing but the hive-wide rules, with a 200 and
# nothing in any log.


@pytest_asyncio.fixture
async def aliased(store) -> tuple[str, str, ha.Caller]:
    holder = unique("skippy")
    provenance = unique("14cb-uuid")
    value = unique("value")
    await ha.put_credential(holder, unique("daniel"), value,
                            mind_id=provenance)
    caller = await ha.credential_for_token(value)
    assert caller is not None
    return holder, provenance, caller


@pytest.mark.asyncio
async def test_the_resolved_caller_files_under_the_uuid(aliased):
    _, provenance, caller = aliased

    assert caller.provenance == provenance
    assert caller.mind_for_write(None) == provenance


@pytest.mark.asyncio
async def test_either_spelling_is_accepted_from_the_caller(aliased):
    holder, provenance, caller = aliased

    assert caller.mind_for_write(provenance) == provenance
    assert caller.mind_for_write(holder) == provenance


@pytest.mark.asyncio
async def test_reads_are_filtered_on_the_uuid_and_the_shared_sentinel(
    aliased
):
    _, provenance, caller = aliased

    assert caller.minds_for_read(None) == [provenance, ha.SHARED]


# ---- the provenance filter itself -----------------------------------------


@pytest.mark.asyncio
async def test_an_empty_allow_list_matches_nothing(store, inspector):
    """Collapsing it to "no filter" is how a scoping check becomes a
    scoping bypass. Asserted on the answer a read gives rather than on the
    SQL it builds: the clause is `"mind_id" = ANY($1::text[])` now and was
    `IN (?,?)` before, and the behaviour is the part that must not move."""
    mind = unique("mind")
    await inspector.execute(
        'INSERT INTO memory.memories '
        '("mind_id", "content", "source", "data_class", "tier", "created_at")'
        ' VALUES ($1, $2, $3, $4, $5, $6)',
        mind, unique("a row that exists"), "session", "current-state",
        "contextual", 1_700_000_000)

    reachable = json.loads(await lucent_memory.memory_list(mind_ids=[mind]))
    nothing = json.loads(await lucent_memory.memory_list(mind_ids=[]))

    assert reachable["total"] == 1, reachable
    assert nothing["total"] == 0, nothing
    assert nothing["entries"] == []


@pytest.mark.asyncio
async def test_a_blank_provenance_matches_nothing_rather_than_every_mind(
    store, inspector
):
    """`if mind_id:` made the clause vanish on the empty string an unset
    `MIND_ID` produces, and a scoping check that becomes a scoping bypass
    on exactly that value is how one mind reads another's memories."""
    mind = unique("mind")
    await inspector.execute(
        'INSERT INTO memory.memories '
        '("mind_id", "content", "source", "data_class", "tier", "created_at")'
        ' VALUES ($1, $2, $3, $4, $5, $6)',
        mind, unique("a row that exists"), "session", "current-state",
        "contextual", 1_700_000_000)

    blank = json.loads(await lucent_memory.memory_list(mind_id="   "))

    assert blank["total"] == 0, blank


# ---- graph writes take their provenance from the credential ---------------
#
# "Graph writes take their provenance from the credential" had no test at
# all: reverting every graph route to `mind_id=body.mind_id` left the entire
# repository green.


async def _create(hive: Hive, token: str, name: str, **body) -> httpx.Response:
    # `mind_id` is required on the wire, which is the point: the body may
    # carry one and the credential decides whether it is honoured.
    payload = {"entity_type": "Person", "name": name,
               "data_class": "person", "mind_id": ""}
    payload.update(body)
    return await hive.client.post(
        "/graph/nodes", headers=hive.h(token), json=payload)


@pytest.mark.asyncio
async def test_a_node_is_filed_under_the_mind_that_presented_the_value(
    hive, inspector
):
    name = unique("Maurice")

    answer = await _create(hive, hive.skippy_token, name)

    assert answer.status_code == 200, answer.text
    assert (await inspector.fetchval(
        'SELECT "mind_id" FROM memory.nodes WHERE "name" = $1',
        name)) == hive.skippy


@pytest.mark.asyncio
async def test_a_node_claiming_another_minds_provenance_is_refused(
    hive, inspector
):
    name = unique("Maurice")

    answer = await _create(hive, hive.skippy_token, name,
                           mind_id=hive.arnold)

    assert answer.status_code == 403, answer.text
    assert (await inspector.fetchval(
        'SELECT count(*) FROM memory.nodes WHERE "name" = $1', name)) == 0


@pytest.mark.asyncio
async def test_a_mind_merges_onto_a_node_its_owners_other_mind_wrote(
    hive, inspector
):
    """The hive half: one person's minds build one graph together."""
    name = unique("Maurice")
    assert (await _create(hive, hive.mordecai_token, name)).status_code == 200

    answer = await hive.client.post(
        "/graph/properties/merge", headers=hive.h(hive.skippy_token),
        json={"entity_type": "Person", "name": name,
              "properties": json.dumps({"runs": "homebridge"})})

    assert answer.status_code == 200, answer.text
    assert answer.json().get("ok") is True, answer.text
    stored = await inspector.fetchval(
        'SELECT "properties" FROM memory.nodes WHERE "name" = $1', name)
    assert json.loads(stored).get("runs") == "homebridge"


@pytest.mark.asyncio
async def test_a_mind_deletes_a_node_its_owners_other_mind_wrote(
    hive, inspector
):
    """A delete resolves its row inside the owner's graph, so a mind of that
    owner reaches it and a mind of another owner cannot."""
    name = unique("Maurice")
    assert (await _create(hive, hive.mordecai_token, name)).status_code == 200

    answer = await hive.client.request(
        "DELETE", "/graph/nodes", headers=hive.h(hive.skippy_token),
        json={"entity_type": "Person", "name": name})

    assert answer.status_code == 200, answer.text
    assert answer.json().get("ok") is True, answer.text
    assert (await inspector.fetchval(
        'SELECT count(*) FROM memory.nodes WHERE "name" = $1', name)) == 0


@pytest.mark.asyncio
async def test_a_graph_route_needs_a_credential(hive, inspector):
    name = unique("Maurice")

    answer = await hive.client.post(
        "/graph/nodes",
        json={"entity_type": "Person", "name": name,
              "data_class": "person", "mind_id": ""})

    assert answer.status_code == 401, answer.text
    assert (await inspector.fetchval(
        'SELECT count(*) FROM memory.nodes WHERE "name" = $1', name)) == 0
