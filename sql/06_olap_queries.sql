/* =============================================================================
   06_olap_queries.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16 and 18)
   Purpose : OLAP (On-Line Analytical Processing) on the star schema:
               0. star-join view used by every query
               1. ROLL-UP     payment method -> contract type -> all   (ROLLUP)
               2. DRILL-DOWN  tenure group -> tenure band -> tenure month
               3. SLICE       month-to-month customers only
               4. DICE        month-to-month + electronic check (+ fiber optic)
               5. PIVOT       contract type x churn Yes/No  (FILTER and crosstab)
               6. CUBE        contract x payment x internet (CUBE + GROUPING)
               7. RANKING     segments by monthly revenue lost to churn (RANK OVER)
               8. EXTRA       churn by internet add-on (support / security)
               9. CHECK       the four reference findings, via GROUPING SETS

   How to run:
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/06_olap_queries.sql
   In pgAdmin's Query Tool you can also highlight ONE query and press F5 to run
   just that query (the \echo lines are psql-only and only print headings).

   Measures used in every query (all computed from the fact table):
     customers             SUM(customer_count)
     churned               SUM(churn_flag)                    churn_flag: 1 = left
     churn_rate_pct        100 * AVG(churn_flag)              share that left
     monthly_revenue       SUM(monthly_charges)               monthly recurring revenue
     monthly_revenue_lost  SUM(monthly_charges) of churned customers
                           = monthly revenue that walked out of the door
   Money is in the currency units of the sample data set.

   IMPORTANT - how to read every result in this file:
   The figures are ASSOCIATIONS observed in this sample of 7,043 customers at
   one point in time. They show WHERE churn is concentrated, not WHY customers
   leave. For example, electronic-check payment is associated with high churn,
   but this does not prove that the payment method causes churn; other factors
   (contract type, tenure, price) are mixed into the same groups. Causes must be
   tested with controlled retention experiments.
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Run this script with -d telco_churn_dw.',
                        current_database();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM dw.fact_customer_churn) THEN
        RAISE EXCEPTION 'The fact table is empty. Run 04_etl_procedure.sql (CALL dw.run_etl()) first.';
    END IF;
END
$$;


/* =============================================================================
   0. THE STAR JOIN AS A VIEW
   -----------------------------------------------------------------------------
   Every OLAP question needs the fact table joined to its dimensions (the
   "star join"). We write that join once, as a view in the olap schema, so the
   queries below can simply say FROM olap.v_customer_churn. A view stores only
   the query, not data, so it is always up to date.
   Each fact row joins to exactly one row in every dimension, so the view has
   exactly 7,043 rows - one per customer.
   ============================================================================= */
CREATE OR REPLACE VIEW olap.v_customer_churn AS
SELECT f.customer_key,
       cu.customer_id, cu.gender, cu.senior_citizen, cu.partner, cu.dependents,
       co.contract_type, co.paperless_billing,
       se.phone_service, se.multiple_lines, se.internet_service,
       se.online_security, se.online_backup, se.device_protection,
       se.tech_support, se.streaming_tv, se.streaming_movies,
       pa.payment_method,
       te.tenure_months, te.tenure_band, te.tenure_band_order,
       te.tenure_group, te.tenure_group_order,
       ch.churn_label, ch.churn_flag,
       f.tenure, f.monthly_charges, f.total_charges, f.customer_count
FROM   dw.fact_customer_churn AS f
JOIN   dw.dim_customer AS cu ON cu.customer_key = f.customer_key
JOIN   dw.dim_contract AS co ON co.contract_key = f.contract_key
JOIN   dw.dim_service  AS se ON se.service_key  = f.service_key
JOIN   dw.dim_payment  AS pa ON pa.payment_key  = f.payment_key
JOIN   dw.dim_tenure   AS te ON te.tenure_key   = f.tenure_key
JOIN   dw.dim_churn    AS ch ON ch.churn_key    = f.churn_key;

