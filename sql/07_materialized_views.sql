/* =============================================================================
   07_materialized_views.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16 and 18)
   Purpose : Save the most useful OLAP results as MATERIALIZED VIEWS in the
             olap schema, and show how to refresh them.

   View vs materialized view:
     - A VIEW (olap.v_customer_churn, script 06) stores only the query. Every
       time you select from it, PostgreSQL runs the joins and aggregations again.
     - A MATERIALIZED VIEW runs the query ONCE and stores the result rows on
       disk like a table. Reading it is instant (ideal for a dashboard), but
       the stored result does not change when the warehouse changes: it must be
       REFRESHED after every load. dw.run_etl() does that automatically.
     - MySQL has NO materialized views. The usual MySQL workaround is a summary
       table filled by INSERT ... SELECT and kept up to date by a scheduled EVENT
       or by triggers.

   Materialized views created here (all read from olap.v_customer_churn, never
   from each other, so they can be refreshed in any order):
       olap.mv_kpi_overview            1 row   headline KPIs for dashboard tiles
       olap.mv_churn_rollup           16 rows  ROLL-UP contract x payment -> totals
       olap.mv_tenure_drilldown       80 rows  DRILL-DOWN tenure group > band > month
       olap.mv_churn_cube            387 rows  CUBE contract x payment x internet x tenure band
       olap.mv_contract_churn_pivot    4 rows  PIVOT contract type x churn No/Yes
       olap.mv_segment_revenue_risk   36 rows  RANKING of segments by revenue lost
       olap.mv_addon_churn            18 rows  churn by add-on service and status

   Every materialized view gets a UNIQUE index. It makes look-ups fast and it is
   REQUIRED for REFRESH MATERIALIZED VIEW CONCURRENTLY (see Part C). To keep
   the unique columns free of NULLs, the subtotal rows use labels such as
   'All contracts' instead of NULL (made with GROUPING(), as in script 06).

   How to run (after 06, which creates olap.v_customer_churn):
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/07_materialized_views.sql
   Re-runnable: yes (each materialized view is dropped and recreated).
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Run this script with -d telco_churn_dw.',
                        current_database();
    END IF;
    IF to_regclass('olap.v_customer_churn') IS NULL THEN
        RAISE EXCEPTION 'olap.v_customer_churn does not exist. Run 06_olap_queries.sql first.';
    END IF;
END
$$;


/* -----------------------------------------------------------------------------
   Part A. Drop the previous versions (re-runnable script).
   ----------------------------------------------------------------------------- */
DROP MATERIALIZED VIEW IF EXISTS
    olap.mv_kpi_overview,
    olap.mv_churn_rollup,
    olap.mv_tenure_drilldown,
    olap.mv_churn_cube,
    olap.mv_contract_churn_pivot,
    olap.mv_segment_revenue_risk,
    olap.mv_addon_churn;


/* -----------------------------------------------------------------------------
   Part B. Create the materialized views.
   WITH DATA (the default) runs the query immediately and stores the rows.
   ----------------------------------------------------------------------------- */

-- B1. Headline KPIs (one row) - the numbers at the top of the dashboard.
--     refreshed_at records when the stored result was last computed.
CREATE MATERIALIZED VIEW olap.mv_kpi_overview AS
SELECT 1::smallint                                                          AS kpi_id,
       SUM(customer_count)                                                  AS customers,
       SUM(churn_flag)                                                      AS churned,
       SUM(customer_count) - SUM(churn_flag)                                AS retained,
       ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct,
       SUM(monthly_charges)                                                 AS monthly_revenue,
       COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)      AS monthly_revenue_lost,
       ROUND(100.0 * COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)
                   / SUM(monthly_charges), 2)                               AS monthly_revenue_lost_pct,
       ROUND(AVG(tenure), 1)                                                AS avg_tenure_months,
       ROUND(AVG(monthly_charges) FILTER (WHERE churn_flag = 1), 2)         AS avg_monthly_charge_churned,
       ROUND(AVG(monthly_charges) FILTER (WHERE churn_flag = 0), 2)         AS avg_monthly_charge_retained,
       now()                                                                AS refreshed_at
FROM   olap.v_customer_churn
WITH DATA;

CREATE UNIQUE INDEX ux_mv_kpi_overview ON olap.mv_kpi_overview (kpi_id);
COMMENT ON MATERIALIZED VIEW olap.mv_kpi_overview IS
    'Headline KPIs (1 row): customers, churn, monthly revenue and revenue lost, averages, refresh time.';


