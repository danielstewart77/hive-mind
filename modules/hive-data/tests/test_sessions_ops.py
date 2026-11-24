"""The session manager's operations that `core` could not express.

Each test names the statement the gateway ran against its SQLite file and
the outcome the port has to keep. What a write produced is read back on a
plain driver connection, for the reason the round-trip suite gives.

The scratch database lives for the whole run, so rows from other tests are
there too: every assertion is scoped to rows this test wrote.
"""

from __future__ import annotations

import uuid

from hive_data import core, sessions_ops


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _session(mind_id: str, clock: float, *, surface: str = "telegram",
                   surface_id: str = "123", status: str = "running",
                   last_active: float | None = None, **extra) -> str:
    session_id = _id("sess")
    await core.create_session(
        id=session_id, surface=surface, surface_id=surface_id,
        mind_id=mind_id, created_at=clock,
        last_active=clock if last_active is None else last_active,
        status=status, conversation_id=_id("conv"))
    if extra:
        await core.update_session(session_id, **extra)
    return session_id


# --------------------------------------------------------------- listings

async def test_a_picker_is_offered_what_other_surfaces_hold_and_not_its_own(
        a_mind, clock):
    """The adoptable half of a picker: conversations on the adoptable
    surfaces held at any address but the asking one. A conversation held
    elsewhere on a surface nobody adopts from is not offered."""
    mine = await _session(a_mind, clock, surface="web", surface_id="me")
    theirs = await _session(a_mind, clock, surface="terminal",
                            surface_id="terminal-x")
    errand = await _session(a_mind, clock, surface="broker",
                            surface_id="elsewhere")

    found = await sessions_ops.find_sessions(
        surfaces=["web", "terminal"], surface_id_not="me", mind_id=a_mind)

    assert [row["id"] for row in found] == [theirs]
    assert mine not in [row["id"] for row in found]
    assert errand not in [row["id"] for row in found]


async def test_the_live_view_lists_only_bound_unretired_sessions_newest_first(
        a_mind, clock):
    older = await _session(a_mind, clock, last_active=clock + 1)
    newer = await _session(a_mind, clock, last_active=clock + 2)
    unbound = await _session(a_mind, clock, last_active=clock + 3)
    retired = await _session(a_mind, clock, status="closed",
                             last_active=clock + 4)
    for session_id in (older, newer, retired):
        await core.bind_surface(client_type="telegram",
                                client_ref=_id("chat"), session_id=session_id)

    found = await sessions_ops.find_sessions(
        mind_id=a_mind, statuses_excluded=("closed", "suspended"), bound=True)

    assert [row["id"] for row in found] == [newer, older]
    assert unbound not in [row["id"] for row in found]


async def test_a_listing_never_carries_a_composed_carry_forward(a_mind, clock):
    """Every listing answers to the token every surface bot holds."""
    await _session(a_mind, clock, carry_forward="the soul and the summary")

    found = await sessions_ops.find_sessions(mind_id=a_mind)

    assert found
    assert all("carry_forward" not in row for row in found)


async def test_an_empty_surface_filter_matches_nothing(a_mind, clock):
    await _session(a_mind, clock)

    assert await sessions_ops.find_sessions(surfaces=[], mind_id=a_mind) == []


async def test_a_binding_to_a_retired_session_is_no_active_session(
        a_mind, clock):
    """A closed session the binding still points at must not be resurrected."""
    chat = _id("chat")
    closed = await _session(a_mind, clock, status="closed")
    await core.bind_surface(client_type="telegram", client_ref=chat,
                            session_id=closed)

    assert await sessions_ops.active_session_id(
        "telegram", chat, statuses_excluded=("closed", "suspended")) is None

    await core.update_session(closed, status="idle")
    assert await sessions_ops.active_session_id(
        "telegram", chat, statuses_excluded=("closed", "suspended")) == closed


