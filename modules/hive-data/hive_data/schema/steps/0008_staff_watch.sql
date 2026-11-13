--
-- Step 8: what the chief of staff's tick has noticed about a task's
-- sessions, and what it has already told the mind about (story 6.3.2).
--
-- The tick is stateless and holds no queue, so its memory has to be the
-- rows themselves: a skipped tick, a restarted container or a tick that
-- dies mid-pass loses nothing, because the next one reads the same two
-- timestamps and reaches the same conclusion.
--
-- `stopped_at` is when a tick first saw every session paired with this task
-- stop producing output - gone from the live feed, or present in it and
-- idle. `woken_at` is when the mind was last told about this task. A wake
-- is therefore pending exactly when `stopped_at` is set and `woken_at` is
-- either null or older than it, which is a comparison and not a flag: a
-- task that stops, is reported, and is handed back to the same session
-- becomes pending again the next time it stops, with no column to clear.
--
-- Deliberately not a status. A status is Daniel's reading of the work and
-- the chief of staff's to move; these two are the machine's record of what
-- it saw and what it sent, and conflating them would have the tick
-- overwriting a status Daniel set.
--

SELECT pg_catalog.set_config('search_path', '', false);

ALTER TABLE staff.tasks
    ADD COLUMN stopped_at double precision,
    ADD COLUMN woken_at double precision;

COMMENT ON COLUMN staff.tasks.stopped_at IS
    'When a tick first saw every session on this task stop producing output; null while one is still running.';
COMMENT ON COLUMN staff.tasks.woken_at IS
    'When the chief of staff was last woken about this task. A wake is pending while stopped_at is newer.';

--
-- The tick's own read: every task with a wake pending. Partial, because the
-- rows it excludes are the overwhelming majority at any moment.
--
CREATE INDEX tasks_stopped_at_idx ON staff.tasks USING btree (stopped_at)
    WHERE stopped_at IS NOT NULL;
