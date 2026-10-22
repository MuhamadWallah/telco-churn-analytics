/* =============================================================================
   03_star_schema_ddl.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16 and 18)
   Purpose : Layer 2 - create the star schema in the dw schema:
               6 dimension tables  dim_customer, dim_contract, dim_service,
                                   dim_payment, dim_churn, dim_tenure (added)
               1 fact table        fact_customer_churn  (grain: 1 row per customer)
             with surrogate keys, primary keys, unique keys, foreign keys,
             CHECK constraints and indexes. The tables are created EMPTY;
             they are filled by dw.run_etl() in 04_etl_procedure.sql.

   How to run:
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/03_star_schema_ddl.sql

   Re-runnable : yes, but it DROPS and recreates every dw table. DROP ... CASCADE
                 also removes any olap view built on these tables, so after
                 re-running this script run 04 (CALL dw.run_etl()) and 07 again.

   Naming conventions used for constraints and indexes:
       pk_<table>            primary key
       uq_<table>_<what>     unique key (natural key / one row per combination)
       ck_<table>_<what>     CHECK rule
       fk_<table>_<dim>      foreign key from the fact table to a dimension
       ix_<table>_<column>   ordinary index
   ============================================================================= */

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Run this script with -d telco_churn_dw.',
                        current_database();
    END IF;
END
$$;


/* -----------------------------------------------------------------------------
   Step 0. Remove the previous version (fact table first, then dimensions).
   CASCADE also drops objects that depend on these tables (olap views).
   ----------------------------------------------------------------------------- */
DROP TABLE IF EXISTS dw.fact_customer_churn CASCADE;
DROP TABLE IF EXISTS dw.dim_customer, dw.dim_contract, dw.dim_service,
                     dw.dim_payment,  dw.dim_churn,    dw.dim_tenure CASCADE;
DROP DOMAIN IF EXISTS dw.addon_status;


/* -----------------------------------------------------------------------------
   Step 1. A DOMAIN for the six internet add-on columns.

   A domain is a named data type with a rule attached. The six add-on columns
   (online_security ... streaming_movies) all allow exactly the same three
   values, so we define the rule ONCE and reuse it six times.

   'No internet service' is kept as its own value on purpose (design decision C):
   "declined the add-on" (No) and "cannot have the add-on" (No internet service)
   are different business situations, and NULL would be confused with the
   subtotal rows produced by ROLLUP and CUBE.

   MySQL has no domains. A MySQL design would repeat
   ENUM('Yes','No','No internet service') on each column. PostgreSQL also has
   enum types (CREATE TYPE ... AS ENUM), but a TEXT domain is easier to change
   and reaches Python and BI tools as plain text.
   ----------------------------------------------------------------------------- */
CREATE DOMAIN dw.addon_status AS TEXT
    CONSTRAINT ck_addon_status CHECK (VALUE IN ('Yes', 'No', 'No internet service'));

COMMENT ON DOMAIN dw.addon_status IS
    'Status of an internet add-on: Yes, No, or No internet service (customer has no internet).';


/* -----------------------------------------------------------------------------
   Step 2. Dimension tables.

   Common features (and the MySQL differences):
     - Surrogate key: INTEGER GENERATED ALWAYS AS IDENTITY.
         MySQL: INT AUTO_INCREMENT. "ALWAYS" means PostgreSQL refuses a
         hand-typed key value, so keys can only come from the sequence.
     - Yes/No and 0/1 flags: BOOLEAN (true/false).
         MySQL: BOOLEAN is only an alias for TINYINT(1), which also accepts
         2, 3, -1 ... PostgreSQL BOOLEAN accepts only true / false (/ NULL).
     - Text labels: TEXT + a CHECK list of allowed values.
         In PostgreSQL VARCHAR(n) and TEXT are stored the same way; (n) is only
         a length check, so a list of allowed values is the stronger rule.
         MySQL only enforces CHECK constraints from version 8.0.16.
     - NOT NULL everywhere: a dimension attribute should never be unknown.
   ----------------------------------------------------------------------------- */

