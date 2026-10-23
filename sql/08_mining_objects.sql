/* =============================================================================
   08_mining_objects.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Owners  : MuhammaD_Wallah and the GROUP are the owners and Developers of this model
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16 and 18)
   Purpose : Database objects used by the data-mining step (mining/churn_mining.py)
               1. olap.v_churn_model_input   the modelling data set, read FROM THE
                                             WAREHOUSE (not from the CSV)
               2. olap.mining_run            one row per run of churn_mining.py
               3. olap.model_metrics         test-set and cross-validation metrics
               4. olap.model_feature_importance  impurity and permutation importance
               5. olap.model_test_prediction     test-set probabilities (ROC curves,
                                                 threshold slider in the dashboard)
               6. olap.customer_churn_score      out-of-fold churn probability for
                                                 every customer (risk lists)
               7. olap.mining_insight            the plain-English explanation of a
                                                 run, one row per section (Markdown)
               8. olap.retention_solution        possible retention solutions, their
                                                 what-if effect, and the recommended one
               9. olap.churn_reason              why customers leave: how often each
                                                 reason factor is the main reason code

   How to run (after 01-07):
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/08_mining_objects.sql
   Re-runnable: yes. The view is replaced; the result tables are created only if
   missing, so earlier mining runs are kept. (A warehouse built before tables 7
   to 9 existed gets them by running this script once more.)

   Design notes
   - The result tables store customer_id (the natural key), NOT customer_key,
     and have NO foreign keys into dw. dw.run_etl() empties and reloads the dw
     tables; a foreign key from olap into dw would block that TRUNCATE, and a
     mining run is a snapshot that should survive a reload.
   - Every child table references olap.mining_run with ON DELETE CASCADE, so an
     old run can be removed with one statement:
         DELETE FROM olap.mining_run WHERE run_id = 3;
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Run this script with -d telco_churn_dw.',
                        current_database();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM dw.fact_customer_churn) THEN
        RAISE EXCEPTION 'The fact table is empty. Run 04_etl_procedure.sql first.';
    END IF;
END
$$;


/* -----------------------------------------------------------------------------
   1. The modelling data set.

   One row per customer, rebuilt from the star schema with the ORIGINAL column
   names and value formats of the IBM file (e.g. "Partner" = 'Yes'/'No',
   "SeniorCitizen" = 0/1). This keeps the model features identical to the
   earlier analysis, so the results can be compared directly.

   - Quoted identifiers ("SeniorCitizen") keep the mixed-case names; PostgreSQL
     would otherwise fold them to lower case.
   - Money columns are cast to double precision (float8): Python receives
     NUMERIC as decimal.Decimal objects, which scikit-learn does not treat as
     numbers.
   - TotalCharges is 0 for the 11 new customers (warehouse rule, Phase 4).
   - source_row_no is the line position in the original CSV. It is used ONLY to
     put the rows in the same order as the file, so that the random 80/20 split
     selects exactly the same customers as the earlier analysis. It is not a
     model feature. (LEFT JOIN: if staging is empty, the value is NULL and the
     script falls back to ordering by customer_id.)
   - customer_id and source_row_no are identifiers and are excluded from the
     features by the Python script.
   ----------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW olap.v_churn_model_input AS
SELECT cu.customer_id,
       r.row_no                                                   AS source_row_no,
       cu.gender                                                  AS "gender",
       CASE WHEN cu.senior_citizen    THEN 1     ELSE 0    END    AS "SeniorCitizen",
       CASE WHEN cu.partner           THEN 'Yes' ELSE 'No' END    AS "Partner",
       CASE WHEN cu.dependents        THEN 'Yes' ELSE 'No' END    AS "Dependents",
       f.tenure::integer                                          AS "tenure",
       CASE WHEN se.phone_service     THEN 'Yes' ELSE 'No' END    AS "PhoneService",
       se.multiple_lines                                          AS "MultipleLines",
       se.internet_service                                        AS "InternetService",
       se.online_security::text                                   AS "OnlineSecurity",
       se.online_backup::text                                     AS "OnlineBackup",
       se.device_protection::text                                 AS "DeviceProtection",
       se.tech_support::text                                      AS "TechSupport",
       se.streaming_tv::text                                      AS "StreamingTV",
       se.streaming_movies::text                                  AS "StreamingMovies",
       co.contract_type                                           AS "Contract",
       CASE WHEN co.paperless_billing THEN 'Yes' ELSE 'No' END    AS "PaperlessBilling",
       pa.payment_method                                          AS "PaymentMethod",
       f.monthly_charges::double precision                        AS "MonthlyCharges",
       f.total_charges::double precision                          AS "TotalCharges",
       ch.churn_flag::integer                                     AS "Churn"
FROM   dw.fact_customer_churn AS f
JOIN   dw.dim_customer AS cu ON cu.customer_key = f.customer_key
JOIN   dw.dim_contract AS co ON co.contract_key = f.contract_key
JOIN   dw.dim_service  AS se ON se.service_key  = f.service_key
JOIN   dw.dim_payment  AS pa ON pa.payment_key  = f.payment_key
JOIN   dw.dim_churn    AS ch ON ch.churn_key    = f.churn_key
LEFT   JOIN staging.telco_raw AS r ON btrim(r.customer_id) = cu.customer_id;

COMMENT ON VIEW olap.v_churn_model_input IS
    'Modelling data set (7,043 rows): 19 features with the original IBM column names + Churn (0/1), rebuilt from the star schema. Read by mining/churn_mining.py.';


/* -----------------------------------------------------------------------------
   2-6. Result tables written by churn_mining.py.
   ----------------------------------------------------------------------------- */
