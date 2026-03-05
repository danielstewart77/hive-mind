"""Technical-config pruning sweep -- verifies stored facts against the codebase.

Queries Neo4j for all data_class=technical-config entries, runs verification
heuristics, marks inaccurate entries as superseded, and sends a Telegram summary.

Called by the scheduler via the /memory/techconfig-sweep gateway endpoint.
"""

from __future__ import annotations

import logging

from core.techconfig_verifier import verify_entry

logger = logging.getLogger(__name__)


def _get_driver():
    """Lazy import to avoid circular dependency and allow mocking."""
    from agents.memory import _get_driver as _mem_get_driver
    return _mem_get_driver()


def _telegram_direct(message: str) -> tuple[bool, str]:
    """Lazy import of the Telegram direct send function."""
    from agents.notify import _telegram_direct as _notify_telegram
    return _notify_telegram(message)


def sweep_techconfig_entries() -> dict:
    """Query Neo4j for technical-config entries and verify against codebase.

    For each entry:
    - verified: no change, count incremented
    - pruned: set superseded=True in Neo4j
    - flagged: no change, collected for review message

    Returns:
        Summary dict with keys: verified, pruned, flagged, errors.
    """
    verified = 0
    pruned = 0
    flagged = 0
    errors = 0
    flagged_entries: list[str] = []

    try:
        driver = _get_driver()
        with driver.session() as session:
            result = session.run(
                """
                MATCH (m:Memory)
                WHERE m.data_class = 'technical-config'
                  AND (m.superseded IS NULL OR m.superseded = false)
                RETURN m.content AS content,
                       m.codebase_ref AS codebase_ref,
                       elementId(m) AS id
                """
            )

            for record in result:
                content = record["content"]
                codebase_ref = record["codebase_ref"]
                element_id = record["id"]

                try:
                    vr = verify_entry(content, element_id, codebase_ref)

                    if vr.status == "verified":
                        verified += 1
                        logger.info(
                            "Verified: %s (ref=%s)",
                            content[:80],
                            codebase_ref,
                        )
                    elif vr.status == "pruned":
                        try:
                            session.run(
                                "MATCH (m) WHERE elementId(m) = $id SET m.superseded = true",
                                id=element_id,
                            )
                            pruned += 1
                            logger.info(
                                "Pruned (superseded): %s (reason=%s)",
                                content[:80],
                                vr.reason,
                            )
                        except Exception:
                            logger.exception(
                                "Failed to mark entry as superseded: %s",
                                element_id,
                            )
                            errors += 1
                    elif vr.status == "flagged":
                        flagged += 1
                        flagged_entries.append(
                            f"- {content[:200]}\n  Reason: {vr.reason}"
                        )
                        logger.info(
                            "Flagged for review: %s (reason=%s)",
                            content[:80],
                            vr.reason,
                        )
                except Exception:
                    logger.exception(
                        "Error verifying entry %s: %s",
                        element_id,
                        content[:80],
                    )
                    errors += 1

    except Exception:
        logger.exception("Technical-config pruning sweep failed")
        errors += 1

    # Send Telegram summary if there were any results
    total = verified + pruned + flagged
    if total > 0:
        summary = (
            f"Technical-config pruning sweep complete:\n\n"
            f"Verified: {verified}\n"
            f"Pruned: {pruned}\n"
            f"Flagged: {flagged}\n"
            f"Errors: {errors}"
        )
        try:
            _telegram_direct(summary)
        except Exception:
            logger.exception("Failed to send techconfig sweep summary via Telegram")

        # Send flagged entries in a separate message
        if flagged_entries:
            flagged_msg = (
                f"Flagged entries for review ({flagged}):\n\n"
                + "\n\n".join(flagged_entries)
            )
            try:
                _telegram_direct(flagged_msg)
            except Exception:
                logger.exception("Failed to send flagged entries via Telegram")

    logger.info(
        "Technical-config pruning sweep complete: verified=%d, pruned=%d, flagged=%d, errors=%d",
        verified,
        pruned,
        flagged,
        errors,
    )
    return {"verified": verified, "pruned": pruned, "flagged": flagged, "errors": errors}