async def test_labels_come_back_for_labelled_sessions_closed_ones_included(
        a_mind, clock):
    named = await _session(a_mind, clock, status="closed", name="Garden")
    coloured = await _session(a_mind, clock, color="#abc")
    plain = await _session(a_mind, clock)

    labels = {row["id"]: row for row in await sessions_ops.session_labels()}

    assert labels[named]["name"] == "Garden"
    assert labels[coloured]["color"] == "#abc"
    assert plain not in labels


# ------------------------------------------------------- context reporting

async def test_a_report_that_measured_nothing_leaves_a_recorded_size_standing(
        a_mind, clock, inspector):
    session_id = await _session(
        a_mind, clock, context_tokens=41_000, context_kind="estimated",
        context_observed_at=clock)

    result = await sessions_ops.record_unmeasured_context(
        session_id, observed_at=clock + 600, tokens=0, window=200_000,
        threshold=0)

    assert result["rows"] == 1
    row = await inspector.fetchrow(
        "SELECT context_tokens, context_kind, context_observed_at, "
        "context_window, context_threshold FROM core.sessions WHERE id = $1",
        session_id)
    assert row["context_tokens"] == 41_000
    assert row["context_kind"] == "estimated"
    assert row["context_observed_at"] == clock
    assert row["context_window"] == 200_000
    # A threshold of nothing is a model nobody measured, not one with no room.
    assert row["context_threshold"] is None


async def test_a_report_that_measured_nothing_fills_a_row_with_no_size(
        a_mind, clock, inspector):
    session_id = await _session(a_mind, clock)

    await sessions_ops.record_unmeasured_context(
        session_id, observed_at=clock + 60, tokens=0)

    row = await inspector.fetchrow(
        "SELECT context_tokens, context_kind, context_observed_at "
        "FROM core.sessions WHERE id = $1", session_id)
    assert (row["context_tokens"], row["context_kind"],
            row["context_observed_at"]) == (0, "none", clock + 60)


async def test_a_report_for_a_session_that_is_gone_changes_no_row(
        a_mind, clock, inspector):
    other = await _session(a_mind, clock)

    result = await sessions_ops.record_unmeasured_context(
        _id("gone"), observed_at=clock + 60, tokens=0, window=200_000)

    assert result["rows"] == 0
    row = await inspector.fetchrow(
        "SELECT context_tokens, context_kind, context_observed_at, "
        "context_window FROM core.sessions WHERE id = $1", other)
    assert tuple(row) == (None, None, None, None)


# ------------------------------------------------------------ carry-forward

async def test_a_straggling_turn_from_the_old_conversation_keeps_the_new_seed(
        a_mind, clock, inspector):
    session_id = await _session(a_mind, clock)
    row = await core.get_session(session_id)
    await core.update_session(
        session_id, carry_forward="seed", carry_forward_sid=row["conversation_id"],
        carry_forward_at=clock)

    result = await sessions_ops.clear_carry_forward(
        session_id, typed_into="the-conversation-it-replaced")

    assert result["rows"] == 0
    assert await inspector.fetchval(
        "SELECT carry_forward FROM core.sessions WHERE id = $1",
        session_id) == "seed"


async def test_a_codex_turn_naming_its_own_thread_clears_the_seed(
        a_mind, clock, inspector):
    """A codex hook knows the thread codex minted, which the row keeps as
    `harness_sid` beside the gateway's id the seed was composed for."""
    session_id = await _session(a_mind, clock, harness_sid="codex-thread")
    row = await core.get_session(session_id)
    await core.update_session(
        session_id, carry_forward="seed",
        carry_forward_sid=row["conversation_id"], carry_forward_at=clock)

    # The same thread name on a row whose seed was composed for some other
    # conversation: the thread is not the one that seed is waiting for.
    elsewhere = await _session(a_mind, clock, harness_sid="codex-thread",
                               carry_forward="other seed",
                               carry_forward_sid=_id("conv"),
                               carry_forward_at=clock)

    await sessions_ops.clear_carry_forward(session_id, typed_into="codex-thread")
    kept = await sessions_ops.clear_carry_forward(
        elsewhere, typed_into="codex-thread")

    cleared = await inspector.fetchrow(
        "SELECT carry_forward, carry_forward_sid, carry_forward_at "
        "FROM core.sessions WHERE id = $1", session_id)
    assert tuple(cleared) == (None, None, None)
    assert kept["rows"] == 0
    assert await inspector.fetchval(
        "SELECT carry_forward FROM core.sessions WHERE id = $1",
        elsewhere) == "other seed"


