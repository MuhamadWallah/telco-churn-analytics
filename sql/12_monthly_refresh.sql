/* =============================================================================
   12_monthly_refresh.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Owners  : MuhammaD_Wallah and the GROUP are the owners and Developers of this model
   Target  : PostgreSQL 15 or later (Neon or local)
   Purpose : The database part of the monthly maintenance job
             (.github/workflows/monthly_refresh.yml):
               1. run every validation check (dw.validate_warehouse, script 05)
                  and STOP with an error if any check fails;
               2. show the headline counts;
               3. REFRESH every materialized view in the olap schema.
             The workflow then re-scores the customers with model/score.py.

   How to run by hand, connected as the OWNER of telco_churn_dw:
       psql "<owner connection string>" -f sql/12_monthly_refresh.sql
   Re-runnable: yes (it only checks and recomputes).

   If you ever load NEW data (new CSV -> staging -> CALL dw.run_etl();), update
   the expected numbers in sql/05_validation_checks.sql first, or step 1 will
   (correctly) stop the job.
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Connect to the database telco_churn_dw.', current_database();
    END IF;
END
$$;


/* -----------------------------------------------------------------------------
   1. Validation.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== 1. Validation checks per group ==='
SELECT check_group,
       count(*)                                  AS checks,
       count(*) FILTER (WHERE result = 'PASS')   AS passed,
       count(*) FILTER (WHERE result <> 'PASS')  AS failed
FROM   dw.validate_warehouse()
GROUP  BY check_group
ORDER  BY check_group;

DO $$
DECLARE
    v_failed  INTEGER;
    v_list    TEXT;
BEGIN
    SELECT count(*),
           string_agg(format('#%s %s (expected %s, got %s)', check_no, check_name, expected, actual),
                      '; ' ORDER BY check_no)
    INTO   v_failed, v_list
    FROM   dw.validate_warehouse()
    WHERE  result <> 'PASS';

    IF v_failed > 0 THEN
        RAISE EXCEPTION 'Warehouse validation failed (% checks): %', v_failed, v_list;
    END IF;
    RAISE NOTICE 'All validation checks passed.';
END
$$;


/* -----------------------------------------------------------------------------
   2. Headline counts (the numbers quoted in the report).
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== 2. Headline counts ==='
SELECT (SELECT count(*) FROM dw.fact_customer_churn)  AS fact_rows,
       (SELECT count(*) FROM dw.dim_contract)         AS dim_contract,
       (SELECT count(*) FROM dw.dim_service)          AS dim_service,
       (SELECT count(*) FROM dw.dim_payment)          AS dim_payment,
       (SELECT count(*) FROM dw.dim_churn)            AS dim_churn,
       (SELECT format('%s / %s',
                      count(*) FILTER (WHERE ch.churn_label = 'No'),
                      count(*) FILTER (WHERE ch.churn_label = 'Yes'))
          FROM dw.fact_customer_churn AS f
          JOIN dw.dim_churn AS ch ON ch.churn_key = f.churn_key) AS churn_no_yes;


/* -----------------------------------------------------------------------------
   3. Refresh the materialized views.
   CONCURRENTLY: the new result is built on the side, so the web app can keep
   reading the old one meanwhile (every view has the required unique index).
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== 3. Refresh the OLAP materialized views ==='
DO $$
DECLARE
    v_mv     RECORD;
    v_start  TIMESTAMPTZ;
BEGIN
    FOR v_mv IN SELECT schemaname, matviewname
                FROM   pg_matviews
                WHERE  schemaname = 'olap'
                ORDER  BY matviewname
    LOOP
        v_start := clock_timestamp();
        EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY %I.%I', v_mv.schemaname, v_mv.matviewname);
        RAISE NOTICE 'Refreshed %.% in % s', v_mv.schemaname, v_mv.matviewname,
                     round(extract(epoch FROM clock_timestamp() - v_start)::numeric, 2);
    END LOOP;
END
$$;

SELECT refreshed_at AS kpi_overview_refreshed_at, customers, churned, churn_rate_pct
FROM   olap.mv_kpi_overview;

\echo 'Done. Next: python model/score.py'
