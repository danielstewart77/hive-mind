"""Backfill review message formatting and response parsing.

Pure logic module -- no I/O, no Neo4j, no Telegram dependencies.
Used by agents/memory_backfill.py to format Telegram review messages
and parse Daniel's /classify_* responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.memory_schema import DATA_CLASS_REGISTRY

if TYPE_CHECKING:
    from agents.memory_backfill import BackfillEntry
    from core.backfill_classifier import ClassificationResult

MAX_CONTENT_LENGTH = 200
MAX_ENTRIES_PER_MESSAGE = 10
MAX_MESSAGE_LENGTH = 4096


def format_review_batch(
    entries: list[tuple["BackfillEntry", "ClassificationResult"]],
) -> list[str]:
    """Format a batch of low-confidence entries for Telegram review.

    Returns a list of messages, each under Telegram's 4096 char limit,
    with up to 10 entries per message.

    Args:
        entries: List of (BackfillEntry, ClassificationResult) tuples.

    Returns:
        List of formatted message strings.
    """
    if not entries:
        return ["No entries need review -- all entries are classified."]

    messages: list[str] = []
    total = len(entries)

    for batch_start in range(0, total, MAX_ENTRIES_PER_MESSAGE):
        batch = entries[batch_start:batch_start + MAX_ENTRIES_PER_MESSAGE]
        lines: list[str] = []

        # Header
        batch_end = min(batch_start + MAX_ENTRIES_PER_MESSAGE, total)
        lines.append(
            f"Backfill review: {total} entries need classification "
            f"(showing {batch_start + 1}-{batch_end})"
        )
        lines.append("")

        for entry, result in batch:
            # Truncate content
            content = entry.content
            if len(content) > MAX_CONTENT_LENGTH:
                content = content[:MAX_CONTENT_LENGTH] + "..."

            # Format candidates
            candidates_str = ", ".join(result.candidates[:3])

            lines.append(f"ID: {entry.element_id}")
            lines.append(f"  Content: {content}")
            lines.append(f"  Type: {entry.node_type}")
            if entry.tags:
                lines.append(f"  Tags: {entry.tags}")
            lines.append(f"  Best guess: {result.data_class} ({result.confidence:.0%})")
            lines.append(f"  Candidates: {candidates_str}")
            lines.append(f"  Reply: /classify_{entry.element_id} <class>")
            lines.append("")

        message = "\n".join(lines)
        if len(message) > MAX_MESSAGE_LENGTH:
            message = message[:MAX_MESSAGE_LENGTH - 3] + "..."
        messages.append(message)

    return messages


def parse_classify_command(text: str) -> tuple[str, str] | None:
    """Parse a /classify_<id> <class> command.

    Args:
        text: The full command text, e.g. "/classify_4:a:0 person"

    Returns:
        Tuple of (entry_id, data_class) on success, None on parse failure.
    """
    text = text.strip()
    if not text.startswith("/classify_"):
        return None

    # Remove the /classify_ prefix
    remainder = text[len("/classify_"):]
    if not remainder:
        return None

    # Split into ID and class -- the class is the last whitespace-separated token
    parts = remainder.rsplit(None, 1)
    if len(parts) != 2:
        return None

    entry_id, data_class = parts
    entry_id = entry_id.strip()
    data_class = data_class.strip()

    if not entry_id or not data_class:
        return None

    return (entry_id, data_class)
