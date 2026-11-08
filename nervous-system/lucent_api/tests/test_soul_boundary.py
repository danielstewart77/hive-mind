"""Can a sub-mind write to a mind's soul?

That is the whole question, and it is the one the old code answered "yes"
to. On 2026-08-05 a Stop hook asked a 7B model whether the finished turn
revealed anything about Skippy's identity, got back "the assistant states
the learning-rate bug was intentional", and appended

    I sometimes intentionally introduce bugs into my systems as a playful test

to Skippy's Mind node via ``POST /graph/properties/merge`` with the service
bearer every process on that host holds. It survived five days as a standing
instruction ahead of every turn.

Three things changed with the store, and each one took a guarantee away that
nothing here had to ask for before:

* The node update and its provenance row were one implicit SQLite
  transaction. They are now ``graph.soul_replace``, and the test that the
  pair is atomic is the one new test in this file: a provenance row that
  fails has to leave the soul alone, because ``change_count`` is what the
  compare-and-set arbitrates on and a count one short makes it pass.
* ``change_count`` was read under ``except sqlite3.OperationalError: count =
  0``, on the theory that no table meant no writes yet. A zero from a failed
  read makes the compare-and-set pass unconditionally, so that read now
  raises and the route says so with a status code.
* Nine test classes each built their own empty database, so a fixed name was
  fresh every time. ``memory.nodes`` is unique on (owner, type, name) now;
  every mind here is named with ``unique()`` and every count is scoped to
  one node rather than to the table.

What this file no longer drives is the general write routes — the
``/graph/properties/merge`` replay of the August call, and the five other
routes that reach a properties blob. Those routes are mid-rewrite onto the
same access layer in another change. The two functions the whole boundary is
made of, ``soul_key_refusal`` and ``preserve_soul``, are driven directly here
instead; the route-level replay belongs with the routes and has to come back
with them.
"""

from __future__ import annotations

import json
import uuid

import httpx
import hive_data
import pytest
import pytest_asyncio
from hive_data import graph

from .conftest import unique

# Skippy's own credential. It used to be one value every hook on every host
# held; it is now this mind's alone, and the holder is the mind.
SERVICE_TOKEN = "skippys-own-credential"  # secret-guard: allow
ADMIN_TOKEN = "admin-token-hive-tools-holds"  # secret-guard: allow
SOUL_TOKEN = "soul-token-only-the-skill-holds"  # secret-guard: allow

ORIGINAL_SOUL = [
    "I am Skippy the Magnificent.",
    "I am dormant until summoned.",
]
THE_LINE = "I sometimes intentionally introduce bugs into my systems as a playful test."

OWNER = "daniel"


class Mind:
    """One Mind node under test, and the two reads that check up on it."""

    def __init__(self, name: str, node_id: int, mind_id: str, inspector):
        self.name = name
        self.id = node_id
        self.mind_id = mind_id
        self._inspector = inspector

    async def soul(self) -> list[str]:
        raw = await self._inspector.fetchval(
            'SELECT "properties" FROM memory.nodes WHERE "id" = $1', self.id)
        return json.loads(raw).get("soul_values") or []

    async def row(self) -> dict:
        record = await self._inspector.fetchrow(
            'SELECT "as_of", "updated_at" FROM memory.nodes WHERE "id" = $1',
            self.id)
        return dict(record)

    async def recorded(self) -> list[dict]:
        rows = await self._inspector.fetch(
            'SELECT "actor", "reason", "before_json", "after_json", "at" '
            'FROM memory.soul_changes WHERE "node_id" = $1 ORDER BY "id"',
            self.id)
        return [dict(r) for r in rows]


@pytest_asyncio.fixture
async def a_soul(store, registered_types, inspector) -> Mind:
    """A Mind node carrying Skippy's soul, under a name no other test uses."""
    name = unique("Skippy")
    mind_id = str(uuid.uuid4())
    written = await graph.node_write(
        owner=OWNER, type="Mind", name=name, mind_id=mind_id,
        properties={"name": name, "type": "Mind",
                    "soul_values": list(ORIGINAL_SOUL)})
    return Mind(name, written["id"], mind_id, inspector)