COMMENT ON VIEW olap.v_customer_churn IS
    'Star join: fact_customer_churn with all six dimensions, one row per customer (7,043). Base of all OLAP queries and materialized views.';


/* =============================================================================
   1. ROLL-UP   (payment method -> contract type -> all customers)
   -----------------------------------------------------------------------------
   What it is: rolling up means moving from detailed figures to summarised
   figures by climbing a hierarchy. Here we start at contract type x payment
   method (12 groups), roll up to contract type (3 subtotals) and then to all
   customers (1 grand total).

   How: GROUP BY ROLLUP (contract_type, payment_method) produces all three
   levels in ONE query. In the subtotal rows the rolled-up column is NULL;
   GROUPING(column) returns 1 in exactly those rows, so we use it to print
   readable labels instead of NULL (a real value can never be confused with a
   subtotal).
   GROUPING(a, b) combines both flags into one number (a = 2, b = 1):
   0 = detail row, 1 = contract subtotal, 3 = grand total.

   MySQL 8: GROUP BY contract_type, payment_method WITH ROLLUP (and GROUPING()
   since 8.0.1). MySQL has no CUBE or GROUPING SETS.

   Business reading (from the results): month-to-month contracts hold 3,875 of
   7,043 customers and 120,847 of the 139,131 monthly revenue lost to churn
   (87%). Within month-to-month, electronic-check customers churn at 53.73%
   versus 31.6-34.1% for the other payment methods. Two-year contracts churn
   at 2.83%.
   ============================================================================= */
\echo ''
\echo '=== 1. ROLL-UP: contract type x payment method -> contract subtotal -> grand total ==='
SELECT CASE GROUPING(contract_type, payment_method)
            WHEN 0 THEN 'detail'
            WHEN 1 THEN 'contract subtotal'
            WHEN 3 THEN 'GRAND TOTAL' END                                    AS level,
       CASE WHEN GROUPING(contract_type)  = 1 THEN 'All contracts'
            ELSE contract_type END                                           AS contract_type,
       CASE WHEN GROUPING(payment_method) = 1 THEN 'All payment methods'
            ELSE payment_method END                                          AS payment_method,
       SUM(customer_count)                                                   AS customers,
       SUM(churn_flag)                                                       AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)                                     AS churn_rate_pct,
       SUM(monthly_charges)                                                  AS monthly_revenue,
       COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)       AS monthly_revenue_lost
FROM   olap.v_customer_churn
GROUP  BY ROLLUP (contract_type, payment_method)
ORDER  BY GROUPING(contract_type), contract_type,
          GROUPING(payment_method), payment_method;


/* =============================================================================
   2. DRILL-DOWN   (tenure group -> tenure band -> tenure month)
   -----------------------------------------------------------------------------
   What it is: the opposite of roll-up. We start with a high-level view and
   open one level of detail at a time, following the hierarchy stored in
   dim_tenure:
       level 1  tenure_group   0-24 months | 25-72 months
       level 2  tenure_band    0-12 | 13-24 | 25-48 | 49-72 months
       level 3  tenure_months  0, 1, 2, ... 72
   (Tenure replaces a calendar time dimension, which the source does not have.)

   Business reading: churn falls steadily as tenure grows - 47.44% in the
   first year (0-12 months: 1,037 of 2,186), 28.71% in the second year,
   20.39% at 25-48 months and 9.51% at 49-72 months. Drilling into the first
   year shows that month 1 is the most dangerous point: 380 of 613 customers
   with tenure 1 have churned (61.99%). This supports early-life onboarding
   and proactive service checks in the first months.
   ============================================================================= */
\echo ''
\echo '=== 2a. DRILL-DOWN level 1: tenure group ==='
SELECT tenure_group,
       SUM(customer_count)                 AS customers,
       SUM(churn_flag)                     AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)   AS churn_rate_pct,
       ROUND(AVG(monthly_charges), 2)      AS avg_monthly_charge
