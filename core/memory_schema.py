"""Memory data classification schema and validation.

Defines the seven data classes from specs/memory-lifecycle.md, along with
validation functions for data_class, source, and metadata building.

This module is shared between agents/memory.py and agents/knowledge_graph.py
to keep validation logic DRY and avoid circular dependencies.
"""

from __future__ import annotations

import logging
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
    "session-log": DataClassDef(
        "session-log", "reviewable", ["reviewable", "session"], False
    ),
    "timed-event": DataClassDef(
        "timed-event", "reviewable", ["reviewable", "event"], True
    ),
    "person": DataClassDef("person", "durable", ["durable", "person"], False),
    "world-event": DataClassDef(
        "world-event", "reviewable", ["reviewable", "world-event"], False
    ),
    "preference": DataClassDef(
        "preference", "durable", ["durable", "preference"], False
    ),
    "intention": DataClassDef(
        "intention", "reviewable", ["reviewable", "intention"], False
    ),
}

VALID_SOURCES = {"user", "tool", "session", "self"}
VALID_TIERS = {"reviewable", "durable"}


def validate_data_class(data_class: str | None) -> DataClassDef | None:
    """Validate a data class name against the registry.

    Args:
        data_class: The data class name to validate, or None for backward compat.

    Returns:
        The DataClassDef for known classes, or None when data_class is None.

    Raises:
        ValueError: When data_class is a non-None string not in the registry.
    """
    if data_class is None:
        logger.warning(
            "data_class not provided -- this is deprecated and will become "
            "required in a future release. Pass a valid data_class from the "
            "registry: %s",
            sorted(DATA_CLASS_REGISTRY.keys()),
        )
        return None

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
) -> dict:
    """Build a metadata dict for a memory or graph entry.

    Validates data_class and source, then assembles the metadata fields.

    Args:
        data_class: The data class name, or None for backward compat.
        source: Origin of the entry ("user", "tool", "session", "self").
        as_of: ISO datetime string; defaults to now if not provided.
        expires_at: ISO datetime string; required for timed-event class.

    Returns:
        Dict with metadata fields: data_class, tier, as_of, source,
        superseded, and optionally expires_at.

    Raises:
        ValueError: On invalid data_class, source, or missing expires_at
            for timed-event.
    """
    validate_source(source)
    cls_def = validate_data_class(data_class)

    if as_of is None:
        as_of = datetime.now(timezone.utc).isoformat()

    meta: dict = {
        "as_of": as_of,
        "source": source,
        "superseded": False,
    }

    if cls_def is not None:
        meta["data_class"] = cls_def.name
        meta["tier"] = cls_def.tier

        if cls_def.requires_expires and not expires_at:
            raise ValueError(
                f"data_class {data_class!r} requires expires_at to be set "
                f"(an ISO datetime for when the event occurs)."
            )
        if expires_at:
            meta["expires_at"] = expires_at
    else:
        meta["data_class"] = None
        meta["tier"] = None
        if expires_at:
            meta["expires_at"] = expires_at

    return meta
