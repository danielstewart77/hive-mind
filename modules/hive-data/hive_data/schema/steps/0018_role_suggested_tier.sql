--
-- Step 18: a role suggests a model tier and names no model (feature 893).
--
-- Daniel, 2026-11-26: "it was never my intention to enforce a model for a
-- role, it was simply a suggestion". A mind set to one model had its
-- conversations opened on the model its role named. The gateway stopped
-- consulting roles about a model the same day; this step takes the model off
-- the role and puts a suggestion in its place.
--
-- The suggestion is one of four tiers, named by the words the Chief of Staff
-- workflow already uses (low, medium, high, extra high; the last stored with
-- a hyphen), or nothing. It is advice shown where a mind's model is chosen
-- and decides nothing: no model carries a tier, and nothing reads this
-- column to pick, move or refuse a model.
--
-- A role that named a model is given a tier once, here, from the family of
-- the model it named, so the hint a person put on the role is not thrown
-- away with the column. Only the Claude families are known; a role naming
-- any other model, or none, starts with no suggestion and is set on the
-- roles page. The shipped roles named no model, so on most hives this
-- fills nothing.
--
-- The `model` column stays where it is, unread and unwritten by this
-- hive-data. Every service opens its pool by checking that each column its
-- own copy declares is there, so dropping a column an older copy still
-- declares would stop that service at its next restart: the console, the
-- tools and lucent each bake their copy in at build. Left in place, an older
-- copy keeps working until it is rebuilt, and a role it writes a model on
-- changes nothing, since nothing enforces one. The column is dropped by a
-- later step, once no copy that declares it is left running anywhere.
--
-- IF EXISTS and IF NOT EXISTS throughout, so a second run is harmless; the
-- fill only touches a role that has no suggestion yet.
--

SELECT pg_catalog.set_config('search_path', '', false);
SET LOCAL lock_timeout = '10s';

ALTER TABLE board.roles ADD COLUMN IF NOT EXISTS suggested_tier text;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'board' AND table_name = 'roles'
                 AND column_name = 'model') THEN
        UPDATE board.roles SET suggested_tier = CASE
            WHEN model ILIKE '%haiku%' THEN 'low'
            WHEN model ILIKE '%sonnet%' THEN 'medium'
            WHEN model ILIKE '%opus%' THEN 'high'
            WHEN model ILIKE '%fable%' OR model ILIKE '%mythos%' THEN 'extra-high'
        END
        WHERE suggested_tier IS NULL AND model IS NOT NULL;
    END IF;
END
$$;

ALTER TABLE board.roles DROP CONSTRAINT IF EXISTS roles_suggested_tier_known;
ALTER TABLE board.roles ADD CONSTRAINT roles_suggested_tier_known CHECK (
    suggested_tier IS NULL
    OR suggested_tier IN ('low', 'medium', 'high', 'extra-high'));
