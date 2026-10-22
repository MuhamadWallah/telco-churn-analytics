/* =============================================================================
   04_etl_procedure.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16 and 18)
   Purpose : ETL (Extract - Transform - Load) from staging into the star schema,
             wrapped in ONE stored procedure, dw.run_etl(), so the whole load can
             be repeated from scratch with a single command:

                 CALL dw.run_etl();

             The procedure performs ETL steps 3-5 of the project plan:
               Step 3  CLEAN & TRANSFORM  staging.telco_raw (all TEXT)
                                          -> staging.telco_clean (typed, cleaned)
               Step 4  LOAD DIMENSIONS    INSERT ... SELECT DISTINCT into the 6 dims
               Step 5  LOAD FACT TABLE    join telco_clean to every dimension to
                                          look up the surrogate keys
             and records every run in dw.etl_run_log.

   How to run (after 01, 02 and 03):
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/04_etl_procedure.sql
   This creates the procedure AND runs it once. Later reloads only need:
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -c "CALL dw.run_etl();"

   Requirement: CALL must NOT be inside BEGIN ... COMMIT, because the procedure
   commits by itself (psql and pgAdmin run each statement in auto-commit mode
   by default, so this only matters if you typed BEGIN yourself).
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Run this script with -d telco_churn_dw.',
                        current_database();
    END IF;
    IF to_regclass('dw.fact_customer_churn') IS NULL THEN
        RAISE EXCEPTION 'The star schema does not exist yet. Run 03_star_schema_ddl.sql first.';
    END IF;
END
$$;


/* -----------------------------------------------------------------------------
   Part A. ETL run log (audit table).
   One row per CALL: when it started and finished, whether it succeeded, how
   many rows each table received, and the error message if it failed.
   CREATE TABLE IF NOT EXISTS keeps the history when this script is re-run.
   ----------------------------------------------------------------------------- */
CREATE TABLE IF NOT EXISTS dw.etl_run_log (
    run_id         INTEGER GENERATED ALWAYS AS IDENTITY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at    TIMESTAMPTZ,
    status         TEXT        NOT NULL DEFAULT 'running',
    staging_rows   INTEGER,
    row_counts     JSONB,
    error_message  TEXT,
    run_by         TEXT        NOT NULL DEFAULT current_user,
    CONSTRAINT pk_etl_run_log        PRIMARY KEY (run_id),
    CONSTRAINT ck_etl_run_log_status CHECK (status IN ('running', 'succeeded', 'failed'))
);
COMMENT ON TABLE dw.etl_run_log IS
    'Audit log: one row per CALL dw.run_etl() with timing, status, row counts per table and any error.';


/* -----------------------------------------------------------------------------
   Part B. staging.telco_clean - the cleaned, correctly typed copy of the data.

   This is the result of ETL step 3. Keeping it as a real table (instead of a
   hidden temporary result) lets you inspect every transformation, e.g.
       SELECT * FROM staging.telco_clean WHERE total_charges_was_blank;
   It has no constraints: the rules are enforced when the data enters dw.
   It is rebuilt on every run of dw.run_etl().
   ----------------------------------------------------------------------------- */
DROP TABLE IF EXISTS staging.telco_clean;

CREATE UNLOGGED TABLE staging.telco_clean (
    source_row_no            INTEGER,         -- = staging.telco_raw.row_no (traceability)
    customer_id              TEXT,
    gender                   TEXT,
    senior_citizen           BOOLEAN,         -- '1'/'0'   -> true/false
    partner                  BOOLEAN,         -- 'Yes'/'No' -> true/false
    dependents               BOOLEAN,
    tenure                   SMALLINT,
    phone_service            BOOLEAN,
    multiple_lines           TEXT,
    internet_service         TEXT,
    online_security          TEXT,
    online_backup            TEXT,
    device_protection        TEXT,
    tech_support             TEXT,
    streaming_tv             TEXT,
    streaming_movies         TEXT,
    contract_type            TEXT,            -- CSV "Contract"
    paperless_billing        BOOLEAN,
    payment_method           TEXT,
    monthly_charges          NUMERIC(8,2),
    total_charges            NUMERIC(10,2),   -- blank -> 0.00 (design decision A)
    total_charges_was_blank  BOOLEAN,         -- audit flag: true for the 11 changed rows
    churn_label              TEXT,            -- 'Yes' / 'No'
    churn_flag               SMALLINT         -- 1 / 0
);
COMMENT ON TABLE staging.telco_clean IS
    'ETL step 3 output: telco_raw after type conversion and cleaning. Rebuilt by dw.run_etl().';