-- 2.1 dim_customer: who the customer is (one row per customer).
CREATE TABLE dw.dim_customer (
    customer_key    INTEGER GENERATED ALWAYS AS IDENTITY,
    customer_id     TEXT     NOT NULL,
    gender          TEXT     NOT NULL,
    senior_citizen  BOOLEAN  NOT NULL,
    partner         BOOLEAN  NOT NULL,
    dependents      BOOLEAN  NOT NULL,
    CONSTRAINT pk_dim_customer              PRIMARY KEY (customer_key),
    CONSTRAINT uq_dim_customer_customer_id  UNIQUE (customer_id),
    CONSTRAINT ck_dim_customer_id_format    CHECK (customer_id ~ '^[0-9]{4}-[A-Z]{5}$'),
    CONSTRAINT ck_dim_customer_gender       CHECK (gender IN ('Female', 'Male'))
);
COMMENT ON TABLE  dw.dim_customer                IS 'Customer demographics. One row per customer (7,043).';
COMMENT ON COLUMN dw.dim_customer.customer_key   IS 'Surrogate key generated by the warehouse.';
COMMENT ON COLUMN dw.dim_customer.customer_id    IS 'Natural key from the source system (CSV customerID), e.g. 7590-VHVEG.';
COMMENT ON COLUMN dw.dim_customer.gender         IS 'Female or Male.';
COMMENT ON COLUMN dw.dim_customer.senior_citizen IS 'true = senior citizen (CSV SeniorCitizen = 1).';
COMMENT ON COLUMN dw.dim_customer.partner        IS 'true = has a partner (CSV Partner = Yes).';
COMMENT ON COLUMN dw.dim_customer.dependents     IS 'true = has dependents (CSV Dependents = Yes).';


-- 2.2 dim_contract: contract type and billing preference (6 combinations).
CREATE TABLE dw.dim_contract (
    contract_key       INTEGER GENERATED ALWAYS AS IDENTITY,
    contract_type      TEXT     NOT NULL,
    paperless_billing  BOOLEAN  NOT NULL,
    CONSTRAINT pk_dim_contract              PRIMARY KEY (contract_key),
    CONSTRAINT uq_dim_contract_combination  UNIQUE (contract_type, paperless_billing),
    CONSTRAINT ck_dim_contract_type         CHECK (contract_type IN ('Month-to-month', 'One year', 'Two year'))
);
COMMENT ON TABLE  dw.dim_contract                   IS 'Contract type x paperless billing. One row per combination (3 x 2 = 6).';
COMMENT ON COLUMN dw.dim_contract.contract_key      IS 'Surrogate key generated by the warehouse.';
COMMENT ON COLUMN dw.dim_contract.contract_type     IS 'Month-to-month, One year or Two year.';
COMMENT ON COLUMN dw.dim_contract.paperless_billing IS 'true = paperless (electronic) billing.';


