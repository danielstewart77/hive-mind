-- What an approved call did when hive-tools replayed it.
--
-- An approval used to end at the decision: the replay's answer was logged
-- and nowhere else, so the mind that asked was told "approved" and never
-- learned whether the tool it was approved to run had run. These four hold
-- that answer on the request itself, so `GET /hitl/{id}` can report it and
-- hive-tools can tell the asking mind.
--
--   outcome_ok      whether the tool did what it was asked; false for a
--                   refused or erroring replay, never true for one
--   outcome_status  the replay's HTTP status; NULL when it never got one
--   outcome_body    what the tool returned, or the error it failed with
--   outcome_at      when the outcome landed; NULL until it has
--
-- All nullable with no default: every request decided before this step has
-- no outcome, and saying so is NULL rather than a fabricated one.

ALTER TABLE tools.hitl_requests
    ADD COLUMN outcome_ok boolean,
    ADD COLUMN outcome_status bigint,
    ADD COLUMN outcome_body text,
    ADD COLUMN outcome_at bigint;