/* -----------------------------------------------------------------------------
   Part C. The ETL procedure.

   Why a PROCEDURE and not a FUNCTION?
     A PostgreSQL procedure (CREATE PROCEDURE, run with CALL) may COMMIT inside
     its body; a function may not. We use that to save the "running" log row
     BEFORE the load starts, so even a failed run leaves a trace in the log.

   All-or-nothing load:
     Steps 3-5 run inside a BEGIN ... EXCEPTION ... END block. If ANY statement
     fails (a bad value, a broken rule, a missing dimension row), PostgreSQL
     rolls back everything done inside the block - including the TRUNCATEs, which
     are transactional in PostgreSQL - so the previous, good warehouse is kept.
     The failure is written to the log and the error is re-raised to the caller.

   MySQL differences:
     - MySQL needs DELIMITER // ... // around a procedure body; PostgreSQL wraps
       the body in dollar quotes $$ ... $$ instead.
     - MySQL: DECLARE EXIT HANDLER FOR SQLEXCEPTION ...
       PostgreSQL: BEGIN ... EXCEPTION WHEN OTHERS THEN ... END;
     - MySQL: SELECT ROW_COUNT();   PostgreSQL: GET DIAGNOSTICS v = ROW_COUNT;
     - MySQL: SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '...';
       PostgreSQL: RAISE EXCEPTION '...';   (RAISE NOTICE prints progress)
     - MySQL TRUNCATE commits immediately and cannot be rolled back;
       PostgreSQL TRUNCATE can be rolled back.
   ----------------------------------------------------------------------------- */
CREATE OR REPLACE PROCEDURE dw.run_etl(p_refresh_olap BOOLEAN DEFAULT true)
LANGUAGE plpgsql
AS $$
DECLARE
    v_run_id        INTEGER;
    v_staging_rows  INTEGER;
    v_rows          INTEGER;
    v_counts        JSONB := '{}'::jsonb;
    v_error         TEXT;
    v_sqlstate      TEXT;
    v_detail        TEXT;
    v_bad_rows      INTEGER;
    v_bad_list      TEXT;
    v_mv            RECORD;
