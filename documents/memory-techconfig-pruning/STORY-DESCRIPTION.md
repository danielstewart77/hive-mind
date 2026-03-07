# [Memory] Technical-Config Pruning — Code Verification Pass

**Card ID:** 1723686012946744860

## Description

The most complex pruning pass. For every `technical-config` memory entry, verify whether the stored fact is still accurate by reading the codebase. Keep if still true, prune if contradicted or no longer applicable.

Depends on: Schema & Metadata story.

## Acceptance Criteria

1. **Pruning job implementation**
   - Runs on a schedule (nightly or weekly — TBD)
   - Queries all entries with `data_class=technical-config` from memory store
   - For each entry, evaluates whether the stored fact is still accurate

2. **Verification logic with codebase_ref**
   - Each `technical-config` entry can reference a codebase location (optional `codebase_ref` field)
   - If `codebase_ref` is present: read that file directly and verify the fact
   - If not present: use the content to infer which files to check (fuzzy match against file structure)
   - Decision tree implemented:
     - Still accurate → no change, log as verified
     - Inaccurate or no longer applicable → prune; optionally store corrected replacement entry
     - Cannot determine → flag for Daniel review

3. **Reporting mechanism**
   - After each pass, send Daniel a Telegram summary with:
     - Count of verified entries
     - Count of pruned entries
     - Count of flagged entries for review
   - Flagged entries batched into a single review message

4. **Open question resolved (before implementation)**
   - Decision: start with a lightweight heuristic (file/symbol existence check), escalate to Claude only for entries that are borderline

5. **Test coverage**
   - Accurate entry is kept
   - Inaccurate entry is pruned
   - Entry with no `codebase_ref` is handled gracefully
   - Summary report is sent after each pass

## Tasks

- [ ] Resolve whether pruning job runs nightly or weekly
- [ ] Design and implement `codebase_ref` field schema
- [ ] Implement lightweight heuristic verification (file/symbol existence)
- [ ] Implement escalation logic for borderline cases
- [ ] Implement Telegram reporting mechanism
- [ ] Add comprehensive test coverage for all scenarios
- [ ] Integrate scheduling (cron or background job)
