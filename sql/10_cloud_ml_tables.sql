/* =============================================================================
   10_cloud_ml_tables.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Owners  : MuhammaD_Wallah and the GROUP are the owners and Developers of this model
   Target  : PostgreSQL 15 or later. Written for Neon (serverless PostgreSQL);
             works on a local server too.
   Purpose : Objects used by the deployed web app and the monthly job
               1. staging.telco_raw is made LOGGED (required on Neon, see below)
               2. dw.model_runs           one row per scoring run: model metrics on
                                          today's data + simple data-drift measures
               3. dw.churn_scores         the latest churn probability and risk label
                                          of EVERY customer (written by model/score.py)
               4. olap.v_customer_risk    scores joined to customer attributes (app)
               5. olap.v_model_runs       the run history (app)

   How to run (after 01-08), connected as the OWNER of the database:
       psql "<owner connection string>" -f sql/10_cloud_ml_tables.sql
   Re-runnable: yes. Tables are created only if missing (history is kept);
   the views are replaced.

   Design notes
   - Like the olap mining tables (script 08), dw.churn_scores stores the natural
     key customer_id and has NO foreign key into the star schema, so
     CALL dw.run_etl() can still empty and reload the dimension and fact tables.
   - dw.run_etl() truncates only the seven star-schema tables by name, so the
     two tables below survive a reload.
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Connect to the database telco_churn_dw.', current_database();
    END IF;
    IF to_regclass('olap.v_churn_model_input') IS NULL OR to_regclass('olap.v_customer_churn') IS NULL THEN
        RAISE EXCEPTION 'Run scripts 01-08 first (olap.v_customer_churn and olap.v_churn_model_input are missing).';
    END IF;
END
$$;


/* -----------------------------------------------------------------------------
   1. Make the staging table LOGGED.

   Script 02 creates staging.telco_raw as UNLOGGED (faster loading: no
   write-ahead log). On Neon, UNLOGGED tables live only on the compute's local
   disk and are EMPTIED whenever the compute restarts or scales to zero (after
   a few idle minutes). The validation checks and the ETL read this table, so
   on Neon it must be an ordinary (logged) table. Costs nothing here: 7,043 rows.
   (staging.telco_clean stays UNLOGGED: it is a work table that every ETL run
   empties and fills again.)
   ----------------------------------------------------------------------------- */
ALTER TABLE staging.telco_raw SET LOGGED;


/* -----------------------------------------------------------------------------
   2. dw.model_runs - one row per run of model/score.py.

   test_*     : the model's quality on the untouched test set when it was trained
                (fixed for one model_version, copied from model/metrics.json)
   current_*  : the same metrics on TODAY's customers and labels. Today's data
                includes the training customers, so the level is optimistic:
                watch how it CHANGES from run to run.
   psi_probability / drift_status : Population Stability Index of the churn
                probabilities vs. training time: < 0.10 OK, 0.10-0.25 Watch,
                >= 0.25 Act (see model/drift.py).
   ----------------------------------------------------------------------------- */
CREATE TABLE IF NOT EXISTS dw.model_runs (
    run_id                INTEGER GENERATED ALWAYS AS IDENTITY,
    run_at                TIMESTAMPTZ    NOT NULL DEFAULT now(),
    run_type              TEXT           NOT NULL,
    model_version         TEXT           NOT NULL,
    model_name            TEXT           NOT NULL,
    trained_at            TIMESTAMPTZ,
    sklearn_version       TEXT,
    decision_threshold    NUMERIC(4,3)   NOT NULL,
    customers_scored      INTEGER        NOT NULL,
    high_risk             INTEGER        NOT NULL,
    medium_risk           INTEGER        NOT NULL,
    low_risk              INTEGER        NOT NULL,
    mean_probability      NUMERIC(6,4),
    actual_churn_rate     NUMERIC(6,4),
    test_recall           NUMERIC(6,4),
    test_precision        NUMERIC(6,4),
    test_f1               NUMERIC(6,4),
    test_roc_auc          NUMERIC(6,4),
    current_accuracy      NUMERIC(6,4),
    current_recall        NUMERIC(6,4),
    current_precision     NUMERIC(6,4),
    current_f1            NUMERIC(6,4),
    current_roc_auc       NUMERIC(6,4),
    psi_probability       NUMERIC(8,4),
    drift_status          TEXT,
    mean_tenure           NUMERIC(6,2),
    mean_monthly_charges  NUMERIC(8,2),
    share_month_to_month  NUMERIC(6,4),
    git_commit            TEXT,
    notes                 TEXT,
    CONSTRAINT pk_model_runs PRIMARY KEY (run_id),
    CONSTRAINT ck_model_runs_type   CHECK (run_type IN ('monthly', 'manual', 'retrain', 'local')),
    CONSTRAINT ck_model_runs_drift  CHECK (drift_status IN ('OK', 'Watch', 'Act')),
    CONSTRAINT ck_model_runs_counts CHECK (high_risk + medium_risk + low_risk = customers_scored)
);
COMMENT ON TABLE dw.model_runs IS
    'One row per scoring run (model/score.py): model version, risk counts, test and current metrics, data-drift measures.';


