/* =============================================================================
   05_validation_checks.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16 and 18)
   Purpose : Prove that the warehouse is complete and correct. Every check is
             printed with its expected value, actual value and PASS / FAIL.

   The checks are stored in a reusable function, so they can be re-run at any
   time (e.g. after every CALL dw.run_etl()) with one statement:

       SELECT * FROM dw.validate_warehouse();

   Check groups:
     A  Row counts            expected sizes of every star-schema table
     B  Referential integrity no fact row points to a missing dimension row
     C  Reconciliation        the warehouse holds exactly what staging held
     D  Data rules            business rules and design decisions hold
     E  Churn distribution    No 5,174 (73.46%) / Yes 1,869 (26.54%)
     F  Reference findings    figures from the earlier MySQL analysis
     G  ETL                   the latest ETL run succeeded

   The expected numbers are specific to the IBM Telco sample (7,043 customers).
   If a count differs, investigate how the data or a dimension was defined
   before changing an expected value.

   How to run (after 01-04):
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/05_validation_checks.sql
   The script ends with an ERROR (and exit code 3) if any check fails, so it
   can also be used in an automated pipeline.
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Run this script with -d telco_churn_dw.',
                        current_database();
    END IF;
    IF to_regclass('dw.etl_run_log') IS NULL THEN
        RAISE EXCEPTION 'The ETL has not been installed yet. Run 04_etl_procedure.sql first.';
    END IF;
END
$$;


/* -----------------------------------------------------------------------------
   The validation function.

   LANGUAGE sql + RETURNS TABLE: a function that returns rows, so it can be used
   like a table:  SELECT * FROM dw.validate_warehouse() WHERE result = 'FAIL';
   STABLE: it only reads data.
   MySQL difference: a MySQL function can only return ONE value; returning a
   result set needs a procedure, and a procedure's result cannot be used in a
   FROM clause.

   Technique: each check is one row of a VALUES list with
   (number, group, name, expected value, actual value). The actual value is a
   scalar subquery. Expected and actual are compared as text, so counts,
   amounts and percentages can share one column.
   ----------------------------------------------------------------------------- */