CREATE TABLE IF NOT EXISTS olap.mining_run (
    run_id             INTEGER GENERATED ALWAYS AS IDENTITY,
    run_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    train_rows         INTEGER     NOT NULL,
    test_rows          INTEGER     NOT NULL,
    random_state       INTEGER     NOT NULL,
    scoring_model      TEXT        NOT NULL,   -- model used for olap.customer_churn_score
    tuned_threshold    NUMERIC(4,2),           -- threshold chosen in the tuning example
    sklearn_version    TEXT,
    notes              TEXT,
    CONSTRAINT pk_mining_run PRIMARY KEY (run_id)
);
COMMENT ON TABLE olap.mining_run IS 'One row per execution of mining/churn_mining.py.';

CREATE TABLE IF NOT EXISTS olap.model_metrics (
    run_id             INTEGER        NOT NULL,
    model_name         TEXT           NOT NULL,
    hyperparameters    TEXT,
    accuracy           NUMERIC(6,4),
    precision_score    NUMERIC(6,4),
    recall_score       NUMERIC(6,4),
    f1_score           NUMERIC(6,4),
    roc_auc            NUMERIC(6,4),
    tn                 INTEGER,
    fp                 INTEGER,
    fn                 INTEGER,
    tp                 INTEGER,
    cv_accuracy_mean   NUMERIC(6,4),
    cv_precision_mean  NUMERIC(6,4),
    cv_recall_mean     NUMERIC(6,4),
    cv_f1_mean         NUMERIC(6,4),
    cv_roc_auc_mean    NUMERIC(6,4),
    cv_roc_auc_std     NUMERIC(6,4),
    CONSTRAINT pk_model_metrics PRIMARY KEY (run_id, model_name),
    CONSTRAINT fk_model_metrics_run FOREIGN KEY (run_id)
        REFERENCES olap.mining_run (run_id) ON DELETE CASCADE
);
COMMENT ON TABLE olap.model_metrics IS
    'Per model and run: test-set metrics at threshold 0.5, confusion matrix, and 5-fold stratified CV means.';

CREATE TABLE IF NOT EXISTS olap.model_feature_importance (
    run_id             INTEGER        NOT NULL,
    model_name         TEXT           NOT NULL,
    method             TEXT           NOT NULL,
    feature            TEXT           NOT NULL,
    importance         NUMERIC(10,6)  NOT NULL,
    importance_std     NUMERIC(10,6),
    rank_no            INTEGER        NOT NULL,
    CONSTRAINT pk_model_feature_importance PRIMARY KEY (run_id, model_name, method, feature),
    CONSTRAINT ck_model_feature_importance_method CHECK (method IN ('impurity', 'permutation')),
    CONSTRAINT fk_model_feature_importance_run FOREIGN KEY (run_id)
        REFERENCES olap.mining_run (run_id) ON DELETE CASCADE
);
COMMENT ON TABLE olap.model_feature_importance IS
    'Random Forest impurity importance (one-hot features) and permutation importance (original columns, mean ROC-AUC decrease on the test set).';

CREATE TABLE IF NOT EXISTS olap.model_test_prediction (
    run_id             INTEGER        NOT NULL,
    model_name         TEXT           NOT NULL,
    customer_id        TEXT           NOT NULL,
    actual_churn       SMALLINT       NOT NULL,
    churn_probability  NUMERIC(7,6)   NOT NULL,
    CONSTRAINT pk_model_test_prediction PRIMARY KEY (run_id, model_name, customer_id),
    CONSTRAINT ck_model_test_prediction_actual CHECK (actual_churn IN (0, 1)),
    CONSTRAINT ck_model_test_prediction_prob   CHECK (churn_probability BETWEEN 0 AND 1),
    CONSTRAINT fk_model_test_prediction_run FOREIGN KEY (run_id)
        REFERENCES olap.mining_run (run_id) ON DELETE CASCADE
);
COMMENT ON TABLE olap.model_test_prediction IS
    'Predicted churn probability of every TEST-set customer (1,409) for every model; used for ROC curves and the threshold slider.';

