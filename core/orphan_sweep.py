"""Orphan node sweep -- finds stale orphan nodes in the knowledge graph.

Identifies entity nodes with zero edges that are older than the grace period
(30 minutes), logs them, and sends a batch Telegram notification for review.
Does NOT auto-delete any nodes.

Called by the scheduler via the /memory/orphan-sweep gateway endpoint.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Grace period in seconds (30 minutes)
GRACE_PERIOD_SECONDS = 1800


def _get_driver():
    """Lazy import to avoid circular dependency and allow mocking."""
    from agents.knowledge_graph import _get_driver as _kg_get_driver

    return _kg_get_driver()


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Lazy import of the Telegram direct send function."""
    from agents.notify import _telegram_direct as _notify_telegram

    return _notify_telegram(message)


def sweep_orphan_nodes(agent_id: str = "ada") -> dict:
    """Query Neo4j for orphan entity nodes and notify via Telegram.

    Finds all entity nodes that have zero relationships and were created
    more than 30 minutes ago (past grace period).

    Does NOT delete any nodes -- only logs and notifies.

    Args:
        agent_id: Which agent's graph to sweep (default "ada").

    Returns:
        Summary dict with keys: orphans_found, notified, errors.
    """
    orphans_found = 0
    notified = False
    errors = 0

    try:
        cutoff = time.time() - GRACE_PERIOD_SECONDS
        driver = _get_driver()

        with driver.session() as session:
            result = session.run(
                """
                MATCH (n)
                WHERE n.agent_id = $agent_id
                  AND NOT (n)--()
                  AND n.created_at < $cutoff
                RETURN n.name AS name,
                       labels(n) AS labels,
                       n.created_at AS created_at,
                       elementId(n) AS id
                """,
                agent_id=agent_id,
                cutoff=cutoff,
            )

            orphan_list = []
            for record in result:
                orphan_list.append({
                    "name": record["name"],
                    "labels": record["labels"],
                    "created_at": record["created_at"],
                    "id": record["id"],
                })

        orphans_found = len(orphan_list)

        if orphan_list:
            for orphan in orphan_list:
                logger.info(
                    "Orphan node found: %s (%s), created_at=%s",
                    orphan["name"],
                    ", ".join(orphan["labels"]),
                    orphan["created_at"],
                )

            # Send batch Telegram message
            node_list = "\n".join(
                f"  - {o['name']} ({', '.join(o['labels'])})"
                for o in orphan_list
            )
            message = (
                f"Orphan nodes found (no edges, older than 30 min):\n"
                f"{node_list}\n\n"
                f"Review and connect or remove manually."
            )
            try:
                success, _detail = _telegram_direct(message)
                notified = success
            except Exception:
                logger.exception("Failed to send orphan sweep Telegram notification")
                errors += 1

    except Exception:
        logger.exception("Orphan node sweep failed")
        errors += 1

    logger.info(
        "Orphan sweep complete: orphans_found=%d, notified=%s, errors=%d",
        orphans_found,
        notified,
        errors,
    )
    return {"orphans_found": orphans_found, "notified": notified, "errors": errors}
