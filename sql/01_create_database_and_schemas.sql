/* =============================================================================
   01_create_database_and_schemas.sql
   -----------------------------------------------------------------------------
   Project : Development of a Data Warehouse and Application of Data Mining
             Techniques for Customer Churn Analysis in the Telecommunications
             Sector
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16)
   Purpose : Create the database telco_churn_dw and its three layers (schemas)

                 staging  ->  dw  ->  olap
                 (raw CSV)    (star    (reporting views and
                               schema)  materialized views)

   How to run (from the project folder, as the postgres superuser):
       psql -U postgres -h localhost -p 5432 -d postgres -f sql/01_create_database_and_schemas.sql

   The script connects to the default "postgres" database first, because a
   database cannot be created while you are connected to it.

   Re-runnable : yes. Every statement checks whether the object already exists,
                 so running the script twice does no harm.

   Note: lines that start with a backslash (\set, \gexec, \connect, \echo) are
   psql meta-commands. They work in psql and in pgAdmin's "PSQL Tool", but NOT
   in pgAdmin's ordinary "Query Tool".
   ============================================================================= */

-- Stop at the first error instead of carrying on with a half-built database.
\set ON_ERROR_STOP on


/* -----------------------------------------------------------------------------
   Step 0. Check the server version.
   We use features that need PostgreSQL 15+ (and 13+ for DROP ... WITH (FORCE)).
   server_version_num is a number such as 160015 (= 16.15).
   MySQL equivalent: SELECT VERSION();  (returns text such as '8.0.36')
   ----------------------------------------------------------------------------- */
DO $$
BEGIN
    IF current_setting('server_version_num')::int < 150000 THEN
        RAISE EXCEPTION 'PostgreSQL 15 or later is required; this server is %',
                        current_setting('server_version');
    END IF;
    RAISE NOTICE 'Server version % - OK', current_setting('server_version');
END
$$;


/* -----------------------------------------------------------------------------
   Optional full reset (normally left commented out).
   Removes the whole warehouse so that you can rebuild it from scratch.
   WITH (FORCE) first disconnects anyone still connected (e.g. pgAdmin).
   MySQL equivalent: DROP DATABASE IF EXISTS telco_churn_dw;  (no FORCE option)
   ----------------------------------------------------------------------------- */
-- DROP DATABASE IF EXISTS telco_churn_dw WITH (FORCE);


/* -----------------------------------------------------------------------------
   Step 1. Create the database (only if it does not exist yet).

   PostgreSQL has no "CREATE DATABASE IF NOT EXISTS" (MySQL does), and
   CREATE DATABASE cannot run inside a DO block or a function. So we use a
   common psql pattern: the SELECT below returns the CREATE DATABASE command as
   text only when the database is missing, and \gexec then executes that text.
   If the database already exists, the SELECT returns no rows and nothing runs.

   ENCODING 'UTF8'      : store text as Unicode (MySQL: CHARACTER SET utf8mb4).
   TEMPLATE template0   : copy the clean, built-in template database. This is
                          required when you state an encoding explicitly, and it
                          avoids inheriting anything that was added to template1.
   ----------------------------------------------------------------------------- */
SELECT 'CREATE DATABASE telco_churn_dw
            WITH ENCODING = ''UTF8''
                 TEMPLATE = template0'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'telco_churn_dw')
\gexec


/* -----------------------------------------------------------------------------
   Step 2. Connect to the new database.
   MySQL: USE telco_churn_dw;   PostgreSQL (psql): \connect telco_churn_dw
   In PostgreSQL this opens a NEW connection. One connection can only see the
   objects of one database, but it can see every schema inside that database,
   which is why our three layers are schemas and not three separate databases.
   ----------------------------------------------------------------------------- */
\connect telco_churn_dw

COMMENT ON DATABASE telco_churn_dw IS
    'Customer-churn data warehouse built from the IBM Telco Customer Churn sample (7,043 customers).';


/* -----------------------------------------------------------------------------
   Step 3. Create the three schemas (layers).
   In MySQL "schema" and "database" mean the same thing. In PostgreSQL a schema
   is a named folder INSIDE a database, so tables in different schemas can be
   joined in one query (for example staging.telco_raw JOIN dw.dim_customer).
   ----------------------------------------------------------------------------- */
CREATE SCHEMA IF NOT EXISTS staging;
COMMENT ON SCHEMA staging IS
    'Layer 1 - landing area: an untouched copy of Telco-Customer-Churn.csv, every column stored as TEXT.';

CREATE SCHEMA IF NOT EXISTS dw;
COMMENT ON SCHEMA dw IS
    'Layer 2 - cleaned and typed star schema (dimension tables and the fact table) plus the ETL procedure.';

CREATE SCHEMA IF NOT EXISTS olap;
COMMENT ON SCHEMA olap IS
    'Layer 3 - reporting layer: views and materialized views used for OLAP analysis, data mining and the dashboard.';


/* -----------------------------------------------------------------------------
   Step 4. Install the extensions we need later.
   tablefunc provides crosstab(), used for the pivot operation in 06_olap_queries.sql.
   It ships with the standard PostgreSQL installer (it is a "contrib" module).
   MySQL has no extension mechanism and no crosstab() function.
   ----------------------------------------------------------------------------- */
CREATE EXTENSION IF NOT EXISTS tablefunc WITH SCHEMA public;


/* -----------------------------------------------------------------------------
   Step 5. Default search path for people who query the warehouse interactively.
   With this setting, "SELECT * FROM fact_customer_churn" finds dw.fact_customer_churn
   without typing the schema name. The staging schema is left out on purpose, so
   that analysts do not query raw data by accident.
   Our scripts always write schema.table in full, so they never depend on this.
   It takes effect for NEW connections only.
   ----------------------------------------------------------------------------- */
ALTER DATABASE telco_churn_dw SET search_path = dw, olap, public;


/* -----------------------------------------------------------------------------
   Step 6. Show what was created and check it (PASS / FAIL).
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Schemas in telco_churn_dw ==='
SELECT n.nspname                           AS schema_name,
       pg_get_userbyid(n.nspowner)         AS owner,
       obj_description(n.oid, 'pg_namespace') AS description
FROM   pg_namespace AS n
WHERE  n.nspname IN ('staging', 'dw', 'olap')
ORDER  BY array_position(ARRAY['staging', 'dw', 'olap'], n.nspname::text);

\echo '=== Phase 1 checks ==='
WITH checks (check_no, check_name, expected, actual) AS (
    VALUES
    (1, 'Connected to telco_churn_dw',
        'telco_churn_dw', current_database()::text),
    (2, 'Database encoding',
        'UTF8', (SELECT pg_encoding_to_char(encoding)
                   FROM pg_database WHERE datname = current_database())::text),
    (3, 'Schemas staging, dw, olap exist',
        '3', (SELECT count(*) FROM pg_namespace
               WHERE nspname IN ('staging', 'dw', 'olap'))::text),
    (4, 'tablefunc extension installed',
        '1', (SELECT count(*) FROM pg_extension WHERE extname = 'tablefunc')::text),
    (5, 'crosstab(text, text) function available',
        'yes', CASE WHEN to_regprocedure('public.crosstab(text, text)') IS NOT NULL
                    THEN 'yes' ELSE 'no' END)
)
SELECT check_no,
       check_name,
       expected,
       actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   checks
ORDER  BY check_no;
