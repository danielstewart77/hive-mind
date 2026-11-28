"""Naming a conversation by replying, rather than by remembering a syntax.

Tapping a command in Telegram's menu *sends* it — it does not type it into
the composer for you to finish. So a command that needs an argument arrives
bare every time it is tapped, and `/rename` answering "Usage: /rename <name>"
means the menu entry for it is decoration: the only way to actually use it is
to know the syntax and type it out, which is what the menu existed to spare
you.

A bare `/rename` therefore asks. The bot sends a prompt with `ForceReply`,
which opens the keyboard with the composer already aimed at that message, and
the operator types the name straight into it.

**The pending rename is held nowhere.** There is no registry of outstanding
prompts keyed by message id, no expiry, no per-chat state. A reply is
recognised by what it is a reply *to*: the bot's own prompt text, which
arrives on the incoming update inside `reply_to_message`. That is what makes
"the prompt never expires" true rather than true-until-the-service-restarts —
a prompt sent before a restart of the mind's service is answerable after it,
because nothing about the answer depends on the process that asked.

Everything here is a pure function over message text. Nothing talks to
Telegram, reads the network, or knows a chat exists.
"""

from __future__ import annotations

# The exact text of the prompt, and the sentinel a reply is recognised by.
# It is deliberately one fixed string rather than a formatted one naming the
# conversation: the recognition is an equality test, and interpolating the
# conversation's name would mean a rename could only be answered while that
# name still resolved to what it said when the prompt was sent.
PROMPT_TEXT = "What should this conversation be called? Reply with the name."

# Telegram truncates nothing for us and the label store takes what it is
# given, so the cap lives here — the same one `session_picker.rename_body`
# applies, because a name arriving by reply and a name arriving as an
# argument have to end up identical.
MAX_NAME_CHARS = 40


def is_prompt(text: object, from_bot: object = True) -> bool:
    """Whether a message is the rename prompt this module sends.

    Compared whole rather than by prefix: a conversation whose *name* happens
    to begin with the prompt's opening words is not a prompt, and a reply to
    one must not be swallowed as a rename.

    ``from_bot`` is whether the message came from the bot. Text alone is not
    enough: the operator can send the prompt's exact words themselves, and a
    mind asked "what does /rename do?" may quote them verbatim — replying to
    either would then write a label. It defaults to ``True`` so a caller that
    genuinely cannot tell is not silently refused, but every caller here can.
    """
    return bool(from_bot) and isinstance(text, str) and text.strip() == PROMPT_TEXT


def name_from_reply(
    reply_to_text: object, body: object, from_bot: object = True
) -> str | None:
    """The new name carried by a reply to the prompt, or ``None``.

    ``None`` means "this is not a rename" and the message goes to the harness
    as an ordinary turn. That is the common case by a wide margin — every
    message the operator sends that is not answering a prompt lands here — so
    the refusal has to be cheap and total: anything not replying to the prompt
    is not a name, whatever it says.

    An empty or whitespace-only reply is refused too. The terminal's label
    route deletes the row outright when name and colour are both blank, so a
    stray reply carrying nothing would erase a name set at the tile.
    """
    if not is_prompt(reply_to_text, from_bot):
        return None
    if not isinstance(body, str):
        return None
    name = body.strip()
    if not name:
        return None
    # Too long to be a name is treated as not a name. Truncating instead named
    # the conversation with the first forty characters of whatever was typed —
    # so an operator who changed their mind and replied with a question got a
    # conversation called "actually never mind, what is the status" and their
    # question was swallowed rather than answered.
    if len(name) > MAX_NAME_CHARS:
        return None
    return name
