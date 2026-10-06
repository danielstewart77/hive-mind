"""The Telegram session picker, at the layer each behaviour actually lands.

The picker is a pure function over the gateway's session list, so the
rendering requirements need no bot at all; the routing ones drive the real
handlers with a stand-in for Telegram's transport, because routing is the
behaviour and Telegram is only how the tap or the command arrives.

What is deliberately *not* here: a test that a tap on a conversation the
gateway no longer holds reports it as gone. The good id would come from the
session list and the bad one would be derived as an id absent from that same
list — but membership in that list is the thing under test, so it would only
prove the list agrees with itself. The behaviour is implemented; it is not
testable without circularity, and asserting the error string is a
spellchecker.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import itertools

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-never-valid")

from hive_surfaces import session_picker as picker  # noqa: E402
import hive_surfaces.telegram_bot as bot  # noqa: E402


# ---------------------------------------------------------------------------
# Requirement 1 — /sessions gives one button per open conversation, in the
# order the gateway returned them, each carrying that conversation's own id.
# ---------------------------------------------------------------------------
def test_one_switch_button_per_conversation_in_gateway_order():
    """Breaks if anyone re-sorts, collapses, or drops the id from the payload."""
    sessions = [
        {"id": "11111111-aaaa", "summary": "Taxes"},
        {"id": "22222222-bbbb", "summary": "Roof quote"},
        {"id": "33333333-cccc", "summary": "Laptop build"},
    ]

    rows = picker.build_session_rows(sessions)

    switch_payloads = [
        row[0].callback_data for row in rows
        if row[0].callback_data.startswith(f"{picker.CB_SWITCH}{picker.CB_SEP}")
    ]
    assert switch_payloads == [
        "sw:11111111-aaaa", "sw:22222222-bbbb", "sw:33333333-cccc",
    ]
    # The operator's label beats the gateway's summary on the face of the
    # button; an unlabelled conversation keeps the summary.
    named = picker.build_session_rows([
        sessions[0],
        {**sessions[1], "name": "New roof", "color": "#3481cc"},
        sessions[2],
    ])
    assert "New roof" in named[1][0].text
    assert "Roof quote" not in named[1][0].text
    assert "Taxes" in named[0][0].text


def test_an_empty_list_still_offers_a_new_session():
    """Breaks if the new-session button is only drawn alongside existing rows."""
    rows = picker.build_session_rows([])
    assert [b.callback_data for row in rows for b in row] == [picker.CB_NEW]


# ---------------------------------------------------------------------------
# Requirement 2 — the new-session button starts a conversation and its payload
# is never read as a session id.
# ---------------------------------------------------------------------------
@pytest.fixture()
def tapped(monkeypatch):
    """Drive the real handler; Telegram's transport is the only thing faked."""
    def _tap(payload: str):
        query = MagicMock()
        query.data = payload
        # A real id, because the single-use claim keys on it. A MagicMock
        # coerces to 1, so every tap in this file would share one claim and
        # the second test to run would find its picker already spent.
        query.message.message_id = next(_message_ids)
        query.answer = AsyncMock()
        query.message.reply_text = AsyncMock()
        query.message.text = "Your conversations:"
        query.edit_message_reply_markup = AsyncMock()
        query.edit_message_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = 4242
        update.effective_chat.id = 99
        # The tap's answer is delivered through the bot rather than by replying
        # to the picker, so that a send which fails can be retried and then
        # queued instead of vanishing into the global error handler.
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()

        commands: list[str] = []
        suspended: list[str] = []

        async def fake_command(content, user_id, chat_id):
            commands.append(content)
            return "ok"

        async def fake_suspend(session_id):
            suspended.append(session_id)
            return {"status": "suspended"}

        # `gateway` is built in main(), so it is None at import time. The HTTP
        # client is the only thing standing in here; the routing under test is
        # the real handler's.
        monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
        monkeypatch.setattr(bot, "_handle_server_command", fake_command)
        monkeypatch.setattr(bot, "gateway", MagicMock(
            suspend_session=fake_suspend,
            find_active_session=AsyncMock(return_value=None),
        ))
        return query, commands, suspended, update, ctx
    return _tap


