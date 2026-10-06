"""The Telegram session picker: conversations as buttons, not a numbered list.

A numbered list makes the reader do the gateway's job. You read "3.", you
type `/switch 3`, and the number means whatever the list meant at the moment
it was printed — which on a phone is frequently days ago and several
conversations back. The buttons carry the conversation's own id instead, so
a tap means the same thing whenever it happens.

Everything here is a pure function over the gateway's session list and the
terminal's label store. Nothing in this module talks to Telegram, reads the
network, or knows a chat exists; `telegram_bot` hands the rows it returns to
the send call and nothing more. That is what makes the picker testable
without a bot at all.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from hive_surfaces.bot_utils import time_ago

# ---------------------------------------------------------------------------
# Callback payloads
# ---------------------------------------------------------------------------
# Telegram caps callback_data at 64 bytes. A prefix plus a 36-character UUID
# fits with room to spare; anything longer than an id does not belong here.
CB_SWITCH = "sw"
CB_NEW = "new"

# `new` carries no id, so it can never be mistaken for one: the handler splits
# on the separator and a payload with no second field is not a target.
CB_SEP = ":"

# Telegram rejects an oversized `callback_data` on the whole sendMessage, so
# a single over-long row takes every other button with it and `/sessions`
# answers with nothing. A row that cannot fit is dropped instead: one
# conversation missing from the picker beats no picker.
CALLBACK_DATA_LIMIT = 64

# What the picker's `CallbackQueryHandler` is registered with, so it stops
# answering callbacks belonging to other features sharing the same channel.
CALLBACK_PATTERN = rf"^({CB_SWITCH}{CB_SEP}|{CB_NEW}$)"


def encode(action: str, session_id: str = "") -> str:
    """The payload a button carries. Ids travel whole, never as positions."""
    return f"{action}{CB_SEP}{session_id}" if session_id else action


def decode(payload: str) -> tuple[str, str]:
    """Split a tapped payload into its action and target id.

    An action with no id yields an empty target rather than a guess, which is
    what keeps `new` from ever resolving to a conversation.
    """
    action, _, target = (payload or "").partition(CB_SEP)
    return action, target


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# The dots Telegram will actually render, with the colour each one reads as.
# A label's colour is a free hex value chosen at the terminal tile, so it will
# usually sit between these; the nearest one is the honest answer.
_DOTS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("\U0001f534", (0xE0, 0x1E, 0x25)),  # red
    ("\U0001f7e0", (0xF1, 0x8F, 0x24)),  # orange
    ("\U0001f7e1", (0xFD, 0xCB, 0x2E)),  # yellow
    ("\U0001f7e2", (0x5C, 0x91, 0x3B)),  # green
    ("\U0001f535", (0x34, 0x81, 0xCC)),  # blue
    ("\U0001f7e3", (0x88, 0x59, 0xA3)),  # purple
    ("\U0001f7e4", (0x7A, 0x4E, 0x2C)),  # brown
    ("⚪", (0xE8, 0xE8, 0xE8)),      # white
)
DEFAULT_DOT = "⚪"

# Whether the conversation is running, asleep or over. A suspended row and a
# live one render identically without this, which makes a suspend tap
# unverifiable: the next picker looks exactly like the last one.
_STATUS_ICONS = {
    "running": "\U0001f7e2",
    "idle": "\U0001f4a4",
    "suspended": "\u23f8",
    "closed": "\U0001f534",
}
DEFAULT_STATUS_ICON = "\u2753"

# Telegram rejects an oversized `reply_markup` outright, and the bot's error
# handler logs that rejection as a transient network blip \u2014 so the operator
# taps /sessions and sees nothing at all. The gateway orders by last activity,
# so the head of the list is the part worth drawing.
MAX_PICKER_ROWS = 12


def dot_for_color(color: object) -> str:
    """The nearest renderable dot to a label's hex colour.

    Anything that is not a parseable hex triple — absent, blank, a number, a
    word — gets the default rather than raising. A label the operator set at
    the tile must never be the reason a picker fails to draw.
    """
    if not isinstance(color, str):
        return DEFAULT_DOT
    value = color.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return DEFAULT_DOT
    try:
        r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return DEFAULT_DOT
    return min(
        _DOTS,
        key=lambda d: (r - d[1][0]) ** 2 + (g - d[1][1]) ** 2 + (b - d[1][2]) ** 2,
    )[0]


def caption_for(session: dict) -> str:
    """What a conversation is called whenever it is named to the operator.

    The operator's own name wins over the gateway's generated summary — they
    named it, and a name they chose is the only thing they can predict. One
    function because the button and the reply that follows tapping it have to
    agree: a picker row reading "dragoman" whose tap answers "Resumed New
    session" reads as having resumed something else entirely. The gateway
    writes a summary only on a chat turn, so every conversation the browser
    terminal holds is called "New session" forever, which is exactly the
    population most likely to carry a name.

    The name is read off the session row, which is the only place it lives.
    It used to arrive as a separate dict fetched from the browser terminal over
    HTTP, and a caller that forgot to populate it drew a picker with no names at
    all while every test still passed — so the parameter is gone rather than
    kept for compatibility.
    """
    name = (str(session.get("name") or "")).strip()
    return name or (str(session.get("summary") or "")).strip() or "Untitled"


def button_text(session: dict) -> str:
    """What one conversation's button says.

    Everything falls back rather than raising, because a malformed row must
    cost its own button's prose, not the whole picker.
    """
    session_id = str(session.get("id") or "")
    caption = caption_for(session)
    # A colour is the operator's own mark and only they can decode it, so it is
    # shown when they set one and never invented when they did not. The status
    # icon is always there, because it is the gateway's fact about the row.
    dot = dot_for_color(session.get("color")) if session.get("color") else ""
    status = _STATUS_ICONS.get(str(session.get("status") or ""), DEFAULT_STATUS_ICON)
    where = ""
    # A conversation another surface is holding has to read as elsewhere:
    # tapping it *moves* it here, ending the process at the other end, and
    # that is not what an unadorned button looks like it will do. An adoptable
    # row whose surface the gateway did not name still says so, because the
    # move happens either way.
    if session.get("adoptable"):
        where = f" — on {session.get('surface') or 'another surface'}"
    # The gateway writes a summary only on a chat turn, so every conversation
    # the browser terminal is holding is called "New session" forever. Without
    # the id and the age those rows are indistinguishable from each other, and
    # a picker you cannot read is worse than the numbered list it replaced.
    short = session_id[:8]
    last = session.get("last_active") or 0
    age = time_ago(last) if last else "?"
    return f"{status}{dot} {caption}{where} \u00b7 {short} \u00b7 {age}"


# The statuses a tap can actually reach, named positively.
#
# A denylist of the dead ones re-opens this bug the day comms adds a status:
# anything unrecognised — a new terminal state, a missing field, a null —
# passes the filter and is drawn with the `?` icon, which is a shrug rather
# than a warning. The requirement is "the conversations a tap can reach", so
# the code names those and nothing else.
#
# `idle` belongs here: it is a live conversation between turns, not a
# sleeping one, and dropping it would empty the picker of exactly the
# conversations an operator walks away from and comes back to.
_REACHABLE_STATUSES = frozenset({"running", "idle"})


def visible_sessions(sessions: list[dict] | None) -> list[dict]:
    """The conversations the picker draws: the ones a tap can actually reach.

    A `suspended` conversation has no process behind it and is still resumable
    by id through `/switch`; what it no longer does is fill the keyboard. A
    `closed` one cannot be resumed at all — comms refuses the switch with
    "Session ... is closed" — so a button for one is guaranteed to fail.

    comms already excludes closed rows from the list it hands back, so this is
    defence at the renderer rather than the only guard. It matters because the
    picker is drawn once and tapped later: a conversation that ends while the
    message sits in scrollback is exactly the tap this protects, and the
    renderer is the last place that can still refuse to draw it.
    """
    return [
        s for s in (sessions or [])
        if str(s.get("status") or "").lower() in _REACHABLE_STATUSES
    ]


def build_session_rows(
    sessions: list[dict], limit: int = MAX_PICKER_ROWS
) -> list[list[InlineKeyboardButton]]:
    """One button per conversation, in the order the gateway returned them.

    The gateway decides the order — it is the party that knows what was last
    active — and re-sorting here would mean the picker and every other surface
    disagree about which conversation is on top. A row is the conversation and
    nothing else: suspending is `/suspend`, where it cannot be reached by a
    thumb landing next to the name it meant to tap. The new-session button is
    always last, so an empty list is still a usable picker rather than a dead
    end.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for session in (sessions or [])[:limit]:
        session_id = str(session.get("id") or "")
        if not session_id:
            continue
        if len(encode(CB_SWITCH, session_id).encode("utf-8")) > CALLBACK_DATA_LIMIT:
            continue
        rows.append([
            InlineKeyboardButton(
                button_text(session),
                callback_data=encode(CB_SWITCH, session_id),
            ),
        ])
    rows.append([InlineKeyboardButton("➕ New session", callback_data=encode(CB_NEW))])
    return rows


def build_session_keyboard(
    sessions: list[dict], limit: int = MAX_PICKER_ROWS
) -> InlineKeyboardMarkup:
    """The rows, wrapped for the send call. No decisions of its own."""
    return InlineKeyboardMarkup(build_session_rows(sessions, limit))


def rename_body(new_name: object) -> dict | None:
    """The PUT body for a rename, or ``None`` when nothing should be sent.

    An empty name clears, and a bare `/rename` with no text is the easiest
    thing in the world to type by accident, so it is refused here rather than
    reaching a write that would erase a name set at the tile.

    A rename sends the name and nothing else. The write is a partial one, so
    the colour the operator picked survives without this having to read it
    back first — which is what it used to do, and what left the colour blank
    whenever that read failed.
    """
    if not isinstance(new_name, str):
        return None
    name = new_name.strip()[:40]
    if not name:
        return None
    return {"name": name}
