"""Memory data classification schema and validation.

Defines the seven data classes from specs/memory-lifecycle.md, along with
validation functions for data_class, source, and metadata building.

This module is shared between agents/memory.py and agents/knowledge_graph.py
to keep validation logic DRY and avoid circular dependencies.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataClassDef:
    """Definition of a memory data class."""

    name: str  # e.g. "technical-config"
    tier: str  # "reviewable" or "durable"
    tags: list[str] = field(default_factory=list)  # e.g. ["reviewable", "technical"]
    requires_expires: bool = False  # True only for timed-event


DATA_CLASS_REGISTRY: dict[str, DataClassDef] = {
    "technical-config": DataClassDef(
        "technical-config", "reviewable", ["reviewable", "technical"], False
    ),
    "timed-event": DataClassDef(
        "timed-event", "reviewable", ["reviewable", "event"], True
    ),
    "person": DataClassDef("person", "durable", ["durable", "person"], False),
    "news-event": DataClassDef(
        "news-event", "reviewable", ["reviewable", "news-event"], False
    ),
    "preference": DataClassDef(
        "preference", "durable", ["durable", "preference"], False
    ),
    "intention": DataClassDef(
        "intention", "reviewable", ["reviewable", "intention"], False
    ),
    "ada-identity": DataClassDef(
        "ada-identity", "durable", ["durable", "identity"], False
    ),
    "future-project": DataClassDef(
        "future-project", "reviewable", ["reviewable", "project"], False
    ),
    "ephemeral": DataClassDef(
        "ephemeral", "reviewable", ["reviewable", "ephemeral"], False
    ),
    "news-digest": DataClassDef(
        "news-digest", "reviewable", ["reviewable", "news"], False
    ),
}

VALID_SOURCES = {"user", "tool", "session", "self"}
VALID_TIERS = {"reviewable", "durable"}

RECURRING_KEYWORDS: frozenset[str] = frozenset({
    "birthday", "anniversary", "weekly", "monthly", "annual", "every", "recurring",
})

_RECURRING_PATTERN = re.compile(
    r"\b(?:" + "|".join(RECURRING_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def detect_recurring(content: str) -> bool:
    """Detect whether content describes a recurring event via keyword heuristics.

    Scans the content string for word-boundary matches against RECURRING_KEYWORDS.
    Case-insensitive.

    Args:
        content: The text content to scan.

    Returns:
        True if any recurring keyword is found, False otherwise.
    """
    if not content:
        return False
    return bool(_RECURRING_PATTERN.search(content))


def validate_expires_at(expires_at: str) -> str:
    """Validate that expires_at is a resolved absolute ISO 8601 datetime.

    Args:
        expires_at: The datetime string to validate.

    Returns:
        The validated expires_at string.

    Raises:
        ValueError: When expires_at is not a valid ISO datetime or is date-only.
    """
    if not expires_at:
        raise ValueError(
            "expires_at must be a resolved absolute ISO datetime "
            "(e.g. '2026-04-01T15:00:00Z'). Relative or unresolved time "
            f"references like '{expires_at}' are not valid. Please resolve to "
            "an absolute datetime, reclassify the entry, or discard."
        )
    # Reject date-only strings (no 'T' separator)
    if "T" not in expires_at:
        raise ValueError(
            "expires_at must be a resolved absolute ISO datetime "
            "(e.g. '2026-04-01T15:00:00Z'). Relative or unresolved time "
            f"references like '{expires_at}' are not valid. Please resolve to "
            "an absolute datetime, reclassify the entry, or discard."
        )
    try:
        # Handle 'Z' suffix which Python < 3.11 may not parse directly
        parse_value = expires_at.replace("Z", "+00:00") if expires_at.endswith("Z") else expires_at
        datetime.fromisoformat(parse_value)
    except (ValueError, TypeError):
        raise ValueError(
            "expires_at must be a resolved absolute ISO datetime "
            "(e.g. '2026-04-01T15:00:00Z'). Relative or unresolved time "
            f"references like '{expires_at}' are not valid. Please resolve to "
            "an absolute datetime, reclassify the entry, or discard."
        )
    return expires_at


def register_new_class(
    class_name: str,
    tier: str = "reviewable",
    tags: list[str] | None = None,
) -> DataClassDef:
    """Register a new data class at runtime.

    Used during backfill when Daniel classifies entries into a new class
    that doesn't exist in the registry yet.

    Args:
        class_name: Name for the new class (e.g. "shopping-list").
        tier: "reviewable" or "durable" (default: "reviewable").
        tags: Optional tag list; defaults to [tier, class_name].

    Returns:
        The newly created DataClassDef.
    """
    if tags is None:
        tags = [tier, class_name]
    new_def = DataClassDef(name=class_name, tier=tier, tags=tags)
    DATA_CLASS_REGISTRY[class_name] = new_def
    logger.info("Registered new data class: %s (tier=%s)", class_name, tier)
    return new_def


def validate_data_class(data_class: str | None) -> DataClassDef:
    """Validate a data class name against the registry.

    Args:
        data_class: The data class name to validate. Required -- cannot be None.

    Returns:
        The DataClassDef for the given class name.

    Raises:
        ValueError: When data_class is None or not in the registry.
    """
    if data_class is None:
        raise ValueError(
            "data_class is required. Pass a valid data_class from the "
            f"registry: {sorted(DATA_CLASS_REGISTRY.keys())}"
        )

    if data_class not in DATA_CLASS_REGISTRY:
        raise ValueError(
            f"Unknown data_class {data_class!r}. I don't have a class defined "
            f"for this type of data. Should I define one, discard it, or handle "
            f"it differently? Valid classes: {sorted(DATA_CLASS_REGISTRY.keys())}"
        )

    return DATA_CLASS_REGISTRY[data_class]


def validate_source(source: str) -> str:
    """Validate a source string against the allowed set.

    Args:
        source: The source identifier to validate.

    Returns:
        The validated source string.

    Raises:
        ValueError: When source is not in VALID_SOURCES.
    """
    if source not in VALID_SOURCES:
        raise ValueError(
            f"Invalid source {source!r}. Must be one of: {sorted(VALID_SOURCES)}"
        )
    return source


def build_metadata(
    data_class: str | None,
    source: str,
    as_of: str | None = None,
    expires_at: str | None = None,
    recurring: bool | None = None,
    content: str | None = None,
) -> dict:
    """Build a metadata dict for a memory or graph entry.

    Validates data_class and source, then assembles the metadata fields.

    Args:
        data_class: The data class name. Required -- cannot be None.
        source: Origin of the entry ("user", "tool", "session", "self").
        as_of: ISO datetime string; defaults to now if not provided.
        expires_at: ISO datetime string; required for timed-event class.
        recurring: Explicit recurring flag (overrides heuristic detection).
            Only used for timed-event class.
        content: Content text for recurring keyword detection.
            Only used for timed-event class.

    Returns:
        Dict with metadata fields: data_class, tier, as_of, source,
        superseded, and optionally expires_at and recurring.

    Raises:
        ValueError: On invalid/missing data_class, source, or missing/invalid
            expires_at for timed-event.
    """
    validate_source(source)
    cls_def = validate_data_class(data_class)

    if as_of is None:
        as_of = datetime.now(timezone.utc).isoformat()

    meta: dict = {
        "data_class": cls_def.name,
        "tier": cls_def.tier,
        "as_of": as_of,
        "source": source,
        "superseded": False,
    }

    if cls_def.requires_expires and not expires_at:
        raise ValueError(
            f"data_class {data_class!r} requires expires_at to be set "
            f"(an ISO datetime for when the event occurs)."
        )
    if expires_at:
        # Validate the format for timed-event entries
        if cls_def.requires_expires:
            validate_expires_at(expires_at)
        meta["expires_at"] = expires_at

    # Add recurring flag for timed-event class
    if cls_def.requires_expires:
        if recurring is not None:
            meta["recurring"] = recurring
        else:
            meta["recurring"] = detect_recurring(content or "")

    return meta