@pytest.mark.asyncio
async def test_the_new_button_starts_a_session_and_is_never_read_as_an_id(tapped):
    """Breaks if the sentinel ever parses as a target, or routes to switch."""
    query, commands, suspended, update, ctx = tapped(picker.CB_NEW)

    await bot.on_session_button(update, ctx)

    assert commands == ["/new"]
    assert suspended == []
    # The payload carries no separator, so nothing downstream can read a
    # target out of it.
    assert picker.decode(picker.CB_NEW) == (picker.CB_NEW, "")


@pytest.mark.asyncio
async def test_a_tapped_conversation_switches_to_that_id(tapped):
    """Breaks if the handler switches on anything but the id it was handed."""
    query, commands, suspended, update, ctx = tapped(
        picker.encode(picker.CB_SWITCH, "22222222-bbbb")
    )

    await bot.on_session_button(update, ctx)

    assert commands == ["/switch 22222222-bbbb"]


# ---------------------------------------------------------------------------
# Requirement 3 — a bare rename leaves an existing label alone.
# ---------------------------------------------------------------------------
def test_a_bare_rename_sends_nothing_rather_than_erasing_the_name():
    """An empty name clears, and `/rename` with no text is easy to send.

    Breaks the moment an empty name is passed through as a body instead of
    being refused here. The body carries the name alone — the write is partial,
    so the colour picked at the tile survives without being read back first.
    """
    assert picker.rename_body("") is None
    assert picker.rename_body("   ") is None

    assert picker.rename_body("Roof, round two") == {"name": "Roof, round two"}


# ---------------------------------------------------------------------------
# Requirement — a reply that names a conversation calls it what the button
# called it, not what the gateway generated.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_switching_reports_the_name_the_button_showed(monkeypatch):
    """A row reading "dragoman" whose tap answered "Resumed New session" reads
    as having resumed something else.

    The name now rides on the row the gateway returns, so the reply reads it
    from the same place the button did. Breaks if the reply goes back to the
    summary while a name is present.
    """
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(server_command=AsyncMock(
        return_value={"id": "77777777-eeee", "summary": "New session",
                      "name": "dragoman"})))

    named = await bot._handle_server_command("/switch 77777777-eeee", 4242, 99)

    assert named == 'Resumed "dragoman"'

    monkeypatch.setattr(bot, "gateway", MagicMock(server_command=AsyncMock(
        return_value={"id": "77777777-eeee", "summary": "New session"})))
    unnamed = await bot._handle_server_command("/switch 77777777-eeee", 4242, 99)

    assert unnamed == 'Resumed "New session"'


def test_the_button_and_the_reply_read_the_same_name():
    """One function names a conversation, so the two cannot drift.

    Both read the name off the session row; there is no separate dict either
    could be handed. Breaks if either side starts resolving the caption for
    itself, and breaks if the name stops being read off the row — which is the
    failure that used to hide behind a `labels` parameter nobody populated.
    """
    named = {"id": "77777777-eeee", "summary": "New session",
             "status": "running", "name": "dragoman"}
    bare = {"id": "77777777-eeee", "summary": "New session", "status": "running"}

    assert picker.caption_for(named) == "dragoman"
    assert "dragoman" in picker.button_text(named)
    assert picker.caption_for(bare) == "New session"
    assert picker.caption_for({"id": "x"}) == "Untitled"