FROM   olap.v_customer_churn
GROUP  BY tenure_group_order, tenure_group
ORDER  BY tenure_group_order;

\echo '=== 2b. DRILL-DOWN level 2: tenure band within each group ==='
SELECT tenure_group,
       tenure_band,
       SUM(customer_count)                 AS customers,
       SUM(churn_flag)                     AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)   AS churn_rate_pct,
       ROUND(AVG(monthly_charges), 2)      AS avg_monthly_charge
FROM   olap.v_customer_churn
GROUP  BY tenure_group_order, tenure_group, tenure_band_order, tenure_band
ORDER  BY tenure_group_order, tenure_band_order;

\echo '=== 2c. DRILL-DOWN level 3: individual months inside the 0-12 months band ==='
SELECT tenure_band,
       tenure_months,
       SUM(customer_count)                 AS customers,
       SUM(churn_flag)                     AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)   AS churn_rate_pct
FROM   olap.v_customer_churn
WHERE  tenure_band = '0-12 months'
GROUP  BY tenure_band, tenure_months
ORDER  BY tenure_months;


/* =============================================================================
   3. SLICE   (contract type = Month-to-month)
   -----------------------------------------------------------------------------
   What it is: slicing fixes ONE dimension to a single value and looks at the
   remaining "slice" of the cube. Here: only month-to-month customers, then
   broken down by other dimensions. The WHERE clause does the slicing.

   Business reading: the slice has 3,875 customers and a 42.71% churn rate
   (1,655 churned). Inside it, fiber-optic customers churn at 54.61% and
   customers in their first year at 51.35%; even long-standing month-to-month
   customers (49-72 months) churn at 26.02%, close to the company average.
   ============================================================================= */
\echo ''
\echo '=== 3a. SLICE: month-to-month customers by internet service (with slice total) ==='
SELECT 'Month-to-month'                                                     AS slice,
       CASE WHEN GROUPING(internet_service) = 1 THEN 'SLICE TOTAL'
            ELSE internet_service END                                       AS internet_service,
       SUM(customer_count)                                                  AS customers,
       SUM(churn_flag)                                                      AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct,
       COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)      AS monthly_revenue_lost
FROM   olap.v_customer_churn
WHERE  contract_type = 'Month-to-month'
GROUP  BY ROLLUP (internet_service)
ORDER  BY GROUPING(internet_service), internet_service;

\echo '=== 3b. SLICE: month-to-month customers by tenure band ==='
SELECT tenure_band,
       SUM(customer_count)                 AS customers,
       SUM(churn_flag)                     AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)   AS churn_rate_pct
FROM   olap.v_customer_churn
WHERE  contract_type = 'Month-to-month'
GROUP  BY tenure_band_order, tenure_band
ORDER  BY tenure_band_order;


/* =============================================================================
   4. DICE   (month-to-month AND electronic check [AND fiber optic])
   -----------------------------------------------------------------------------
   What it is: dicing fixes values on TWO OR MORE dimensions at once and cuts a
   smaller sub-cube out of the data.

   Business reading: month-to-month + electronic check = 1,850 customers with
   a 53.73% churn rate (994 churned). Adding the third condition, fiber-optic
   internet, gives the highest-risk sub-cube: 1,307 customers, 789 churned
   (60.37%), losing 68,281.50 of monthly revenue - almost half (49%) of all
   monthly revenue lost to churn. The opposite dice (two-year contract +
   automatic payment) churns at only 2.79%.
   ============================================================================= */
\echo ''
\echo '=== 4a. DICE: month-to-month + electronic check, by internet service ==='
SELECT 'Month-to-month + Electronic check'                                  AS dice,
       CASE WHEN GROUPING(internet_service) = 1 THEN 'DICE TOTAL'
            ELSE internet_service END                                       AS internet_service,
       SUM(customer_count)                                                  AS customers,
       SUM(churn_flag)                                                      AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct,
       COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)      AS monthly_revenue_lost