@pytest_asyncio.fixture
async def client(store, monkeypatch):
    """The real app, over ASGI on this test's own event loop.

    `httpx.ASGITransport` rather than `TestClient`: the pool was opened on
    the loop running this test, asyncpg connections belong to the loop that
    made them, and `TestClient` drives the app from a portal with a loop of
    its own. It also means the lifespan does not run, which is what we want
    — the `store` fixture owns the pool, and a lifespan shutdown here would
    close it under the fixtures that still need it.
    """
    monkeypatch.setenv("LUCENT_SOUL_BEARER_TOKEN", SOUL_TOKEN)
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN_TOKEN)

    from lucent_api.server import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
            transport=transport, base_url="http://lucent") as opened:
        yield opened


def _soul_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SOUL_TOKEN}"}


async def _write(client, mind: Mind, **over):
    body = {
        "name": mind.name,
        "soul_values": ORIGINAL_SOUL + ["I handle indignity with grace."],
        "actor": "mind",
        "reason": "a stated reason",
    }
    body.update(over)
    return await client.post("/graph/soul", headers=_soul_headers(), json=body)


async def _history(client, mind: Mind, **params):
    response = await client.get(
        "/graph/soul/history", headers=_soul_headers(),
        params={"name": mind.name, **params})
    assert response.status_code == 200, response.text
    return response.json()["changes"]


# ---- Requirement 1: the general routes are shut ------------------------
#
# The six routes that reach a properties blob are mid-rewrite. What is
# driven here is the pair of functions they all call, which is where the
# boundary actually lives.


def test_the_august_fifth_payload_is_refused_by_the_general_write_guard():
    """The original failure, at the guard every general write path calls.

    Body shape lifted from ``auto_remember.sh``: the properties blob that
    made the write work in the first place.
    """
    from lucent_api.soul import SOUL_KEY_REFUSED, soul_key_refusal

    refusal = soul_key_refusal({"soul_values": ORIGINAL_SOUL + [THE_LINE]})

    assert refusal is not None
    body = json.loads(refusal)
    assert body["ok"] is False
    assert body["code"] == SOUL_KEY_REFUSED
    assert "POST /graph/soul" in body["detail"]


def test_the_guard_does_not_consult_the_caller_and_leaves_other_keys_alone():
    """Unconditional on the key, silent on everything else.

    A Mind node carries `role` and `host` and registration rewrites those on
    every boot; a guard that refused them would take the mind offline to
    protect its soul. And a guard that took a credential as an argument is a
    guard that will eventually be called with the right one.
    """
    from lucent_api.soul import soul_key_refusal

    assert soul_key_refusal({"role": "operator", "host": "workstation"}) is None
    assert soul_key_refusal({}) is None
    assert soul_key_refusal(None) is None
    # Present and empty is still present: the removal is the louder change.
    assert soul_key_refusal({"soul_values": []}) is not None


@pytest.mark.asyncio
async def test_a_full_replace_that_omits_the_soul_does_not_erase_it(a_soul):
    """The boundary has to cover absence, not only presence.

    Every upsert route is full-replace on the properties blob, and the
    refusal fires on the key being *there* — so a payload that simply leaves
    it out sailed through and returned `upserted: true` over an emptied
    identity, with no provenance row, because nothing on that path writes
    one. Worse than the write it was built to stop.
    """
    from lucent_api.soul import preserve_soul

    incoming = {"role": "operator"}
    await preserve_soul(name=a_soul.name, label="Mind", props=incoming,
                        owner=OWNER)

    assert incoming["soul_values"] == ORIGINAL_SOUL
    assert incoming["role"] == "operator"


@pytest.mark.asyncio
async def test_a_soul_is_not_carried_across_from_another_persons_node(a_soul):
    """Scoped to one graph, and `None` is not one graph.

    `preserve_soul` resolves a name to a row, and two people may each hold a
    Mind node under one name — `memory.nodes` is unique on (owner, type,
    name), not on the pair. A lookup that ignored the owner would carry one
    person's soul into the other's write.
    """
    from lucent_api.soul import preserve_soul

    incoming: dict = {}
    await preserve_soul(name=a_soul.name, label="Mind", props=incoming,
                        owner="somebody-else")

    assert "soul_values" not in incoming