-- 2.3 dim_service: junk dimension - one row per DISTINCT combination of the
--     nine service attributes that actually occurs in the data (322).
CREATE TABLE dw.dim_service (
    service_key        INTEGER GENERATED ALWAYS AS IDENTITY,
    phone_service      BOOLEAN          NOT NULL,
    multiple_lines     TEXT             NOT NULL,
    internet_service   TEXT             NOT NULL,
    online_security    dw.addon_status  NOT NULL,
    online_backup      dw.addon_status  NOT NULL,
    device_protection  dw.addon_status  NOT NULL,
    tech_support       dw.addon_status  NOT NULL,
    streaming_tv       dw.addon_status  NOT NULL,
    streaming_movies   dw.addon_status  NOT NULL,
    CONSTRAINT pk_dim_service PRIMARY KEY (service_key),
    -- the same combination may not be stored twice
    CONSTRAINT uq_dim_service_combination UNIQUE (
        phone_service, multiple_lines, internet_service,
        online_security, online_backup, device_protection,
        tech_support, streaming_tv, streaming_movies),
    CONSTRAINT ck_dim_service_multiple_lines   CHECK (multiple_lines   IN ('Yes', 'No', 'No phone service')),
    CONSTRAINT ck_dim_service_internet_service CHECK (internet_service IN ('DSL', 'Fiber optic', 'No')),
    -- consistency rule 1: no phone service  <=>  multiple_lines = 'No phone service'
    CONSTRAINT ck_dim_service_phone_rule CHECK (
        (NOT phone_service) = (multiple_lines = 'No phone service')),
    -- consistency rule 2: no internet  <=>  every add-on = 'No internet service'
    -- (comparing two booleans with "=" means "both true or both false")
    CONSTRAINT ck_dim_service_internet_rule CHECK (
            (internet_service = 'No') = (online_security   = 'No internet service')
        AND (internet_service = 'No') = (online_backup     = 'No internet service')
        AND (internet_service = 'No') = (device_protection = 'No internet service')
        AND (internet_service = 'No') = (tech_support      = 'No internet service')
        AND (internet_service = 'No') = (streaming_tv      = 'No internet service')
        AND (internet_service = 'No') = (streaming_movies  = 'No internet service'))
);
COMMENT ON TABLE  dw.dim_service                   IS 'Junk dimension: one row per distinct combination of the 9 service attributes found in the data (322).';
COMMENT ON COLUMN dw.dim_service.service_key       IS 'Surrogate key generated by the warehouse.';
COMMENT ON COLUMN dw.dim_service.phone_service     IS 'true = has phone service.';
COMMENT ON COLUMN dw.dim_service.multiple_lines    IS 'Yes, No, or No phone service.';
COMMENT ON COLUMN dw.dim_service.internet_service  IS 'DSL, Fiber optic, or No.';
COMMENT ON COLUMN dw.dim_service.online_security   IS 'Yes, No, or No internet service.';
COMMENT ON COLUMN dw.dim_service.online_backup     IS 'Yes, No, or No internet service.';
COMMENT ON COLUMN dw.dim_service.device_protection IS 'Yes, No, or No internet service.';
COMMENT ON COLUMN dw.dim_service.tech_support      IS 'Yes, No, or No internet service.';
COMMENT ON COLUMN dw.dim_service.streaming_tv      IS 'Yes, No, or No internet service.';
COMMENT ON COLUMN dw.dim_service.streaming_movies  IS 'Yes, No, or No internet service.';


-- 2.4 dim_payment: how the customer pays (4 methods).
CREATE TABLE dw.dim_payment (
    payment_key     INTEGER GENERATED ALWAYS AS IDENTITY,
    payment_method  TEXT NOT NULL,
    CONSTRAINT pk_dim_payment         PRIMARY KEY (payment_key),
    CONSTRAINT uq_dim_payment_method  UNIQUE (payment_method),
    CONSTRAINT ck_dim_payment_method  CHECK (payment_method IN (
        'Electronic check', 'Mailed check',
        'Bank transfer (automatic)', 'Credit card (automatic)'))
);
COMMENT ON TABLE  dw.dim_payment                IS 'Payment method. One row per method (4).';
COMMENT ON COLUMN dw.dim_payment.payment_key    IS 'Surrogate key generated by the warehouse.';
COMMENT ON COLUMN dw.dim_payment.payment_method IS 'Electronic check, Mailed check, Bank transfer (automatic) or Credit card (automatic).';


-- 2.5 dim_churn: the outcome (2 rows).
--     churn_flag stays SMALLINT 0/1 (not BOOLEAN) because it is the modelling
--     target defined as 0/1, and AVG(churn_flag) gives the churn rate directly.
CREATE TABLE dw.dim_churn (
    churn_key    INTEGER GENERATED ALWAYS AS IDENTITY,
    churn_label  TEXT      NOT NULL,
    churn_flag   SMALLINT  NOT NULL,
    CONSTRAINT pk_dim_churn             PRIMARY KEY (churn_key),
    CONSTRAINT uq_dim_churn_label       UNIQUE (churn_label),
    CONSTRAINT uq_dim_churn_flag        UNIQUE (churn_flag),
    CONSTRAINT ck_dim_churn_label       CHECK (churn_label IN ('No', 'Yes')),
    CONSTRAINT ck_dim_churn_flag        CHECK (churn_flag IN (0, 1)),
    -- label and flag must agree: Yes <=> 1, No <=> 0
    CONSTRAINT ck_dim_churn_label_flag  CHECK ((churn_label = 'Yes') = (churn_flag = 1))
);
COMMENT ON TABLE  dw.dim_churn             IS 'Churn outcome. 2 rows: No (0) and Yes (1).';
COMMENT ON COLUMN dw.dim_churn.churn_key   IS 'Surrogate key generated by the warehouse.';
COMMENT ON COLUMN dw.dim_churn.churn_label IS 'No = still a customer, Yes = left within the last month (CSV Churn).';
COMMENT ON COLUMN dw.dim_churn.churn_flag  IS 'Numeric target for data mining: 0 = No, 1 = Yes. AVG(churn_flag) = churn rate.';


