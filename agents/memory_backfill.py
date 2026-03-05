"""Memory backfill tool -- one-time scan and classification of unclassified entries.

Scans Neo4j for Memory nodes and entity nodes lacking a data_class field,
classifies them using heuristics, auto-assigns high-confidence matches,
and queues ambiguous entries for Daniel's review via Telegram.

MCP tools:
  - memory_backfill: Run the full backfill scan/classify/assign flow
  - memory_backfill_status: Check how many entries remain unclassified
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from agent_tooling import tool
from agents.secret_manager import get_credential
from core.backfill_classifier import (
    HIGH_CONFIDENCE_THRESHOLD,
    ClassificationResult,
    classify_entity_node,
    classify_entry,
)
from core.backfill_review import format_review_batch
from core.memory_schema import DATA_CLASS_REGISTRY
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

# --- Neo4j connection (reuse pattern from agents/memory.py) ---

NEO4J_URI = get_credential("NEO4J_URI") or "bolt://neo4j:7687"
NEO4J_AUTH_ENV = get_credential("NEO4J_AUTH") or "neo4j/hivemind-memory"
_NEO4J_USER, _, _NEO4J_PASS = NEO4J_AUTH_ENV.partition("/")

_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(_NEO4J_USER, _NEO4J_PASS))
    return _driver


# --- Data classes ---

@dataclass
class BackfillEntry:
    """A single entry needing classification."""

    element_id: str  # Neo4j element ID
    content: str  # Memory content or entity name
    tags: str  # Existing tags
    created_at: int | None  # Unix timestamp if available
    source: str  # Existing source field
    node_type: str  # "memory" or "entity"
    entity_type: str | None  # For entities: Person, Project, etc.


# --- Scanning ---

_ENTITY_LABELS = ("Person", "Project", "System", "Concept", "Preference")


def _scan_unclassified_memories(driver) -> list[BackfillEntry]:
    """Query Memory nodes where data_class IS NULL."""
    try:
        with driver.session() as session:
            result = session.run(
                """
                MATCH (m:Memory)
                WHERE m.data_class IS NULL
                RETURN elementId(m) AS id,
                       m.content AS content,
                       m.tags AS tags,
                       m.created_at AS created_at,
                       m.source AS source
                """
            )
            entries = []
            for record in result:
                entries.append(BackfillEntry(
                    element_id=record["id"],
                    content=record["content"] or "",
                    tags=record["tags"] or "",
                    created_at=record["created_at"],
                    source=record["source"] or "user",
                    node_type="memory",
                    entity_type=None,
                ))
            return entries
    except Exception:
        logger.exception("Failed to scan unclassified memories")
        return []


def _scan_unclassified_entities(driver) -> list[BackfillEntry]:
    """Query entity nodes (Person, Project, etc.) where data_class IS NULL."""
    try:
        with driver.session() as session:
            # Query across all entity labels
            query = """
                MATCH (n)
                WHERE (n:Person OR n:Project OR n:System OR n:Concept OR n:Preference)
                  AND n.data_class IS NULL
                RETURN elementId(n) AS id,
                       n.name AS name,
                       labels(n) AS labels,
                       properties(n) AS properties
                """
            result = session.run(query)
            entries = []
            for record in result:
                labels = record["labels"]
                entity_type = next(
                    (l for l in labels if l in _ENTITY_LABELS), "Concept"
                )
                entries.append(BackfillEntry(
                    element_id=record["id"],
                    content=record["name"] or "",
                    tags="",
                    created_at=None,
                    source="user",
                    node_type="entity",
                    entity_type=entity_type,
                ))
            return entries
    except Exception:
        logger.exception("Failed to scan unclassified entities")
        return []


# --- Assignment ---

def _assign_classification(
    driver,
    entry: BackfillEntry,
    result: ClassificationResult,
) -> bool:
    """Apply a high-confidence classification to a Neo4j node.

    Returns True if assigned, False if skipped (low confidence or error).
    """
    if result.confidence < HIGH_CONFIDENCE_THRESHOLD:
        return False

    if not result.data_class or result.data_class not in DATA_CLASS_REGISTRY:
        return False

    cls_def = DATA_CLASS_REGISTRY[result.data_class]

    # Compute as_of from created_at or current time
    if entry.created_at:
        as_of = datetime.fromtimestamp(entry.created_at, tz=timezone.utc).isoformat()
    else:
        as_of = datetime.now(timezone.utc).isoformat()

    try:
        with driver.session() as session:
            session.run(
                """
                MATCH (n)
                WHERE elementId(n) = $id
                SET n.data_class = $data_class,
                    n.tier = $tier,
                    n.as_of = $as_of
                """,
                id=entry.element_id,
                data_class=cls_def.name,
                tier=cls_def.tier,
                as_of=as_of,
            )
        return True
    except Exception:
        logger.exception("Failed to assign classification to %s", entry.element_id)
        return False


def _auto_assign_batch(
    driver,
    entries: list[BackfillEntry],
) -> tuple[dict[str, int], list[tuple[BackfillEntry, ClassificationResult]]]:
    """Classify and auto-assign a batch of entries.

    Returns (counts_dict, low_confidence_entries).
    """
    counts = {"assigned": 0, "skipped": 0, "errors": 0}
    low_confidence: list[tuple[BackfillEntry, ClassificationResult]] = []

    for entry in entries:
        if entry.node_type == "entity" and entry.entity_type:
            result = classify_entity_node(
                name=entry.content,
                entity_type=entry.entity_type,
                properties={},
            )
        else:
            result = classify_entry(
                content=entry.content,
                tags=entry.tags,
                entity_type=entry.entity_type,
            )

        if result.confidence >= HIGH_CONFIDENCE_THRESHOLD:
            success = _assign_classification(driver, entry, result)
            if success:
                counts["assigned"] += 1
            else:
                counts["errors"] += 1
        else:
            counts["skipped"] += 1
            low_confidence.append((entry, result))

    return counts, low_confidence


# --- Telegram review ---

def _send_review_batches(
    entries: list[tuple[BackfillEntry, ClassificationResult]],
) -> None:
    """Send review messages to Daniel via Telegram."""
    import httpx

    messages = format_review_batch(entries)
    token = get_credential("TELEGRAM_BOT_TOKEN")
    chat_id = get_credential("TELEGRAM_OWNER_CHAT_ID")

    if not token or not chat_id:
        logger.warning("Cannot send review batches -- Telegram credentials not configured")
        return

    for message in messages:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10,
            )
        except Exception:
            logger.exception("Failed to send review batch message")


# --- Apply classification from user response ---

def apply_classification(
    element_id: str,
    data_class: str,
    source: str = "user",
) -> str:
    """Apply a user-specified classification to a Neo4j node.

    Called from the Telegram /classify_* command handler.

    Returns a confirmation or error message.
    """
    # Handle new class registration
    if data_class.startswith("new:"):
        new_class_name = data_class[4:]
        from core.memory_schema import register_new_class
        register_new_class(new_class_name)
        data_class = new_class_name

    if data_class not in DATA_CLASS_REGISTRY:
        return f"Error: Unknown data class '{data_class}'. Valid: {sorted(DATA_CLASS_REGISTRY.keys())}"

    cls_def = DATA_CLASS_REGISTRY[data_class]
    as_of = datetime.now(timezone.utc).isoformat()

    try:
        driver = _get_driver()
        with driver.session() as session:
            session.run(
                """
                MATCH (n)
                WHERE elementId(n) = $id
                SET n.data_class = $data_class,
                    n.tier = $tier,
                    n.as_of = $as_of
                """,
                id=element_id,
                data_class=cls_def.name,
                tier=cls_def.tier,
                as_of=as_of,
            )
        return f"Classified {element_id} as {data_class} (tier: {cls_def.tier})"
    except Exception as e:
        logger.exception("Failed to apply classification")
        return f"Error applying classification: {e}"


# --- MCP Tools ---

@tool(tags=["memory"])
def memory_backfill() -> str:
    """Scan all Neo4j entries missing data_class and classify them.

    High-confidence matches are auto-assigned. Low-confidence entries
    are sent to Daniel via Telegram for review.

    Returns:
        JSON summary with total_scanned, auto_assigned, needs_review, errors.
    """
    try:
        driver = _get_driver()
    except Exception as e:
        return json.dumps({"error": f"Cannot connect to Neo4j: {e}"})

    # Scan
    mem_entries = _scan_unclassified_memories(driver)
    entity_entries = _scan_unclassified_entities(driver)
    all_entries = mem_entries + entity_entries

    if not all_entries:
        return json.dumps({
            "total_scanned": 0,
            "auto_assigned": 0,
            "needs_review": 0,
            "errors": 0,
            "message": "All entries already classified.",
        })

    # Classify and auto-assign
    counts, low_confidence = _auto_assign_batch(driver, all_entries)

    # Send low-confidence entries for review
    if low_confidence:
        _send_review_batches(low_confidence)

    return json.dumps({
        "total_scanned": len(all_entries),
        "auto_assigned": counts["assigned"],
        "needs_review": len(low_confidence),
        "errors": counts["errors"],
        "memory_entries": len(mem_entries),
        "entity_entries": len(entity_entries),
    })


@tool(tags=["memory"])
def memory_backfill_status() -> str:
    """Check the classification status of all Neo4j entries.

    Returns:
        JSON with complete flag, unclassified count, and class distribution.
    """
    try:
        driver = _get_driver()
        with driver.session() as session:
            # Count Memory nodes by data_class
            result = session.run(
                """
                MATCH (m:Memory)
                RETURN m.data_class AS class, count(*) AS count
                """
            )
            distribution: dict[str, int] = {}
            unclassified = 0
            for record in result:
                cls = record["class"]
                count = record["count"]
                if cls is None:
                    unclassified += count
                else:
                    distribution[cls] = count

            # Also count entity nodes by data_class
            entity_result = session.run(
                """
                MATCH (n)
                WHERE (n:Person OR n:Project OR n:System OR n:Concept OR n:Preference)
                RETURN n.data_class AS class, count(*) AS count
                """
            )
            entity_distribution: dict[str, int] = {}
            entity_unclassified = 0
            for record in entity_result:
                cls = record["class"]
                count = record["count"]
                if cls is None:
                    entity_unclassified += count
                else:
                    entity_distribution[cls] = count

        total_unclassified = unclassified + entity_unclassified
        return json.dumps({
            "complete": total_unclassified == 0,
            "unclassified": total_unclassified,
            "total": sum(distribution.values()) + unclassified + sum(entity_distribution.values()) + entity_unclassified,
            "distribution": distribution,
            "entity_distribution": entity_distribution,
            "memory_unclassified": unclassified,
            "entity_unclassified": entity_unclassified,
        })
    except Exception as e:
        return json.dumps({"error": f"Cannot query Neo4j: {e}"})
