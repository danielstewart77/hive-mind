"""The settings in a mind's `runtime.yaml`, as a form rather than a file.

`runtime_api` owns the file: reading it, the allowlisted public view, and the
atomic line-substituting write. This module owns what the console's settings
panel is allowed to offer and how a value is rendered back into YAML.

A declared schema rather than "every scalar key in the file", for two
reasons. A file carries a mind's identity and its plumbing beside its
settings — the UUID, the gateway address, the config directories, and an
`env` block holding this mind's proxy key — and a form generated from the
file's own keys would offer all of it. And a setting the file has no line for
yet would be unreachable: a dsh mind that has never had a `goal_rounds` line
would never get the control that adds one.

Which settings appear is decided by the harness, not by what happens to be
in the file. Only dsh drives a turn as a goal and only dsh enforces a turn
timeout, so a Claude or Codex mind is not offered controls its runner would
ignore — an offered setting that does nothing is worse than an absent one,
because the operator has no way to tell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Every harness, when a setting names none.
EVERY_HARNESS: tuple[str, ...] = ()

TEXT = "text"
NUMBER = "number"
BOOLEAN = "boolean"
CHOICE = "choice"


@dataclass(frozen=True)
class Setting:
    """One control on the settings panel, and the rule for writing it."""

    key: str
    label: str
    kind: str
    help: str
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    #: Harnesses whose runner honours this setting. Empty means all of them.
    harnesses: tuple[str, ...] = EVERY_HARNESS
    #: Written above the line when this setting is created on a mind whose
    #: file has none, so the next person to open the file is not reading a
    #: bare key.
    comment: tuple[str, ...] = field(default_factory=tuple)


#: The settings panel's whole vocabulary.
#:
#: The model, the provider and the voice are deliberately absent: each has its
#: own panel on the same page, and two renderings of one setting on screen at
#: once is how an operator edits the one that is not being read.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        key="description",
        label="Description",
        kind=TEXT,
        help="What this Mind is for. Shown wherever Minds are listed.",
    ),
    Setting(
        key="rotation_threshold_percent",
        label="Rotation threshold",
        kind=NUMBER,
        minimum=5,
        maximum=95,
        help=(
            "How full the context window gets before a conversation rotates."
            " A percentage rather than a token count, because the count stops"
            " being right the moment the model changes."
        ),
        comment=(
            "# How full the context window gets before a conversation rotates,"
            " as a",
            "# percentage of the model's own window. A token count would be"
            " wrong the",
            "# moment the model changed under it.",
        ),
    ),
    Setting(
        key="on_harness_error",
        label="When a turn fails",
        kind=CHOICE,
        choices=("session",),
        help=(
            "Where a harness failure is reported. Today it is told to the"
            " conversation and nowhere else; escalating to a Mind or to a bug"
            " needs the mechanism underneath it built first."
        ),
        comment=(
            "# Where a harness failure goes. `session` tells the conversation"
            " and",
            "# nothing else, which is the only behaviour implemented so far.",
        ),
    ),
    Setting(
        key="turn_timeout_seconds",
        label="Turn timeout",
        kind=NUMBER,
        minimum=0,
        maximum=86_400,
        harnesses=("dsh_cli",),
        help=(
            "How long one dispatch may run before it is killed, in seconds."
            " Zero means no bound at all — the only automatic recovery from a"
            " genuinely hung model request goes with it."
        ),
        comment=(
            "# How long one dispatch may run, in seconds. Zero means no bound"
            " at all.",
            "# A goal-driven dispatch is many model turns in one process, so"
            " this is",
            "# sized for the job rather than for a single reply.",
        ),
    ),
    Setting(
        key="goal_rounds",
        label="Goal rounds",
        kind=NUMBER,
        minimum=1,
        maximum=200,
        harnesses=("dsh_cli",),
        help=(
            "How many times one dispatch re-enters the model before giving up."
            " One makes every message a single reply, which is what a person"
            " asking a question expects."
        ),
        comment=(
            "# How many times one dispatch re-enters the model at an idle"
            " checkpoint",
            "# before giving up. One is a single reply, which is what a chat"
            " mind wants.",
        ),
    ),
    Setting(
        key="stop_on_failed_call",
        label="Stop on a failed tool call",
        kind=BOOLEAN,
        harnesses=("dsh_cli",),
        help=(
            "Whether one refused tool call ends the whole dispatch. A read"
            " that correctly reports something absent is a failed call too,"
            " which is why this is normally off."
        ),
        comment=(
            "# Whether one failed tool call ends the dispatch. A read that"
            " correctly",
            "# reports an absent file is a failed call too, so this is"
            " normally off.",
        ),
    ),
    Setting(
        key="dsh_profile",
        label="dsh profile",
        kind=TEXT,
        harnesses=("dsh_cli",),
        help=(
            "Which profile under this Mind's dsh home supplies its bundle"
            " layers. Naming one that is not installed is a Mind with no"
            " runner."
        ),
    ),
)

SETTINGS_BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

#: Reported beside the settings but never offered as one: the window belongs
#: to the model, is read from the inference proxy, and is written into the
#: file when the model is saved so a per-turn hook can multiply it by the
#: rotation percentage without making a network call.
CONTEXT_WINDOW_FIELD = "context_window"

#: A text setting's value. Newlines are refused outright rather than escaped:
#: this writer substitutes one line, and a value carrying a line break would
#: append arbitrary YAML to the file.
_TEXT_RE = re.compile(r"[^\n\r]{0,500}")

#: Characters that make a bare YAML scalar mean something else. A description
#: holding a colon is the ordinary case — "Cypher: the dsh mind" — and a bare
#: one splits the line into a nested mapping.
_NEEDS_QUOTING = set(":#{}[]&*!|>%@`\"',")


def settings_for_harness(harness: str) -> tuple[Setting, ...]:
    """The settings this harness's runner actually honours."""
    name = str(harness or "").strip()
    return tuple(
        setting
        for setting in SETTINGS
        if not setting.harnesses or name in setting.harnesses
    )