-- B2. ROLL-UP: contract type x payment method -> contract subtotal -> grand total.
CREATE MATERIALIZED VIEW olap.mv_churn_rollup AS
SELECT GROUPING(contract_type, payment_method)                              AS grouping_id,
       CASE GROUPING(contract_type, payment_method)
            WHEN 0 THEN 'detail'
            WHEN 1 THEN 'contract subtotal'
            WHEN 3 THEN 'grand total' END                                   AS level,
       CASE WHEN GROUPING(contract_type)  = 1 THEN 'All contracts'
            ELSE contract_type END                                          AS contract_type,
       CASE WHEN GROUPING(payment_method) = 1 THEN 'All payment methods'
            ELSE payment_method END                                         AS payment_method,
       SUM(customer_count)                                                  AS customers,
       SUM(churn_flag)                                                      AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct,
       SUM(monthly_charges)                                                 AS monthly_revenue,
       COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)      AS monthly_revenue_lost
FROM   olap.v_customer_churn
GROUP  BY ROLLUP (contract_type, payment_method)
WITH DATA;

CREATE UNIQUE INDEX ux_mv_churn_rollup ON olap.mv_churn_rollup (contract_type, payment_method);
COMMENT ON MATERIALIZED VIEW olap.mv_churn_rollup IS
    'ROLL-UP of churn and revenue: contract type x payment method, contract subtotals, grand total (16 rows).';


-- B3. DRILL-DOWN: tenure group -> band -> month, all levels in one result.
--     ROLLUP over COMPOSITE columns: (order, label) pairs are rolled up together.
--     tenure_months stays NULL above month level (useful as a chart axis);
--     tenure_month_label ('All months' or '7') is the non-NULL key column.
CREATE MATERIALIZED VIEW olap.mv_tenure_drilldown AS
SELECT CASE GROUPING(tenure_group, tenure_band, tenure_months)
            WHEN 0 THEN 'month'
            WHEN 1 THEN 'band'
            WHEN 3 THEN 'group'
            WHEN 7 THEN 'total' END                                         AS drill_level,
       COALESCE(tenure_group_order, 0)                                      AS tenure_group_order,
       CASE WHEN GROUPING(tenure_group) = 1 THEN 'All tenures'
            ELSE tenure_group END                                           AS tenure_group,
       COALESCE(tenure_band_order, 0)                                       AS tenure_band_order,
       CASE WHEN GROUPING(tenure_band) = 1 THEN 'All bands'
            ELSE tenure_band END                                            AS tenure_band,
       tenure_months,
       CASE WHEN GROUPING(tenure_months) = 1 THEN 'All months'
            ELSE tenure_months::text END                                    AS tenure_month_label,
       SUM(customer_count)                                                  AS customers,
       SUM(churn_flag)                                                      AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct,
       ROUND(AVG(monthly_charges), 2)                                       AS avg_monthly_charge,
       COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)      AS monthly_revenue_lost
FROM   olap.v_customer_churn
GROUP  BY ROLLUP ((tenure_group_order, tenure_group),
                  (tenure_band_order,  tenure_band),
                  tenure_months)
WITH DATA;

CREATE UNIQUE INDEX ux_mv_tenure_drilldown
    ON olap.mv_tenure_drilldown (tenure_group, tenure_band, tenure_month_label);
COMMENT ON MATERIALIZED VIEW olap.mv_tenure_drilldown IS
    'DRILL-DOWN hierarchy tenure group > band > month with churn at every level (80 rows).';


-- B4. CUBE: every combination of contract, payment method, internet service and
--     tenure band (2^4 = 16 grouping levels). The dashboard filters this one
--     table for any slice or dice, e.g. contract = 'Month-to-month' AND
--     payment_method = 'Electronic check' AND the other two = 'All ...'.
--     aggregation_level lists the dimensions that are NOT rolled up.
CREATE MATERIALIZED VIEW olap.mv_churn_cube AS
SELECT GROUPING(contract_type, payment_method, internet_service, tenure_band)  AS grouping_id,
       COALESCE(NULLIF(concat_ws(' x ',
                    CASE WHEN GROUPING(contract_type)    = 0 THEN 'contract' END,
                    CASE WHEN GROUPING(payment_method)   = 0 THEN 'payment'  END,
                    CASE WHEN GROUPING(internet_service) = 0 THEN 'internet' END,
                    CASE WHEN GROUPING(tenure_band)      = 0 THEN 'tenure'   END), ''),
                'grand total')                                                  AS aggregation_level,
       CASE WHEN GROUPING(contract_type)    = 1 THEN 'All contracts'
            ELSE contract_type END                                              AS contract_type,
       CASE WHEN GROUPING(payment_method)   = 1 THEN 'All payment methods'
            ELSE payment_method END                                             AS payment_method,
       CASE WHEN GROUPING(internet_service) = 1 THEN 'All internet types'
            ELSE internet_service END                                           AS internet_service,
       CASE WHEN GROUPING(tenure_band)      = 1 THEN 'All tenure bands'
            ELSE tenure_band END                                                AS tenure_band,
       CASE WHEN GROUPING(tenure_band)      = 1 THEN 0
            ELSE MIN(tenure_band_order) END                                     AS tenure_band_order,
       SUM(customer_count)                                                      AS customers,
       SUM(churn_flag)                                                          AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)                                        AS churn_rate_pct,
       SUM(monthly_charges)                                                     AS monthly_revenue,
       COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)          AS monthly_revenue_lost