CREATE OR REPLACE FUNCTION dw.validate_warehouse()
RETURNS TABLE (
    check_no    INTEGER,
    check_group TEXT,
    check_name  TEXT,
    expected    TEXT,
    actual      TEXT,
    result      TEXT
)
LANGUAGE sql
STABLE
AS $$
WITH
-- Fact rows with the dimension attributes needed by groups E and F.
base AS (
    SELECT f.customer_key, f.tenure, f.total_charges,
           co.contract_type, pa.payment_method, te.tenure_band,
           ch.churn_label, ch.churn_flag
    FROM   dw.fact_customer_churn AS f
    JOIN   dw.dim_contract AS co ON co.contract_key = f.contract_key
    JOIN   dw.dim_payment  AS pa ON pa.payment_key  = f.payment_key
    JOIN   dw.dim_tenure   AS te ON te.tenure_key   = f.tenure_key
    JOIN   dw.dim_churn    AS ch ON ch.churn_key    = f.churn_key
),
-- Staging data, written the way the ETL interprets it (group C).
src AS (
    SELECT btrim(customer_id)        AS customer_id,
           btrim(gender)             AS gender,
           btrim(senior_citizen)     AS senior_citizen,
           btrim(partner)            AS partner,
           btrim(dependents)         AS dependents,
           btrim(tenure)::int        AS tenure,
           btrim(phone_service)      AS phone_service,
           btrim(multiple_lines)     AS multiple_lines,
           btrim(internet_service)   AS internet_service,
           btrim(online_security)    AS online_security,
           btrim(online_backup)      AS online_backup,
           btrim(device_protection)  AS device_protection,
           btrim(tech_support)       AS tech_support,
           btrim(streaming_tv)       AS streaming_tv,
           btrim(streaming_movies)   AS streaming_movies,
           btrim(contract)           AS contract,
           btrim(paperless_billing)  AS paperless_billing,
           btrim(payment_method)     AS payment_method,
           btrim(monthly_charges)::numeric                          AS monthly_charges,
           COALESCE(NULLIF(btrim(total_charges), '')::numeric, 0)   AS total_charges,
           btrim(churn)              AS churn
    FROM   staging.telco_raw
),
-- The same 21 columns rebuilt from the star schema (booleans turned back into
-- the source spellings). If src and rebuilt are identical, no information was
-- lost or changed by the ETL.
rebuilt AS (
    SELECT cu.customer_id,
           cu.gender,
           CASE WHEN cu.senior_citizen    THEN '1'   ELSE '0'  END,
           CASE WHEN cu.partner           THEN 'Yes' ELSE 'No' END,
           CASE WHEN cu.dependents        THEN 'Yes' ELSE 'No' END,
           f.tenure::int,
           CASE WHEN se.phone_service     THEN 'Yes' ELSE 'No' END,
           se.multiple_lines,
           se.internet_service,
           se.online_security::text,
           se.online_backup::text,
           se.device_protection::text,
           se.tech_support::text,
           se.streaming_tv::text,
           se.streaming_movies::text,
           co.contract_type,
           CASE WHEN co.paperless_billing THEN 'Yes' ELSE 'No' END,
           pa.payment_method,
           f.monthly_charges::numeric,
           f.total_charges::numeric,
           ch.churn_label
    FROM   dw.fact_customer_churn AS f
    JOIN   dw.dim_customer AS cu ON cu.customer_key = f.customer_key
    JOIN   dw.dim_contract AS co ON co.contract_key = f.contract_key
    JOIN   dw.dim_service  AS se ON se.service_key  = f.service_key
    JOIN   dw.dim_payment  AS pa ON pa.payment_key  = f.payment_key
    JOIN   dw.dim_churn    AS ch ON ch.churn_key    = f.churn_key
),
-- Churn per customer segment (group F). Each fact row is tested against every
-- segment definition; it is counted in each segment it belongs to.
seg AS (
    SELECT s.segment,
           count(*)          AS customers,
           sum(b.churn_flag) AS churned
    FROM   base AS b
    CROSS  JOIN LATERAL (VALUES
               ('Month-to-month',   b.contract_type  = 'Month-to-month'),
               ('One year',         b.contract_type  = 'One year'),
               ('Two year',         b.contract_type  = 'Two year'),
               ('Tenure 0-12',      b.tenure_band    = '0-12 months'),
               ('Electronic check', b.payment_method = 'Electronic check'),
               ('M2M + e-check',    b.contract_type  = 'Month-to-month'
                                AND b.payment_method = 'Electronic check')
           ) AS s (segment, is_member)
    WHERE  s.is_member
    GROUP  BY s.segment
),
seg_text AS (   -- "churned / customers (rate%)", the format of the expected values
    SELECT segment,
           format('%s / %s (%s%%)', churned, customers,
                  round(100.0 * churned / customers, 2)) AS summary
    FROM   seg
),
checks (check_no, check_group, check_name, expected, actual) AS (
    VALUES
    -- ---------------- A. Row counts ---------------------------------------
    (1,  'A Row counts', 'dim_customer rows',        '7043', (SELECT count(*) FROM dw.dim_customer)::text),
    (2,  'A Row counts', 'dim_contract rows',        '6',    (SELECT count(*) FROM dw.dim_contract)::text),
    (3,  'A Row counts', 'dim_service rows',         '322',  (SELECT count(*) FROM dw.dim_service)::text),
    (4,  'A Row counts', 'dim_payment rows',         '4',    (SELECT count(*) FROM dw.dim_payment)::text),
    (5,  'A Row counts', 'dim_churn rows',           '2',    (SELECT count(*) FROM dw.dim_churn)::text),
    (6,  'A Row counts', 'dim_tenure rows (added)',  '73',   (SELECT count(*) FROM dw.dim_tenure)::text),
    (7,  'A Row counts', 'fact_customer_churn rows', '7043', (SELECT count(*) FROM dw.fact_customer_churn)::text),
    (8,  'A Row counts', 'Unique customer keys in fact (grain: 1 row per customer)',
                          '7043', (SELECT count(DISTINCT customer_key) FROM dw.fact_customer_churn)::text),

    -- ---------------- B. Referential integrity --------------------------
    -- The foreign keys already prevent orphans; these queries prove it
    -- independently (they would also catch a dropped or disabled FK).
    (9,  'B Integrity', 'Fact rows with an invalid customer_key', '0',
         (SELECT count(*) FROM dw.fact_customer_churn f
           WHERE NOT EXISTS (SELECT 1 FROM dw.dim_customer d WHERE d.customer_key = f.customer_key))::text),
    (10, 'B Integrity', 'Fact rows with an invalid contract_key', '0',
         (SELECT count(*) FROM dw.fact_customer_churn f
           WHERE NOT EXISTS (SELECT 1 FROM dw.dim_contract d WHERE d.contract_key = f.contract_key))::text),
    (11, 'B Integrity', 'Fact rows with an invalid service_key', '0',
         (SELECT count(*) FROM dw.fact_customer_churn f
           WHERE NOT EXISTS (SELECT 1 FROM dw.dim_service d WHERE d.service_key = f.service_key))::text),
    (12, 'B Integrity', 'Fact rows with an invalid payment_key', '0',
         (SELECT count(*) FROM dw.fact_customer_churn f
           WHERE NOT EXISTS (SELECT 1 FROM dw.dim_payment d WHERE d.payment_key = f.payment_key))::text),
    (13, 'B Integrity', 'Fact rows with an invalid churn_key', '0',
         (SELECT count(*) FROM dw.fact_customer_churn f
           WHERE NOT EXISTS (SELECT 1 FROM dw.dim_churn d WHERE d.churn_key = f.churn_key))::text),
    (14, 'B Integrity', 'Fact rows with an invalid tenure_key', '0',
         (SELECT count(*) FROM dw.fact_customer_churn f
           WHERE NOT EXISTS (SELECT 1 FROM dw.dim_tenure d WHERE d.tenure_key = f.tenure_key))::text),
    (15, 'B Integrity', 'Dimension rows not used by any fact row', '0',
         ((SELECT count(*) FROM dw.dim_customer d WHERE NOT EXISTS (SELECT 1 FROM dw.fact_customer_churn f WHERE f.customer_key = d.customer_key))
        + (SELECT count(*) FROM dw.dim_contract d WHERE NOT EXISTS (SELECT 1 FROM dw.fact_customer_churn f WHERE f.contract_key = d.contract_key))
        + (SELECT count(*) FROM dw.dim_service  d WHERE NOT EXISTS (SELECT 1 FROM dw.fact_customer_churn f WHERE f.service_key  = d.service_key))
        + (SELECT count(*) FROM dw.dim_payment  d WHERE NOT EXISTS (SELECT 1 FROM dw.fact_customer_churn f WHERE f.payment_key  = d.payment_key))
        + (SELECT count(*) FROM dw.dim_churn    d WHERE NOT EXISTS (SELECT 1 FROM dw.fact_customer_churn f WHERE f.churn_key    = d.churn_key))
        + (SELECT count(*) FROM dw.dim_tenure   d WHERE NOT EXISTS (SELECT 1 FROM dw.fact_customer_churn f WHERE f.tenure_key   = d.tenure_key))
         )::text),
    (16, 'B Integrity', 'Duplicate natural keys / combinations in dimensions', '0',
         ((SELECT count(*) - count(DISTINCT customer_id) FROM dw.dim_customer)
        + (SELECT count(*) - count(DISTINCT (contract_type, paperless_billing)) FROM dw.dim_contract)
        + (SELECT count(*) - count(DISTINCT (phone_service, multiple_lines, internet_service,
                                             online_security, online_backup, device_protection,
                                             tech_support, streaming_tv, streaming_movies)) FROM dw.dim_service)
        + (SELECT count(*) - count(DISTINCT payment_method) FROM dw.dim_payment)
        + (SELECT count(*) - count(DISTINCT churn_label)    FROM dw.dim_churn)
        + (SELECT count(*) - count(DISTINCT tenure_months)  FROM dw.dim_tenure)
         )::text),

    -- ---------------- C. Reconciliation staging <-> warehouse ------------
    (17, 'C Reconciliation', 'Fact rows = staging rows',
         (SELECT count(*) FROM staging.telco_raw)::text,
         (SELECT count(*) FROM dw.fact_customer_churn)::text),
    (18, 'C Reconciliation', 'Staging customers missing from dim_customer', '0',
         (SELECT count(*) FROM (SELECT customer_id FROM src
                                EXCEPT
                                SELECT customer_id FROM dw.dim_customer) x)::text),
    (19, 'C Reconciliation', 'dim_customer customers not in staging', '0',
         (SELECT count(*) FROM (SELECT customer_id FROM dw.dim_customer
                                EXCEPT
                                SELECT customer_id FROM src) x)::text),
    (20, 'C Reconciliation', 'Sum of monthly_charges (staging vs fact)',
         (SELECT sum(monthly_charges) FROM src)::numeric(14,2)::text,
         (SELECT sum(monthly_charges) FROM dw.fact_customer_churn)::numeric(14,2)::text),
    (21, 'C Reconciliation', 'Sum of total_charges (staging, blank = 0, vs fact)',
         (SELECT sum(total_charges) FROM src)::numeric(14,2)::text,
         (SELECT sum(total_charges) FROM dw.fact_customer_churn)::numeric(14,2)::text),
    (22, 'C Reconciliation', 'Rows that differ after rebuilding all 21 columns from the star schema', '0',
         (SELECT count(*) FROM ((SELECT * FROM src     EXCEPT ALL SELECT * FROM rebuilt)
                                UNION ALL
                                (SELECT * FROM rebuilt EXCEPT ALL SELECT * FROM src)) x)::text),

    -- ---------------- D. Data rules --------------------------------------
    (23, 'D Data rules', 'Fact tenure equals dim_tenure.tenure_months', '0 mismatches',
         (SELECT count(*) FROM dw.fact_customer_churn f
            JOIN dw.dim_tenure t ON t.tenure_key = f.tenure_key
           WHERE t.tenure_months <> f.tenure)::text || ' mismatches'),
    (24, 'D Data rules', 'Customers with total_charges = 0 (blank TotalCharges)', '11',
         (SELECT count(*) FROM dw.fact_customer_churn WHERE total_charges = 0)::text),
    (25, 'D Data rules', '... of which tenure is not 0', '0',
         (SELECT count(*) FROM dw.fact_customer_churn WHERE total_charges = 0 AND tenure <> 0)::text),
    (26, 'D Data rules', 'SUM(customer_count) = number of customers', '7043',
         (SELECT sum(customer_count) FROM dw.fact_customer_churn)::text),
    (27, 'D Data rules', 'Tenure hierarchy: groups / bands / months', '2 / 4 / 73',
         (SELECT format('%s / %s / %s', count(DISTINCT tenure_group), count(DISTINCT tenure_band),
                        count(DISTINCT tenure_months)) FROM dw.dim_tenure)),
    (28, 'D Data rules', 'Tenure bands that belong to more than one group', '0',
         (SELECT count(*) FROM (SELECT tenure_band FROM dw.dim_tenure
                                GROUP BY tenure_band HAVING count(DISTINCT tenure_group) > 1) x)::text),
    (29, 'D Data rules', 'Service rows breaking the no-internet / no-phone rules', '0',
         (SELECT count(*) FROM dw.dim_service
           WHERE NOT (    (internet_service = 'No') = (online_security   = 'No internet service')
                      AND (internet_service = 'No') = (online_backup     = 'No internet service')
                      AND (internet_service = 'No') = (device_protection = 'No internet service')
                      AND (internet_service = 'No') = (tech_support      = 'No internet service')
                      AND (internet_service = 'No') = (streaming_tv      = 'No internet service')
                      AND (internet_service = 'No') = (streaming_movies  = 'No internet service')
                      AND (NOT phone_service)       = (multiple_lines    = 'No phone service')))::text),

    -- ---------------- E. Churn distribution ------------------------------
    (30, 'E Churn', 'Churn = No  (customers)', '5174',
         (SELECT count(*) FROM base WHERE churn_label = 'No')::text),
    (31, 'E Churn', 'Churn = No  (% of all)',  '73.46',
         (SELECT round(100.0 * count(*) FILTER (WHERE churn_label = 'No') / count(*), 2) FROM base)::text),
    (32, 'E Churn', 'Churn = Yes (customers)', '1869',
         (SELECT count(*) FROM base WHERE churn_label = 'Yes')::text),
    (33, 'E Churn', 'Churn = Yes (% of all)',  '26.54',
         (SELECT round(100.0 * count(*) FILTER (WHERE churn_label = 'Yes') / count(*), 2) FROM base)::text),
    (34, 'E Churn', 'Churn rate = AVG(churn_flag) (%)', '26.54',
         (SELECT round(100.0 * avg(churn_flag), 2) FROM base)::text),

    -- ---------------- F. Reference findings (earlier analysis) -----------
    (35, 'F Reference', 'Month-to-month: churned / customers (rate)', '1655 / 3875 (42.71%)',
         (SELECT summary FROM seg_text WHERE segment = 'Month-to-month')),
    (36, 'F Reference', 'One year: churned / customers (rate)',       '166 / 1473 (11.27%)',
         (SELECT summary FROM seg_text WHERE segment = 'One year')),
    (37, 'F Reference', 'Two year: churned / customers (rate)',       '48 / 1695 (2.83%)',
         (SELECT summary FROM seg_text WHERE segment = 'Two year')),
    (38, 'F Reference', 'Tenure 0-12 months: churned / customers',     '1037 / 2186 (47.44%)',
         (SELECT summary FROM seg_text WHERE segment = 'Tenure 0-12')),
    (39, 'F Reference', 'Electronic check: churned / customers',       '1071 / 2365 (45.29%)',
         (SELECT summary FROM seg_text WHERE segment = 'Electronic check')),
    (40, 'F Reference', 'Month-to-month + electronic check',           '994 / 1850 (53.73%)',
         (SELECT summary FROM seg_text WHERE segment = 'M2M + e-check')),

    -- ---------------- G. ETL --------------------------------------------
    (41, 'G ETL', 'Status of the latest ETL run', 'succeeded',
         (SELECT status FROM dw.etl_run_log ORDER BY run_id DESC LIMIT 1))
)
SELECT c.check_no,
       c.check_group,
       c.check_name,
       c.expected,
       coalesce(c.actual, '(none)'),
       CASE WHEN c.expected = c.actual THEN 'PASS' ELSE 'FAIL' END
