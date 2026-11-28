--
-- Step 20: a key that ends (feature 834, story 911).
--
-- The one record of keys (`core.credentials`) learns a key made for one
-- person at one sign-in, with the time it stops working. `ends_at` is null
-- for every long-lived key (a mind's, a service's, a person's own), which
-- never ends by the clock, and set for a sign-in. A person may have several
-- sign-ins at once: the record's key is `holder`, and each sign-in is held
-- under its own name, so no constraint changes. The gateway, the memory store
-- and the tools refuse a sign-in from its `ends_at` on, or once `revoked_at`
-- is set, on the request.
--
-- `core.sql` carries the column too (as step 17 does for its tables), so a
-- database made from it alone is whole; this step adds it to one made before.
-- Nullable with no default, so the previous release's writes (which name no
-- such column) still land. The previous release would not itself refuse an
-- ended sign-in, but nothing writes one until the sign-in story ships.
-- The index serves ending every sign-in of one person and clearing out the
-- ended ones. Run twice, the second run changes nothing.
--
ALTER TABLE core.credentials
    ADD COLUMN IF NOT EXISTS ends_at double precision;

CREATE INDEX IF NOT EXISTS core_credentials_sign_ins
    ON core.credentials (username, ends_at)
    WHERE ends_at IS NOT NULL;