CREATE TABLE IF NOT EXISTS olap.customer_churn_score (
    run_id             INTEGER        NOT NULL,
    customer_id        TEXT           NOT NULL,
    churn_probability  NUMERIC(7,6)   NOT NULL,
    risk_band          TEXT           NOT NULL,
    actual_churn       SMALLINT       NOT NULL,
    CONSTRAINT pk_customer_churn_score PRIMARY KEY (run_id, customer_id),
    CONSTRAINT ck_customer_churn_score_band CHECK (risk_band IN ('High', 'Medium', 'Low')),
    CONSTRAINT ck_customer_churn_score_prob CHECK (churn_probability BETWEEN 0 AND 1),
    CONSTRAINT fk_customer_churn_score_run FOREIGN KEY (run_id)
        REFERENCES olap.mining_run (run_id) ON DELETE CASCADE
);
COMMENT ON TABLE olap.customer_churn_score IS
    'Out-of-fold (5-fold CV) churn probability for every customer: each customer is scored by a model that did not see that customer. Risk band: High >= 0.50, Medium 0.25-0.50, Low < 0.25.';


/* -----------------------------------------------------------------------------
   7-8. Explanation and recommendation, written by mining/insights.py
        (called from churn_mining.py).

   - olap.mining_insight keeps the explanation as Markdown text, one row per
     section, so the dashboard can show it exactly as in insights_report.md.
   - olap.retention_solution has one row per candidate solution. Simulated
     solutions have a rank (1 = most expected churners prevented); solutions
     the model cannot simulate have rank_no NULL. "Expected churners" = sum of
     the predicted churn probabilities of the targeted ACTIVE customers.
   - At most ONE recommended solution per run is enforced with a PARTIAL
     unique index (an index on only the rows WHERE is_recommended).
       MySQL difference: MySQL 8.0 has no partial indexes; the same rule would
       need a trigger or a generated column that is NULL for the other rows.
   ----------------------------------------------------------------------------- */
CREATE TABLE IF NOT EXISTS olap.mining_insight (
    run_id             INTEGER        NOT NULL,
    section_no         INTEGER        NOT NULL,
    section_title      TEXT           NOT NULL,
    body_markdown      TEXT           NOT NULL,
    CONSTRAINT pk_mining_insight PRIMARY KEY (run_id, section_no),
    CONSTRAINT fk_mining_insight_run FOREIGN KEY (run_id)
        REFERENCES olap.mining_run (run_id) ON DELETE CASCADE
);
COMMENT ON TABLE olap.mining_insight IS
    'Plain-English explanation of a mining run (one Markdown section per row), generated by rules from the run''s numbers by mining/insights.py.';