FROM   olap.v_customer_churn
WHERE  contract_type  = 'Month-to-month'
  AND  payment_method = 'Electronic check'
GROUP  BY ROLLUP (internet_service)
ORDER  BY GROUPING(internet_service), internet_service;

\echo '=== 4b. DICE: high-risk vs low-risk sub-cubes compared ==='
SELECT d.dice,
       SUM(v.customer_count)                                                AS customers,
       SUM(v.churn_flag)                                                    AS churned,
       ROUND(100.0 * AVG(v.churn_flag), 2)                                  AS churn_rate_pct,
       COALESCE(SUM(v.monthly_charges) FILTER (WHERE v.churn_flag = 1), 0)  AS monthly_revenue_lost
FROM   olap.v_customer_churn AS v
CROSS  JOIN LATERAL (VALUES
           (1, 'Month-to-month + Electronic check + Fiber optic',
               v.contract_type = 'Month-to-month' AND v.payment_method = 'Electronic check'
               AND v.internet_service = 'Fiber optic'),
           (2, 'Month-to-month + Electronic check',
               v.contract_type = 'Month-to-month' AND v.payment_method = 'Electronic check'),
           (3, 'Two year + automatic payment (bank transfer or credit card)',
               v.contract_type = 'Two year'
               AND v.payment_method IN ('Bank transfer (automatic)', 'Credit card (automatic)'))
       ) AS d (dice_no, dice, is_member)
WHERE  d.is_member
GROUP  BY d.dice_no, d.dice
ORDER  BY d.dice_no;


/* =============================================================================
   5. PIVOT   (contract type as rows, churn No / Yes as columns)
   -----------------------------------------------------------------------------
   What it is: pivoting rotates the result so that the values of one dimension
   (churn No / Yes) become COLUMNS. This gives a compact cross-tabulation that
   is easy to read in a report.

   Method A - aggregate FILTER (standard SQL, PostgreSQL 9.4+):
       SUM(x) FILTER (WHERE churn_label = 'Yes')
     MySQL has no FILTER; it needs SUM(CASE WHEN churn_label = 'Yes' THEN x ELSE 0 END).
   Method B - crosstab() from the tablefunc extension (installed in script 01):
     crosstab(source_sql, category_sql) turns rows (row, category, value) into
     columns. The output columns must be declared in the AS clause.
     MySQL has no crosstab function.

   Business reading: two-year contracts keep 97.2% of customers
   (1,647 No vs 48 Yes); month-to-month contracts keep only 57.3%
   (2,220 No vs 1,655 Yes).
   ============================================================================= */
\echo ''
\echo '=== 5a. PIVOT with SUM ... FILTER ==='
SELECT CASE WHEN GROUPING(contract_type) = 1 THEN 'All contracts'
            ELSE contract_type END                                          AS contract_type,
       SUM(customer_count) FILTER (WHERE churn_label = 'No')                AS churn_no,
       SUM(customer_count) FILTER (WHERE churn_label = 'Yes')               AS churn_yes,
       SUM(customer_count)                                                  AS total,
       ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct
FROM   olap.v_customer_churn
GROUP  BY ROLLUP (contract_type)
ORDER  BY GROUPING(contract_type), contract_type;

\echo '=== 5b. PIVOT with crosstab() (tablefunc extension) ==='
SELECT *
FROM   public.crosstab(
           -- source: one row per (row label, column label, value), ordered by row label
           $src$
           SELECT contract_type, churn_label, SUM(customer_count)
           FROM   olap.v_customer_churn
           GROUP  BY contract_type, churn_label
           ORDER  BY contract_type, churn_label
           $src$,
           -- categories: the list (and order) of output columns
           $cat$ VALUES ('No'), ('Yes') $cat$
       ) AS ct (contract_type TEXT, churn_no BIGINT, churn_yes BIGINT);