FROM   olap.v_customer_churn
GROUP  BY CUBE (contract_type, payment_method, internet_service, tenure_band)
WITH DATA;

CREATE UNIQUE INDEX ux_mv_churn_cube
    ON olap.mv_churn_cube (contract_type, payment_method, internet_service, tenure_band);
COMMENT ON MATERIALIZED VIEW olap.mv_churn_cube IS
    'Data cube: churn and revenue for every combination of contract, payment method, internet service and tenure band (387 rows, 16 grouping levels).';


-- B5. PIVOT: contract type rows, churn No / Yes columns, plus a total row.
CREATE MATERIALIZED VIEW olap.mv_contract_churn_pivot AS
SELECT CASE WHEN GROUPING(contract_type) = 1 THEN 'All contracts'
            ELSE contract_type END                                          AS contract_type,
       GROUPING(contract_type)                                              AS is_total,
       SUM(customer_count) FILTER (WHERE churn_label = 'No')                AS churn_no,
       SUM(customer_count) FILTER (WHERE churn_label = 'Yes')               AS churn_yes,
       SUM(customer_count)                                                  AS total,
       ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct
FROM   olap.v_customer_churn
GROUP  BY ROLLUP (contract_type)
WITH DATA;

CREATE UNIQUE INDEX ux_mv_contract_churn_pivot ON olap.mv_contract_churn_pivot (contract_type);
COMMENT ON MATERIALIZED VIEW olap.mv_contract_churn_pivot IS
    'PIVOT: contract type x churn No/Yes with totals and churn rate (4 rows).';


-- B6. RANKING: all 36 contract x payment x internet segments ranked by monthly
--     revenue lost to churn, with share and running (cumulative) share.
CREATE MATERIALIZED VIEW olap.mv_segment_revenue_risk AS
WITH segment AS (
    SELECT contract_type,
           payment_method,
           internet_service,
           SUM(customer_count)                                                  AS customers,
           SUM(churn_flag)                                                      AS churned,
           ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct,
           SUM(monthly_charges)                                                 AS monthly_revenue,
           COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)      AS monthly_revenue_lost
    FROM   olap.v_customer_churn
    GROUP  BY contract_type, payment_method, internet_service
)
SELECT RANK() OVER (ORDER BY monthly_revenue_lost DESC)                         AS rank_no,
       contract_type,
       payment_method,
       internet_service,
       customers,
       churned,
       churn_rate_pct,
       monthly_revenue,
       monthly_revenue_lost,
       ROUND(100.0 * monthly_revenue_lost / SUM(monthly_revenue_lost) OVER (), 2) AS share_of_lost_pct,
       ROUND(100.0 * SUM(monthly_revenue_lost) OVER (ORDER BY monthly_revenue_lost DESC,
                                                             contract_type, payment_method, internet_service
                                                    ROWS UNBOUNDED PRECEDING)
                   / SUM(monthly_revenue_lost) OVER (), 2)                      AS cumulative_share_pct
FROM   segment
WITH DATA;

CREATE UNIQUE INDEX ux_mv_segment_revenue_risk
    ON olap.mv_segment_revenue_risk (contract_type, payment_method, internet_service);
COMMENT ON MATERIALIZED VIEW olap.mv_segment_revenue_risk IS
    'RANKING: 36 contract x payment x internet segments by monthly revenue lost to churn, with share and cumulative share.';


-- B7. Churn by internet add-on service and status (6 add-ons x 3 statuses).
CREATE MATERIALIZED VIEW olap.mv_addon_churn AS
SELECT a.addon,
       a.status,
       SUM(v.customer_count)                    AS customers,
       SUM(v.churn_flag)                        AS churned,
       ROUND(100.0 * AVG(v.churn_flag), 2)      AS churn_rate_pct
