"""The Telegram harness picker: which CLI runs a conversation, as buttons.

A harness switch names two things — the harness and a model that harness can
send — and a model is only meaningful against the harness it was listed for.
So the picker is two steps on one message: the harnesses the mind offers,
then, once one is tapped, that harness's models in place of them. Only the
model tap switches anything.

What is offered comes from the gateway, which asked the mind: which harnesses
are installed and logged in here, and which models the mind's proxy key may
address on each. Nothing here holds a table of harnesses or of the models
they take.

Pure functions over the gateway's answers, like `model_picker`.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from hive_surfaces import model_picker

# Two prefixes, two handlers. A harness tap only redraws the message, so it
# must not spend the picker — the model tap that follows lands on the same
# message and has to be the one that claims it. A model tap goes through the
# session picker's handler, where the single-use claim, the keyboard removal
# and the acknowledgement live. Both prefixes are distinct from every other
# picker's.
CB_HARNESS = "hh"
CB_HARNESS_MODEL = "hm"
CB_SEP = ":"

HARNESS_PATTERN = rf"^{CB_HARNESS}{CB_SEP}"
MODEL_PATTERN = rf"^{CB_HARNESS_MODEL}{CB_SEP}"

# Telegram refuses the whole message over one oversized `callback_data`.
CALLBACK_DATA_LIMIT = model_picker.CALLBACK_DATA_LIMIT


def encode_harness(name: str) -> str:
    """The payload one harness's button carries."""
    return f"{CB_HARNESS}{CB_SEP}{name}"


def encode_model(harness: str, model: str) -> str:
    """The payload one model's button carries: both names, whole.

    The harness goes first because a harness name never holds the separator
    and a model name may — an Ollama tag is `name:size` — so splitting once
    from the left recovers both.
    """
    return f"{CB_HARNESS_MODEL}{CB_SEP}{harness}{CB_SEP}{model}"


def split_target(target: str) -> tuple[str, str]:
    """The harness and model inside a decoded model payload's target."""
    harness, _, model = (target or "").partition(CB_SEP)
    return harness, model


def harnesses_from(result: object) -> list[dict]:
    """The harness rows inside the gateway's answer to a bare ``/harness``."""
    rows = result.get("harnesses") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("name") or "")]


def unavailable_lines(result: object) -> list[str]:
    """One line per harness the mind cannot run, with the mind's reason.

    Listed rather than drawn: a button for a harness that is not installed
    invites the one tap guaranteed to be refused, and leaving it out entirely
    hides why the operator's harness is missing.
    """
    lines = []
    for row in harnesses_from(result):
        if row.get("available"):
            continue
        reason = str(row.get("reason") or "").strip()
        lines.append(f"{row['name']} — {reason}" if reason else str(row["name"]))
    return lines


def build_harness_keyboard(result: object) -> InlineKeyboardMarkup | None:
    """One button per harness the mind can run, the conversation's ticked.

    ``None`` when none can be offered, so the caller says so in a sentence.
    """
    current = result.get("current") if isinstance(result, dict) else None
    buttons = []
    for row in harnesses_from(result):
        name = str(row["name"])
        if not row.get("available"):
            continue
        if len(encode_harness(name).encode("utf-8")) > CALLBACK_DATA_LIMIT:
            continue
        buttons.append([InlineKeyboardButton(
            f"{name} ✓" if name == current else name,
            callback_data=encode_harness(name),
        )])
    return InlineKeyboardMarkup(buttons) if buttons else None


def harness_header(result: object) -> str:
    """The picker's text: the prompt, then what cannot be picked and why."""
    lines = ["Pick a harness:"]
    unavailable = unavailable_lines(result)
    if unavailable:
        lines.append("")
        lines.append("Not available here:")
        lines.extend(unavailable)
    return "\n".join(lines)


def build_model_keyboard(
    harness: str, result: object, limit: int = model_picker.MAX_PICKER_ROWS,
) -> InlineKeyboardMarkup | None:
    """One button per model the named harness can send, in the mind's order.

    A model whose payload would not fit is dropped rather than taking every
    other button with it — it stays switchable by typing
    `/harness <name> <model>`.
    """
    current = result.get("current") if isinstance(result, dict) else None
    buttons = []
    for row in model_picker.models_from(result)[:limit]:
        name = str(row["name"])
        payload = encode_model(harness, name)
        if len(payload.encode("utf-8")) > CALLBACK_DATA_LIMIT:
            continue
        buttons.append([InlineKeyboardButton(
            model_picker.button_text(row, current), callback_data=payload,
        )])
    return InlineKeyboardMarkup(buttons) if buttons else None


def format_switch_result(result: object) -> str:
    """What a `/harness <name> <model>` reports once the gateway answers."""
    if not isinstance(result, dict) or not result.get("harness") or not result.get("model"):
        return "No harness change was made — send /harness to pick one."
    return f"Switched to {result['harness']} on {result['model']}"
