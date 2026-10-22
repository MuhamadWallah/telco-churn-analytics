/* =============================================================================
   09_dashboard_access.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16 and 18)
   Purpose : Security for the interactive dashboard (dashboard/app.py):

     1. Schema "app" (a 4th schema, separate from the analytical layers) with
          app.dashboard_user   dashboard accounts, passwords stored as bcrypt HASHES
          app.login_audit      every login attempt (success or failure)
     2. Functions that check a login INSIDE the database, so password hashes
        never leave PostgreSQL. After 5 failed attempts an account is locked
        for 15 minutes.
     3. A database login role "churn_dashboard" for the dashboard program,
        following the principle of LEAST PRIVILEGE: it can READ the olap
        schema and call a few app functions - nothing else. It cannot read
        staging or dw, cannot read the users table, and cannot change data.
     4. Two dashboard accounts:  admin (role admin) and analyst (role analyst).

   How to run (after 01-08, as the postgres superuser):
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/09_dashboard_access.sql
   psql then asks for three passwords (they are shown while you type):
       - the database password of the churn_dashboard role (also goes into
         .streamlit/secrets.toml so the dashboard can connect)
       - the dashboard password of the user "admin"     (at least 8 characters)
       - the dashboard password of the user "analyst"   (at least 8 characters)
   You can also pass them on the command line instead of typing them:
       -v dashboard_db_password=... -v admin_password=... -v analyst_password=...

   Re-runnable: yes. Re-running sets the three passwords again.

   MySQL differences used in this script:
     - MySQL: CREATE USER 'x'@'localhost' IDENTIFIED BY '...'
       PostgreSQL: CREATE ROLE x LOGIN PASSWORD '...'  (users and groups are both
       "roles"; which hosts may connect is set in pg_hba.conf, not in the user name)
     - MySQL: GRANT SELECT ON telco_churn_dw.* TO ...   (whole database)
       PostgreSQL: GRANT USAGE ON SCHEMA olap + GRANT SELECT ON ALL TABLES IN
       SCHEMA olap, plus ALTER DEFAULT PRIVILEGES for objects created later.
     - MySQL has no built-in bcrypt; PostgreSQL's pgcrypto extension provides
       crypt() and gen_salt('bf').
     - MySQL: INSERT ... ON DUPLICATE KEY UPDATE
       PostgreSQL: INSERT ... ON CONFLICT (column) DO UPDATE
     - Both have SECURITY DEFINER routines (MySQL: SQL SECURITY DEFINER).
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Run this script with -d telco_churn_dw.',
                        current_database();
    END IF;
    IF to_regclass('olap.mining_run') IS NULL OR to_regclass('olap.mv_kpi_overview') IS NULL THEN
        RAISE EXCEPTION 'Run scripts 06, 07 and 08 first (the dashboard reads their objects).';
    END IF;
END
$$;

-- Ask for the passwords unless they were given with -v on the command line.
\if :{?dashboard_db_password}
\else
    \prompt 'Database password for the role churn_dashboard: ' dashboard_db_password
\endif
\if :{?admin_password}
\else
    \prompt 'Dashboard password for user admin (min. 8 characters): ' admin_password
\endif
\if :{?analyst_password}
\else
    \prompt 'Dashboard password for user analyst (min. 8 characters): ' analyst_password
\endif

-- Check the passwords BEFORE changing anything, so a too-short password cannot
-- leave the database half configured (role created, accounts missing).
SELECT length(:'admin_password') >= 8 AND length(:'analyst_password') >= 8 AS passwords_ok \gset
\if :passwords_ok
\else
    \echo ''
    \echo 'STOPPED: the admin and analyst passwords must have at least 8 characters.'
    \echo 'Nothing was changed. Run the script again with longer passwords.'
    \quit
\endif


/* -----------------------------------------------------------------------------
   Part A. pgcrypto - bcrypt password hashing.
   crypt(password, gen_salt('bf', 10)) returns a bcrypt hash such as
   $2a$10$N9qo8uLOickgx2ZMRZoMy... Each hash has its own random salt, so two
   users with the same password still get different hashes, and the original
   password cannot be read back. To check a password, PostgreSQL hashes the
   typed password with the stored salt and compares the two hashes.
   ----------------------------------------------------------------------------- */
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