FROM   olap.v_customer_churn AS v
CROSS  JOIN LATERAL (VALUES
           ('Online security',   v.online_security::text),
           ('Online backup',     v.online_backup::text),
           ('Device protection', v.device_protection::text),
           ('Tech support',      v.tech_support::text),
           ('Streaming TV',      v.streaming_tv::text),
           ('Streaming movies',  v.streaming_movies::text)
       ) AS a (addon, status)
GROUP  BY a.addon, a.status
WITH DATA;

CREATE UNIQUE INDEX ux_mv_addon_churn ON olap.mv_addon_churn (addon, status);
COMMENT ON MATERIALIZED VIEW olap.mv_addon_churn IS
    'Churn by internet add-on (security, backup, protection, support, streaming) and status Yes/No/No internet service (18 rows).';


/* -----------------------------------------------------------------------------
   Part C. How to refresh the materialized views.

   1. REFRESH MATERIALIZED VIEW name;
        Re-runs the query and replaces the stored rows. While it runs, the
        view is LOCKED: dashboard queries must wait until it finishes.
   2. REFRESH MATERIALIZED VIEW CONCURRENTLY name;
        Builds the new result on the side and then applies only the
        differences, so readers are NEVER blocked. Slower, and it needs a
        UNIQUE index (which every view here has).
   3. CALL dw.run_etl();
        The ETL procedure (Phase 4) reloads the warehouse and then refreshes
        EVERY materialized view in the olap schema automatically, inside the
        same all-or-nothing transaction.
   The demonstration below runs all three; refreshed_at changes each time.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== C. Refresh demonstration ==='
SELECT 'after CREATE'                AS step, refreshed_at FROM olap.mv_kpi_overview;

REFRESH MATERIALIZED VIEW olap.mv_kpi_overview;
SELECT 'after REFRESH'               AS step, refreshed_at FROM olap.mv_kpi_overview;

REFRESH MATERIALIZED VIEW CONCURRENTLY olap.mv_kpi_overview;
REFRESH MATERIALIZED VIEW CONCURRENTLY olap.mv_churn_cube;
SELECT 'after REFRESH CONCURRENTLY'  AS step, refreshed_at FROM olap.mv_kpi_overview;

CALL dw.run_etl();     -- reload + refresh all 7 (watch the "Refreshed olap.mv_..." notices)
SELECT 'after CALL dw.run_etl()'     AS step, refreshed_at FROM olap.mv_kpi_overview;


/* -----------------------------------------------------------------------------
   Part D. What is stored, and how big it is.
   ----------------------------------------------------------------------------- */
-- ANALYZE fills in the planner statistics (including the row estimate shown
-- below as rows_stored); a freshly created or refreshed view has none yet.
ANALYZE olap.mv_kpi_overview, olap.mv_churn_rollup, olap.mv_tenure_drilldown,
        olap.mv_churn_cube, olap.mv_contract_churn_pivot,
        olap.mv_segment_revenue_risk, olap.mv_addon_churn;

\echo ''
\echo '=== D. Materialized views in the olap schema ==='
SELECT m.matviewname                                             AS materialized_view,
       m.ispopulated                                              AS populated,
       c.reltuples::bigint                                        AS rows_stored,
       pg_size_pretty(pg_total_relation_size(c.oid))              AS size_on_disk,
       (SELECT count(*) FROM pg_index i
         WHERE i.indrelid = c.oid AND i.indisunique)              AS unique_indexes,
       obj_description(c.oid, 'pg_class')                         AS description
FROM   pg_matviews  AS m
JOIN   pg_class     AS c ON c.relname = m.matviewname
JOIN   pg_namespace AS n ON n.oid = c.relnamespace AND n.nspname = m.schemaname
WHERE  m.schemaname = 'olap'
ORDER  BY m.matviewname;

\echo '=== D2. Example: read the cube for a dice (no aggregation needed at read time) ==='
SELECT contract_type, payment_method, internet_service, tenure_band,
       customers, churned, churn_rate_pct, monthly_revenue_lost
FROM   olap.mv_churn_cube
WHERE  contract_type    = 'Month-to-month'
  AND  payment_method   = 'Electronic check'
  AND  tenure_band      = 'All tenure bands'
ORDER  BY internet_service = 'All internet types', internet_service;


