--
-- Step 19: an approval records who decided it (feature 834, story 908).
--
-- Approving moves to the hive site, where a signed-in person decides with
-- their own key; the tools record that person's name on the request.
-- Nullable with no default: a request decided before this step was decided
-- by the approver credential, which stood for nobody, and saying so is NULL.
--
-- The approver credential's row in `tools.api_tokens` goes: the credential
-- is removed with the Telegram and Discord buttons. Run twice, the second
-- run deletes nothing.
--
ALTER TABLE tools.hitl_requests
    ADD COLUMN IF NOT EXISTS decided_by text;

DELETE FROM tools.api_tokens WHERE lower(name) = 'hitl-approver';
