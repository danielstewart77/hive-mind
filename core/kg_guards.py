"""Knowledge graph write guards -- disambiguation and orphan node protection.

Provides two guards for graph_upsert:
1. Disambiguation: query-first check for duplicate/similar nodes before writing.
2. Orphan guard: reject writes without at least one edge (with grace period option).

Also includes Telegram notification for disambiguation messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DisambiguationResult:
    """Result of a disambiguation check against the knowledge graph."""

    action: str  # "proceed", "merge", "disambiguate"
    existing_nodes: list[dict] = field(default_factory=list)
    message: str = ""


def _get_driver():
    """Lazy import to avoid circular dependency and allow mocking."""
    from agents.knowledge_graph import _get_driver as _kg_get_driver

    return _kg_get_driver()


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Lazy import of the Telegram direct send function."""
    from agents.notify import _telegram_direct as _notify_telegram

    return _notify_telegram(message)


def check_disambiguation(
    name: str, entity_type: str, agent_id: str
) -> DisambiguationResult:
    """Query the knowledge graph for similar nodes before writing.

    Uses a Cypher CONTAINS query (case-insensitive) to find similar nodes.

    Args:
        name: Proposed entity name.
        entity_type: Node label (e.g. "Person", "Project"). Accepted for API
            consistency and future use, but intentionally not used as a Cypher
            filter -- the CONTAINS name match is cross-type so that duplicates
            across entity types are caught (e.g. a Person and a System sharing
            a name).
        agent_id: Which agent's graph to search.

    Returns:
        DisambiguationResult with action, existing_nodes, and message.
    """
    # NOTE: entity_type is intentionally omitted from the Cypher WHERE clause.
    # Disambiguation must check across all node types to catch cross-entity
    # duplicates (e.g. "Hive Mind" as both a Project and a System). The
    # parameter is retained in the signature for API consistency with
    # graph_upsert and for potential future label-scoped queries.
    driver = _get_driver()
    with driver.session() as session:
        result = session.run(
            """
            MATCH (n)
            WHERE n.agent_id = $agent_id
              AND (toLower(n.name) = toLower($name)
                   OR toLower(n.name) CONTAINS toLower($name)
                   OR toLower($name) CONTAINS toLower(n.name))
            RETURN n.name AS name, labels(n) AS labels, elementId(n) AS id
            """,
            name=name,
            agent_id=agent_id,
        )
        rows = result.data()

    if not rows:
        return DisambiguationResult(
            action="proceed",
            existing_nodes=[],
            message=f"No existing nodes match '{name}'. Proceeding with write.",
        )

    # Check for exact match (case-insensitive)
    exact_matches = [r for r in rows if r["name"].lower() == name.lower()]
    if exact_matches:
        return DisambiguationResult(
            action="merge",
            existing_nodes=rows,
            message=(
                f"Exact match found for '{name}': "
                f"{', '.join(r['name'] for r in exact_matches)}. "
                f"Will merge/update existing node."
            ),
        )

    # Similar but not exact -- disambiguation required
    existing_names = ", ".join(r["name"] for r in rows)
    return DisambiguationResult(
        action="disambiguate",
        existing_nodes=rows,
        message=(
            f"Similar nodes found for '{name}': {existing_names}. "
            f"Disambiguation required before writing."
        ),
    )


def check_orphan_guard(
    relation: str, target_name: str, grace_period: bool = False
) -> tuple[bool, str]:
    """Check whether a graph write has at least one edge.

    Args:
        relation: Relationship type (e.g. "MANAGES").
        target_name: Name of the target node.
        grace_period: If True, allow orphan writes (for epilogue use).

    Returns:
        Tuple of (allowed, error_message). If allowed, error_message is "".
    """
    if grace_period:
        return True, ""

    if relation and target_name:
        return True, ""

    return False, (
        "Cannot create a node without at least one edge. "
        "Provide a relation and target, or defer until the relationship is known."
    )


def send_disambiguation_message(
    proposed_name: str, existing_nodes: list[dict]
) -> bool:
    """Send a disambiguation message via Telegram (non-blocking, not HITL).

    Args:
        proposed_name: The proposed node name.
        existing_nodes: List of dicts with name/labels of matching nodes.

    Returns:
        True if Telegram send succeeded, False otherwise.
    """
    existing_list = "\n".join(
        f"  - {node['name']} ({', '.join(node.get('labels', []))})"
        for node in existing_nodes
    )
    message = (
        f"I'm about to add [{proposed_name}] to the graph. "
        f"Found similar nodes:\n{existing_list}\n\n"
        f"Is this the same entity? (yes = merge, no = create new, skip = defer)"
    )

    try:
        success, _detail = _telegram_direct(message)
        return success
    except Exception:
        logger.exception(
            "Failed to send disambiguation message for '%s'", proposed_name
        )
        return False
