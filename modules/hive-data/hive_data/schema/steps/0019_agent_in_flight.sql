--
-- Step 19: a sub-agent record says what the child has in flight (feature 916).
--
-- On 2026-11-27 a builder sub-agent ran one test suite for ten minutes and
-- was drawn as a still node, indistinguishable from a dead one: the record
-- could say "produced output recently" and nothing else, and a tool call
-- produces nothing until it returns. Daniel: "It looks like nothing's going
-- on".
--
-- `tool` is a few words naming the tool call in flight, empty when none is;
-- `tool_started_at` is when that call began, on the recorder's clock.
-- `thinking_at` is when the child was last seen thinking. All three are
-- written only by a reporter that reads the child's own log and says what is
-- in flight right now, as one report: a separate "started" and "finished"
-- would race, and a lost "finished" would leave the child reading busy for
-- ever.
--
-- `reports_tools`, `reports_output` and `reports_thinking` say what the
-- reporter that wrote this record is able to see: a child's tool calls, the
-- output it produces between them, and its thinking. A reporter that sees
-- tool calls and not output (codex, which gives a child no log of its own)
-- can say a call is in flight and can say nothing about the silence between
-- two calls, so that silence is never called a stall. All default false, which is what every record
-- written before this step is, and what an older reporter goes on writing:
-- such a record is read as it always was and is never called stalled on
-- silence alone. Only a reporter that can see tool calls turns the new
-- readings on, and once on they stay on for that record.
--
-- Columns are only added, so a service still on an older hive-data opens its
-- pool as before. IF NOT EXISTS throughout, so a second run is harmless.
--

SELECT pg_catalog.set_config('search_path', '', false);
SET LOCAL lock_timeout = '10s';

ALTER TABLE staff.agents
    ADD COLUMN IF NOT EXISTS tool text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS tool_started_at double precision,
    ADD COLUMN IF NOT EXISTS thinking_at double precision,
    ADD COLUMN IF NOT EXISTS reports_tools boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS reports_output boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS reports_thinking boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN staff.agents.tool IS
    'A few words naming the tool call in flight; empty when none is.';
COMMENT ON COLUMN staff.agents.reports_tools IS
    'Whether the reporter that wrote this record can see tool calls at all.';
