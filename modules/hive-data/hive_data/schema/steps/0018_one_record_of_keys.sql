--
-- Step 18: one record of keys (feature 834, story 906).
--
-- The gateway, the memory store and the tools used to keep a mind's key in
-- three tables. They now all recognise a key from `core.credentials`, so
-- `memory.credentials` and `tools.api_tokens` are emptied. The digests of live
-- keys are in `core.credentials` already; any live mind row the old tables
-- hold that the record lacks is copied first (never replacing a row, never
-- bringing back a key that was cut off). Run twice, the second run copies and
-- deletes nothing.
--
-- The tables stay (the previous release still names them; nothing is dropped
-- or truncated). The approver's row in `tools.api_tokens` stays: approvals
-- move to the hive site in another story and that credential goes with it.
--
SELECT pg_catalog.set_config('search_path', '', false);

INSERT INTO core.credentials (holder, username, role, token_sha, created_at)
SELECT m.holder, m.username, 'mind', m.token_sha, m.created_at
  FROM memory.credentials m
 WHERE m.role = 'mind' AND m.revoked_at IS NULL
   AND EXISTS (SELECT 1 FROM core.minds c
                WHERE lower(c.name) = lower(m.holder) OR c.mind_id = m.holder)
ON CONFLICT DO NOTHING;

INSERT INTO core.credentials (holder, username, role, token_sha, created_at)
SELECT DISTINCT ON (lower(t.name)) lower(t.name), lower(c.owner), 'mind',
       t.token_hash, t.created_at::double precision
  FROM tools.api_tokens t
  JOIN core.minds c ON lower(c.name) = lower(t.name)
 WHERE t.revoked_at IS NULL AND c.owner IS NOT NULL AND c.owner <> ''
 ORDER BY lower(t.name), t.created_at DESC
ON CONFLICT DO NOTHING;

DELETE FROM memory.credentials;

DELETE FROM tools.api_tokens WHERE lower(name) <> 'hitl-approver';
