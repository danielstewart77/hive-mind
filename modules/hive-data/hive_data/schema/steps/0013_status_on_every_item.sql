--
-- Step 13: every item on the board has a status (feature 3.7, story 3.7.1).
--
-- Until now only a story carried one: an epic and a feature were written
-- with status NULL, and a change of type to either cleared it. Decided
-- 2026-11-15: status applies to every type - epic, feature, story and the
-- new task - and is one of backlog, committed, in progress or done, so an
-- epic or a feature can be done.
--
-- An item with no status yet starts as backlog. Written here rather than
-- defaulted at read time, so the store holds what the board shows and no
-- reader (the console, the chief of staff, a mind with the board bearer)
-- has to know that NULL means backlog.
--
-- NOT NULL so the rule is the store's and not each writer's, and a default
-- so a row written without one lands in backlog like a new story does.
-- Repeatable: on a hive that already ran it the UPDATE matches nothing and
-- the two ALTERs set what is already set.
--

SELECT pg_catalog.set_config('search_path', '', false);

UPDATE board.work_items SET status = 'backlog' WHERE status IS NULL;

ALTER TABLE board.work_items
    ALTER COLUMN status SET DEFAULT 'backlog',
    ALTER COLUMN status SET NOT NULL;
