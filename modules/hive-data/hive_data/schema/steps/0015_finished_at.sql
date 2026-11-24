--
-- Step 15: when a piece of work finished (feature 6.11, story 6.11.1).
--
-- Daniel, 2026-11-16: the Chief of Staff barrel shows only what finished
-- within a time window, counted from when the work finished, and nothing but
-- time hides finished work. Neither the board nor the ledger recorded when
-- anything finished - a board item carries no timestamp at all, and a ledger
-- task's `updated_at` moves with any later edit, a watch's stop mark
-- included - so each gains `done_at`, the moment its status moved into done.
--
-- Stamped by the database, not by a writer. Every writer of a status (the
-- console, a mind holding the board bearer, the chief of staff's ledger
-- tools) gets the rule for nothing, and a hive still running an older
-- hive-data - one whose column list does not name `done_at` yet - stamps
-- it all the same, so there is no window between this step and a rebuild
-- in which a finish goes unrecorded. A value a writer passes is overwritten:
--
--   * status moves into done (or a row is inserted done): `done_at` is now.
--     For a ledger task "now" is the row's own `updated_at`, which the
--     ledger writes on every status change and a test can fix; the board
--     has no clock of its own, so a board item takes the transaction time.
--   * status stays done: `done_at` keeps what it was. A title edit on a
--     finished story does not make it finish again.
--   * status moves out of done: `done_at` is cleared. Reopened work is not
--     finished, and finishing it again is a new finish.
--
-- Existing finished rows are given a time once, here. A done ledger task
-- takes its `updated_at`, the best record of its finish there is. A done
-- board item takes the latest finish of a done ledger task naming it, then
-- of one naming its parent (a story whose feature the chief of staff
-- closed), and otherwise the moment this step runs - the board never
-- recorded one, and
-- the alternatives are to hide every finished item at once or to show it
-- forever, and both are hiding or keeping by something other than time.
--
-- ADD COLUMN IF NOT EXISTS and CREATE OR REPLACE, so a second run is
-- harmless. It also backfills nothing: the backfill runs before the
-- triggers exist the first time, and once they do no done row is left
-- without a time to fill.
--

SELECT pg_catalog.set_config('search_path', '', false);
SET LOCAL lock_timeout = '10s';

ALTER TABLE board.work_items ADD COLUMN IF NOT EXISTS done_at double precision;
ALTER TABLE staff.tasks ADD COLUMN IF NOT EXISTS done_at double precision;

UPDATE staff.tasks SET done_at = updated_at
WHERE status = 'done' AND done_at IS NULL;

UPDATE board.work_items AS item
SET done_at = COALESCE(
    (SELECT max(task.done_at) FROM staff.tasks AS task
     WHERE task.board_item = item.id AND task.status = 'done'),
    (SELECT max(task.done_at) FROM staff.tasks AS task
     WHERE task.board_item = item.parent_id AND task.status = 'done'),
    extract(epoch FROM now()))
WHERE item.status = 'done' AND item.done_at IS NULL;

CREATE OR REPLACE FUNCTION board.stamp_done_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM 'done' THEN
        NEW.done_at := NULL;
    ELSIF TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'done' THEN
        NEW.done_at := extract(epoch FROM now());
    ELSE
        NEW.done_at := OLD.done_at;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION staff.stamp_done_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM 'done' THEN
        NEW.done_at := NULL;
    ELSIF TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'done' THEN
        NEW.done_at := NEW.updated_at;
    ELSE
        NEW.done_at := OLD.done_at;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS work_items_stamp_done_at ON board.work_items;
CREATE TRIGGER work_items_stamp_done_at
    BEFORE INSERT OR UPDATE ON board.work_items
    FOR EACH ROW EXECUTE FUNCTION board.stamp_done_at();

DROP TRIGGER IF EXISTS tasks_stamp_done_at ON staff.tasks;
CREATE TRIGGER tasks_stamp_done_at
    BEFORE INSERT OR UPDATE ON staff.tasks
    FOR EACH ROW EXECUTE FUNCTION staff.stamp_done_at();
