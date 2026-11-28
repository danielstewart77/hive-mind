--
-- Step 9: what the Chief of Staff page needs to draw, which the ledger did
-- not hold (feature 6.7).
--
-- The page is a tree: the chief of staff at the root, a node per task
-- beneath him, and the task's own children beneath that. Three things were
-- missing and none of them could be inferred from the rows that existed.
--
-- `board_item` is the work item the task covers, as a `board.work_items`
-- id. Without it a task that *is* feature 6.4 is a single node, because
-- nothing on the row says which feature, and the depth Daniel asked for -
-- a simple task one node, a feature a node with children, an epic a node
-- whose children have children - has no source. Deliberately not a foreign
-- key: the ledger is reached by a role that may not read `board`, so a
-- reference checked by the engine would need a grant that hands the chief
-- of staff the whole board. A dangling id draws as a plain task, which is
-- what a task with no board item draws as anyway.
--
-- `question` is the one thing the task is waiting on Daniel for. The status
-- already said that a task was waiting; the question itself existed only in
-- the chat message the chief of staff sent, so a bubble could say that an
-- answer was owed but never which. Kept beside the status rather than
-- derived from it: a task can be `in progress` and still hold a question
-- (the delegate works on while the answer is owed), which is precisely the
-- case the page draws as a bubble on a live node.
--

SELECT pg_catalog.set_config('search_path', '', false);

ALTER TABLE staff.tasks
    ADD COLUMN board_item text,
    ADD COLUMN question text;

COMMENT ON COLUMN staff.tasks.board_item IS
    'The board.work_items id this task covers, or null. Not a foreign key: the ledger role may not read board.';
COMMENT ON COLUMN staff.tasks.question IS
    'The one thing this task is waiting on Daniel for, or null. A node holding one draws a question bubble.';

--
-- The sub-agents working a task, while they are working it.
--
-- A sub-agent is an in-process child of the mind's turn and is invisible
-- from outside that process: `list_agents` answers only inside the turn
-- that spawned it, so nothing a page could call knows one exists. The
-- children therefore register themselves here on the way in and retire
-- themselves on the way out, which is the only record of them that outlives
-- the turn.
--
-- `finished_at` null is a child still working. That is not the same as a
-- child still *alive*: a child dies with the turn's process, so one left
-- open past the turn timeout is an orphan rather than work in flight, and
-- the reader decides that from `started_at` rather than from a column a
-- dead child could never have written.
--
CREATE TABLE staff.agents (
    id text NOT NULL,
    task_id text NOT NULL,
    brief text NOT NULL,
    about text NOT NULL DEFAULT '',
    outcome text NOT NULL DEFAULT '',
    started_at double precision NOT NULL,
    finished_at double precision,
    CONSTRAINT agents_pkey PRIMARY KEY (id),
    CONSTRAINT agents_id_not_blank CHECK (id !~ '^[\s\u001c-\u001f\u0085  ᠎ -‍    ⁠　﻿]*$'),
    CONSTRAINT agents_brief_not_blank CHECK (brief !~ '^[\s\u001c-\u001f\u0085  ᠎ -‍    ⁠　﻿]*$'),
    -- The child is part of the task's record: deleting a task opened in
    -- error takes the children opened under it.
    CONSTRAINT agents_task_id_fk_tasks FOREIGN KEY (task_id)
        REFERENCES staff.tasks(id) ON DELETE CASCADE
);

CREATE INDEX agents_task_id_idx ON staff.agents USING btree (task_id);

-- The page's own read: every child still working. Partial, because a hive
-- that has run for a week holds thousands of finished ones and draws none.
CREATE INDEX agents_working_idx ON staff.agents USING btree (started_at)
    WHERE finished_at IS NULL;