\echo '=== 5c. PIVOT of churn RATES: contract type x payment method (a "heat map" table) ==='
SELECT contract_type,
       ROUND(100.0 * AVG(churn_flag) FILTER (WHERE payment_method = 'Electronic check'), 2)          AS electronic_check_pct,
       ROUND(100.0 * AVG(churn_flag) FILTER (WHERE payment_method = 'Mailed check'), 2)              AS mailed_check_pct,
       ROUND(100.0 * AVG(churn_flag) FILTER (WHERE payment_method = 'Bank transfer (automatic)'), 2) AS bank_transfer_pct,
       ROUND(100.0 * AVG(churn_flag) FILTER (WHERE payment_method = 'Credit card (automatic)'), 2)   AS credit_card_pct,
       ROUND(100.0 * AVG(churn_flag), 2)                                                             AS all_methods_pct
FROM   olap.v_customer_churn
GROUP  BY contract_type
ORDER  BY contract_type;


/* =============================================================================
   6. CUBE   (contract type x payment method x internet service)
   -----------------------------------------------------------------------------
   What it is: GROUP BY CUBE (a, b, c) computes the aggregates for EVERY
   combination of the listed dimensions: (a,b,c), (a,b), (a,c), (b,c), (a),
   (b), (c) and () - 2^3 = 8 grouping levels in one query. It is the
   relational version of the OLAP "data cube": every roll-up, slice and dice of
   these three dimensions can be read from its result.
   Result size: 36 detail rows + 12 + 9 + 12 + 3 + 4 + 3 + 1 = 80 rows.

   GROUPING(contract_type, payment_method, internet_service) returns a number
   from 0 to 7 that identifies the level (bit 4 = contract, 2 = payment,
   1 = internet; a set bit means "rolled up / all"). We turn it into a readable
   aggregation_level label and replace the subtotal NULLs with "All ..." text.

   MySQL 8 has no CUBE; it would need 8 GROUP BY queries joined with UNION ALL.

   Business reading: the single-dimension rows of the cube repeat the main
   findings (month-to-month 42.71%, electronic check 45.29%, fiber optic 41.89%)
   and the detail rows show where they overlap; see section 7 for the ranking.
   ============================================================================= */
\echo ''
\echo '=== 6. CUBE: every combination of contract, payment method and internet service (80 rows) ==='
SELECT GROUPING(contract_type, payment_method, internet_service)                AS grouping_id,
       CASE GROUPING(contract_type, payment_method, internet_service)
            WHEN 0 THEN 'contract x payment x internet'
            WHEN 1 THEN 'contract x payment'
            WHEN 2 THEN 'contract x internet'
            WHEN 3 THEN 'contract'
            WHEN 4 THEN 'payment x internet'
            WHEN 5 THEN 'payment'
            WHEN 6 THEN 'internet'
            WHEN 7 THEN 'GRAND TOTAL' END                                        AS aggregation_level,
       CASE WHEN GROUPING(contract_type)    = 1 THEN 'All contracts'
            ELSE contract_type END                                               AS contract_type,
       CASE WHEN GROUPING(payment_method)   = 1 THEN 'All payment methods'
            ELSE payment_method END                                              AS payment_method,
       CASE WHEN GROUPING(internet_service) = 1 THEN 'All internet types'
            ELSE internet_service END                                            AS internet_service,
       SUM(customer_count)                                                       AS customers,
       SUM(churn_flag)                                                           AS churned,
       ROUND(100.0 * AVG(churn_flag), 2)                                         AS churn_rate_pct,
       COALESCE(SUM(monthly_charges) FILTER (WHERE churn_flag = 1), 0)           AS monthly_revenue_lost
FROM   olap.v_customer_churn
GROUP  BY CUBE (contract_type, payment_method, internet_service)
ORDER  BY grouping_id DESC, contract_type, payment_method, internet_service;