def settings_view(loaded: dict[str, Any], harness: str = "") -> dict[str, Any]:
    """What the settings panel renders, for one mind's loaded file.

    `harness` defaults to the file's own, so a caller holding the document
    needs nothing else. Each entry carries the control's shape, its current
    value, and whether the file has a line for it at all — an absent setting
    is still offered, and the panel says it is absent rather than showing a
    default as though it had been chosen.
    """
    which = str(harness or loaded.get("harness") or "").strip()
    entries = []
    for setting in settings_for_harness(which):
        present = setting.key in loaded
        entries.append(
            {
                "key": setting.key,
                "label": setting.label,
                "kind": setting.kind,
                "help": setting.help,
                "choices": list(setting.choices),
                "minimum": setting.minimum,
                "maximum": setting.maximum,
                "present": present,
                "value": loaded.get(setting.key) if present else None,
            }
        )
    window = loaded.get(CONTEXT_WINDOW_FIELD)
    return {
        "harness": which,
        "settings": entries,
        "context_window": window if isinstance(window, int) else None,
    }


def render_value(key: str, value: Any) -> str:
    """One setting's value as the YAML scalar to write, or raise.

    Rendering is per kind rather than per `str()`: Python's `False` is a
    string to a YAML reader, so a switch written that way reads back as truthy
    and arms the thing the operator just turned off.
    """
    setting = SETTINGS_BY_KEY.get(key)
    if setting is None:
        raise ValueError(f"Not a writable setting: {key}")

    if setting.kind == BOOLEAN:
        return "true" if _as_bool(setting, value) else "false"

    if setting.kind == NUMBER:
        number = _as_int(setting, value)
        if setting.minimum is not None and number < setting.minimum:
            raise ValueError(f"{setting.label} cannot be below {setting.minimum}")
        if setting.maximum is not None and number > setting.maximum:
            raise ValueError(f"{setting.label} cannot be above {setting.maximum}")
        return str(number)

    text = "" if value is None else str(value)
    if setting.kind == CHOICE:
        if text not in setting.choices:
            offered = ", ".join(setting.choices)
            raise ValueError(f"{setting.label} must be one of: {offered}")
        return _quote(text)

    if not _TEXT_RE.fullmatch(text):
        raise ValueError(f"{setting.label} cannot span more than one line")
    if not text.strip():
        raise ValueError(f"{setting.label} cannot be empty")
    return _quote(text)


def _as_bool(setting: Setting, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    raise ValueError(f"{setting.label} must be true or false")


def _as_int(setting: Setting, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{setting.label} must be a whole number")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{setting.label} must be a whole number") from None


def _quote(text: str) -> str:
    """A scalar safe to write on one line, quoted only when it has to be."""
    if text and not (set(text) & _NEEDS_QUOTING) and text == text.strip():
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