-- 2.6 dim_tenure (ADDED, 6th dimension): drill-down hierarchy for tenure,
--     used instead of a calendar time dimension because the source has no dates.
--         tenure_group  (2)  ->  tenure_band  (4)  ->  tenure_months  (73)
--     Only tenure_months is inserted. The band and group columns are GENERATED
--     columns: PostgreSQL calculates them from tenure_months, so the hierarchy
--     can never be inconsistent (MySQL 8 has the same feature: ... AS (expr) STORED).
--     The *_order columns give the correct sort order in reports.
CREATE TABLE dw.dim_tenure (
    tenure_key          INTEGER GENERATED ALWAYS AS IDENTITY,
    tenure_months       SMALLINT NOT NULL,
    tenure_band         TEXT GENERATED ALWAYS AS (
                            CASE WHEN tenure_months <= 12 THEN '0-12 months'
                                 WHEN tenure_months <= 24 THEN '13-24 months'
                                 WHEN tenure_months <= 48 THEN '25-48 months'
                                 ELSE                          '49-72 months' END) STORED NOT NULL,
    tenure_band_order   SMALLINT GENERATED ALWAYS AS (
                            CASE WHEN tenure_months <= 12 THEN 1
                                 WHEN tenure_months <= 24 THEN 2
                                 WHEN tenure_months <= 48 THEN 3
                                 ELSE                          4 END) STORED NOT NULL,
    tenure_group        TEXT GENERATED ALWAYS AS (
                            CASE WHEN tenure_months <= 24 THEN '0-24 months'
                                 ELSE                          '25-72 months' END) STORED NOT NULL,
    tenure_group_order  SMALLINT GENERATED ALWAYS AS (
                            CASE WHEN tenure_months <= 24 THEN 1 ELSE 2 END) STORED NOT NULL,
    CONSTRAINT pk_dim_tenure         PRIMARY KEY (tenure_key),
    CONSTRAINT uq_dim_tenure_months  UNIQUE (tenure_months),
    CONSTRAINT ck_dim_tenure_range   CHECK (tenure_months BETWEEN 0 AND 72)
);
COMMENT ON TABLE  dw.dim_tenure                    IS 'Added dimension: tenure hierarchy group -> band -> month, one row per tenure month 0-72 (73). Substitute for a time dimension (no dates in the source).';
COMMENT ON COLUMN dw.dim_tenure.tenure_key         IS 'Surrogate key generated by the warehouse.';
COMMENT ON COLUMN dw.dim_tenure.tenure_months      IS 'Months the customer has stayed with the company (lowest level).';
COMMENT ON COLUMN dw.dim_tenure.tenure_band        IS 'Middle level: 0-12, 13-24, 25-48 or 49-72 months (generated).';
COMMENT ON COLUMN dw.dim_tenure.tenure_band_order  IS 'Sort order of tenure_band, 1-4 (generated).';
COMMENT ON COLUMN dw.dim_tenure.tenure_group       IS 'Top level: 0-24 months or 25-72 months (generated).';
COMMENT ON COLUMN dw.dim_tenure.tenure_group_order IS 'Sort order of tenure_group, 1-2 (generated).';