BEGIN
    -- ---------------------------------------------------------------------
    -- Log the start of the run and COMMIT, so the log row survives a failure.
    -- ---------------------------------------------------------------------
    SELECT count(*) INTO v_staging_rows FROM staging.telco_raw;

    INSERT INTO dw.etl_run_log (staging_rows)
    VALUES (v_staging_rows)
    RETURNING run_id INTO v_run_id;
    COMMIT;

    RAISE NOTICE 'ETL run % started: % rows in staging.telco_raw', v_run_id, v_staging_rows;

    BEGIN   -- ===== all-or-nothing block =====

        IF v_staging_rows = 0 THEN
            RAISE EXCEPTION 'staging.telco_raw is empty - run 02_staging_load.sql first';
        END IF;

        -- =================================================================
        -- STEP 3. CLEAN AND TRANSFORM  (telco_raw -> telco_clean)
        --   * btrim() removes spaces around every value (label tidying);
        --     the labels themselves are already consistent (Phase 2 profile).
        --   * '1'/'0' and 'Yes'/'No' -> BOOLEAN with a ::boolean cast, because
        --     PostgreSQL accepts both spellings as boolean input. The gate in
        --     3a has already stopped the load if any other value (e.g. 'Maybe')
        --     is present, so nothing is ever guessed.
        --     (MySQL has no real boolean: you would write IF(x = 'Yes', 1, 0).)
        --   * tenure / charges: text -> SMALLINT / NUMERIC with ::type casts
        --     (MySQL: CAST(x AS DECIMAL(10,2))).
        --   * TotalCharges: blank -> NULL (NULLIF) -> 0.00 (COALESCE), because a
        --     customer with tenure 0 has not received a bill yet.
        --   * Churn: label kept as 'Yes'/'No', plus a numeric flag 1/0.
        --     An unknown label gives NULL here and is rejected later by the
        --     NOT NULL rule of dim_churn.
        -- =================================================================
        -- 3a. Gate: refuse to convert if any raw value is not in the expected
        --     format. This gives a clear message with the CSV row numbers,
        --     instead of a cast error that does not say which row was wrong.
        --     "(...) IS NOT TRUE" also catches NULLs.
        SELECT count(*),
               string_agg(row_no::text, ', ' ORDER BY row_no) FILTER (WHERE rn <= 10)
        INTO   v_bad_rows, v_bad_list
        FROM  (SELECT r.row_no, row_number() OVER (ORDER BY r.row_no) AS rn
               FROM   staging.telco_raw AS r
               WHERE  (    btrim(r.customer_id)       <> ''
                       AND btrim(r.senior_citizen)    IN ('0', '1')
                       AND btrim(r.partner)           IN ('Yes', 'No')
                       AND btrim(r.dependents)        IN ('Yes', 'No')
                       AND btrim(r.phone_service)     IN ('Yes', 'No')
                       AND btrim(r.paperless_billing) IN ('Yes', 'No')
                       AND btrim(r.churn)             IN ('Yes', 'No')
                       AND btrim(r.tenure)            ~ '^[0-9]+$'
                       AND btrim(r.monthly_charges)   ~ '^[0-9]+(\.[0-9]+)?$'
                       AND (btrim(r.total_charges) = ''
                            OR btrim(r.total_charges) ~ '^[0-9]+(\.[0-9]+)?$')
                      ) IS NOT TRUE
              ) AS bad;

        IF v_bad_rows > 0 THEN
            RAISE EXCEPTION '% staging row(s) have values that cannot be converted', v_bad_rows
                  USING DETAIL = 'First bad rows (staging.telco_raw.row_no): ' || v_bad_list;
        END IF;

        -- 3b. Convert and clean.
        TRUNCATE staging.telco_clean;

        INSERT INTO staging.telco_clean (
            source_row_no, customer_id, gender, senior_citizen, partner, dependents,
            tenure, phone_service, multiple_lines, internet_service,
            online_security, online_backup, device_protection, tech_support,
            streaming_tv, streaming_movies, contract_type, paperless_billing,
            payment_method, monthly_charges, total_charges, total_charges_was_blank,
            churn_label, churn_flag)
        SELECT r.row_no,
               btrim(r.customer_id),
               btrim(r.gender),
               btrim(r.senior_citizen)::boolean,
               btrim(r.partner)::boolean,
               btrim(r.dependents)::boolean,
               btrim(r.tenure)::smallint,
               btrim(r.phone_service)::boolean,
               btrim(r.multiple_lines),
               btrim(r.internet_service),
               btrim(r.online_security),
               btrim(r.online_backup),
               btrim(r.device_protection),
               btrim(r.tech_support),
               btrim(r.streaming_tv),
               btrim(r.streaming_movies),
               btrim(r.contract),
               btrim(r.paperless_billing)::boolean,
               btrim(r.payment_method),
               btrim(r.monthly_charges)::numeric(8,2),
               COALESCE(NULLIF(btrim(r.total_charges), '')::numeric(10,2), 0.00),
               btrim(r.total_charges) = '',
               btrim(r.churn),
               CASE btrim(r.churn) WHEN 'Yes' THEN 1 WHEN 'No' THEN 0 END
        FROM   staging.telco_raw AS r;

        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object('staging.telco_clean', v_rows);
        RAISE NOTICE 'Step 3  staging.telco_clean     % rows', v_rows;

        -- =================================================================
        -- Empty the warehouse. One TRUNCATE for all seven tables (the fact
        -- table references the dimensions, so they must be emptied together).
        -- RESTART IDENTITY makes every surrogate key start again at 1, so a
        -- re-run produces exactly the same keys.
        -- =================================================================
        TRUNCATE dw.fact_customer_churn,
                 dw.dim_customer, dw.dim_contract, dw.dim_service,
                 dw.dim_payment,  dw.dim_churn,    dw.dim_tenure
                 RESTART IDENTITY;

        -- =================================================================
        -- STEP 4. LOAD THE DIMENSIONS with INSERT ... SELECT DISTINCT.
        -- DISTINCT keeps one row per different value / combination. ORDER BY
        -- fixes the order in which surrogate keys are handed out, so the keys
        -- are the same on every run.
        -- =================================================================

        -- 4.1 dim_customer: one row per customer (natural key customer_id)
        INSERT INTO dw.dim_customer (customer_id, gender, senior_citizen, partner, dependents)
        SELECT DISTINCT customer_id, gender, senior_citizen, partner, dependents
        FROM   staging.telco_clean
        ORDER  BY customer_id;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object('dw.dim_customer', v_rows);
        RAISE NOTICE 'Step 4  dw.dim_customer         % rows', v_rows;

        -- 4.2 dim_contract: each contract type x paperless billing combination
        INSERT INTO dw.dim_contract (contract_type, paperless_billing)
        SELECT DISTINCT contract_type, paperless_billing
        FROM   staging.telco_clean
        ORDER  BY contract_type, paperless_billing;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object('dw.dim_contract', v_rows);
        RAISE NOTICE 'Step 4  dw.dim_contract         % rows', v_rows;

        -- 4.3 dim_service (junk dimension): each combination that occurs
        INSERT INTO dw.dim_service (phone_service, multiple_lines, internet_service,
                                    online_security, online_backup, device_protection,
                                    tech_support, streaming_tv, streaming_movies)
        SELECT DISTINCT phone_service, multiple_lines, internet_service,
                        online_security, online_backup, device_protection,
                        tech_support, streaming_tv, streaming_movies
        FROM   staging.telco_clean
        ORDER  BY phone_service, multiple_lines, internet_service,
                  online_security, online_backup, device_protection,
                  tech_support, streaming_tv, streaming_movies;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object('dw.dim_service', v_rows);
        RAISE NOTICE 'Step 4  dw.dim_service          % rows', v_rows;

        -- 4.4 dim_payment
        INSERT INTO dw.dim_payment (payment_method)
        SELECT DISTINCT payment_method
        FROM   staging.telco_clean
        ORDER  BY payment_method;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object('dw.dim_payment', v_rows);
        RAISE NOTICE 'Step 4  dw.dim_payment          % rows', v_rows;

        -- 4.5 dim_churn (No = key 1, Yes = key 2)
        INSERT INTO dw.dim_churn (churn_label, churn_flag)
        SELECT DISTINCT churn_label, churn_flag
        FROM   staging.telco_clean
        ORDER  BY churn_flag;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object('dw.dim_churn', v_rows);
        RAISE NOTICE 'Step 4  dw.dim_churn            % rows', v_rows;

        -- 4.6 dim_tenure: generated with generate_series(0, 72), NOT from the
        --     data, so the hierarchy is complete even for a month in which no
        --     customer happens to fall. Only tenure_months is inserted; band and
        --     group are calculated by the generated columns.
        --     (MySQL 8 would need a recursive CTE to produce 0..72.)
        INSERT INTO dw.dim_tenure (tenure_months)
        SELECT m
        FROM   generate_series(0, 72) AS g(m)
        ORDER  BY m;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object('dw.dim_tenure', v_rows);
        RAISE NOTICE 'Step 4  dw.dim_tenure           % rows', v_rows;

        -- =================================================================
        -- STEP 5. LOAD THE FACT TABLE.
        -- Each cleaned row is joined to every dimension on its natural
        -- attributes to find the surrogate keys.
        --
        -- LEFT JOIN (not INNER JOIN) on purpose: if a row found no matching
        -- dimension row, an INNER JOIN would silently DROP that customer.
        -- With LEFT JOIN the missing key is NULL and the NOT NULL rule of the
        -- fact table stops the load with an error instead.
        --
        -- dim_service is matched on all nine columns at once with a row
        -- comparison: (a, b, c) = (x, y, z).
        -- =================================================================
        INSERT INTO dw.fact_customer_churn (
            customer_key, contract_key, service_key, payment_key, churn_key, tenure_key,
            tenure, monthly_charges, total_charges, customer_count)
        SELECT cu.customer_key,
               co.contract_key,
               se.service_key,
               pa.payment_key,
               ch.churn_key,
               te.tenure_key,
               c.tenure,
               c.monthly_charges,
               c.total_charges,
               1
        FROM   staging.telco_clean AS c
        LEFT   JOIN dw.dim_customer AS cu
               ON cu.customer_id = c.customer_id
        LEFT   JOIN dw.dim_contract AS co
               ON  co.contract_type     = c.contract_type
               AND co.paperless_billing = c.paperless_billing
        LEFT   JOIN dw.dim_service AS se
               ON (se.phone_service, se.multiple_lines, se.internet_service,
                   se.online_security, se.online_backup, se.device_protection,
                   se.tech_support, se.streaming_tv, se.streaming_movies)
                = (c.phone_service, c.multiple_lines, c.internet_service,
                   c.online_security, c.online_backup, c.device_protection,
                   c.tech_support, c.streaming_tv, c.streaming_movies)
        LEFT   JOIN dw.dim_payment AS pa
               ON pa.payment_method = c.payment_method
        LEFT   JOIN dw.dim_churn AS ch
               ON ch.churn_label = c.churn_label
        LEFT   JOIN dw.dim_tenure AS te
               ON te.tenure_months = c.tenure
        ORDER  BY cu.customer_key;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_counts := v_counts || jsonb_build_object('dw.fact_customer_churn', v_rows);
        RAISE NOTICE 'Step 5  dw.fact_customer_churn  % rows', v_rows;

        -- Reconciliation: every staging row must have become exactly one fact row.
        IF v_rows <> v_staging_rows THEN
            RAISE EXCEPTION 'Fact rows (%) <> staging rows (%)', v_rows, v_staging_rows;
        END IF;

        -- Fresh statistics so the query planner knows the new table sizes.
        ANALYZE dw.dim_customer, dw.dim_contract, dw.dim_service, dw.dim_payment,
                dw.dim_churn, dw.dim_tenure, dw.fact_customer_churn;

        -- Refresh the reporting layer (materialized views in olap, created in
        -- Phase 6). If none exist yet, this loop simply does nothing.
        IF p_refresh_olap THEN
            FOR v_mv IN
                SELECT schemaname, matviewname
                FROM   pg_matviews
                WHERE  schemaname = 'olap'
                ORDER  BY matviewname
            LOOP
                EXECUTE format('REFRESH MATERIALIZED VIEW %I.%I', v_mv.schemaname, v_mv.matviewname);
                RAISE NOTICE 'Refreshed %.%', v_mv.schemaname, v_mv.matviewname;
            END LOOP;
        END IF;

    EXCEPTION WHEN OTHERS THEN
        -- Everything inside the block has been rolled back at this point.
        v_error    := SQLERRM;
        v_sqlstate := SQLSTATE;
        GET STACKED DIAGNOSTICS v_detail = PG_EXCEPTION_DETAIL;   -- e.g. the failing row
    END;    -- ===== end of all-or-nothing block =====

    IF v_error IS NOT NULL THEN
        UPDATE dw.etl_run_log
        SET    status        = 'failed',
               finished_at   = clock_timestamp(),
               error_message = concat_ws(' | ', v_error, NULLIF(v_detail, ''))
        WHERE  run_id = v_run_id;
        COMMIT;   -- keep the 'failed' log entry
        RAISE EXCEPTION 'ETL run % failed: %', v_run_id, v_error
              USING ERRCODE = v_sqlstate,
                    DETAIL  = coalesce(NULLIF(v_detail, ''), 'See dw.etl_run_log, run_id ' || v_run_id),
                    HINT    = 'Nothing was changed: the warehouse still holds the data of the last successful run.';
    END IF;

    UPDATE dw.etl_run_log
    SET    status = 'succeeded', finished_at = clock_timestamp(), row_counts = v_counts
    WHERE  run_id = v_run_id;
    COMMIT;

    RAISE NOTICE 'ETL run % succeeded', v_run_id;
