--
-- Step 20: one record of keys (feature 834, story 906).
--
-- The gateway, the memory store and the tools used to keep a mind's key in
-- three tables. They now recognise a key from `core.credentials`. A mind that
-- has not yet been moved to its one key (items 841 and 842) still presents
-- the memory-store and tools keys it has, so those two tables are KEPT, as
-- they are, and read as a fallback for exactly those minds until each is
-- moved (the move cuts its old rows off) or the old keys are retired (which
-- deletes the rows). Nothing is emptied here.
--
-- Any live mind row the old tables hold that the record lacks is copied first
-- (never replacing a row, never bringing back a key that was cut off). Run
-- twice, the second run copies nothing.
--
-- The approver's row in `tools.api_tokens` is removed by step 21, with the
-- approver credential.
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
   AND lower(t.name) NOT IN ('hitl-approver', 'internal', 'skippy')
 ORDER BY lower(t.name), t.created_at DESC
ON CONFLICT DO NOTHING;