/* -----------------------------------------------------------------------------
   Step 3. Fact table.

   Grain: ONE ROW PER CUSTOMER. customer_key is both the primary key and a
   foreign key, so the database itself rejects a second row for a customer.

   Measures:
     tenure           months with the company (average it; summing it means little)
     monthly_charges  current monthly bill   (additive: SUM = monthly revenue)
     total_charges    lifetime billed amount (additive)
     customer_count   always 1 (added), so SUM(customer_count) counts customers
                      in any tool, exactly like COUNT(*)

   Money uses NUMERIC(p,2): exact decimal arithmetic, no rounding errors
   (never FLOAT for money). MySQL: DECIMAL(10,2) - same idea, different name.

   Foreign keys use the default rule (NO ACTION): a dimension row that is still
   referenced by a fact row cannot be deleted.
   ----------------------------------------------------------------------------- */
CREATE TABLE dw.fact_customer_churn (
    customer_key     INTEGER        NOT NULL,
    contract_key     INTEGER        NOT NULL,
    service_key      INTEGER        NOT NULL,
    payment_key      INTEGER        NOT NULL,
    churn_key        INTEGER        NOT NULL,
    tenure_key       INTEGER        NOT NULL,
    tenure           SMALLINT       NOT NULL,
    monthly_charges  NUMERIC(8,2)   NOT NULL,
    total_charges    NUMERIC(10,2)  NOT NULL,
    customer_count   SMALLINT       NOT NULL DEFAULT 1,
    CONSTRAINT pk_fact_customer_churn PRIMARY KEY (customer_key),
    CONSTRAINT fk_fact_customer_churn_customer FOREIGN KEY (customer_key) REFERENCES dw.dim_customer (customer_key),
    CONSTRAINT fk_fact_customer_churn_contract FOREIGN KEY (contract_key) REFERENCES dw.dim_contract (contract_key),
    CONSTRAINT fk_fact_customer_churn_service  FOREIGN KEY (service_key)  REFERENCES dw.dim_service  (service_key),
    CONSTRAINT fk_fact_customer_churn_payment  FOREIGN KEY (payment_key)  REFERENCES dw.dim_payment  (payment_key),
    CONSTRAINT fk_fact_customer_churn_churn    FOREIGN KEY (churn_key)    REFERENCES dw.dim_churn    (churn_key),
    CONSTRAINT fk_fact_customer_churn_tenure   FOREIGN KEY (tenure_key)   REFERENCES dw.dim_tenure   (tenure_key),
    CONSTRAINT ck_fact_customer_churn_tenure          CHECK (tenure BETWEEN 0 AND 72),
    CONSTRAINT ck_fact_customer_churn_monthly         CHECK (monthly_charges >= 0),
    CONSTRAINT ck_fact_customer_churn_total           CHECK (total_charges   >= 0),
    CONSTRAINT ck_fact_customer_churn_count           CHECK (customer_count = 1),
    -- design decision A: a customer with tenure 0 has not been billed yet
    CONSTRAINT ck_fact_customer_churn_new_customer    CHECK (tenure > 0 OR total_charges = 0)
);
COMMENT ON TABLE  dw.fact_customer_churn                 IS 'Fact table. Grain: one row per customer (7,043). Links the six dimensions and holds the measures.';
COMMENT ON COLUMN dw.fact_customer_churn.customer_key    IS 'PK and FK to dim_customer (enforces one row per customer).';
COMMENT ON COLUMN dw.fact_customer_churn.contract_key    IS 'FK to dim_contract.';
COMMENT ON COLUMN dw.fact_customer_churn.service_key     IS 'FK to dim_service.';
COMMENT ON COLUMN dw.fact_customer_churn.payment_key     IS 'FK to dim_payment.';
COMMENT ON COLUMN dw.fact_customer_churn.churn_key       IS 'FK to dim_churn.';
COMMENT ON COLUMN dw.fact_customer_churn.tenure_key      IS 'FK to dim_tenure (added dimension).';
COMMENT ON COLUMN dw.fact_customer_churn.tenure          IS 'Measure: months with the company (0-72). Use AVG, not SUM.';
COMMENT ON COLUMN dw.fact_customer_churn.monthly_charges IS 'Measure: current monthly charge (additive).';
COMMENT ON COLUMN dw.fact_customer_churn.total_charges   IS 'Measure: total charged to date (additive). 0.00 for the 11 customers with tenure 0 (blank in the CSV).';
COMMENT ON COLUMN dw.fact_customer_churn.customer_count  IS 'Measure (added): always 1, so SUM(customer_count) = number of customers.';