# ---------------------------------------------------------------------------
# Requirement — the picker lists live conversations only; a suspended one is
# not drawn and is not counted.
# ---------------------------------------------------------------------------
def test_a_suspended_conversation_is_not_in_the_picker():
    """Breaks if the filter is dropped, or if it starts eating idle rows —
    idle is a live conversation between turns, not a sleeping one."""
    sessions = [
        {"id": "11111111-aaaa", "summary": "Taxes", "status": "running"},
        {"id": "22222222-bbbb", "summary": "Roof", "status": "suspended"},
        {"id": "33333333-cccc", "summary": "Laptop", "status": "idle"},
        {"id": "44444444-dddd", "summary": "Shouty", "status": "SUSPENDED"},
    ]

    assert [s["id"] for s in picker.visible_sessions(sessions)] == [
        "11111111-aaaa", "33333333-cccc",
    ]


# ---------------------------------------------------------------------------
# Requirement 1 — the picker never offers a conversation that has ended.
# ---------------------------------------------------------------------------
def test_an_ended_conversation_is_not_in_the_picker():
    """A closed conversation cannot be switched to — comms refuses it — so a
    button for one is a button that is guaranteed to fail. On 2026-09-18 the
    host held 6,899 closed rows against 19 live ones, so the picker was
    almost entirely dead buttons.

    Breaks if `closed` is dropped from the filter, and breaks if the filter
    grows an appetite for `idle`, which is a live conversation between turns.
    """
    sessions = [
        {"id": "11111111-aaaa", "summary": "Taxes", "status": "running"},
        {"id": "22222222-bbbb", "summary": "Roof", "status": "closed"},
        {"id": "33333333-cccc", "summary": "Laptop", "status": "idle"},
        {"id": "44444444-dddd", "summary": "Shouty", "status": "CLOSED"},
        {"id": "55555555-eeee", "summary": "Asleep", "status": "suspended"},
    ]

    assert [s["id"] for s in picker.visible_sessions(sessions)] == [
        "11111111-aaaa", "33333333-cccc",
    ]


@pytest.mark.asyncio
async def test_the_sessions_command_neither_draws_nor_counts_an_ended_one(monkeypatch):
    """Requirement 1, at the command rather than the renderer: the command is
    what fetches the list and writes the count. Breaks if the filter lands
    after the count, which would report a total the keyboard does not show."""
    sessions = [
        {"id": f"{i:08d}-dead", "summary": str(i), "status": "closed", "last_active": 0}
        for i in range(20)
    ] + [
        {"id": "99999999-live", "summary": "awake", "status": "running", "last_active": 0},
    ]
    recorder = _Recorder()
    update, ctx = _chat_update(recorder)
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(server_command=AsyncMock(return_value=sessions)))

    await bot.cmd_sessions(update, ctx)

    payloads = [b.callback_data for row in recorder.markup.inline_keyboard for b in row]
    assert payloads == ["sw:99999999-live", picker.CB_NEW]
    assert "21" not in recorder.text, f"an ended conversation was counted: {recorder.text!r}"


@pytest.mark.asyncio
async def test_the_sessions_command_neither_draws_nor_counts_a_suspended_one(monkeypatch):
    """The renderer's own test cannot see this: the command is what fetches
    the list and writes the count. Breaks if the filter lands after the
    count, which would report a total the keyboard does not show."""
    sessions = [
        {"id": f"{i:08d}-xxxx", "summary": str(i), "status": "suspended", "last_active": 0}
        for i in range(20)
    ] + [
        {"id": "99999999-live", "summary": "awake", "status": "running", "last_active": 0},
    ]
    recorder = _Recorder()
    update, ctx = _chat_update(recorder)
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(server_command=AsyncMock(return_value=sessions)))

    await bot.cmd_sessions(update, ctx)

    payloads = [b.callback_data for row in recorder.markup.inline_keyboard for b in row]
    assert payloads == ["sw:99999999-live", picker.CB_NEW]
    # 21 is the unfiltered total. A header naming it means the count was
    # taken before the filter and describes a list the keyboard does not show.
    assert "21" not in recorder.text, f"a suspended conversation was counted: {recorder.text!r}"