/* -----------------------------------------------------------------------------
   Part B. Schema app and its tables.
   ----------------------------------------------------------------------------- */
CREATE SCHEMA IF NOT EXISTS app;
COMMENT ON SCHEMA app IS
    'Application security for the dashboard: users (bcrypt hashes), login audit and the functions the dashboard may call.';
REVOKE ALL ON SCHEMA app FROM PUBLIC;

CREATE TABLE IF NOT EXISTS app.dashboard_user (
    user_id              INTEGER GENERATED ALWAYS AS IDENTITY,
    username             TEXT        NOT NULL,
    full_name            TEXT        NOT NULL,
    user_role            TEXT        NOT NULL,
    password_hash        TEXT        NOT NULL,
    is_active            BOOLEAN     NOT NULL DEFAULT true,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    password_changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at        TIMESTAMPTZ,
    CONSTRAINT pk_dashboard_user          PRIMARY KEY (user_id),
    CONSTRAINT uq_dashboard_user_username UNIQUE (username),
    CONSTRAINT ck_dashboard_user_username CHECK (username ~ '^[a-z][a-z0-9_]{2,29}$'),
    CONSTRAINT ck_dashboard_user_role     CHECK (user_role IN ('admin', 'analyst')),
    CONSTRAINT ck_dashboard_user_hash     CHECK (password_hash LIKE '$2%')   -- bcrypt format only
);
COMMENT ON TABLE app.dashboard_user IS
    'Dashboard accounts. password_hash is a bcrypt hash (pgcrypto); plain-text passwords are never stored.';

CREATE TABLE IF NOT EXISTS app.login_audit (
    attempt_id    BIGINT GENERATED ALWAYS AS IDENTITY,
    username      TEXT        NOT NULL,
    attempted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    success       BOOLEAN     NOT NULL,
    note          TEXT,
    CONSTRAINT pk_login_audit PRIMARY KEY (attempt_id)
);
CREATE INDEX IF NOT EXISTS ix_login_audit_username_time ON app.login_audit (username, attempted_at);
COMMENT ON TABLE app.login_audit IS 'Every dashboard login attempt, written by app.check_login().';


/* -----------------------------------------------------------------------------
   Part C. Functions.

   SECURITY DEFINER = the function runs with the rights of its OWNER (postgres),
   not of the caller. That is how the low-privilege dashboard role can check a
   password or read the ETL log without being allowed to read those tables
   itself. Such functions must fix their search_path (SET search_path = ...),
   so a caller cannot trick them into using a look-alike object; every object
   inside is also written with its schema name.
   ----------------------------------------------------------------------------- */

-- C1. Create or update an account (for the administrator running this script;
--     NOT callable by the dashboard role).
CREATE OR REPLACE FUNCTION app.set_user(p_username TEXT, p_full_name TEXT,
                                        p_role TEXT, p_password TEXT)
RETURNS TEXT
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_inserted BOOLEAN;
BEGIN
    IF p_password IS NULL OR length(p_password) < 8 THEN
        RAISE EXCEPTION 'The password for "%" must have at least 8 characters.', p_username;
    END IF;

    INSERT INTO app.dashboard_user AS u (username, full_name, user_role, password_hash)
    VALUES (lower(p_username), p_full_name, p_role,
            public.crypt(p_password, public.gen_salt('bf', 10)))
    ON CONFLICT (username) DO UPDATE
        SET full_name           = EXCLUDED.full_name,
            user_role           = EXCLUDED.user_role,
            password_hash       = EXCLUDED.password_hash,
            password_changed_at = now(),
            is_active           = true
    RETURNING (u.xmax = 0) INTO v_inserted;     -- xmax = 0 means "new row inserted"

    RETURN format('%s account "%s" (%s)',
                  CASE WHEN v_inserted THEN 'created' ELSE 'updated' END,
                  lower(p_username), p_role);