/* -----------------------------------------------------------------------------
   Step 4. Indexes on the fact table's foreign keys.

   PostgreSQL does NOT create indexes on foreign-key columns automatically
   (MySQL/InnoDB does). Without them, joins and filters by a dimension, and
   deleting a dimension row (PostgreSQL must check that no fact row uses it),
   would scan the whole fact table. customer_key is already indexed by the
   primary key. With only 7,043 rows the planner may still prefer a full scan,
   but the indexes matter as soon as the fact table grows.
   ----------------------------------------------------------------------------- */
CREATE INDEX ix_fact_customer_churn_contract_key ON dw.fact_customer_churn (contract_key);
CREATE INDEX ix_fact_customer_churn_service_key  ON dw.fact_customer_churn (service_key);
CREATE INDEX ix_fact_customer_churn_payment_key  ON dw.fact_customer_churn (payment_key);
CREATE INDEX ix_fact_customer_churn_churn_key    ON dw.fact_customer_churn (churn_key);
CREATE INDEX ix_fact_customer_churn_tenure_key   ON dw.fact_customer_churn (tenure_key);


/* -----------------------------------------------------------------------------
   Step 5. Data dictionary (useful for the written report).
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Data dictionary: dw tables ==='
SELECT c.relname                                   AS table_name,
       count(a.attnum)                             AS columns,
       obj_description(c.oid, 'pg_class')          AS description
FROM   pg_class AS c
JOIN   pg_namespace AS n ON n.oid = c.relnamespace
JOIN   pg_attribute AS a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE  n.nspname = 'dw' AND c.relkind = 'r'
  AND  c.relname ~ '^(dim|fact)_'      -- star-schema tables only (not the ETL log)
GROUP  BY c.oid, c.relname
ORDER  BY c.relname LIKE 'fact%', c.relname;

\echo '=== Data dictionary: dw columns ==='
SELECT c.relname                                         AS table_name,
       a.attname                                         AS column_name,
       format_type(a.atttypid, a.atttypmod)              AS data_type,
       CASE WHEN a.attnotnull THEN 'NOT NULL' ELSE '' END AS nullable,
       CASE WHEN a.attidentity = 'a' THEN 'identity (always)'
            WHEN a.attgenerated = 's' THEN 'generated'
            ELSE '' END                                  AS filled_by,
       col_description(c.oid, a.attnum)                  AS description
FROM   pg_class AS c
JOIN   pg_namespace AS n ON n.oid = c.relnamespace
JOIN   pg_attribute AS a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE  n.nspname = 'dw' AND c.relkind = 'r'
  AND  c.relname ~ '^(dim|fact)_'      -- star-schema tables only (not the ETL log)
ORDER  BY c.relname LIKE 'fact%', c.relname, a.attnum;


/* -----------------------------------------------------------------------------
   Step 6. Constraint tests: try to insert BAD data and confirm it is rejected.

   pg_temp.expect_error() runs one statement. If the statement fails with the
   expected error code it returns PASS; if it succeeds it returns FAIL. Either
   way the statement is rolled back (the BEGIN ... EXCEPTION block is a
   savepoint), so the tables stay empty.
   PostgreSQL error codes: 23514 check_violation, 23505 unique_violation,
   23503 foreign_key_violation, 428C9 generated_always (writing to an IDENTITY
   ALWAYS or a generated column).
   ----------------------------------------------------------------------------- */
CREATE FUNCTION pg_temp.expect_error(p_sql text, p_expected_sqlstate text)
RETURNS text
LANGUAGE plpgsql
AS $$
BEGIN
    EXECUTE p_sql;
    -- reaching this line means the bad row was ACCEPTED; force a rollback
    RAISE EXCEPTION 'statement succeeded' USING ERRCODE = 'P0001';