# ---------------------------------------------------------------------------
# Requirement 4 — /suspend puts a conversation to sleep: the chat's own when
# given nothing, the named one when given an id.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_bare_suspend_suspends_the_conversation_this_chat_is_in(monkeypatch):
    """Breaks if the bare form stops resolving the chat's own binding, or
    starts demanding an id the operator would have to copy out of a picker."""
    suspended = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value="sess-here"),
        suspend_session=AsyncMock(
            side_effect=lambda sid: suspended.append(sid) or {"status": "suspended"}),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=[])
    await bot.cmd_suspend(update, ctx)

    assert suspended == ["sess-here"]


@pytest.mark.asyncio
async def test_suspend_with_an_id_suspends_that_one_not_the_current_one(monkeypatch):
    """The picker draws ids and a conversation held elsewhere has no other way
    to be reached. Breaks if the argument is ignored for the chat's binding."""
    suspended = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value="sess-here"),
        suspend_session=AsyncMock(
            side_effect=lambda sid: suspended.append(sid) or {"status": "suspended"}),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["33333333-cccc"])
    await bot.cmd_suspend(update, ctx)

    assert suspended == ["33333333-cccc"]


@pytest.mark.asyncio
async def test_a_bare_suspend_with_nothing_to_suspend_suspends_nothing(monkeypatch):
    """An unbound chat has no conversation to name. Breaks if the empty
    lookup is ever passed through to the gateway as a target."""
    suspended = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value=None),
        suspend_session=AsyncMock(
            side_effect=lambda sid: suspended.append(sid) or {"status": "suspended"}),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=[])
    await bot.cmd_suspend(update, ctx)

    assert suspended == []


# ---------------------------------------------------------------------------
# Carried forward from tests/unit/test_telegram_session_picker.py, which tested
# the numbered text list this replaced. The rendering moved from
# `_format_sessions` to `button_text`; the behaviour did not, so the test moved
# with it rather than being deleted.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "session, expected",
    [
        ({"id": "a", "summary": "mine", "adoptable": False, "surface": "telegram"}, ""),
        ({"id": "a", "summary": "mine", "adoptable": True, "surface": "terminal"},
         "— on terminal"),
        ({"id": "a", "summary": "mine", "adoptable": True}, "— on another surface"),
    ],
    ids=["own", "held-elsewhere", "held-elsewhere-unnamed"],
)
def test_a_conversation_held_elsewhere_says_so_on_its_button(session, expected):
    """Tapping an adoptable row *moves* the conversation, ending it at the
    other end. A button that looks like every other button hides that.

    Breaks if the marker is dropped, or if it starts appearing on the
    operator's own conversations.
    """
    text = picker.button_text(session)
    if expected:
        assert expected in text
    else:
        assert "on " not in text


# ===========================================================================
# The grill found four behaviours asserted nowhere: `/sessions` itself never
# ran, `/rename` itself never ran, the suspend call's URL was never seen, and
# the callback was never proved answered. Each mutation below was survived by
# the original suite.
# ===========================================================================
_message_ids = itertools.count(1000)


@pytest.fixture(autouse=True)
def _isolated_picker_claims(tmp_path, monkeypatch):
    """Claims are persisted, so a tap in a test writes a file.

    Pointed at a temp directory, or the suite leaves claims in `data/` that
    the next run reads back as pickers already used.
    """
    monkeypatch.setenv("PICKER_STATE_PATH", str(tmp_path / "spent_pickers.json"))
    from hive_surfaces import bot_utils

    bot_utils._reset_pickers()
    yield
    bot_utils._reset_pickers()



class _Recorder:
    """Records what the bot sent, standing in for Telegram only."""

    def __init__(self):
        self.text = None
        self.markup = None

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.text = text
        self.markup = reply_markup

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kwargs):
        # Command replies go out on the bot rather than as a reply to the
        # message, so a failed send can be retried and then queued instead of
        # vanishing into the error handler at INFO. The picker goes out this
        # way too, because the same drawing code serves `/sessions` and the
        # redraw after a failed tap, which has no message to reply to.
        self.text = text
        if reply_markup is not None:
            self.markup = reply_markup