async def test_an_unscoped_turn_clears_whatever_seed_is_stored(
        a_mind, clock, inspector):
    session_id = await _session(
        a_mind, clock, carry_forward="seed", carry_forward_sid="anything")

    await sessions_ops.clear_carry_forward(session_id)

    assert await inspector.fetchval(
        "SELECT carry_forward FROM core.sessions WHERE id = $1",
        session_id) is None


# ------------------------------------------------------------------ overhead

async def test_the_fallback_overhead_is_the_least_on_the_asked_surface_only(
        a_mind, clock):
    """A pane and a process ship different prompts: a pane's lower figure
    must not size a process's opening."""
    for model, surface, tokens in (("opus", "process", 42_000),
                                   ("sonnet", "process", 41_000),
                                   ("opus", "pane", 9_000)):
        await core.record_overhead(
            mind_id=a_mind, harness="claude", model=model, surface=surface,
            tokens=tokens, observed_at=clock)

    assert await sessions_ops.least_overhead_on_surface(
        mind_id=a_mind, harness="claude", surface="process") == 41_000
    assert await sessions_ops.least_overhead_on_surface(
        mind_id=a_mind, harness="codex", surface="process") is None


# -------------------------------------------------------------- the ledger

async def test_session_memory_answers_with_the_newest_under_either_id(
        a_mind, clock):
    gateway_id, thread_id = _id("conv"), _id("thread")
    for conversation_id, body, at in ((gateway_id, "older", clock),
                                      (thread_id, "newer", clock + 5),
                                      (_id("other"), "unrelated", clock + 9)):
        await core.add_session_memory(
            mind_id=a_mind, mind_name="m", client_ref="123",
            conversation_id=conversation_id, body=body, created_at=at)

    row = await sessions_ops.latest_session_memory_for([gateway_id, thread_id])

    assert row is not None and row["body"] == "newer"
    assert await sessions_ops.latest_session_memory_for(["", None]) is None


async def test_the_last_turns_are_the_newest_in_the_order_they_were_said(
        a_mind, clock, inspector):
    """Two turns in one second are told apart by identity, or the mind
    appears to answer before it was asked."""
    session_id = await _session(a_mind, clock)
    for role, content in (("user", "first"), ("assistant", "one"),
                          ("user", "second"), ("assistant", "two")):
        await core.record_turn(session_id=session_id, role=role,
                               content=content, created_at=clock - 1)
    # Moved onto one second newest identity first, so the rows now lie -
    # in the table and in the (session_id, created_at) index alike - in the
    # reverse of the order they were said. An order that leaned on how the
    # rows happen to lie instead of on identity reads them backwards.
    for content in ("two", "second", "one", "first"):
        await inspector.execute(
            "UPDATE core.session_turns SET created_at = $3 "
            "WHERE session_id = $1 AND content = $2",
            session_id, content, clock)

    turns = await sessions_ops.last_turns(session_id, 2)

    assert [t["content"] for t in turns] == ["second", "two"]


async def test_a_session_with_no_conversation_id_is_given_one(
        a_mind, clock, inspector):
    blank = await _session(a_mind, clock)
    kept = await _session(a_mind, clock)
    await inspector.execute(
        "UPDATE core.sessions SET conversation_id = '' WHERE id = $1", blank)
    before = await inspector.fetchval(
        "SELECT conversation_id FROM core.sessions WHERE id = $1", kept)

    await sessions_ops.mint_missing_conversation_ids()

    minted = await inspector.fetchval(
        "SELECT conversation_id FROM core.sessions WHERE id = $1", blank)
    assert minted and str(uuid.UUID(minted)) == minted
    assert await inspector.fetchval(
        "SELECT conversation_id FROM core.sessions WHERE id = $1",
        kept) == before
