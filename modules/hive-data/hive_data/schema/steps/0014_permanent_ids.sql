--
-- Step 14: every item on the board has a permanent whole-number ID (feature
-- 3.7, story 3.7.7).
--
-- Decided 2026-11-15: one running count shared by epics, features, stories
-- and tasks, never reused and never changed by reordering. The text `id`
-- stays the primary key - every URL, foreign key and reference to an item
-- keeps naming it - and `item_number` is the whole number shown beside it.
--
-- An identity column rather than MAX + 1. A sequence never hands a value
-- back: not after a delete, not after a rolled-back create. A rollback
-- leaves a gap, which is allowed; the requirement is only that no number is
-- ever issued twice. GENERATED ALWAYS, so Postgres itself refuses an insert
-- or update that names a value: nothing renumbers an item, by accident or
-- otherwise.
--
-- Existing items are numbered in current board order (decided 2026-11-16:
-- the board never recorded when an item was created). Current board order
-- is the hierarchy number walked in order - 1, 1.1, 1.1.1, 1.2, 2 - exactly
-- as `hive_data.board.numbers_for` numbers the rows `work_items()` returns,
-- so the parent each row is numbered under below is `numbering_parent`
-- transcribed, and siblings are ordered as `work_items()` orders them, by
-- position then id. A row no root reaches (a parent cycle) numbers as
-- nothing on the board, and goes last, by position then id.
--
-- The unique constraint is added after the backfill rather than before it:
-- Postgres checks a non-deferrable unique constraint row by row, so a
-- backfill under it can collide with a value it is about to overwrite.
--
-- Not repeatable and not meant to be: the record in hive_meta.migrations is
-- what stops it running twice, and a second run fails on ADD COLUMN rather
-- than renumber anything. There is no way back that keeps the numbers: a
-- dropped column re-added later is numbered against the board as it is
-- then, so an item quoted by its ID would silently become another item.
--
-- ADD COLUMN takes the table's exclusive lock and every board read queues
-- behind it while it waits, so the wait is bounded: a busy board fails the
-- step, which leaves nothing, rather than stalling the console.
--

SELECT pg_catalog.set_config('search_path', '', false);
SET LOCAL lock_timeout = '10s';

ALTER TABLE board.work_items ADD COLUMN item_number bigint;

WITH RECURSIVE
rows AS (
    SELECT item.id, item."position",
           CASE
               WHEN item.kind IN ('feature', 'task') THEN item.parent_id
               WHEN item.kind = 'story'
                    AND parent.kind IN ('feature', 'epic', 'task')
                   THEN item.parent_id
           END AS numbered_under
    FROM board.work_items AS item
    LEFT JOIN board.work_items AS parent ON parent.id = item.parent_id
),
siblings AS (
    SELECT id, numbered_under,
           row_number() OVER (PARTITION BY numbered_under
                              ORDER BY "position", id) AS place
    FROM rows
),
walked AS (
    SELECT id, ARRAY[place] AS path
    FROM siblings WHERE numbered_under IS NULL
    UNION ALL
    SELECT child.id, walked.path || child.place
    FROM siblings AS child JOIN walked ON child.numbered_under = walked.id
),
ordered AS (
    SELECT rows.id,
           row_number() OVER (
               ORDER BY walked.path IS NULL, walked.path,
                        rows."position", rows.id) AS n
    FROM rows LEFT JOIN walked ON walked.id = rows.id
)
UPDATE board.work_items AS item SET item_number = ordered.n
FROM ordered WHERE ordered.id = item.id;

ALTER TABLE board.work_items
    ALTER COLUMN item_number SET NOT NULL,
    ADD CONSTRAINT work_items_item_number_key UNIQUE (item_number);

ALTER TABLE board.work_items
    ALTER COLUMN item_number ADD GENERATED ALWAYS AS IDENTITY;

SELECT pg_catalog.setval(
    pg_catalog.pg_get_serial_sequence('board.work_items', 'item_number'),
    COALESCE((SELECT max(item_number) FROM board.work_items), 0) + 1,
    false);