END
$$;


-- C2. Check a login. Returns one row (name, display name, role) if the
--     username and password are correct and the account is active, otherwise
--     no rows. Every attempt is written to app.login_audit.
--     Lock-out: 5 failed attempts since the last successful login, within
--     15 minutes, block further attempts for that username.
CREATE OR REPLACE FUNCTION app.check_login(p_username TEXT, p_password TEXT)
RETURNS TABLE (user_name TEXT, display_name TEXT, app_role TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_username  TEXT := lower(btrim(coalesce(p_username, '')));
    v_user      app.dashboard_user%ROWTYPE;
    v_failures  INTEGER;
    v_found     BOOLEAN;
    v_ok        BOOLEAN := false;
BEGIN
    SELECT count(*) INTO v_failures
    FROM   app.login_audit AS a
    WHERE  a.username = v_username
      AND  NOT a.success
      AND  a.attempted_at > now() - interval '15 minutes'
      AND  a.attempted_at > coalesce((SELECT max(s.attempted_at) FROM app.login_audit AS s
                                       WHERE s.username = v_username AND s.success),
                                      '-infinity');
    IF v_failures >= 5 THEN
        INSERT INTO app.login_audit (username, success, note)
        VALUES (v_username, false, 'locked: 5 failed attempts in 15 minutes');
        RETURN;
    END IF;

    SELECT * INTO v_user
    FROM   app.dashboard_user AS u
    WHERE  u.username = v_username AND u.is_active;
    v_found := FOUND;              -- remember now: later statements change FOUND

    IF v_found THEN
        v_ok := v_user.password_hash = public.crypt(coalesce(p_password, ''), v_user.password_hash);
    ELSE
        -- Spend the same hashing time for an unknown user, so the response
        -- time does not reveal whether a username exists.
        PERFORM public.crypt(coalesce(p_password, ''), public.gen_salt('bf', 10));
    END IF;

    INSERT INTO app.login_audit (username, success, note)
    VALUES (v_username, v_ok,
            CASE WHEN v_ok      THEN 'ok'
                 WHEN NOT v_found THEN 'unknown or inactive user'
                 ELSE                'wrong password' END);

    IF v_ok THEN
        UPDATE app.dashboard_user SET last_login_at = now() WHERE user_id = v_user.user_id;
        RETURN QUERY SELECT v_user.username, v_user.full_name, v_user.user_role;
    END IF;
END
$$;


-- C3. Read-only helpers for the dashboard's admin and data-quality pages.
CREATE OR REPLACE FUNCTION app.list_users()
RETURNS TABLE (username TEXT, full_name TEXT, user_role TEXT, is_active BOOLEAN,
               created_at TIMESTAMPTZ, password_changed_at TIMESTAMPTZ, last_login_at TIMESTAMPTZ)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT u.username, u.full_name, u.user_role, u.is_active,
           u.created_at, u.password_changed_at, u.last_login_at       -- never the hash
    FROM   app.dashboard_user AS u
    ORDER  BY u.username;
$$;

CREATE OR REPLACE FUNCTION app.recent_logins(p_limit INTEGER DEFAULT 50)
RETURNS TABLE (attempted_at TIMESTAMPTZ, username TEXT, success BOOLEAN, note TEXT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT a.attempted_at, a.username, a.success, a.note
    FROM   app.login_audit AS a
    ORDER  BY a.attempt_id DESC
    LIMIT  greatest(1, least(p_limit, 500));
$$;

CREATE OR REPLACE FUNCTION app.etl_runs(p_limit INTEGER DEFAULT 20)
RETURNS TABLE (run_id INTEGER, status TEXT, started_at TIMESTAMPTZ, seconds NUMERIC,
               staging_rows INTEGER, error_message TEXT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT r.run_id, r.status, r.started_at,
           round(extract(epoch FROM r.finished_at - r.started_at)::numeric, 2),
           r.staging_rows, r.error_message
    FROM   dw.etl_run_log AS r
    ORDER  BY r.run_id DESC
    LIMIT  greatest(1, least(p_limit, 200));
$$;

-- The 41 warehouse checks of script 05 (they read dw and staging, which the
-- dashboard role cannot see directly).
CREATE OR REPLACE FUNCTION app.data_quality()
RETURNS TABLE (check_no INTEGER, check_group TEXT, check_name TEXT,
               expected TEXT, actual TEXT, result TEXT)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT * FROM dw.validate_warehouse();
$$;

-- Refresh every olap materialized view WITHOUT blocking readers
-- (CONCURRENTLY; each view has the required unique index). Used by the admin
-- page. Harmless: it only recomputes results from data already in the warehouse.
CREATE OR REPLACE FUNCTION app.refresh_reporting()
RETURNS TABLE (materialized_view TEXT, seconds NUMERIC)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_mv     RECORD;
    v_start  TIMESTAMPTZ;
BEGIN
    FOR v_mv IN SELECT m.schemaname, m.matviewname
                FROM   pg_matviews AS m
                WHERE  m.schemaname = 'olap'
                ORDER  BY m.matviewname
    LOOP
        v_start := clock_timestamp();
        EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %I.%I', v_mv.schemaname, v_mv.matviewname);
        materialized_view := v_mv.schemaname || '.' || v_mv.matviewname;
        seconds := round(extract(epoch FROM clock_timestamp() - v_start)::numeric, 3);
        RETURN NEXT;
    END LOOP;
END
$$;


/* -----------------------------------------------------------------------------
   Part D. The dashboard's database role (least privilege).
   ----------------------------------------------------------------------------- */
-- PostgreSQL has no CREATE ROLE IF NOT EXISTS: same \gexec pattern as script 01.
SELECT 'CREATE ROLE churn_dashboard LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'churn_dashboard')
\gexec

-- The password is stored by PostgreSQL as a SCRAM-SHA-256 hash.
ALTER ROLE churn_dashboard WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      CONNECTION LIMIT 20 PASSWORD :'dashboard_db_password';
ALTER ROLE churn_dashboard SET search_path = olap, app;
ALTER ROLE churn_dashboard SET statement_timeout = '30s';   -- protect the server from runaway queries
COMMENT ON ROLE churn_dashboard IS 'Login role of the Streamlit dashboard: read-only on olap, a few app functions.';

-- Only explicitly allowed roles may connect to this database.
REVOKE CONNECT ON DATABASE telco_churn_dw FROM PUBLIC;
GRANT  CONNECT ON DATABASE telco_churn_dw TO churn_dashboard;

-- olap: read everything that exists now, and everything created later by
-- postgres (scripts 06-08 drop and recreate views; default privileges cover them).
GRANT USAGE  ON SCHEMA olap TO churn_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA olap TO churn_dashboard;   -- tables, views and materialized views
ALTER DEFAULT PRIVILEGES IN SCHEMA olap GRANT SELECT ON TABLES TO churn_dashboard;

-- app: may call the listed functions only; may NOT read the tables.
-- (Functions are executable by PUBLIC by default in PostgreSQL, so revoke first.)
GRANT  USAGE ON SCHEMA app TO churn_dashboard;
REVOKE ALL ON ALL TABLES    IN SCHEMA app FROM PUBLIC, churn_dashboard;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.check_login(TEXT, TEXT),
                          app.list_users(),
                          app.recent_logins(INTEGER),
                          app.etl_runs(INTEGER),
                          app.data_quality(),
                          app.refresh_reporting()
      TO churn_dashboard;
-- staging and dw: nothing is granted, so the role cannot even see inside them.


/* -----------------------------------------------------------------------------
   Part E. The two dashboard accounts.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Dashboard accounts ==='
SELECT app.set_user('admin',   'Dashboard administrator', 'admin',   :'admin_password')   AS result
UNION ALL
SELECT app.set_user('analyst', 'Churn analyst',           'analyst', :'analyst_password');

SELECT username, full_name, user_role, is_active,
       left(password_hash, 7) || '...' AS hash_starts_with     -- $2a$10$ = bcrypt, cost 10
FROM   app.dashboard_user
ORDER  BY username;


/* -----------------------------------------------------------------------------
   Part F. Checks (PASS / FAIL).
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Phase 8 security checks ==='
WITH checks (check_no, check_name, expected, actual) AS (
    VALUES
    (1,  'pgcrypto extension installed', 'yes',
         CASE WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto') THEN 'yes' ELSE 'no' END),
    (2,  'Active dashboard accounts', '2',
         (SELECT count(*) FROM app.dashboard_user WHERE is_active)::text),
    (3,  'Passwords stored as bcrypt hashes (not plain text)', '2',
         (SELECT count(*) FROM app.dashboard_user WHERE password_hash LIKE '$2a$10$%')::text),
    (4,  'Correct admin password is accepted (role returned)', 'admin',
         (SELECT app_role FROM app.check_login('admin', :'admin_password'))),
    (5,  'Correct analyst password is accepted (role returned)', 'analyst',
         (SELECT app_role FROM app.check_login('analyst', :'analyst_password'))),
    (6,  'Wrong password is rejected (rows returned)', '0',
         (SELECT count(*) FROM app.check_login('selftest_nobody', 'wrong-password'))::text),
    (7,  'Dashboard role can read olap materialized views', 'true',
         has_table_privilege('churn_dashboard', 'olap.mv_kpi_overview', 'SELECT')::text),
    (8,  'Dashboard role can read mining results', 'true',
         has_table_privilege('churn_dashboard', 'olap.customer_churn_score', 'SELECT')::text),
    (9,  'Dashboard role can NOT change mining results', 'false',
         has_table_privilege('churn_dashboard', 'olap.mining_run', 'INSERT, UPDATE, DELETE')::text),
    (10, 'Dashboard role can NOT use schema dw', 'false',
         has_schema_privilege('churn_dashboard', 'dw', 'USAGE')::text),
    (11, 'Dashboard role can NOT use schema staging', 'false',
         has_schema_privilege('churn_dashboard', 'staging', 'USAGE')::text),
    (12, 'Dashboard role can NOT read app.dashboard_user', 'false',
         has_table_privilege('churn_dashboard', 'app.dashboard_user', 'SELECT')::text),
    (13, 'Dashboard role can call app.check_login()', 'true',
         has_function_privilege('churn_dashboard', 'app.check_login(text, text)', 'EXECUTE')::text),
    (14, 'Dashboard role can NOT call app.set_user()', 'false',
         has_function_privilege('churn_dashboard', 'app.set_user(text, text, text, text)', 'EXECUTE')::text),
    (15, 'Dashboard role is not a superuser', 'false',
         (SELECT rolsuper FROM pg_roles WHERE rolname = 'churn_dashboard')::text)
)
SELECT check_no, check_name, expected, actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   checks
ORDER  BY check_no;


/* -----------------------------------------------------------------------------
   Part G. Live demonstration: act as the dashboard role.
   SET ROLE switches this session to churn_dashboard's privileges. The first
   query works; the second is refused by PostgreSQL. ON_ERROR_STOP is switched
   off for a moment so the expected error does not stop the script.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Acting as churn_dashboard: reading olap is allowed ==='
SET ROLE churn_dashboard;
SELECT customers, churn_rate_pct, monthly_revenue_lost FROM olap.mv_kpi_overview;

\echo '=== Acting as churn_dashboard: reading dw must be refused (an ERROR is EXPECTED here) ==='
\set ON_ERROR_STOP off
SELECT count(*) FROM dw.fact_customer_churn;
\set ON_ERROR_STOP on
RESET ROLE;

\echo ''
\echo 'Next: put the churn_dashboard password into .streamlit/secrets.toml and run:'
\echo '      python -m streamlit run dashboard/app.py'
