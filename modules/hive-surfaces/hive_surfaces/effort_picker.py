"""The Telegram effort picker: a conversation's reasoning effort as buttons.

Which levels exist is the model's business, reported by the inference proxy
and relayed by the mind and the gateway — the same path the model list takes.
Nothing here holds a table of levels or of which models take them, so a model
added to the proxy needs no bot change to get its buttons.

Pure functions over the gateway's answer, like `model_picker`.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Routed through the same callback handler as the session and model pickers,
# for the single-use claim, the keyboard removal and the acknowledgement that
# live there. The prefix has to be distinct from both of theirs.
CB_EFFORT = "ef"
CB_SEP = ":"
CALLBACK_PATTERN = rf"^{CB_EFFORT}{CB_SEP}"


def encode(level: str) -> str:
    """The payload one level's button carries."""
    return f"{CB_EFFORT}{CB_SEP}{level}"


def levels_from(result: object) -> list[str]:
    """The levels in the gateway's answer to a bare ``/effort``."""
    levels = result.get("levels") if isinstance(result, dict) else None
    if not isinstance(levels, list):
        return []
    return [str(level) for level in levels if isinstance(level, str) and level]


#: The level that hands the conversation back to the harness's own setting.
DEFAULT = "default"


def build_effort_keyboard(result: object) -> InlineKeyboardMarkup | None:
    """One button per level in the model's order, the current one ticked,
    then one that returns the conversation to the harness's own level.

    ``None`` when the model takes no effort setting — the caller says so in a
    sentence rather than drawing an empty keyboard.
    """
    levels = levels_from(result)
    if not levels:
        return None
    current = result.get("current") if isinstance(result, dict) else None
    rows = [
        [InlineKeyboardButton(
            f"{level} \u2713" if level == current else level,
            callback_data=encode(level),
        )]
        for level in levels
    ]
    rows.append([InlineKeyboardButton(
        f"{DEFAULT} \u2713" if not current else DEFAULT, callback_data=encode(DEFAULT),
    )])
    return InlineKeyboardMarkup(rows)


def format_effort_result(result: object) -> str:
    """What a typed `/effort <level>` reports once the gateway answers."""
    if not isinstance(result, dict) or "effort" not in result:
        return "No effort change was made \u2014 send /effort to pick one."
    if not result.get("effort"):
        return "Effort back to the harness default"
    return f"Effort set to {result['effort']}"
