/* =============================================================================
   11_app_reader_role.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Owners  : MuhammaD_Wallah and the GROUP are the owners and Developers of this model
   Target  : PostgreSQL 15 or later (Neon or local)
   Purpose : A READ-ONLY login role for the public web app: churn_app_reader

   Why the app must NOT use the owner account
     The web app is public: anyone with the link can use it, and its password
     sits on Streamlit's servers. If the app connected as the owner, a bug, a
     leaked secret or a crafted input could change or delete the warehouse.
     With churn_app_reader the worst case is that someone READS the reporting
     data, which the app shows anyway ("least privilege"). The owner password
     is used only on your PC and in GitHub Actions (for the monthly job).

   What the role may do
     - read everything in the olap schema (views, materialized views, results)
     - read dw.churn_scores and dw.model_runs
     - nothing else: no staging, no star-schema tables, no writes
     - every transaction is read-only, queries stop after 30 seconds

   How to run, connected as the OWNER of telco_churn_dw:
       psql "<owner connection string>" -f sql/11_app_reader_role.sql
   psql asks for the new role's password (or pass -v reader_password=...).
   Re-runnable: yes. Re-running sets the password again.

   On Neon
     - Create this role with SQL (this script), NOT in the Neon Console: roles
       made in the Console become members of neon_superuser and get broad rights.
     - Neon refuses weak passwords for roles created with SQL. Use a long random
       one (for example 24 letters and digits) and keep it only in the
       Streamlit secrets.
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Connect to the database telco_churn_dw.', current_database();
    END IF;
    IF to_regclass('dw.churn_scores') IS NULL THEN
        RAISE EXCEPTION 'dw.churn_scores does not exist. Run sql/10_cloud_ml_tables.sql first.';
    END IF;
END
$$;

-- Ask for the password unless it was given with -v reader_password=...
\if :{?reader_password}
\else
    \prompt 'Password for the new read-only role churn_app_reader: ' reader_password
\endif


/* -----------------------------------------------------------------------------
   1. The role. PostgreSQL has no CREATE ROLE IF NOT EXISTS, so (as in script
   09) a SELECT writes the command only when the role is missing and \gexec runs it.
   A new role is already NOSUPERUSER, NOCREATEDB and NOCREATEROLE; those
   attributes are not repeated here, because on Neon only a superuser may
   mention them in ALTER ROLE.
   ----------------------------------------------------------------------------- */
SELECT 'CREATE ROLE churn_app_reader LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'churn_app_reader')
\gexec

ALTER ROLE churn_app_reader WITH LOGIN CONNECTION LIMIT 10 PASSWORD :'reader_password';
ALTER ROLE churn_app_reader SET default_transaction_read_only = on;     -- refuses writes even by mistake
ALTER ROLE churn_app_reader SET statement_timeout = '30s';             -- no runaway queries
ALTER ROLE churn_app_reader SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE churn_app_reader SET search_path = olap, dw;
COMMENT ON ROLE churn_app_reader IS 'Login role of the public Streamlit app: read-only on olap, dw.churn_scores and dw.model_runs.';


/* -----------------------------------------------------------------------------
   2. Privileges (only what the app needs).
   ----------------------------------------------------------------------------- */
GRANT CONNECT ON DATABASE telco_churn_dw TO churn_app_reader;

-- olap: read everything now, and everything this owner creates later
-- (scripts 06-08 and 10 drop and re-create views; default privileges cover them).
GRANT USAGE  ON SCHEMA olap TO churn_app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA olap TO churn_app_reader;     -- tables, views, materialized views
ALTER DEFAULT PRIVILEGES IN SCHEMA olap GRANT SELECT ON TABLES TO churn_app_reader;

-- dw: only the two model tables. USAGE lets the role see the schema; it gives
-- no right on any table that is not named here.
GRANT USAGE  ON SCHEMA dw TO churn_app_reader;
GRANT SELECT ON dw.churn_scores, dw.model_runs TO churn_app_reader;
-- staging and the star-schema tables: nothing is granted.


/* -----------------------------------------------------------------------------
   3. Checks (PASS / FAIL).
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Read-only role checks ==='
WITH checks (check_no, check_name, expected, actual) AS (
    VALUES
    (1,  'Role churn_app_reader can log in', 'true',
         (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'churn_app_reader')::text),
    (2,  'Not superuser / no CREATEDB / no CREATEROLE', 'false / false / false',
         (SELECT format('%s / %s / %s', rolsuper::text, rolcreatedb::text, rolcreaterole::text)
            FROM pg_roles WHERE rolname = 'churn_app_reader')),
    (3,  'Transactions are read-only by default', 'yes',
         CASE WHEN EXISTS (SELECT 1 FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole
                            WHERE r.rolname = 'churn_app_reader'
                              AND 'default_transaction_read_only=on' = ANY (s.setconfig))
              THEN 'yes' ELSE 'no' END),
    (4,  'Can read olap.mv_kpi_overview', 'true',
         has_table_privilege('churn_app_reader', 'olap.mv_kpi_overview', 'SELECT')::text),
    (5,  'Can read olap.mv_churn_cube', 'true',
         has_table_privilege('churn_app_reader', 'olap.mv_churn_cube', 'SELECT')::text),
    (6,  'Can read olap.v_customer_risk', 'true',
         has_table_privilege('churn_app_reader', 'olap.v_customer_risk', 'SELECT')::text),
    (7,  'Can read dw.churn_scores', 'true',
         has_table_privilege('churn_app_reader', 'dw.churn_scores', 'SELECT')::text),
    (8,  'Can NOT change dw.churn_scores', 'false',
         has_table_privilege('churn_app_reader', 'dw.churn_scores', 'INSERT, UPDATE, DELETE, TRUNCATE')::text),
    (9,  'Can NOT read dw.fact_customer_churn', 'false',
         has_table_privilege('churn_app_reader', 'dw.fact_customer_churn', 'SELECT')::text),
    (10, 'Can NOT use schema staging', 'false',
         has_schema_privilege('churn_app_reader', 'staging', 'USAGE')::text),
    (11, 'Can NOT create objects in olap', 'false',
         has_schema_privilege('churn_app_reader', 'olap', 'CREATE')::text)
)
SELECT check_no, check_name, expected, actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   checks
ORDER  BY check_no;

\echo 'Next: build the app connection string with this role, for example'
\echo '  postgresql://churn_app_reader:PASSWORD@HOST/telco_churn_dw?sslmode=require'
\echo 'and put it ONLY in .streamlit/secrets.toml (local) and the Streamlit Cloud secrets.'