/* =============================================================================
   7. RANKING   (segments by monthly revenue lost to churn)
   -----------------------------------------------------------------------------
   What it is: window functions rank or compare rows WITHOUT collapsing them.
     RANK() OVER (ORDER BY x DESC)          1, 2, 3 ... (ties share a rank)
     SUM(x) OVER ()                         grand total next to every row
     SUM(x) OVER (ORDER BY ... ROWS UNBOUNDED PRECEDING)   running total
     RANK() OVER (PARTITION BY c ORDER BY x)  ranking restarted for each c
   A segment = one combination of contract type x payment method x internet
   service (36 segments).
   MySQL 8 supports the same window functions (they were added in 8.0).

   Business reading: the top segment (month-to-month + electronic check + fiber
   optic) alone accounts for 49.08% of all monthly revenue lost to churn, and
   the top 5 segments - all month-to-month - account for about 78.5%. Retention
   effort can therefore be focused on a few segments. Section 7b shows that
   "electronic check + fiber optic" is the highest-risk segment inside EVERY
   contract type (60.37% month-to-month, 26.02% one year, 9.78% two year).
   ============================================================================= */
\echo ''
\echo '=== 7a. RANKING: top 10 segments by monthly revenue lost to churn ==='
WITH segment AS (
    SELECT contract_type,
           payment_method,
           internet_service,
           SUM(customer_count)                                                  AS customers,
           SUM(churn_flag)                                                      AS churned,
           ROUND(100.0 * AVG(churn_flag), 2)                                    AS churn_rate_pct,
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
       monthly_revenue_lost,
       ROUND(100.0 * monthly_revenue_lost / SUM(monthly_revenue_lost) OVER (), 2) AS share_of_lost_pct,
       ROUND(100.0 * SUM(monthly_revenue_lost) OVER (ORDER BY monthly_revenue_lost DESC
                                                    ROWS UNBOUNDED PRECEDING)
                   / SUM(monthly_revenue_lost) OVER (), 2)                      AS cumulative_share_pct
FROM   segment
ORDER  BY rank_no
LIMIT  10;

\echo '=== 7b. RANKING inside each contract type: top 3 segments by churn rate (segments of 50+ customers) ==='
WITH segment AS (
    SELECT contract_type,
           payment_method,
           internet_service,
           SUM(customer_count)                 AS customers,
           ROUND(100.0 * AVG(churn_flag), 2)   AS churn_rate_pct
    FROM   olap.v_customer_churn
    GROUP  BY contract_type, payment_method, internet_service
    HAVING SUM(customer_count) >= 50          -- ignore tiny segments: their rates are unreliable
),
ranked AS (
    SELECT s.*,
           RANK() OVER (PARTITION BY contract_type ORDER BY churn_rate_pct DESC) AS rank_in_contract
    FROM   segment AS s
)
SELECT contract_type, rank_in_contract, payment_method, internet_service, customers, churn_rate_pct
FROM   ranked
WHERE  rank_in_contract <= 3
ORDER  BY contract_type, rank_in_contract;


/* =============================================================================
   8. EXTRA: CHURN BY INTERNET ADD-ON   (support for the "support / security
      service needs" recommendation)
   -----------------------------------------------------------------------------
   The six add-on columns are "unpivoted" with CROSS JOIN LATERAL (VALUES ...):
   each customer row becomes six rows (one per add-on), so one GROUP BY can
   compare all add-ons side by side.

   Business reading: customers WITHOUT online security churn at 41.77% versus
   14.61% with it; without tech support 41.64% versus 15.17% with it. The gap is
   much smaller for streaming services (about 30-34% either way). Customers
   with no internet at all churn at 7.40%. Again, this is an association: for
   example, customers who buy security may also be more committed in general.
   ============================================================================= */
\echo ''
\echo '=== 8. EXTRA: churn rate by add-on service and status ==='
SELECT a.addon,
       SUM(v.customer_count) FILTER (WHERE a.status = 'Yes')                             AS customers_with,
       ROUND(100.0 * AVG(v.churn_flag) FILTER (WHERE a.status = 'Yes'), 2)               AS churn_pct_with,
       SUM(v.customer_count) FILTER (WHERE a.status = 'No')                              AS customers_without,
       ROUND(100.0 * AVG(v.churn_flag) FILTER (WHERE a.status = 'No'), 2)                AS churn_pct_without,
       ROUND(100.0 * AVG(v.churn_flag) FILTER (WHERE a.status = 'No')
           - 100.0 * AVG(v.churn_flag) FILTER (WHERE a.status = 'Yes'), 2)               AS gap_pct_points,
       ROUND(100.0 * AVG(v.churn_flag) FILTER (WHERE a.status = 'No internet service'), 2) AS churn_pct_no_internet
FROM   olap.v_customer_churn AS v
CROSS  JOIN LATERAL (VALUES
           ('Online security',   v.online_security::text),
           ('Online backup',     v.online_backup::text),
           ('Device protection', v.device_protection::text),
           ('Tech support',      v.tech_support::text),
           ('Streaming TV',      v.streaming_tv::text),
           ('Streaming movies',  v.streaming_movies::text)
       ) AS a (addon, status)
GROUP  BY a.addon
ORDER  BY gap_pct_points DESC;


/* =============================================================================
   9. CHECK: do the OLAP results reproduce the earlier findings?
   -----------------------------------------------------------------------------
   GROUPING SETS lists exactly the groupings we want - here four different
   ones in a single pass over the data (MySQL would need UNION ALL):
       (contract_type)                    -> the three contract rates
       (tenure_band)                      -> the 0-12 months band
       (payment_method)                   -> electronic check
       (contract_type, payment_method)    -> month-to-month + electronic check
   ============================================================================= */
\echo ''
\echo '=== 9. Reference findings reproduced by the OLAP queries ==='
WITH sets AS (
    SELECT contract_type, tenure_band, payment_method,
           GROUPING(contract_type, tenure_band, payment_method) AS g,
           SUM(customer_count)                AS customers,
           SUM(churn_flag)                    AS churned,
           ROUND(100.0 * AVG(churn_flag), 2)  AS rate
    FROM   olap.v_customer_churn
    GROUP  BY GROUPING SETS ((contract_type), (tenure_band), (payment_method),
                             (contract_type, payment_method))
),
found (finding_no, finding, expected, actual) AS (
    SELECT 1, 'Month-to-month churn', '1655 / 3875 (42.71%)',
           (SELECT format('%s / %s (%s%%)', churned, customers, rate) FROM sets
             WHERE g = 3 AND contract_type = 'Month-to-month')
    UNION ALL
    SELECT 2, 'One-year churn', '166 / 1473 (11.27%)',
           (SELECT format('%s / %s (%s%%)', churned, customers, rate) FROM sets
             WHERE g = 3 AND contract_type = 'One year')
    UNION ALL
    SELECT 3, 'Two-year churn', '48 / 1695 (2.83%)',
           (SELECT format('%s / %s (%s%%)', churned, customers, rate) FROM sets
             WHERE g = 3 AND contract_type = 'Two year')
    UNION ALL
    SELECT 4, 'Tenure 0-12 months churn', '1037 / 2186 (47.44%)',
           (SELECT format('%s / %s (%s%%)', churned, customers, rate) FROM sets
             WHERE g = 5 AND tenure_band = '0-12 months')
    UNION ALL
    SELECT 5, 'Electronic check churn', '1071 / 2365 (45.29%)',
           (SELECT format('%s / %s (%s%%)', churned, customers, rate) FROM sets
             WHERE g = 6 AND payment_method = 'Electronic check')
    UNION ALL
    SELECT 6, 'Month-to-month + electronic check churn', '994 / 1850 (53.73%)',
           (SELECT format('%s / %s (%s%%)', churned, customers, rate) FROM sets
             WHERE g = 2 AND contract_type = 'Month-to-month' AND payment_method = 'Electronic check')
)
SELECT finding_no, finding, expected, actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   found
ORDER  BY finding_no;