FROM   checks AS c
ORDER  BY c.check_no;
$$;

COMMENT ON FUNCTION dw.validate_warehouse() IS
    'Data-warehouse validation: 41 checks (row counts, integrity, reconciliation, data rules, churn, '
    'reference findings, ETL status) with PASS/FAIL. Usage: SELECT * FROM dw.validate_warehouse();';


/* -----------------------------------------------------------------------------
   Run the checks and print them.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Data-warehouse validation (SELECT * FROM dw.validate_warehouse()) ==='
SELECT * FROM dw.validate_warehouse();

\echo '=== Summary by group ==='
-- ROLLUP adds a grand-total row; GROUPING(check_group) = 1 marks that row,
-- so we can label it 'TOTAL' (this is previewed here and explained in Phase 6).
SELECT CASE WHEN GROUPING(check_group) = 1 THEN 'TOTAL' ELSE check_group END AS check_group,
       count(*)                                  AS checks,
       count(*) FILTER (WHERE result = 'PASS')   AS passed,
       count(*) FILTER (WHERE result = 'FAIL')   AS failed
FROM   dw.validate_warehouse()
GROUP  BY ROLLUP (check_group)
ORDER  BY GROUPING(check_group), check_group;


/* -----------------------------------------------------------------------------
   Final verdict. RAISE EXCEPTION makes psql stop with exit code 3 when any
   check failed, which an automated job can detect.
   ----------------------------------------------------------------------------- */
DO $$
DECLARE
    v_total  INTEGER;
    v_failed INTEGER;
BEGIN
    SELECT count(*), count(*) FILTER (WHERE result <> 'PASS')
    INTO   v_total, v_failed
    FROM   dw.validate_warehouse();

    IF v_failed > 0 THEN
        RAISE EXCEPTION 'VALIDATION FAILED: % of % checks failed', v_failed, v_total
              USING HINT = 'Run: SELECT * FROM dw.validate_warehouse() WHERE result = ''FAIL'';';
    END IF;
    RAISE NOTICE 'VALIDATION PASSED: all % checks passed', v_total;
END
$$;
