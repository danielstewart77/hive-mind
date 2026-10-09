# Backlog → kanban detail import

Importer: `import_batch.py` (idempotent by title). Run:
`docker cp <batch>.json hive-site:/tmp/ && docker exec hive-site python3 /tmp/import_batch.py /tmp/<batch>.json`

## Done (44 of 56 source docs)
- batch-01: crm-mvp, terminal-feature-wishlist, unified-logging-standard, mind-install-automation,
  sentinel-project, ollama-skill-agent, hive-mind-consolidation, graph-query-leaks-names-into-urls,
  credential-rotation-console
- batch-02: skippy-rotation-backport, upstream-minds-ns-migration-punchlist, job-queuing-architecture,
  gnome-shell-mitigations, refactor-pass-runner-requirements, upstream-minds-strict-schema-and-onboarding,
  BUG-terminal-pane, gmail-tool-attachment-access, audible-plus-cyberpunk-checklist
- batch-03: dragoman 00-06 (epic + 6 features)
- batch-04: cypher-the-dsh-mind (epic), cypher-hooks-and-skills (feature)
- batch-05: hive-settings-page, agent-native-memory-review, orchestration-info-surface,
  multi-user-authorization, consolidated-surface-bots
- batch-06: switchboard, app-monitoring-agent, bitcoin-buy-signal-alerter, agentic-session-rotation,
  hive-situational-awareness-claims, arnold-spoke-outstanding
- batch-07: mind-registration-service, tui-mind
- batch-08: iterative-graph-memory-reconstruction, hive-health-walkthrough-requirements
- batch-09: hermes-agent-backlog, agent-document-management-system
- batch-10: hive-setup-wizard, soul-write-authority
- batch-11: model-viewer
- batch-12: skills-enhancement
- batch-13: mind-to-mind-communication
- batch-14: rebuild-runner-requirements
- batch-15: deepseek-harness-as-a-hive-harness (feature under the Cypher epic)
- batch-16: harness-training-loop
- batch-17: mini-boss
- batch-18: harness-finetuning-pipeline
- batch-19: dependency-counterparty-risk-scanner

## Remaining
None. All 56 source docs are detailed (skippy-mind-id-partition-repair's epic was deleted on request).

## Result
48 epics, 204 features, 639 stories. 46 stories marked `done` where the source doc records the work
as complete. Integrity clean, no orphans, no duplicate positions.

## Notes
- Daniel's own rows are left alone and carry no generated detail: epic "Unsorted", epic
  "Hive Terminal Updates" + feature "Telegramification", features "DSH Terminal Improvments" /
  "Harness Improvments" under Cypher, and 5 of his stories.
- Statuses: stories the source doc marks complete are set `done`; everything else `backlog`.
- `import_batch.py` is idempotent by title — re-running a batch updates rather than duplicates.
  `hive-site` restarting clears /tmp, so re-copy the importer before a later run.