END
$$;

COMMENT ON PROCEDURE dw.run_etl(BOOLEAN) IS
    'Rebuilds the star schema from staging.telco_raw: clean -> dimensions -> fact (all-or-nothing), '
    'refreshes olap materialized views, and logs the run in dw.etl_run_log. Usage: CALL dw.run_etl();';


/* -----------------------------------------------------------------------------
   Part D. Run the ETL once.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Running CALL dw.run_etl() ==='
CALL dw.run_etl();


/* -----------------------------------------------------------------------------
   Part E. Show the result.
   (The full PASS/FAIL validation is in 05_validation_checks.sql.)
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Last ETL runs (dw.etl_run_log) ==='
SELECT run_id,
       status,
       to_char(started_at, 'YYYY-MM-DD HH24:MI:SS')                       AS started,
       round(extract(epoch FROM finished_at - started_at)::numeric, 2)    AS seconds,
       staging_rows,
       error_message
FROM   dw.etl_run_log
ORDER  BY run_id DESC
LIMIT  5;

\echo '=== Rows per table after the load ==='
SELECT t.table_name, t.expected_rows, t.actual_rows,
       CASE WHEN t.expected_rows = t.actual_rows THEN 'PASS' ELSE 'FAIL' END AS result
FROM (VALUES
        (1, 'staging.telco_clean',    7043, (SELECT count(*) FROM staging.telco_clean)),
        (2, 'dw.dim_customer',        7043, (SELECT count(*) FROM dw.dim_customer)),
        (3, 'dw.dim_contract',           6, (SELECT count(*) FROM dw.dim_contract)),
        (4, 'dw.dim_service',          322, (SELECT count(*) FROM dw.dim_service)),
        (5, 'dw.dim_payment',            4, (SELECT count(*) FROM dw.dim_payment)),
        (6, 'dw.dim_churn',              2, (SELECT count(*) FROM dw.dim_churn)),
        (7, 'dw.dim_tenure',            73, (SELECT count(*) FROM dw.dim_tenure)),
        (8, 'dw.fact_customer_churn', 7043, (SELECT count(*) FROM dw.fact_customer_churn))
     ) AS t (n, table_name, expected_rows, actual_rows)
ORDER BY t.n;

\echo '=== Example transformation: the first 3 CSV rows, raw text vs cleaned values ==='
SELECT r.row_no, r.customer_id,
       r.senior_citizen AS raw_senior,  c.senior_citizen,
       r.partner        AS raw_partner, c.partner,
       r.tenure         AS raw_tenure,  c.tenure,
       r.total_charges  AS raw_total,   c.total_charges,
       r.churn          AS raw_churn,   c.churn_flag
FROM   staging.telco_raw   AS r
JOIN   staging.telco_clean AS c ON c.source_row_no = r.row_no
ORDER  BY r.row_no
LIMIT  3;

\echo '=== The 11 customers whose blank TotalCharges became 0.00 ==='
SELECT c.source_row_no, c.customer_id, c.tenure, c.monthly_charges,
       c.total_charges, f.total_charges AS total_in_fact
FROM   staging.telco_clean AS c
JOIN   dw.dim_customer        AS d ON d.customer_id  = c.customer_id
JOIN   dw.fact_customer_churn AS f ON f.customer_key = d.customer_key
WHERE  c.total_charges_was_blank
ORDER  BY c.source_row_no;

\echo '=== The small dimensions and their surrogate keys ==='
SELECT 'dim_contract' AS dimension, contract_key AS key,
       contract_type || ' / paperless=' || paperless_billing AS value
FROM dw.dim_contract
UNION ALL
SELECT 'dim_payment', payment_key, payment_method FROM dw.dim_payment
UNION ALL
SELECT 'dim_churn', churn_key, churn_label || ' (flag ' || churn_flag || ')' FROM dw.dim_churn
ORDER BY 1, 2;