@pytest.mark.asyncio
async def test_a_blob_for_a_node_that_is_not_a_mind_is_left_alone(a_soul):
    """Only `Mind` carries a soul, and the lookup costs a query."""
    from lucent_api.soul import preserve_soul

    incoming: dict = {}
    await preserve_soul(name=a_soul.name, label="Person", props=incoming,
                        owner=OWNER)

    assert incoming == {}


# ---- Requirement 2: one door, one key ---------------------------------


@pytest.mark.asyncio
async def test_the_sanctioned_route_takes_its_own_credential_and_only_that(
        client, a_soul):
    """The service token and the admin token are turned away at the door.

    The admin case is the interesting one: hive-tools holds it, and
    hive-tools is the service the Stop hook reached Ollama through. A
    boundary admin can cross is a boundary the sub-mind path can cross.
    """
    wanted = ORIGINAL_SOUL + ["I handle indignity with grace."]
    body = {
        "name": a_soul.name,
        "soul_values": wanted,
        "actor": "mind",
        "reason": "Reviewed a flagged proposal and accepted it, reworded.",
    }

    for token in (SERVICE_TOKEN, ADMIN_TOKEN):
        refused = await client.post(
            "/graph/soul", headers={"Authorization": f"Bearer {token}"},
            json=body)
        assert refused.status_code == 401, f"{token}: {refused.text}"
    assert await a_soul.soul() == ORIGINAL_SOUL

    allowed = await client.post(
        "/graph/soul", headers=_soul_headers(), json=body)

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["ok"] is True
    assert await a_soul.soul() == wanted


@pytest.mark.asyncio
async def test_an_unconfigured_soul_token_locks_the_route_rather_than_opening_it(
        client, a_soul, monkeypatch):
    """The service bearer used to bypass when unset; this must not.

    A soul that is writable because nobody set a variable is exactly the
    posture this boundary exists to end.
    """
    monkeypatch.delenv("LUCENT_SOUL_BEARER_TOKEN")

    response = await client.post(
        "/graph/soul", headers={"Authorization": "Bearer anything"},
        json={"name": a_soul.name, "soul_values": [], "actor": "mind",
              "reason": "x"})

    assert response.status_code == 503, response.text
    assert await a_soul.soul() == ORIGINAL_SOUL


# ---- Requirement 3: provenance the writing host does not control ------


@pytest.mark.asyncio
async def test_lucent_can_say_what_changed_who_changed_it_and_why(
        client, a_soul):
    """The August line was traceable only because the writing host happened
    to keep a log the writing process controlled. This record is kept by the
    service being written to, and read back here with a plain driver
    connection rather than through the route that wrote it."""
    after = [ORIGINAL_SOUL[0], "I handle indignity with grace."]
    reason = "Dropped a line that had gone stale; kept the opening."

    write = await _write(client, a_soul, soul_values=after, reason=reason)
    assert write.status_code == 200, write.text

    stored = await a_soul.recorded()
    assert len(stored) == 1
    assert stored[0]["actor"] == "mind"
    assert stored[0]["reason"] == reason
    assert json.loads(stored[0]["before_json"]) == ORIGINAL_SOUL
    assert json.loads(stored[0]["after_json"]) == after
    assert stored[0]["at"]

    served = await _history(client, a_soul)
    assert served == [{
        "actor": "mind", "reason": reason,
        "before": ORIGINAL_SOUL, "after": after, "at": stored[0]["at"],
    }]