/* -----------------------------------------------------------------------------
   3. dw.churn_scores - the latest score of every customer.
   score.py replaces all rows in ONE transaction, so readers always see either
   the complete old set or the complete new set.
   ----------------------------------------------------------------------------- */
CREATE TABLE IF NOT EXISTS dw.churn_scores (
    customer_id        TEXT           NOT NULL,
    churn_probability  NUMERIC(7,6)   NOT NULL,
    risk_label         TEXT           NOT NULL,
    predicted_churn    BOOLEAN        NOT NULL,   -- probability >= decision threshold
    risk_rank          INTEGER        NOT NULL,   -- 1 = highest probability
    model_version      TEXT           NOT NULL,
    run_id             INTEGER        NOT NULL,
    scored_at          TIMESTAMPTZ    NOT NULL DEFAULT now(),
    CONSTRAINT pk_churn_scores PRIMARY KEY (customer_id),
    CONSTRAINT uq_churn_scores_rank  UNIQUE (risk_rank),
    CONSTRAINT ck_churn_scores_prob  CHECK (churn_probability BETWEEN 0 AND 1),
    CONSTRAINT ck_churn_scores_label CHECK (risk_label IN ('High', 'Medium', 'Low')),
    CONSTRAINT fk_churn_scores_run   FOREIGN KEY (run_id) REFERENCES dw.model_runs (run_id)
);
COMMENT ON TABLE dw.churn_scores IS
    'Latest churn probability and risk label (High/Medium/Low) of every customer, with the model version and scoring time.';


/* -----------------------------------------------------------------------------
   4-5. Views for the web app. A view runs with its OWNER's rights, so the
   read-only app role can read these without any right on the star schema.
   ----------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW olap.v_customer_risk AS
SELECT s.risk_rank,
       s.customer_id,
       s.churn_probability::double precision   AS churn_probability,
       s.risk_label,
       s.predicted_churn,
       v.churn_label                           AS actual_churn,
       v.contract_type,
       v.payment_method,
       v.internet_service,
       v.tech_support::text                    AS tech_support,
       v.online_security::text                 AS online_security,
       v.paperless_billing,
       v.senior_citizen,
       v.tenure,
       v.tenure_band,
       v.monthly_charges::double precision     AS monthly_charges,
       v.total_charges::double precision       AS total_charges,
       s.model_version,
       s.scored_at
FROM   dw.churn_scores        AS s
JOIN   olap.v_customer_churn  AS v ON v.customer_id = s.customer_id;

COMMENT ON VIEW olap.v_customer_risk IS
    'Every scored customer with risk rank, probability, label and the main customer attributes (web app: At-risk customers).';

CREATE OR REPLACE VIEW olap.v_model_runs AS
SELECT * FROM dw.model_runs;

COMMENT ON VIEW olap.v_model_runs IS 'History of scoring runs (web app: Model performance).';


/* -----------------------------------------------------------------------------
   Checks (PASS / FAIL).
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Script 10 checks ==='
WITH checks (check_no, check_name, expected, actual) AS (
    VALUES
    (1, 'staging.telco_raw is LOGGED (p = permanent)', 'p',
        (SELECT relpersistence::text FROM pg_class WHERE oid = 'staging.telco_raw'::regclass)),
    (2, 'staging.telco_raw rows', '7043',
        (SELECT count(*) FROM staging.telco_raw)::text),
    (3, 'dw.model_runs and dw.churn_scores exist', '2',
        (SELECT count(*) FROM pg_tables WHERE schemaname = 'dw'
            AND tablename IN ('model_runs', 'churn_scores'))::text),
    (4, 'olap.v_customer_risk and olap.v_model_runs exist', '2',
        (SELECT count(*) FROM pg_views WHERE schemaname = 'olap'
            AND viewname IN ('v_customer_risk', 'v_model_runs'))::text),
    (5, 'Materialized views in olap, all populated', '7',
        (SELECT count(*) FROM pg_matviews WHERE schemaname = 'olap' AND ispopulated)::text)
)
SELECT check_no, check_name, expected, actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   checks
ORDER  BY check_no;

\echo 'Next: create the read-only role for the app with sql/11_app_reader_role.sql,'
\echo 'then train (python model/train.py) and score (python model/score.py).'
