"""Backfill classification engine for memory entries and entity nodes.

Pure-logic module with keyword/tag heuristics that map content and tags
to the 7 data classes defined in core/memory_schema.py. Returns a
ClassificationResult with class name and confidence score.

This module has zero external dependencies (no Neo4j, no Telegram)
and is fully unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.memory_schema import DATA_CLASS_REGISTRY

# High-confidence threshold -- below this, entries go to human review
HIGH_CONFIDENCE_THRESHOLD = 0.7


@dataclass
class ClassificationResult:
    """Result of classifying a memory entry or entity node."""

    data_class: str | None  # The assigned class name, or None if unclassifiable
    confidence: float  # 0.0 to 1.0
    reason: str  # Human-readable explanation of the classification
    candidates: list[str] = field(default_factory=list)  # Top candidate classes


# Keyword lists per data class, used for content-based heuristics
DATA_CLASS_KEYWORDS: dict[str, list[str]] = {
    "person": [
        "person", "people", "family", "wife", "husband", "friend",
        "colleague", "brother", "sister", "mother", "father",
        "named", "contacted", "born in", "married",
    ],
    "preference": [
        "prefers", "likes", "favorite", "favourite", "preference",
        "rather", "always uses", "default choice", "habit",
        "dislikes", "avoids", "chosen",
    ],
    "technical-config": [
        "configured", "configuration", "endpoint", "port", "server",
        "docker", "container", "mount", "volume", "database",
        "schema", "api", "deploy", "architecture", "codebase",
        "implemented", "refactored", "module", "package",
    ],
    "session-log": [
        "session", "conversation", "discussed", "recovered",
        "epilogue", "transcript", "morning briefing", "nightly run",
        "afternoon", "evening",
    ],
    "timed-event": [
        "scheduled", "appointment", "meeting", "deadline",
        "delivery", "game", "at \\d{1,2}:\\d{2}",
        "\\d{4}-\\d{2}-\\d{2}", "tomorrow", "next week",
        "on monday", "on tuesday", "on wednesday", "on thursday",
        "on friday", "on saturday", "on sunday",
    ],
    "world-event": [
        "news", "shooting", "earthquake", "election",
        "geopolitical", "attack", "incident", "outbreak",
        "announcement", "launched", "press release",
        "mass shooting", "war", "treaty", "sanctions",
    ],
    "intention": [
        "plan to", "plans to", "want to", "wants to", "goal",
        "intend", "intention", "going to", "backlog", "roadmap",
        "todo", "to-do", "aspire", "aim to", "target",
    ],
}

# Tag-to-class direct mapping (highest signal)
TAG_CLASS_MAP: dict[str, str] = {
    "person": "person",
    "durable": "person",  # durable alone is less specific, but person is most common durable
    "preference": "preference",
    "technical": "technical-config",
    "session": "session-log",
    "epilogue": "session-log",
    "event": "timed-event",
    "world-event": "world-event",
    "intention": "intention",
    "reviewable": "",  # too generic alone
}

# Entity type to data class mapping
ENTITY_TYPE_CLASS_MAP: dict[str, str] = {
    "Person": "person",
    "Preference": "preference",
    "Project": "technical-config",
    "System": "technical-config",
    "Concept": "session-log",
}


def classify_entry(
    content: str,
    tags: str,
    entity_type: str | None = None,
) -> ClassificationResult:
    """Classify a memory entry based on tags and content keywords.

    Uses tag-matching first (highest signal), then keyword heuristics
    on content, then entity_type fallback.

    Args:
        content: The text content of the memory entry.
        tags: Comma-separated tag string.
        entity_type: Optional entity type for entity nodes.

    Returns:
        ClassificationResult with class name, confidence, reason, and candidates.
    """
    content = content.strip() if content else ""
    tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()] if tags else []

    # Empty content gets low confidence
    if not content and not tag_list:
        return ClassificationResult(
            data_class=None,
            confidence=0.1,
            reason="Empty content and no tags",
            candidates=list(DATA_CLASS_REGISTRY.keys()),
        )

    # Phase 1: Tag-based classification (highest signal)
    tag_scores: dict[str, float] = {}
    for tag in tag_list:
        mapped_class = TAG_CLASS_MAP.get(tag, "")
        if mapped_class:
            tag_scores[mapped_class] = tag_scores.get(mapped_class, 0) + 0.5

    # Special tag combos
    if "durable" in tag_list and "person" in tag_list:
        tag_scores["person"] = max(tag_scores.get("person", 0), 0.9)
    if "durable" in tag_list and "preference" in tag_list:
        tag_scores["preference"] = max(tag_scores.get("preference", 0), 0.9)
    if "reviewable" in tag_list and "technical" in tag_list:
        tag_scores["technical-config"] = max(tag_scores.get("technical-config", 0), 0.9)
    if "session" in tag_list:
        tag_scores["session-log"] = max(tag_scores.get("session-log", 0), 0.8)
    if "epilogue" in tag_list:
        tag_scores["session-log"] = max(tag_scores.get("session-log", 0), 0.8)

    # Phase 2: Content keyword scoring
    content_lower = content.lower()
    keyword_scores: dict[str, float] = {}
    for cls_name, keywords in DATA_CLASS_KEYWORDS.items():
        matches = 0
        for keyword in keywords:
            if "\\" in keyword:
                # Regex pattern
                if re.search(keyword, content_lower):
                    matches += 1
            else:
                if keyword in content_lower:
                    matches += 1
        if matches > 0:
            # Scale: 1 match = 0.45, 2 = 0.7, 3+ = 0.8, 4+ = 0.85
            score = min(0.45 + (matches - 1) * 0.25, 0.85)
            keyword_scores[cls_name] = score

    # Phase 3: Combine scores
    combined: dict[str, float] = {}
    all_classes = set(list(tag_scores.keys()) + list(keyword_scores.keys()))
    for cls in all_classes:
        t_score = tag_scores.get(cls, 0.0)
        k_score = keyword_scores.get(cls, 0.0)
        # Tags dominate; keywords add a boost
        combined[cls] = min(t_score + k_score * 0.5, 1.0)

    # If no tag score, use keyword scores directly
    if not tag_scores:
        combined = keyword_scores

    # Phase 4: Entity type fallback
    if entity_type and entity_type in ENTITY_TYPE_CLASS_MAP:
        et_class = ENTITY_TYPE_CLASS_MAP[entity_type]
        combined[et_class] = max(combined.get(et_class, 0.0), 0.6)

    if not combined:
        return ClassificationResult(
            data_class=None,
            confidence=0.1,
            reason="No matching patterns found in content or tags",
            candidates=list(DATA_CLASS_REGISTRY.keys()),
        )

    # Sort by score descending
    sorted_classes = sorted(combined.items(), key=lambda x: x[1], reverse=True)
    best_class, best_score = sorted_classes[0]
    candidates = [cls for cls, _ in sorted_classes[:3]]

    reason_parts = []
    if best_class in tag_scores:
        reason_parts.append(f"tag match: {[t for t in tag_list if TAG_CLASS_MAP.get(t) == best_class]}")
    if best_class in keyword_scores:
        reason_parts.append(f"keyword match in content")
    reason = "; ".join(reason_parts) if reason_parts else "combined heuristic"

    return ClassificationResult(
        data_class=best_class,
        confidence=best_score,
        reason=reason,
        candidates=candidates,
    )


def classify_entity_node(
    name: str,
    entity_type: str,
    properties: dict,
) -> ClassificationResult:
    """Classify a knowledge graph entity node based on its type.

    Maps entity types directly to data classes:
    - Person -> person
    - Preference -> preference
    - Project -> technical-config
    - System -> technical-config
    - Concept -> session-log (catch-all)

    Args:
        name: The entity name.
        entity_type: The Neo4j label (Person, Project, etc.).
        properties: Additional node properties.

    Returns:
        ClassificationResult with the mapped class.
    """
    if entity_type in ENTITY_TYPE_CLASS_MAP:
        data_class = ENTITY_TYPE_CLASS_MAP[entity_type]
        confidence = 0.85 if entity_type != "Concept" else 0.55
        return ClassificationResult(
            data_class=data_class,
            confidence=confidence,
            reason=f"Entity type {entity_type!r} maps to {data_class!r}",
            candidates=[data_class],
        )

    # Unknown entity type -- try to classify from properties
    context = properties.get("context", "")
    if context:
        return classify_entry(content=context, tags="", entity_type=entity_type)

    return ClassificationResult(
        data_class=None,
        confidence=0.2,
        reason=f"Unknown entity type {entity_type!r} with no classifiable properties",
        candidates=list(DATA_CLASS_REGISTRY.keys()),
    )