EXCEPTION WHEN OTHERS THEN
    RETURN CASE WHEN SQLSTATE = p_expected_sqlstate THEN 'PASS'
                ELSE format('FAIL (%s: %s)', SQLSTATE, SQLERRM) END;
END
$$;

\echo ''
\echo '=== Constraint tests (each bad row must be rejected) ==='
SELECT test_no,
       rule_tested,
       expected_error,
       pg_temp.expect_error(test_sql, sqlstate) AS result
FROM (VALUES
    (1,  'Gender must be Female or Male', 'check_violation', '23514',
         $q$INSERT INTO dw.dim_customer (customer_id, gender, senior_citizen, partner, dependents)
            VALUES ('0000-TESTA', 'M', false, false, false)$q$),
    (2,  'customer_id must look like 7590-VHVEG', 'check_violation', '23514',
         $q$INSERT INTO dw.dim_customer (customer_id, gender, senior_citizen, partner, dependents)
            VALUES ('ABC', 'Male', false, false, false)$q$),
    (3,  'customer_id must be unique', 'unique_violation', '23505',
         $q$INSERT INTO dw.dim_customer (customer_id, gender, senior_citizen, partner, dependents)
            VALUES ('0000-TESTA', 'Male', false, false, false),
                   ('0000-TESTA', 'Female', true, true, true)$q$),
    (4,  'Surrogate key cannot be typed by hand (IDENTITY ALWAYS)', 'generated_always', '428C9',
         $q$INSERT INTO dw.dim_payment (payment_key, payment_method) VALUES (1, 'Mailed check')$q$),
    (5,  'Same contract combination cannot be stored twice', 'unique_violation', '23505',
         $q$INSERT INTO dw.dim_contract (contract_type, paperless_billing)
            VALUES ('One year', true), ('One year', true)$q$),
    (6,  'No internet => add-ons must be No internet service', 'check_violation', '23514',
         $q$INSERT INTO dw.dim_service (phone_service, multiple_lines, internet_service, online_security,
                online_backup, device_protection, tech_support, streaming_tv, streaming_movies)
            VALUES (true, 'No', 'No', 'Yes', 'No internet service', 'No internet service',
                    'No internet service', 'No internet service', 'No internet service')$q$),
    (7,  'No phone service => multiple_lines = No phone service', 'check_violation', '23514',
         $q$INSERT INTO dw.dim_service (phone_service, multiple_lines, internet_service, online_security,
                online_backup, device_protection, tech_support, streaming_tv, streaming_movies)
            VALUES (false, 'Yes', 'DSL', 'Yes', 'No', 'No', 'No', 'No', 'No')$q$),
    (8,  'Add-on value must be in the domain list', 'check_violation', '23514',
         $q$INSERT INTO dw.dim_service (phone_service, multiple_lines, internet_service, online_security,
                online_backup, device_protection, tech_support, streaming_tv, streaming_movies)
            VALUES (true, 'No', 'DSL', 'Maybe', 'No', 'No', 'No', 'No', 'No')$q$),
    (9,  'Churn label and flag must agree', 'check_violation', '23514',
         $q$INSERT INTO dw.dim_churn (churn_label, churn_flag) VALUES ('Yes', 0)$q$),
    (10, 'Tenure band is calculated, cannot be typed', 'generated_always', '428C9',
         $q$INSERT INTO dw.dim_tenure (tenure_months, tenure_band) VALUES (5, '49-72 months')$q$),
    (11, 'Tenure must be 0-72 months', 'check_violation', '23514',
         $q$INSERT INTO dw.dim_tenure (tenure_months) VALUES (80)$q$),
    (12, 'Fact row must point to existing dimension rows', 'foreign_key_violation', '23503',
         $q$INSERT INTO dw.fact_customer_churn (customer_key, contract_key, service_key, payment_key,
                churn_key, tenure_key, tenure, monthly_charges, total_charges)
            VALUES (999999, 1, 1, 1, 1, 1, 10, 50.00, 500.00)$q$),
    (13, 'Charges cannot be negative', 'check_violation', '23514',
         $q$INSERT INTO dw.fact_customer_churn (customer_key, contract_key, service_key, payment_key,
                churn_key, tenure_key, tenure, monthly_charges, total_charges)
            VALUES (1, 1, 1, 1, 1, 1, 10, -5.00, 500.00)$q$),
    (14, 'Tenure 0 => total_charges must be 0', 'check_violation', '23514',
         $q$INSERT INTO dw.fact_customer_churn (customer_key, contract_key, service_key, payment_key,
                churn_key, tenure_key, tenure, monthly_charges, total_charges)
            VALUES (1, 1, 1, 1, 1, 1, 0, 50.00, 50.00)$q$),
    (15, 'customer_count must be 1', 'check_violation', '23514',
         $q$INSERT INTO dw.fact_customer_churn (customer_key, contract_key, service_key, payment_key,
                churn_key, tenure_key, tenure, monthly_charges, total_charges, customer_count)
            VALUES (1, 1, 1, 1, 1, 1, 10, 50.00, 500.00, 2)$q$)
) AS t (test_no, rule_tested, expected_error, sqlstate, test_sql)
ORDER BY test_no;

