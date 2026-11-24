--
-- Step 7: the role the chief of staff reaches its ledger as (story 5.2.3).
--
-- The chief of staff holds the hive's widest reach over conversations and
-- runs no code of its own, so its database credential is the narrowest one
-- there is: `hive_staff` may connect, use the `staff` schema and read and
-- write its tables, and nothing else. Every other schema refuses it.
--
-- NOLOGIN. A role is cluster-wide and a password does not belong in a
-- tracked file, so the login that carries a password is made once per hive
-- by hand, as a member of this one (README, "The chief of staff's role").
--
-- Created only when absent, and a concurrent creator is not an error: every
-- scratch database a suite run migrates shares one server, and so this one
-- role.
--

SELECT pg_catalog.set_config('search_path', '', false);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                   WHERE rolname = 'hive_staff') THEN
        BEGIN
            CREATE ROLE hive_staff NOLOGIN;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END;
    END IF;
    EXECUTE pg_catalog.format('GRANT CONNECT ON DATABASE %I TO hive_staff',
                              pg_catalog.current_database());
END
$$;

GRANT USAGE ON SCHEMA staff TO hive_staff;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA staff
    TO hive_staff;
-- A staff table a later step adds is the ledger's too.
ALTER DEFAULT PRIVILEGES IN SCHEMA staff
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hive_staff;
