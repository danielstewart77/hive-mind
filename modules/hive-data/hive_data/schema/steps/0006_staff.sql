--
-- Step 6: the chief of staff's ledger (stories 5.2.1 and 5.2.2).
--
-- What Daniel has handed the chief of staff to watch, and the hive-comms
-- sessions working each. Kept deliberately small: the chief of staff's
-- skills carry the behaviour, not the columns. There is no mind column (the
-- mind comes from the session), no board-link column (a board item is named
-- in the description text) and nothing recording how a session arrived.
--

SELECT pg_catalog.set_config('search_path', '', false);

CREATE SCHEMA staff;

--
-- What a name made only of whitespace looks like, Unicode included: a
-- no-break space or a zero-width space reads as blank on every surface the
-- ledger is shown on, and `btrim` strips neither.
--

COMMENT ON SCHEMA staff IS 'The chief of staff''s ledger: tasks it watches and the sessions working them.';

--
-- A task's status names a row here, so a status is added with an INSERT
-- rather than a migration, and a typo is refused by the key rather than
-- stored.
--
CREATE TABLE staff.statuses (
    name text NOT NULL,
    CONSTRAINT statuses_pkey PRIMARY KEY (name),
    CONSTRAINT statuses_name_not_blank CHECK (name !~ '^[\s\u001c-\u001f\u0085\u00a0\u1680\u180e\u2000-\u200d\u2028\u2029\u202f\u205f\u2060\u3000\ufeff]*$')
);

INSERT INTO staff.statuses (name) VALUES
    ('in progress'), ('waiting on response'), ('on hold'), ('done');

CREATE TABLE staff.tasks (
    id text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    status text NOT NULL,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    CONSTRAINT tasks_pkey PRIMARY KEY (id),
    CONSTRAINT tasks_id_not_blank CHECK (id !~ '^[\s\u001c-\u001f\u0085\u00a0\u1680\u180e\u2000-\u200d\u2028\u2029\u202f\u205f\u2060\u3000\ufeff]*$'),
    CONSTRAINT tasks_name_not_blank CHECK (name !~ '^[\s\u001c-\u001f\u0085\u00a0\u1680\u180e\u2000-\u200d\u2028\u2029\u202f\u205f\u2060\u3000\ufeff]*$'),
    -- ON UPDATE CASCADE so renaming a status relabels its tasks; RESTRICT on
    -- delete so a status still in use cannot vanish from under them.
    CONSTRAINT tasks_status_fk_statuses FOREIGN KEY (status)
        REFERENCES staff.statuses(name) ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX tasks_status_idx ON staff.tasks USING btree (status);

--
-- A task paired with each session working it. Usually one pair; a bake-off
-- is several under one task. A session works one task at a time, so the
-- session id is the key, which is what makes "the task for a session" one
-- answer.
--
-- The session is `core.sessions.id`: the hive-comms session id, permanent
-- across rotations (a rotation rewrites the row's conversation id, never
-- its id), text as it is there. Deliberately NOT a foreign key. Comms
-- deletes a session row whenever a conversation ends (/new, /clear, /prune,
-- broker cleanup), and the ledger's record that a session worked a task must
-- survive that: no ON DELETE action both keeps this row and lets the session
-- go. A paired id with no `core.sessions` row is a session that has ended,
-- and that is how the tick reads it.
--
CREATE TABLE staff.task_sessions (
    session_id text NOT NULL,
    task_id text NOT NULL,
    attached_at double precision NOT NULL,
    CONSTRAINT task_sessions_pkey PRIMARY KEY (session_id),
    CONSTRAINT task_sessions_session_id_not_blank CHECK (session_id !~ '^[\s\u001c-\u001f\u0085\u00a0\u1680\u180e\u2000-\u200d\u2028\u2029\u202f\u205f\u2060\u3000\ufeff]*$'),
    CONSTRAINT task_sessions_task_id_fk_tasks FOREIGN KEY (task_id)
        REFERENCES staff.tasks(id) ON DELETE CASCADE
);

CREATE INDEX task_sessions_task_id_idx ON staff.task_sessions USING btree (task_id);