/* -----------------------------------------------------------------------------
   Part E. Phase 6/7 checks: the materialized views reproduce the expected
   results and agree with the live star schema.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== E. Materialized-view checks ==='
WITH cube AS (SELECT * FROM olap.mv_churn_cube),
one AS (        -- helper: "churned / customers (rate%)" for one cube cell
    SELECT contract_type, payment_method, internet_service, tenure_band,
           format('%s / %s (%s%%)', churned, customers, churn_rate_pct) AS summary
    FROM   cube
),
checks (check_no, check_name, expected, actual) AS (
    VALUES
    (1,  'Materialized views in olap, all populated', '7',
         (SELECT count(*) FROM pg_matviews WHERE schemaname = 'olap' AND ispopulated)::text),
    (2,  'Row counts kpi / rollup / drilldown / cube / pivot / segments / addons',
         '1 / 16 / 80 / 387 / 4 / 36 / 18',
         format('%s / %s / %s / %s / %s / %s / %s',
                (SELECT count(*) FROM olap.mv_kpi_overview),
                (SELECT count(*) FROM olap.mv_churn_rollup),
                (SELECT count(*) FROM olap.mv_tenure_drilldown),
                (SELECT count(*) FROM olap.mv_churn_cube),
                (SELECT count(*) FROM olap.mv_contract_churn_pivot),
                (SELECT count(*) FROM olap.mv_segment_revenue_risk),
                (SELECT count(*) FROM olap.mv_addon_churn))),
    (3,  'KPI: customers / churned / churn rate %', '7043 / 1869 / 26.54',
         (SELECT format('%s / %s / %s', customers, churned, churn_rate_pct) FROM olap.mv_kpi_overview)),
    (4,  'KPI matches the live fact table (customers, monthly revenue)',
         (SELECT format('%s / %s', count(*), sum(monthly_charges)) FROM dw.fact_customer_churn),
         (SELECT format('%s / %s', customers, monthly_revenue) FROM olap.mv_kpi_overview)),
    (5,  'Cube: month-to-month', '1655 / 3875 (42.71%)',
         (SELECT summary FROM one WHERE contract_type = 'Month-to-month' AND payment_method = 'All payment methods'
             AND internet_service = 'All internet types' AND tenure_band = 'All tenure bands')),
    (6,  'Cube: one year', '166 / 1473 (11.27%)',
         (SELECT summary FROM one WHERE contract_type = 'One year' AND payment_method = 'All payment methods'
             AND internet_service = 'All internet types' AND tenure_band = 'All tenure bands')),
    (7,  'Cube: two year', '48 / 1695 (2.83%)',
         (SELECT summary FROM one WHERE contract_type = 'Two year' AND payment_method = 'All payment methods'
             AND internet_service = 'All internet types' AND tenure_band = 'All tenure bands')),
    (8,  'Cube: tenure 0-12 months', '1037 / 2186 (47.44%)',
         (SELECT summary FROM one WHERE contract_type = 'All contracts' AND payment_method = 'All payment methods'
             AND internet_service = 'All internet types' AND tenure_band = '0-12 months')),
    (9,  'Cube: electronic check', '1071 / 2365 (45.29%)',
         (SELECT summary FROM one WHERE contract_type = 'All contracts' AND payment_method = 'Electronic check'
             AND internet_service = 'All internet types' AND tenure_band = 'All tenure bands')),
    (10, 'Cube: month-to-month + electronic check', '994 / 1850 (53.73%)',
         (SELECT summary FROM one WHERE contract_type = 'Month-to-month' AND payment_method = 'Electronic check'
             AND internet_service = 'All internet types' AND tenure_band = 'All tenure bands')),
    (11, 'Drill-down: 0-12 months band', '1037 / 2186 (47.44%)',
         (SELECT format('%s / %s (%s%%)', churned, customers, churn_rate_pct)
            FROM olap.mv_tenure_drilldown WHERE drill_level = 'band' AND tenure_band = '0-12 months')),
    (12, 'Pivot: month-to-month No / Yes', '2220 / 1655',
         (SELECT format('%s / %s', churn_no, churn_yes) FROM olap.mv_contract_churn_pivot
           WHERE contract_type = 'Month-to-month')),
    (13, 'Ranking: segment ranked 1', 'Month-to-month | Electronic check | Fiber optic',
         (SELECT concat_ws(' | ', contract_type, payment_method, internet_service)
            FROM olap.mv_segment_revenue_risk WHERE rank_no = 1)),
    (14, 'Ranking: shares add up to 100%', '100.00',
         (SELECT max(cumulative_share_pct)::text FROM olap.mv_segment_revenue_risk)),
    (15, 'Cube detail rows add up to all customers', '7043',
         (SELECT sum(customers)::text FROM cube WHERE grouping_id = 0))
)
SELECT check_no, check_name, expected, actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   checks
ORDER  BY check_no;