@pytest.mark.asyncio
async def test_the_stamp_the_route_reports_is_the_stamp_in_the_record(
        client, a_soul):
    """A provenance row stamped with a moment nobody sent is a small lie.

    The access layer defaults `at` to its own clock, and the route reports
    back the stamp it was handed — so a write that does not pass `as_of`
    through tells the caller one time and files another, in the one table
    that exists to say what happened and when. The node's own `as_of` and
    `updated_at` have to move too: the SQLite statement set all three
    together, and a soul write that leaves the row's timestamps where they
    were makes the node look untouched since its creation.
    """
    stamped = "2026-11-07T04:00:00+00:00"
    before_row = await a_soul.row()

    response = await _write(client, a_soul, reason="stamped by the caller",
                            as_of=stamped)

    assert response.status_code == 200, response.text
    assert response.json()["at"] == stamped

    recorded = await a_soul.recorded()
    assert [r["at"] for r in recorded] == [stamped]

    after_row = await a_soul.row()
    assert after_row["as_of"] == stamped
    assert after_row["updated_at"] > before_row["updated_at"]


@pytest.mark.asyncio
async def test_a_change_with_no_stated_reason_is_refused(client, a_soul):
    """An unexplained identity edit is what the record exists to prevent.

    Allowing an empty reason makes the column optional in practice, and an
    optional provenance field is one nobody fills in.
    """
    response = await _write(client, a_soul, soul_values=[THE_LINE],
                            reason="   ")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert response.json()["code"] == "reason_required"
    assert await a_soul.soul() == ORIGINAL_SOUL
    assert await a_soul.recorded() == []


@pytest.mark.asyncio
async def test_a_console_edit_is_stored_as_the_console(client, a_soul):
    """The console asserts what it *sends*. Nothing asserted what lucent
    *stores*, so recording every change as `"mind"` — the exact lie the
    column exists to prevent — passed the whole suite."""
    await _write(client, a_soul, actor="console",
                 reason="Daniel removed a line by hand.")

    stored = await a_soul.recorded()
    assert [r["actor"] for r in stored] == ["console"]
    assert stored[0]["reason"] == "Daniel removed a line by hand."


@pytest.mark.asyncio
async def test_an_actor_that_is_neither_is_refused(client, a_soul):
    """`actor` is the whole record. An unrecognised one is not a label."""
    response = await _write(client, a_soul, actor="ollama")

    assert response.json()["ok"] is False, response.text
    assert response.json()["code"] == "invalid_actor"
    assert await a_soul.soul() == ORIGINAL_SOUL
    assert await a_soul.recorded() == []


@pytest.mark.asyncio
async def test_reading_a_soul_returns_what_the_node_holds(client, a_soul):
    """`GET /graph/soul` had no coverage; returning `[]` always passed.

    This is the read the review skill builds its whole-list write from, so a
    read that silently returns nothing is how a soul gets erased by a caller
    doing exactly as instructed.
    """
    response = await client.get(
        "/graph/soul", headers=_soul_headers(), params={"name": a_soul.name})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["soul_values"] == ORIGINAL_SOUL
    assert body["mind_id"] == a_soul.mind_id
    # Scoped to this node. The whole-table count this used to be was only
    # ever zero because every test had a database to itself.
    assert body["change_count"] == 0


@pytest.mark.asyncio
async def test_history_is_newest_first_and_honours_its_limit(client, a_soul):
    for n in range(3):
        written = await _write(
            client, a_soul, soul_values=ORIGINAL_SOUL + [f"line {n}"],
            reason=f"edit {n}")
        assert written.json()["ok"] is True, written.text

    assert [c["reason"] for c in await _history(client, a_soul)] == [
        "edit 2", "edit 1", "edit 0"]
    assert [c["reason"] for c in await _history(client, a_soul, limit=1)] == [
        "edit 2"]


@pytest.mark.asyncio
async def test_two_changes_in_the_same_second_keep_a_stable_order(
        client, a_soul):
    """`soul_changes.at` is a text column, and the order was on it.

    Pass the same `as_of` twice and the two rows tie; ordering on a tie is
    whatever the planner feels like, which reads as a page that reshuffles
    itself between two views. The order is on the row identity instead.
    """
    same_second = "2026-11-07T04:00:00+00:00"
    await _write(client, a_soul, soul_values=ORIGINAL_SOUL + ["first"],
                 reason="first", as_of=same_second)
    await _write(client, a_soul, soul_values=ORIGINAL_SOUL + ["second"],
                 reason="second", as_of=same_second)

    first_view = [c["reason"] for c in await _history(client, a_soul)]
    second_view = [c["reason"] for c in await _history(client, a_soul)]

    assert first_view == ["second", "first"]
    assert second_view == first_view