CREATE TABLE IF NOT EXISTS olap.retention_solution (
    run_id                    INTEGER        NOT NULL,
    solution_key              TEXT           NOT NULL,
    solution_name             TEXT           NOT NULL,
    finding                   TEXT           NOT NULL,   -- model column(s) the solution answers
    action                    TEXT           NOT NULL,   -- what the company would do
    target_rule               TEXT           NOT NULL,   -- who is targeted, in words
    simulated                 BOOLEAN        NOT NULL,   -- false = the model cannot estimate the effect
    rank_no                   INTEGER,                   -- 1 = most churners prevented; NULL if not simulated
    is_recommended            BOOLEAN        NOT NULL DEFAULT false,
    target_customers          INTEGER        NOT NULL,   -- active (Churn = 0) customers targeted
    high_risk_customers       INTEGER        NOT NULL,   -- of those, in the High risk band
    expected_churners_before  NUMERIC(10,2)  NOT NULL,
    expected_churners_after   NUMERIC(10,2),
    churners_prevented        NUMERIC(10,2),
    prevented_per_100         NUMERIC(6,2),              -- churners prevented per 100 customers contacted
    revenue_protected         NUMERIC(12,2),             -- monthly revenue of the customers kept
    net_revenue_change        NUMERIC(12,2),             -- only when the offer cost is known (discounts)
    reasons                   TEXT,                      -- why the solution was (not) chosen
    CONSTRAINT pk_retention_solution PRIMARY KEY (run_id, solution_key),
    CONSTRAINT ck_retention_solution_rank CHECK (simulated OR rank_no IS NULL),
    CONSTRAINT fk_retention_solution_run FOREIGN KEY (run_id)
        REFERENCES olap.mining_run (run_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_retention_solution_one_recommended
    ON olap.retention_solution (run_id) WHERE is_recommended;
COMMENT ON TABLE olap.retention_solution IS
    'Candidate retention solutions per mining run with their out-of-fold what-if effect on active customers; is_recommended marks the solution chosen by the decision rule (most expected churners prevented, known cost not above the revenue protected).';


/* -----------------------------------------------------------------------------
   9. Why customers leave (written by mining/insights.py).

   The IBM sample records THAT a customer left, not WHY, so the reasons are
   INFERRED. For every customer the scoring model measures how much each reason
   factor (e.g. contract type) raises the churn probability compared with
   typical values of that factor (a "reason code", in percentage points). Every
   factor that adds at least 5 points is a contributing reason (most leavers
   have several); the largest one is the customer's main reason code.
   reason_key 'none' counts customers without any such factor.
   evidence_strength: Strong = churn-rate gap >= 2 x in the data AND a
   contributing reason for >= 25% of leavers; Moderate = one of the two; Weak = neither.
   ----------------------------------------------------------------------------- */
CREATE TABLE IF NOT EXISTS olap.churn_reason (
    run_id                     INTEGER        NOT NULL,
    reason_key                 TEXT           NOT NULL,
    reason_name                TEXT           NOT NULL,
    factor_columns             TEXT           NOT NULL,   -- model columns the factor changes
    churners_main              INTEGER        NOT NULL,   -- customers who left with this main reason code
    churners_main_pct          NUMERIC(6,2)   NOT NULL,   -- ... as % of all customers who left
    churners_contributing_pct  NUMERIC(6,2),              -- % of leavers for whom it adds >= 5 points
    avg_points_churners        NUMERIC(6,2),              -- average points it adds for the leavers
    at_risk_main               INTEGER        NOT NULL,   -- active High-risk customers with this main reason
    at_risk_main_pct           NUMERIC(6,2)   NOT NULL,
    at_risk_contributing_pct   NUMERIC(6,2),              -- % of active High-risk customers for whom it adds >= 5 points
    data_gap_ratio             NUMERIC(6,2),              -- strongest churn-rate gap in the data (x times)
    evidence_strength          TEXT,
    likely_explanation         TEXT,                      -- interpretation, not measured
    how_to_confirm             TEXT,
    linked_solution            TEXT,
    CONSTRAINT pk_churn_reason PRIMARY KEY (run_id, reason_key),
    CONSTRAINT ck_churn_reason_strength CHECK (evidence_strength IN ('Strong', 'Moderate', 'Weak', '')),
    CONSTRAINT fk_churn_reason_run FOREIGN KEY (run_id)
        REFERENCES olap.mining_run (run_id) ON DELETE CASCADE
);
COMMENT ON TABLE olap.churn_reason IS
    'Why customers leave (inferred): per reason factor, how often it is the main model reason code of the customers who left and of the active High-risk customers, with the evidence strength, likely explanation and how to confirm it.';


/* -----------------------------------------------------------------------------
   Checks.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Phase 7 (SQL part) checks ==='
WITH checks (check_no, check_name, expected, actual) AS (
    VALUES
    (1, 'Rows in olap.v_churn_model_input', '7043',
        (SELECT count(*) FROM olap.v_churn_model_input)::text),
    (2, 'Columns: 2 identifiers + 19 features + Churn', '22',
        (SELECT count(*) FROM information_schema.columns
          WHERE table_schema = 'olap' AND table_name = 'v_churn_model_input')::text),
    (3, 'Churn = 1 in the model input', '1869',
        (SELECT sum("Churn") FROM olap.v_churn_model_input)::text),
    (4, 'Rows with a source_row_no (same order as the CSV)', '7043',
        (SELECT count(source_row_no) FROM olap.v_churn_model_input)::text),
    (5, 'TotalCharges = 0 rows (all with tenure 0)', '11 / 11',
        (SELECT count(*) FILTER (WHERE "TotalCharges" = 0) || ' / ' ||
                count(*) FILTER (WHERE "TotalCharges" = 0 AND "tenure" = 0)
           FROM olap.v_churn_model_input)),
    (6, 'Mining result tables present', '8',
        (SELECT count(*) FROM pg_tables WHERE schemaname = 'olap'
            AND tablename IN ('mining_run', 'model_metrics', 'model_feature_importance',
                              'model_test_prediction', 'customer_churn_score',
                              'mining_insight', 'retention_solution', 'churn_reason'))::text)
)
SELECT check_no, check_name, expected, actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   checks
ORDER  BY check_no;

\echo '=== First 3 rows of the model input ==='
SELECT * FROM olap.v_churn_model_input ORDER BY source_row_no LIMIT 3;
