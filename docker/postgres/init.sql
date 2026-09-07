-- =============================================================================
-- docker/postgres/init.sql  --  roles + database bootstrap (docs/architecture.md §3)
--
-- The postgres:16 entrypoint runs everything in /docker-entrypoint-initdb.d ONCE,
-- as the superuser, on the FIRST boot of an empty data directory.  It is not
-- Alembic's job: Alembic connects as ca_owner and therefore cannot create it.
--
--   ca_owner  owns every object and runs the migrations (as owner it bypasses RLS).
--   ca_app    is the runtime role for api + worker: NOBYPASSRLS, no DDL, so a
--             query that forgets SET LOCAL app.portal_id returns zero rows.
--
-- Written to be idempotent (IF NOT EXISTS / ALTER on re-run) so that re-running it
-- by hand against an existing cluster - e.g. to rotate the role passwords - never
-- fails half way and never drops anything.
--
-- Passwords come from the environment the entrypoint exports into psql:
--   POSTGRES_CA_OWNER_PASSWORD, POSTGRES_CA_APP_PASSWORD  (see .env.example).
-- They are read with \getenv and handed to the DO blocks through a session GUC,
-- never interpolated into a dollar-quoted body, and never echoed.
-- =============================================================================

\set ON_ERROR_STOP on

\getenv ca_owner_pw POSTGRES_CA_OWNER_PASSWORD
\getenv ca_app_pw   POSTGRES_CA_APP_PASSWORD

SELECT set_config('init.ca_owner_pw', :'ca_owner_pw', false),
       set_config('init.ca_app_pw',   :'ca_app_pw',   false);

-- ---------------------------------------------------------------- ca_owner
DO $$
DECLARE
    pw text := current_setting('init.ca_owner_pw', true);
BEGIN
    IF pw IS NULL OR pw = '' THEN
        RAISE EXCEPTION 'POSTGRES_CA_OWNER_PASSWORD is not set; refusing to create a passwordless owner role';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_owner') THEN
        EXECUTE format('CREATE ROLE ca_owner LOGIN PASSWORD %L', pw);
    ELSE
        EXECUTE format('ALTER ROLE ca_owner LOGIN PASSWORD %L', pw);
    END IF;

    -- No CREATEDB / CREATEROLE / SUPERUSER: ca_owner only owns this one database.
    EXECUTE 'ALTER ROLE ca_owner NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION';
END
$$;

-- ---------------------------------------------------------------- ca_app
DO $$
DECLARE
    pw text := current_setting('init.ca_app_pw', true);
BEGIN
    IF pw IS NULL OR pw = '' THEN
        RAISE EXCEPTION 'POSTGRES_CA_APP_PASSWORD is not set; refusing to create a passwordless runtime role';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_app') THEN
        EXECUTE format('CREATE ROLE ca_app LOGIN NOBYPASSRLS PASSWORD %L', pw);
    ELSE
        EXECUTE format('ALTER ROLE ca_app LOGIN PASSWORD %L', pw);
    END IF;

    -- NOBYPASSRLS is the structural isolation guarantee of §3; assert it on every run.
    EXECUTE 'ALTER ROLE ca_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
END
$$;

-- Scrub the passwords out of the session before anything else runs.
SELECT set_config('init.ca_owner_pw', '', false),
       set_config('init.ca_app_pw',   '', false);

-- ---------------------------------------------------------------- database
-- CREATE DATABASE cannot run inside a DO block (no transaction), so it is
-- generated conditionally and executed with \gexec.
SELECT format('CREATE DATABASE callanalytics OWNER ca_owner ENCODING ''UTF8'' TEMPLATE template0')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'callanalytics')
\gexec

ALTER DATABASE callanalytics OWNER TO ca_owner;

-- ---------------------------------------------------------------- schema
\connect callanalytics

-- ca_owner must be able to create the §3 objects; nobody else may add objects to
-- public.  Table/sequence grants for ca_app live in the Alembic baseline (§3).
ALTER SCHEMA public OWNER TO ca_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO ca_owner;
GRANT USAGE ON SCHEMA public TO ca_app;

-- ca_app never connects to anything else, and template/other databases stay closed.
REVOKE ALL ON DATABASE callanalytics FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE callanalytics TO ca_owner;
GRANT CONNECT ON DATABASE callanalytics TO ca_app;