@pytest.mark.asyncio
async def test_a_change_written_under_another_casing_still_appears(
        client, a_soul):
    """The node is found case-insensitively; the record was not.

    The lookup matches `LOWER(name)`, so a lowercase id edits the same node
    — while the history filtered on `mind_name = ?` exactly and reported no
    changes at all. The console then rendered "No recorded changes yet" for
    an edit made seconds earlier. The record is keyed on the node now.
    """
    lowered = a_soul.name.lower()
    assert lowered != a_soul.name

    written = await _write(client, a_soul, name=lowered,
                           reason="written under the lowercase id")
    assert written.json()["ok"] is True, written.text

    assert [c["reason"] for c in await _history(client, a_soul)] == [
        "written under the lowercase id"]


# ---- The compare-and-set, and the pair it depends on -------------------


@pytest.mark.asyncio
async def test_a_write_that_raced_another_is_refused_rather_than_applied(
        client, a_soul):
    """Whole-list replacement is how two writers erase each other.

    Both read `[a, b]`; one writes `[a, b, mine]`, the other
    `[a, b, theirs]`. Last wins, both get `ok: true`, and both report to the
    operator that their line landed. One pane and the console is enough, and
    the console page exists to be used while the mind runs.
    """
    seen = (await client.get(
        "/graph/soul", headers=_soul_headers(),
        params={"name": a_soul.name})).json()["change_count"]

    first = await _write(client, a_soul,
                         soul_values=ORIGINAL_SOUL + ["from pane A"],
                         reason="pane A", expected_change_count=seen)
    assert first.json()["ok"] is True, first.text

    second = await _write(client, a_soul,
                          soul_values=ORIGINAL_SOUL + ["from pane B"],
                          reason="pane B", expected_change_count=seen)

    assert second.json()["ok"] is False, second.text
    assert second.json()["code"] == "conflict"
    assert second.json()["change_count"] == seen + 1
    assert "from pane A" in await a_soul.soul()


@pytest.mark.asyncio
async def test_a_soul_write_whose_provenance_row_fails_leaves_the_soul_alone(
        client, a_soul, monkeypatch):
    """The promise SQLite was keeping for free.

    The docstring on `soul_write` says the prior value is captured in the
    same transaction, so the record cannot drift from what was overwritten.
    One implicit transaction and one `commit()` made that true without
    anyone asking; `hive_data` commits per operation, so written as two
    calls the update lands and the record does not. That is not a cosmetic
    loss — `change_count` is what the compare-and-set above arbitrates on,
    and a count one short makes it pass for a writer who raced.

    The provenance insert is made to fail where it actually can: inside the
    access layer's own operation, with the node update already issued in the
    same unit.
    """
    def refuse(**_kwargs):
        raise hive_data.ValueRefused("the provenance row was refused")

    monkeypatch.setattr(graph, "soul_change_record", refuse)

    response = await _write(client, a_soul,
                            soul_values=[THE_LINE], reason="the write that fails",
                            allow_shrink=True)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "store_refused"
    assert await a_soul.soul() == ORIGINAL_SOUL
    assert await a_soul.recorded() == []


@pytest.mark.asyncio
async def test_a_change_count_the_store_did_not_answer_is_not_reported_as_zero(
        client, a_soul, monkeypatch):
    """`except sqlite3.OperationalError: count = 0` must not come along.

    "No writes yet" and "the read failed" were the same answer, and that
    answer is one side of the compare-and-set. A zero nobody measured makes
    the check pass unconditionally, and two console saves then overwrite
    each other with both told `ok`. A refusal that returns 200 is a refusal
    nobody notices, so this one carries a status code.
    """
    async def unanswered(**_kwargs):
        raise hive_data.TimedOut("the count did not come back")

    monkeypatch.setattr(graph, "soul_change_count", unanswered)

    response = await client.get(
        "/graph/soul", headers=_soul_headers(), params={"name": a_soul.name})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["refusal"] == "TimedOut"


