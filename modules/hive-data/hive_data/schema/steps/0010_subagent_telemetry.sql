--
-- Step 10: one shape for a sub-agent, whichever harness spawned it
-- (feature 6.9, story 6.9.1).
--
-- Step 9 made `staff.agents` for the children the chief of staff's own
-- delegate briefs register: a child knows the ledger task it was handed, so
-- the row hangs off a task and carries the brief file it was given. Nothing
-- outside that one path could write a row, because nothing else knows a
-- task id.
--
-- A harness reporting its own sub-agents knows none of that. It knows which
-- mind it is, which conversation it is in, what kind of child it spawned and
-- which child spawned it; it does not know the ledger, and the mind it runs
-- as holds no credential that could read one. So the record keys on the
-- child's own identity and the conversation it belongs to, and the task is
-- derived from that conversation when the row is *read* — a session attached
-- to a task after a child started is the work claiming the child, which is
-- right, and resolving it at write time would freeze the wrong answer.
--
-- Three harnesses, three vocabularies, and the differences are the reason
-- almost every column added here is nullable:
--
--   * dsh's `subagent.started` carries the parent session and the child
--     session and nothing else - no kind, no label - so a row written from
--     one has neither. `brief` therefore loses its NOT NULL rather than
--     being handed a placeholder: a made-up label is an invented record, and
--     the requirement is that a record nobody could write is absent rather
--     than fabricated. The not-blank check stays and still refuses a brief
--     of spaces, because a CHECK fails only on false and passes on null.
--
--   * claude's hook names the agent type; codex's names the agent type; dsh
--     names the provider only when the child finishes. `agent_type` is the
--     one field that means "what kind of child is this, as its own writer
--     names it", and it is empty rather than null when the writer could not
--     say.
--
--     What that buys is one field to read rather than two, and `brief`
--     keeping its own narrow meaning - the file the chief handed a delegate.
--     What it does not buy is one vocabulary: this field holds `chase.md`
--     from a delegate and `refute-tests` from a claude hook, because those
--     are what those writers call the kind of child they spawned. Anything
--     grouping on it is grouping names from several vocabularies, and the
--     row says which writer it came from - a delegate has a brief, a
--     reported child has a mind and a conversation.
--
--   * the id a harness hands out is unique inside its own process and
--     nowhere else. dsh says so in as many words (run ids are "unique in
--     the parent namespace"). The primary key here covers the whole hive, so
--     the recorder namespaces what it is given by the mind that reported it
--     before it lands. Without that, two minds' children collide: the second
--     start is refused and the second mind's ending closes the first mind's
--     row.
--
-- `parent_agent_id` is deliberately *not* a foreign key, for the reason
-- `staff.tasks.board_item` is not one: a child's parent may legitimately be
-- missing - the spawn is never delayed for the recorder, so a lost parent
-- write is allowed - and a reference checked by the engine would turn one
-- lost row into a lost subtree. A parent that resolves to nothing draws as
-- unplaceable, which is what an unplaceable child draws as anyway.
--
-- `active_at` is the one thing that separates "started and not yet ended"
-- from "still producing work right now". It is a stamp rather than a flag
-- because a flag nobody clears is a lie the moment the child dies, and it is
-- separate from `started_at` because the sweep below must not retire a child
-- that is still reporting. Nothing writes it until a harness that can
-- genuinely observe ongoing work does; until then every open row reads as
-- running, which is the honest answer rather than a spinner nobody earned.
--
-- Every timestamp on these rows is the *recorder's* clock, never the
-- reporting host's. A hive has minds on other machines and in containers,
-- and a host whose clock runs ahead would otherwise hold a child in the
-- future - reading as freshly active, and out of the sweep's reach, for as
-- long as the drift lasts.
--

SELECT pg_catalog.set_config('search_path', '', false);

ALTER TABLE staff.agents
    ALTER COLUMN task_id DROP NOT NULL,
    ALTER COLUMN brief DROP NOT NULL,
    ADD COLUMN mind_id text,
    ADD COLUMN agent_type text NOT NULL DEFAULT '',
    ADD COLUMN session_id text,
    ADD COLUMN conversation_ref text,
    ADD COLUMN parent_agent_id text,
    ADD COLUMN active_at double precision;

-- The children already recorded are the chief's delegates, and the brief
-- file each was handed is exactly what kind of child it is. Carrying it
-- across means the kind field is populated for every row that has one
-- rather than only for rows written after this step, so a reader never has
-- to know which era a row came from.
UPDATE staff.agents SET agent_type = brief
    WHERE agent_type = '' AND brief IS NOT NULL;

COMMENT ON COLUMN staff.agents.task_id IS
    'The ledger task this child works, when its writer knew one. Null for a child a harness reported: the task is derived from the conversation at read time.';
COMMENT ON COLUMN staff.agents.brief IS
    'The brief file the chief handed this child, or null. A harness reporting a child has no brief and is never given a placeholder.';
COMMENT ON COLUMN staff.agents.mind_id IS
    'The mind whose turn spawned this child. Also what namespaces the id, since a harness agent id is unique only inside its own process.';
COMMENT ON COLUMN staff.agents.agent_type IS
    'What kind of child this is, as its own writer names it. Empty when the writer could not say.';
COMMENT ON COLUMN staff.agents.session_id IS
    'The core.sessions id this child belongs to, resolved by the recorder, or null when it resolved to none. Not a foreign key: comms deletes an ended conversation and the record outlives it.';
COMMENT ON COLUMN staff.agents.conversation_ref IS
    'The id the reporter actually sent, whatever kind it was. Kept so an unresolved record still says where it came from.';
COMMENT ON COLUMN staff.agents.parent_agent_id IS
    'The child that spawned this one, or null when the mind itself did. Not a foreign key: a lost parent must not lose the subtree.';
COMMENT ON COLUMN staff.agents.active_at IS
    'The last moment a harness positively reported this child still producing work, on the recorder''s clock. Null means no harness ever could.';

-- The page's reads. One by conversation, because the tree is built from the
-- sessions working each task and the children hanging off them; one by when
-- a child ran, because "what ran overnight" is a read over every mind at
-- once and the existing partial index covers only open rows.
CREATE INDEX agents_session_id_idx ON staff.agents USING btree (session_id)
    WHERE session_id IS NOT NULL;
CREATE INDEX agents_started_at_idx ON staff.agents USING btree (started_at);
