# Epilogue Exception Triggers

Phase 3 of the session epilogue system removes all threshold-based HITL routing. Every session auto-writes by default. This spec defines the exception triggers -- specific edge-case conditions that still warrant human review after auto-write has completed.

## Exception Triggers

### 1. `high_novel_entities`

**Condition:** `novel_entity_count > 10`

**Rationale:** A single session producing more than 10 novel entities may indicate data quality issues, hallucinated entities, or a conversation that wandered into speculative territory. The writes still happen (auto-write), but the operator is notified so they can review what was written.

**HITL message:** "High novel entity count ({count}) in session {session_id}. Review written entities for accuracy."

### 2. `high_error_rate`

**Condition:** `write_errors / total_writes > 0.5` (more than 50% of write operations failed), only when `total_writes > 0`

**Rationale:** A high failure rate during auto-write suggests a systemic problem (Neo4j down, schema mismatch, corrupted data). The operator needs to know so they can investigate and potentially re-run the epilogue.

**HITL message:** "High error rate ({errors}/{total} writes failed) in session {session_id}. Investigate write failures."

### 3. `conflicting_entity`

**Condition:** The digest contains an entity whose name matches an existing graph node with a different entity type.

**Rationale:** Writing an entity with a mismatched type (e.g., "Hive Mind" as Person when it already exists as Project) could corrupt the knowledge graph. This is reserved for future implementation when entity extraction populates the digest.

**Note:** This trigger is defined but not yet implemented in the `check_exceptions()` function. It will be added when entity extraction is connected to the epilogue pipeline.

## Behavior

- All sessions auto-write via `auto_write_digest()` first.
- After auto-write, `check_exceptions()` inspects the digest and write results.
- If exceptions are found: a WARNING log is emitted and an informational HITL notification is sent (non-blocking, fire-and-forget). The writes are NOT rolled back.
- If no exceptions: processing completes silently.

## Non-exception sessions

Any session that does not trigger an exception condition completes without human notification. There is no threshold-based routing. The only remaining HITL path is the `/remember` force-trigger, which is a separate flow.