def _chat_update(recorder, args=None):
    update = MagicMock()
    update.message = recorder
    update.effective_user.id = 4242
    update.effective_chat.id = 99
    update.get_bot = lambda: recorder
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot = recorder
    return update, ctx


# --- Requirement 1, at the command rather than the renderer ----------------
@pytest.mark.asyncio
async def test_the_sessions_command_sends_a_keyboard_not_a_list(monkeypatch):
    """Breaks if `/sessions` stops attaching the markup, which the renderer's
    own tests cannot see: they never run the command."""
    sessions = [
        {"id": "11111111-aaaa", "summary": "Taxes", "status": "running", "last_active": 0},
        {"id": "22222222-bbbb", "summary": "Roof", "status": "idle", "last_active": 0},
    ]
    recorder = _Recorder()
    update, ctx = _chat_update(recorder)
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(server_command=AsyncMock(return_value=sessions)))

    await bot.cmd_sessions(update, ctx)

    assert recorder.markup is not None, "/sessions sent no buttons at all"
    payloads = [b.callback_data for row in recorder.markup.inline_keyboard for b in row]
    assert "sw:11111111-aaaa" in payloads
    assert "sw:22222222-bbbb" in payloads


@pytest.mark.asyncio
async def test_a_long_session_list_is_capped_and_says_so(monkeypatch):
    """Telegram rejects an oversized keyboard and the error handler swallows
    it, so the operator sees nothing. Breaks if the cap is removed or the
    count stops being reported."""
    sessions = [
        {"id": f"{i:08d}-xxxx", "summary": str(i), "status": "running", "last_active": 0}
        for i in range(30)
    ]
    recorder = _Recorder()
    update, ctx = _chat_update(recorder)
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(server_command=AsyncMock(return_value=sessions)))

    await bot.cmd_sessions(update, ctx)

    switch_rows = [
        b for row in recorder.markup.inline_keyboard for b in row
        if b.callback_data.startswith("sw:")
    ]
    assert len(switch_rows) == 12
    assert "12 of 30" in recorder.text


# --- Requirement 3, against the payload the button really carries ----------
def test_the_new_button_carries_a_payload_with_no_target_in_it():
    """The original assertion decoded a module constant, which proved nothing
    about the button. Breaks if `encode` ever emits a separator for it."""
    rows = picker.build_session_rows(
        [{"id": "11111111-aaaa", "summary": "x", "status": "running"}]
    )
    new_payload = rows[-1][0].callback_data

    assert picker.decode(new_payload) == (picker.CB_NEW, "")


# --- Requirement 4, at the HTTP call rather than the argument boundary -----
@pytest.mark.asyncio
async def test_suspend_posts_to_the_url_of_the_session_it_was_given():
    """Breaks if the client ever posts to a fixed id — which the handler test
    cannot see, because it replaces this method entirely."""
    from hive_surfaces.gateway_client import GatewayClient

    posted = []

    class _Resp:
        status = 200

        async def json(self):
            return {"status": "suspended"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Http:
        def post(self, url, **kwargs):
            posted.append(url)
            return _Resp()

    client = GatewayClient(
        _Http(), "http://gw.test", "telegram", mind_id="m", bearer_token="t"
    )

    await client.suspend_session("33333333-cccc")

    assert posted == ["http://gw.test/sessions/33333333-cccc/suspend"]


# --- Requirement 5, at the command rather than the body builder ------------
@pytest.mark.asyncio
async def test_a_bare_rename_command_writes_nothing_at_all(monkeypatch):
    """`rename_body` returning None only helps if the handler obeys it.
    Breaks if `cmd_rename` ever PUTs on an empty name."""
    writes = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value="sess-1"),
        rename_session=AsyncMock(
            side_effect=lambda sid, body: writes.append((sid, body)) or 200),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=[])
    await bot.cmd_rename(update, ctx)
    assert writes == [], "a bare /rename reached the gateway"

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["New", "roof"])
    await bot.cmd_rename(update, ctx)
    # The name alone. A body carrying a colour would be a whole-record write,
    # which is how a rename used to blank the colour picked at the tile.
    assert writes == [("sess-1", {"name": "New roof"})]