-- The failed test inserts still used up identity numbers. Reset every identity
-- so the ETL's first rows get key 1. (The tables are empty, so nothing is lost.)
TRUNCATE dw.fact_customer_churn, dw.dim_customer, dw.dim_contract, dw.dim_service,
         dw.dim_payment, dw.dim_churn, dw.dim_tenure RESTART IDENTITY;


/* -----------------------------------------------------------------------------
   Step 7. Phase 3 checks (PASS / FAIL).
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Phase 3 checks ==='
WITH dw_tables AS (
    SELECT c.oid, c.relname
    FROM   pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE  n.nspname = 'dw' AND c.relkind = 'r'
      AND  c.relname ~ '^(dim|fact)_'      -- star-schema tables only (not the ETL log)
),
fk_cols AS (            -- the single column of each foreign key on the fact table
    SELECT con.conkey[1] AS attnum
    FROM   pg_constraint con
    WHERE  con.conrelid = 'dw.fact_customer_churn'::regclass AND con.contype = 'f'
),
checks (check_no, check_name, expected, actual) AS (
    VALUES
    (1, 'Tables in dw (6 dimensions + 1 fact)',
        7::bigint, (SELECT count(*) FROM dw_tables)),
    (2, 'Primary keys',
        7, (SELECT count(*) FROM pg_constraint
             WHERE contype = 'p' AND conrelid IN (SELECT oid FROM dw_tables))),
    (3, 'Foreign keys on fact_customer_churn',
        6, (SELECT count(*) FROM fk_cols)),
    (4, 'Foreign-key columns with an index (leading column)',
        6, (SELECT count(*) FROM fk_cols f
             WHERE EXISTS (SELECT 1 FROM pg_index i
                            WHERE i.indrelid = 'dw.fact_customer_churn'::regclass
                              AND i.indkey[0] = f.attnum))),
    (5, 'Surrogate keys defined as IDENTITY ALWAYS',
        6, (SELECT count(*) FROM pg_attribute
             WHERE attrelid IN (SELECT oid FROM dw_tables) AND attidentity = 'a')),
    (6, 'CHECK constraints on dw tables',
        17, (SELECT count(*) FROM pg_constraint
              WHERE contype = 'c' AND conrelid IN (SELECT oid FROM dw_tables))),
    (7, 'Domain dw.addon_status used by add-on columns',
        6, (SELECT count(*) FROM information_schema.columns
             WHERE table_schema = 'dw' AND domain_name = 'addon_status')),
    (8, 'Rows in dw tables (empty until the ETL runs)',
        0, (SELECT (SELECT count(*) FROM dw.dim_customer) + (SELECT count(*) FROM dw.dim_contract)
                 + (SELECT count(*) FROM dw.dim_service)  + (SELECT count(*) FROM dw.dim_payment)
                 + (SELECT count(*) FROM dw.dim_churn)    + (SELECT count(*) FROM dw.dim_tenure)
                 + (SELECT count(*) FROM dw.fact_customer_churn)))
)
SELECT check_no,
       check_name,
       expected,
       actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   checks
ORDER  BY check_no;
