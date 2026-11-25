"""The Telegram model picker: models as buttons, not a name to retype.

`/model <name>` asks the operator to transcribe a deployment name — and the
names are deployment names, not aliases, so they look like
`claude-sonnet-5-5` and `gpt-5.6-terra` and are wrong one character at a
time. A bare `/model` therefore draws what the mind can actually run and lets
a thumb pick it.

Where the list comes from matters. The gateway answers a bare `/model` by
asking the mind, which asks its own inference proxy with its own key — so the
buttons are exactly the deployments this mind may address, and a model it
would be refused never appears. Nothing here invents a name, shortens one or
maps one to a provider.

Like `session_picker`, this module is pure functions over that answer. It
talks to neither Telegram nor the network, which is what makes it testable
without a bot.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------------------------------------------------------------------
# Callback payloads
# ---------------------------------------------------------------------------
# A model tap travels through the same callback handler the session picker
# uses, because that handler is where the single-use claim, the keyboard
# removal and the acknowledgement live. The separator is therefore the same
# one, and the action prefix has to be distinct from every action over there.
CB_PICK = "md"
CB_SEP = ":"

# Telegram rejects an oversized `callback_data` on the whole sendMessage, so
# one over-long row would take every other button with it and `/model` would
# answer with nothing at all. A name that will not fit is dropped from the
# keyboard instead — it remains switchable by typing `/model <name>`.
CALLBACK_DATA_LIMIT = 64

# What this picker's `CallbackQueryHandler` is registered with, so it answers
# its own buttons and no others.
CALLBACK_PATTERN = rf"^{CB_PICK}{CB_SEP}"

# Telegram rejects an oversized `reply_markup` outright, and the bot's error
# handler reports that as a network blip — so the operator taps /model and
# sees nothing. A proxy serving more deployments than this is a list to read,
# not a keyboard to thumb, and `/models` is the command that prints it.
MAX_PICKER_ROWS = 24


def encode(name: str) -> str:
    """The payload one model's button carries: the deployment name, whole.

    The name travels rather than a position in the list, so a tap means the
    same model whenever it lands — the message sits in scrollback and the
    mind's catalog changes underneath it.
    """
    return f"{CB_PICK}{CB_SEP}{name}"


# No `decode` here. The handler these buttons are registered on decodes with
# `session_picker.decode`, and a second inverse living beside `encode` is a
# function nothing runs — tested, it proves nothing about the bot, and the
# day the two disagree the tested one is the one that still passes.


def models_from(result: object) -> list[dict]:
    """The model rows inside the gateway's answer to a bare ``/model``.

    The gateway answers with ``{"models": [...]}``. The bot used to test the
    answer for being a *list*, which a dict is not, so the listing fell
    through to the switch-report branch and read "Switched to None" — a switch
    nothing had performed, reported over a command that had changed nothing.
    Both shapes are accepted here so neither can be mistaken for the other,
    and anything else is no models rather than a guess.
    """
    if isinstance(result, dict):
        rows = result.get("models")
    else:
        rows = result
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("name") or "")]


def button_text(row: dict, default_model: str | None = None) -> str:
    """What one model's button says.

    The deployment name is the label, because it is the thing the operator
    sees reported back on a switch and in `/status` — a friendly label alone
    would make the confirmation read as a different model. The provider is
    named when the mind supplied one, since one proxy fronts several and
    "which vendor is this" is otherwise unanswerable from the keyboard. The
    mind's own default is marked rather than reordered: moving it would make
    the keyboard disagree with the order the mind returned.
    """
    name = str(row.get("name") or "")
    provider = str(row.get("provider_label") or row.get("provider") or "").strip()
    mark = " ✓" if default_model and name == default_model else ""
    where = f" · {provider}" if provider else ""
    return f"{name}{where}{mark}"


def build_model_rows(
    rows: list[dict] | object,
    default_model: str | None = None,
    limit: int = MAX_PICKER_ROWS,
) -> list[list[InlineKeyboardButton]]:
    """One button per model, in the order the mind returned them.

    The mind decides the order — it is the party that asked the proxy — and
    re-sorting here would make the keyboard disagree with `/models`, which
    prints the same answer.

    No fallback button. The session picker ends with "New session" because an
    empty conversation list is still something to act on; an empty model list
    is a mind that cannot reach its provider, and there is no model to offer
    in its place. The caller says that in a sentence instead.
    """
    buttons: list[list[InlineKeyboardButton]] = []
    for row in models_from(rows)[:limit]:
        name = str(row.get("name") or "")
        if len(encode(name).encode("utf-8")) > CALLBACK_DATA_LIMIT:
            continue
        buttons.append([
            InlineKeyboardButton(button_text(row, default_model), callback_data=encode(name)),
        ])
    return buttons


def build_model_keyboard(
    rows: list[dict] | object,
    default_model: str | None = None,
    limit: int = MAX_PICKER_ROWS,
) -> InlineKeyboardMarkup | None:
    """The rows, wrapped for the send call, or ``None`` when there are none.

    ``None`` rather than an empty markup: Telegram renders an empty keyboard
    as a message with a blank attachment, which looks like a picker that
    failed to load rather than a mind with nothing to offer.
    """
    buttons = build_model_rows(rows, default_model, limit)
    return InlineKeyboardMarkup(buttons) if buttons else None