@pytest.mark.asyncio
async def test_each_refused_rename_says_which_refusal_it_was(monkeypatch):
    """"It didn't work" sends the operator to restart something.

    A 409 means the conversation rotated away and the only thing that helps is
    tapping the live one; an unreachable gateway means try again. Breaks if the
    handler folds the statuses together, which is what it did when the write
    returned a bool.
    """
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)

    async def rename_with(status):
        monkeypatch.setattr(bot, "gateway", MagicMock(
            find_active_session=AsyncMock(return_value="sess-1"),
            rename_session=AsyncMock(return_value=status),
        ))
        return await bot._apply_rename("sess-1", "New roof")

    assert "rotated away" in await rename_with(409)
    assert "reach the gateway" in await rename_with(0)
    assert await rename_with(200) == 'Renamed to "New roof".'


# --- The spinner, and the gate on it ---------------------------------------
@pytest.mark.asyncio
async def test_every_tap_answers_the_callback(tapped):
    """An unanswered callback spins on the phone forever with no error
    anywhere. Breaks if any path returns before answering."""
    for payload in (picker.CB_NEW, picker.encode(picker.CB_SWITCH, "a"), "nonsense"):
        query, _c, _s, update, ctx = tapped(payload)
        await bot.on_session_button(update, ctx)
        assert query.answer.await_count == 1, f"{payload} left the button spinning"


@pytest.mark.asyncio
async def test_a_tap_from_anyone_but_the_owner_does_nothing(tapped, monkeypatch):
    """Every typed command goes through `_auth_check`; a tap has its own gate.
    Breaks if that gate is dropped, which the other handler tests cannot see
    because they all stub it to True."""
    query, commands, suspended, update, ctx = tapped(picker.encode(picker.CB_SWITCH, "a"))
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: False)

    await bot.on_session_button(update, ctx)

    assert commands == []
    assert suspended == []


# ===========================================================================
# From the edge review: three failures that are invisible from where the
# operator stands.
# ===========================================================================
@pytest.mark.asyncio
async def test_rename_never_creates_the_conversation_it_is_naming(monkeypatch):
    """`ensure_session` creates when it finds nothing — minting a conversation,
    binding the chat to it and spawning a harness. A rename against a suspended
    conversation therefore started an empty one, named that, and said it worked.

    Breaks if the lookup ever creates again.
    """
    created = []
    writes = []
    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value=None),
        ensure_session=AsyncMock(side_effect=lambda u, c: created.append(c) or "brand-new"),
        rename_session=AsyncMock(side_effect=lambda sid, body: writes.append(sid) or 200),
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["weekend", "notes"])
    await bot.cmd_rename(update, ctx)

    assert created == [], "/rename started a conversation"
    assert writes == [], "/rename named a conversation it had just created"


@pytest.mark.asyncio
async def test_a_rejected_suspend_body_is_reported_as_an_error(monkeypatch):
    """comms raises through its own handlers as {"error": ...}, but a body
    FastAPI rejects comes back as {"detail": ...}. Reading only the first
    reported a 422 as a successful suspend.

    Breaks if the handler goes back to checking one key.
    """
    async def fastapi_rejection(session_id):
        return {"detail": [{"loc": ["path", "session_id"], "msg": "value is not valid"}]}

    monkeypatch.setattr(bot, "_is_allowed_user", lambda uid: True)
    monkeypatch.setattr(bot, "gateway", MagicMock(
        find_active_session=AsyncMock(return_value="sess-here"),
        suspend_session=fastapi_rejection,
    ))

    recorder = _Recorder()
    update, ctx = _chat_update(recorder, args=["33333333-cccc"])
    await bot.cmd_suspend(update, ctx)

    assert recorder.text.startswith("Error:"), \
        f"a rejected suspend reported as {recorder.text!r}"