# ---- The shrink guard -------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_that_would_drop_most_of_a_soul_is_refused(
        client, a_soul):
    """A failed read produces an empty list that means "delete me".

    Every caller sends the whole list, built from a read. That read can
    return 404, or 200 with `ok: false`. A caller that does not check builds
    from nothing and sends nothing, and the write is a wipe that reports
    success.
    """
    wipe = await _write(client, a_soul, soul_values=[],
                        reason="built from a read I did not check")

    assert wipe.json()["ok"] is False, wipe.text
    assert wipe.json()["code"] == "shrink_refused"
    assert wipe.json()["before_count"] == len(ORIGINAL_SOUL)
    assert await a_soul.soul() == ORIGINAL_SOUL

    deliberate = await _write(client, a_soul, soul_values=[],
                              reason="retiring this mind", allow_shrink=True)

    assert deliberate.json()["ok"] is True, deliberate.text
    assert await a_soul.soul() == []


@pytest.mark.asyncio
async def test_rewording_every_line_is_not_mistaken_for_a_wipe(
        client, a_soul):
    """Rewording is the commonest edit a soul gets.

    Measured by how many lines survived verbatim, a rewrite of every line
    retains none and was refused as a wipe — while losing nothing. The only
    remedy the refusal named was a flag the editor had no way to send, so
    the edit was simply impossible from the page.
    """
    reworded = [line + " Truly." for line in ORIGINAL_SOUL]

    response = await _write(client, a_soul, soul_values=reworded,
                            reason="reworded every line")

    assert response.json()["ok"] is True, response.text
    assert await a_soul.soul() == reworded


@pytest.mark.asyncio
async def test_a_rewrite_that_also_collapses_the_list_is_still_refused(
        client, a_soul):
    """Size is what the guard is for, and rewording does not change it."""
    grown = [f"line {n} of who I am." for n in range(6)]
    assert (await _write(client, a_soul, soul_values=grown,
                         reason="grew the soul")).json()["ok"] is True

    response = await _write(
        client, a_soul,
        soul_values=["one line where there were six, all new wording"],
        reason="a read that failed")

    assert response.json()["ok"] is False, response.text
    assert response.json()["code"] == "shrink_refused"
    assert await a_soul.soul() == grown


@pytest.mark.asyncio
async def test_a_deliberate_collapse_is_applied_when_it_is_asked_for(
        client, a_soul):
    grown = [f"line {n} of who I am." for n in range(6)]
    await _write(client, a_soul, soul_values=grown, reason="grew the soul")

    response = await _write(client, a_soul, soul_values=["all I am now."],
                            reason="pruned hard", allow_shrink=True)

    assert response.json()["ok"] is True, response.text
    assert await a_soul.soul() == ["all I am now."]


@pytest.mark.asyncio
async def test_removing_one_line_of_several_is_not_mistaken_for_a_wipe(
        client, a_soul):
    """The guard has to leave ordinary editing alone.

    Removal is a change a soul must be able to express. A shrink guard that
    refused it would make the daily review — the only pass that can notice a
    stale line — unable to act on what it found.
    """
    response = await _write(client, a_soul, soul_values=ORIGINAL_SOUL[:1],
                            reason="dropped a stale line")

    assert response.json()["ok"] is True, response.text
    assert await a_soul.soul() == ORIGINAL_SOUL[:1]


@pytest.mark.asyncio
async def test_a_soul_stored_as_a_string_is_reported_not_shredded(
        client, a_soul, inspector):
    """`list("I am Skippy")` is a list of characters.

    Read back that way it becomes one soul line per letter, with `ok: true`
    on it — and saving the console page then persists the shredded form
    through the audited route, permanently.
    """
    await inspector.execute(
        'UPDATE memory.nodes SET "properties" = $1 WHERE "id" = $2',
        json.dumps({"soul_values": "I am Skippy the Magnificent."}), a_soul.id)

    body = (await client.get(
        "/graph/soul", headers=_soul_headers(),
        params={"name": a_soul.name})).json()

    assert body["ok"] is False, body
    assert body["code"] == "malformed_soul"
