--
-- Step 11: which person a Telegram or Discord account is (feature 2.1,
-- story 2.1.15).
--
-- A bot answers one claimed account, and until now nothing said whose
-- account that was: every surface bot presents a service credential whose
-- username is the operator's, so a second person's turns were recorded as
-- his. Decided 2026-10-22: identity comes from the sender's account, looked
-- up per surface, and one account is never recorded against two people.
--
-- Keyed by the bare surface name (`telegram`, `discord`) - the same value a
-- claim row carries - and the sender's id as text. The person is the
-- lowercase username, the owner key `core.minds.owner` already uses (decision
-- D1, 2026-10-14). It is not a foreign key to `core.users`: at Daniel's home
-- the people live in auth-service, so `core.users` does not hold them, and a
-- key into it would refuse every real link on that install.
--
-- The primary key is the whole rule: an account recorded against one person
-- cannot be recorded against a second. A link is only ever inserted, never
-- overwritten, so an account changes hands only by an operator deleting it.
--
-- Written to run whether or not `core.sql` already made the table: a fresh
-- database gets it from the baseline, a live one from here, and both then
-- run this step.
--

SELECT pg_catalog.set_config('search_path', '', false);

CREATE TABLE IF NOT EXISTS core.surface_accounts (
    surface text NOT NULL,
    account_ref text NOT NULL,
    username text NOT NULL,
    linked_at double precision NOT NULL,
    CONSTRAINT surface_accounts_pkey PRIMARY KEY (surface, account_ref),
    CONSTRAINT surface_accounts_surface_known CHECK (surface IN ('telegram', 'discord')),
    CONSTRAINT surface_accounts_ref_not_blank CHECK (btrim(account_ref) <> ''),
    CONSTRAINT surface_accounts_username_lower CHECK (username = lower(btrim(username)) AND username <> '')
);

CREATE INDEX IF NOT EXISTS surface_accounts_username ON core.surface_accounts USING btree (username);

--
-- The accounts every bot already answers. Each live claim's account becomes
-- its mind's owner's, but only where every live claim on that account sits on
-- minds of one owner: an account claimed on two people's minds is the
-- conflict this table exists to stop, and guessing which of them it belongs
-- to is how an account changes hands in silence. Such an account is left
-- unlinked, so the gateway refuses it everywhere until an operator decides -
-- loud, rather than wrong.
--
INSERT INTO core.surface_accounts (surface, account_ref, username, linked_at)
SELECT c.surface, c.claimed_ref, min(lower(btrim(m.owner))),
       EXTRACT(epoch FROM now())
FROM core.surface_claims c
JOIN core.minds m ON m.mind_id = c.mind_id
WHERE c.claimed_ref IS NOT NULL
  AND btrim(c.claimed_ref) <> ''
  AND c.surface IN ('telegram', 'discord')
  AND m.owner IS NOT NULL
  AND btrim(m.owner) <> ''
GROUP BY c.surface, c.claimed_ref
HAVING count(DISTINCT lower(btrim(m.owner))) = 1
ON CONFLICT (surface, account_ref) DO NOTHING;