def test_one_unsendable_row_does_not_take_the_whole_picker_with_it():
    """Telegram rejects an oversized callback_data on the entire sendMessage,
    so one bad row means no buttons at all and `/sessions` replies with
    nothing. Breaks if the per-row limit is dropped.
    """
    sessions = [
        {"id": "11111111-aaaa", "summary": "fine", "status": "running"},
        {"id": "x" * 200, "summary": "far too long", "status": "running"},
        {"id": "22222222-bbbb", "summary": "also fine", "status": "running"},
    ]

    payloads = [
        b.callback_data for row in picker.build_session_rows(sessions) for b in row
    ]

    assert "sw:11111111-aaaa" in payloads
    assert "sw:22222222-bbbb" in payloads
    assert all(
        len(p.encode("utf-8")) <= picker.CALLBACK_DATA_LIMIT for p in payloads
    )


# --- The rename client itself, which every handler test replaces wholesale ---
@pytest.mark.asyncio
async def test_the_rename_client_puts_to_the_named_session_with_its_credential():
    """R2 and R8, at the HTTP call rather than at the argument boundary.

    Every handler test patches `rename_session` with a mock, so nothing else
    proves this method addresses the right route or carries the right key. Breaks
    if the path changes, and breaks if the rename credential stops being sent as
    its own header — in which case comms falls back to the service bearer, which
    it refuses, and three surfaces report a network fault.
    """
    from hive_surfaces.gateway_client import GatewayClient

    calls = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Http:
        def put(self, url, **kwargs):
            calls.append((url, kwargs.get("json"), kwargs.get("headers") or {}))
            return _Resp()

    client = GatewayClient(
        _Http(), "http://gw.test", "telegram", mind_id="m",
        bearer_token="service", rename_token="rename-only",
    )

    status = await client.rename_session("33333333-cccc", {"name": "Health"})

    assert status == 200
    url, body, headers = calls[0]
    assert url == "http://gw.test/sessions/33333333-cccc/name"
    assert body == {"name": "Health"}
    assert headers["X-Rename-Token"] == "rename-only"
    assert headers["Authorization"] == "Bearer service"


@pytest.mark.asyncio
async def test_an_unreachable_gateway_is_a_status_the_reply_can_read():
    """R7: the client reports the failure rather than raising through the handler.

    Breaks if the exception is allowed out — the rename command would die inside
    Telegram's error handler, which logs a network blip at INFO and answers the
    operator with nothing at all.
    """
    from hive_surfaces.gateway_client import GatewayClient

    class _Http:
        def put(self, url, **kwargs):
            raise OSError("no route to host")

    client = GatewayClient(
        _Http(), "http://gw.test", "telegram", mind_id="m", bearer_token="t"
    )

    assert await client.rename_session("sess-1", {"name": "Health"}) == 0


# --- The colour mark, which moved onto the session row with the name ---------
def test_a_conversations_colour_mark_comes_off_its_session_row():
    """The dot is the operator's own mark and the only text-readable trace of a
    colour anywhere.

    It used to be read from a separate labels dict. Breaks if the key changes or
    the lookup is dropped — and a conversation with no colour must stay bare,
    since inventing a mark claims the operator set one.
    """
    # A mark whose nearest dot is not the one the running-status icon already
    # uses, so the assertion is about the colour and not about the status.
    marked = {"id": "11111111-aaaa", "summary": "Roof", "status": "running",
              "color": "#8859a3"}
    bare = {"id": "11111111-aaaa", "summary": "Roof", "status": "running"}

    dot = picker.dot_for_color("#8859a3")
    assert dot in picker.button_text(marked)
    assert dot not in picker.button_text(bare)
