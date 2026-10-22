/* =============================================================================
   02_staging_load.sql
   -----------------------------------------------------------------------------
   Project : Customer-churn data warehouse (IBM Telco Customer Churn sample)
   Target  : PostgreSQL 15 or later (tested on PostgreSQL 16 and 18)
   Purpose : Layer 1 - load Telco-Customer-Churn.csv, unchanged, into
             staging.telco_raw, then profile the raw data (data-quality report).

   How to run (from the PROJECT FOLDER, because the CSV path below is relative):
       cd "M:\PROJECTS\Telco DataWarehouse"
       psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/02_staging_load.sql

   Re-runnable : yes. The staging table is created once and EMPTIED (TRUNCATE ...
                 RESTART IDENTITY) before every load, so the file is always loaded
                 into an empty table. It is not dropped, because later objects depend
                 on it (the mining view of script 08), and PostgreSQL refuses to drop
                 a table that a view still uses.

   Why staging keeps everything as TEXT:
     - Loading never fails because of a bad value (for example the blank
       TotalCharges). Every row arrives, and problems are found by the profiling
       queries below instead of by a failed load.
     - We keep an exact copy of the source, so any number in the warehouse can be
       traced back to the original line in the file.
     - Cleaning and type conversion happen later, in one place: dw.run_etl().
   ============================================================================= */

\set ON_ERROR_STOP on

-- Make sure we are in the right database (the script does not switch for you).
DO $$
BEGIN
    IF current_database() <> 'telco_churn_dw' THEN
        RAISE EXCEPTION 'Connected to "%". Run this script with -d telco_churn_dw.',
                        current_database();
    END IF;
END
$$;


/* -----------------------------------------------------------------------------
   Step 1. Create the staging table.

   - Column names are the CSV headers converted to lower-case snake_case, in the
     SAME ORDER as the file (customerID -> customer_id, SeniorCitizen ->
     senior_citizen, ...). PostgreSQL folds unquoted names to lower case, so
     keeping "customerID" would force us to write it in double quotes forever.
   - No primary key, NOT NULL or CHECK constraints on the source columns: staging
     must accept the data exactly as it is, even if it is wrong.
   - Two audit columns are added (they are not loaded from the file):
       row_no    : position of the line in the file (1 = first data line)
       loaded_at : when the load ran
   - UNLOGGED: PostgreSQL skips the write-ahead log for this table, which makes
     loading faster. The price is that the table is emptied after a server crash,
     which is acceptable for staging because we can simply reload the CSV.
     (MySQL has no direct equivalent.)

   MySQL differences:
     - MySQL TEXT is a "large object" type with limits (no DEFAULT before 8.0.13,
       prefix-only indexes). PostgreSQL TEXT is just a string of any length and is
       exactly as fast as VARCHAR(n).
     - AUTO_INCREMENT  ->  GENERATED ALWAYS AS IDENTITY (standard SQL).
     - DATETIME / CURRENT_TIMESTAMP  ->  TIMESTAMPTZ / now() (stores the time zone).
   ----------------------------------------------------------------------------- */
CREATE UNLOGGED TABLE IF NOT EXISTS staging.telco_raw (
    row_no             INTEGER GENERATED ALWAYS AS IDENTITY,
    customer_id        TEXT,   -- CSV: customerID
    gender             TEXT,   -- CSV: gender
    senior_citizen     TEXT,   -- CSV: SeniorCitizen     ('0' / '1')
    partner            TEXT,   -- CSV: Partner           ('Yes' / 'No')
    dependents         TEXT,   -- CSV: Dependents
    tenure             TEXT,   -- CSV: tenure            (months)
    phone_service      TEXT,   -- CSV: PhoneService
    multiple_lines     TEXT,   -- CSV: MultipleLines     (incl. 'No phone service')
    internet_service   TEXT,   -- CSV: InternetService
    online_security    TEXT,   -- CSV: OnlineSecurity    (incl. 'No internet service')
    online_backup      TEXT,   -- CSV: OnlineBackup
    device_protection  TEXT,   -- CSV: DeviceProtection
    tech_support       TEXT,   -- CSV: TechSupport
    streaming_tv       TEXT,   -- CSV: StreamingTV
    streaming_movies   TEXT,   -- CSV: StreamingMovies
    contract           TEXT,   -- CSV: Contract
    paperless_billing  TEXT,   -- CSV: PaperlessBilling
    payment_method     TEXT,   -- CSV: PaymentMethod
    monthly_charges    TEXT,   -- CSV: MonthlyCharges
    total_charges      TEXT,   -- CSV: TotalCharges      (11 values are a single space)
    churn              TEXT,   -- CSV: Churn             ('Yes' / 'No')
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE staging.telco_raw IS
    'Raw, unchanged copy of Telco-Customer-Churn.csv (one row per CSV line). '
    'Columns = CSV headers in snake_case, same order, all TEXT. Cleaned by dw.run_etl().';
COMMENT ON COLUMN staging.telco_raw.row_no    IS 'Audit: line position in the CSV file (1 = first data line).';
COMMENT ON COLUMN staging.telco_raw.loaded_at IS 'Audit: timestamp of the load.';

-- Empty the table before loading; RESTART IDENTITY makes row_no start at 1 again.
-- (MySQL: TRUNCATE resets AUTO_INCREMENT automatically.)
TRUNCATE staging.telco_raw RESTART IDENTITY;


/* -----------------------------------------------------------------------------
   Step 2. Load the CSV with \copy.

   \copy (with a backslash) is a psql command. psql reads the file on YOUR
   computer, with your Windows permissions, and streams it to the server.
   COPY (without a backslash) is a server command: the PostgreSQL service itself
   must be able to open the file, it needs superuser rights (or the
   pg_read_server_files role), and on Windows the service account usually cannot
   read files in your own folders. That is why we use \copy.

   Options:
     FORMAT csv     : understands commas, quotes and both Windows (CRLF) and
                      Unix (LF) line endings automatically.
     HEADER true    : skip the first line (the column names).
     ENCODING UTF8  : how the file's text is encoded.
   The column list maps the 21 CSV columns, by position, to our 21 columns;
   row_no and loaded_at are filled automatically.

   In CSV format an EMPTY unquoted field becomes NULL, but the 11 blank
   TotalCharges are a single SPACE, so they arrive as the text ' '. We keep
   them as they are; the ETL decides what to do with them.

   COPY is all-or-nothing: if any line is malformed, NO rows are loaded and the
   error names the line number.

   psql limits:
     - a \copy command must be written on ONE line;
     - psql variables (:name) are NOT expanded inside \copy, so the path is
       written literally. It is relative to the folder psql was started from.
       If your CSV is elsewhere, edit the path below (use forward slashes, e.g.
       'C:/Users/me/Downloads/Telco-Customer-Churn.csv').

   MySQL equivalent:
     LOAD DATA LOCAL INFILE 'data/Telco-Customer-Churn.csv'
     INTO TABLE telco_raw
     FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
     LINES TERMINATED BY '\n'          -- '\r\n' needed for Windows files
     IGNORE 1 LINES;
   (MySQL also needs local_infile=1 on both client and server, and in
    non-strict mode it silently converts bad values with warnings instead of
    failing.)
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Loading data/Telco-Customer-Churn.csv into staging.telco_raw ==='
\copy staging.telco_raw (customer_id, gender, senior_citizen, partner, dependents, tenure, phone_service, multiple_lines, internet_service, online_security, online_backup, device_protection, tech_support, streaming_tv, streaming_movies, contract, paperless_billing, payment_method, monthly_charges, total_charges, churn) FROM 'data/Telco-Customer-Churn.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')

-- Refresh the planner's statistics for the new data (MySQL: ANALYZE TABLE).
ANALYZE staging.telco_raw;


/* -----------------------------------------------------------------------------
   Step 3. Profile the raw data (data-quality report).

   3a. One line per column: NULLs, blanks, untrimmed values, distinct values.
   Trick: to_jsonb(row) turns a row into {"column": "value", ...} and
   jsonb_each_text() turns that into one (key, value) row per column. This
   "unpivots" all 21 columns at once, so we do not need 21 separate queries.
   We join to information_schema.columns only to print columns in table order.
   (MySQL would need JSON_TABLE or 21 UNION ALL queries.)
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== 3a. Column profile (NULL, blank, untrimmed and distinct values) ==='
SELECT ic.ordinal_position - 1                                        AS col_no,
       col.key                                                        AS column_name,
       count(*) FILTER (WHERE col.value IS NULL)                      AS null_values,
       count(*) FILTER (WHERE btrim(col.value) = '')                  AS blank_values,
       count(*) FILTER (WHERE col.value <> btrim(col.value))          AS untrimmed_values,
       count(DISTINCT col.value)                                      AS distinct_values
FROM   staging.telco_raw AS r
CROSS  JOIN LATERAL jsonb_each_text(to_jsonb(r) - 'row_no' - 'loaded_at') AS col(key, value)
JOIN   information_schema.columns AS ic
       ON  ic.table_schema = 'staging'
       AND ic.table_name   = 'telco_raw'
       AND ic.column_name  = col.key
GROUP  BY ic.ordinal_position, col.key
ORDER  BY ic.ordinal_position;


/* 3b. Every value (with its count) of the categorical columns, i.e. the
       columns with at most 10 different values. This is where we see
       'No internet service', 'No phone service', the 0/1 SeniorCitizen flag and
       the spelling of every label. */
\echo ''
\echo '=== 3b. Values of the categorical columns (value (count)) ==='
WITH long_form AS (
    SELECT col.key AS column_name, col.value, count(*) AS n
    FROM   staging.telco_raw AS r
    CROSS  JOIN LATERAL jsonb_each_text(to_jsonb(r) - 'row_no' - 'loaded_at') AS col(key, value)
    GROUP  BY col.key, col.value
)
SELECT lf.column_name,
       count(*)                                                       AS distinct_values,
       string_agg(format('%s (%s)', lf.value, lf.n), ', ' ORDER BY lf.n DESC, lf.value) AS value_counts
FROM   long_form AS lf
JOIN   information_schema.columns AS ic
       ON  ic.table_schema = 'staging'
       AND ic.table_name   = 'telco_raw'
       AND ic.column_name  = lf.column_name
GROUP  BY ic.ordinal_position, lf.column_name
HAVING count(*) <= 10
ORDER  BY ic.ordinal_position;


/* 3c. The three numeric columns. A regular expression decides whether the text
       is a valid number BEFORE we cast it, so a bad value cannot stop the query.
       (MySQL: col REGEXP '...'   PostgreSQL: col ~ '...')
       PostgreSQL 16+ also offers pg_input_is_valid(value, 'numeric'), but we use
       a regex so the script also runs on PostgreSQL 15. */
\echo ''
\echo '=== 3c. Numeric columns (stored as text in staging) ==='
WITH typed AS (
    SELECT CASE WHEN tenure          ~ '^[0-9]+$'               THEN tenure::int              END AS tenure_num,
           CASE WHEN monthly_charges ~ '^[0-9]+(\.[0-9]+)?$'    THEN monthly_charges::numeric END AS monthly_num,
           CASE WHEN total_charges   ~ '^[0-9]+(\.[0-9]+)?$'    THEN total_charges::numeric   END AS total_num
    FROM   staging.telco_raw
)
SELECT 'tenure'          AS column_name,
       count(*) - count(tenure_num)   AS not_a_number,
       min(tenure_num)::text          AS min_value,
       max(tenure_num)::text          AS max_value,
       round(avg(tenure_num), 2)::text AS avg_value
FROM typed
UNION ALL
SELECT 'monthly_charges', count(*) - count(monthly_num),
       min(monthly_num)::text, max(monthly_num)::text, round(avg(monthly_num), 2)::text
FROM typed
UNION ALL
SELECT 'total_charges', count(*) - count(total_num),
       min(total_num)::text, max(total_num)::text, round(avg(total_num), 2)::text
FROM typed;


/* 3d. Who are the customers with a blank TotalCharges?
       If they all have tenure 0, the blank means "no bill issued yet". */
\echo ''
\echo '=== 3d. Rows whose total_charges is not a number ==='
SELECT row_no,
       customer_id,
       tenure,
       monthly_charges,
       format('%L', total_charges) AS total_charges_raw,   -- %L shows the value in quotes, so ' ' is visible
       contract,
       churn
FROM   staging.telco_raw
WHERE  total_charges !~ '^[0-9]+(\.[0-9]+)?$'
ORDER  BY row_no;


/* -----------------------------------------------------------------------------
   Step 4. Phase 2 checks (PASS / FAIL).
   These confirm the load is complete and record the quality issues that the
   ETL must handle. "Expected" values come from the original MySQL project and
   from profiling the CSV.
   ----------------------------------------------------------------------------- */
\echo ''
\echo '=== Phase 2 checks ==='
WITH s AS (SELECT * FROM staging.telco_raw),
checks (check_no, check_name, expected, actual) AS (
    VALUES
    (1,  'Rows loaded into staging',
         7043::bigint, (SELECT count(*) FROM s)),
    (2,  'Distinct customer_id values (no duplicates)',
         7043, (SELECT count(DISTINCT customer_id) FROM s)),
    (3,  'Header line loaded as data by mistake',
         0, (SELECT count(*) FROM s WHERE customer_id = 'customerID')),
    (4,  'NULL values in any source column',
         0, (SELECT count(*)
               FROM s CROSS JOIN LATERAL jsonb_each_text(to_jsonb(s) - 'row_no' - 'loaded_at') AS c(k, v)
              WHERE v IS NULL)),
    (5,  'Blank total_charges values',
         11, (SELECT count(*) FROM s WHERE btrim(total_charges) = '')),
    (6,  'Blank total_charges where tenure is not 0',
         0, (SELECT count(*) FROM s WHERE btrim(total_charges) = '' AND tenure <> '0')),
    (7,  'Non-numeric tenure or monthly_charges',
         0, (SELECT count(*) FROM s
              WHERE tenure !~ '^[0-9]+$' OR monthly_charges !~ '^[0-9]+(\.[0-9]+)?$')),
    (8,  'Non-numeric total_charges other than blanks',
         0, (SELECT count(*) FROM s
              WHERE btrim(total_charges) <> '' AND total_charges !~ '^[0-9]+(\.[0-9]+)?$')),
    (9,  'Unexpected values in gender / 0-1 / Yes-No columns',
         0, (SELECT count(*) FROM s
              WHERE gender NOT IN ('Female', 'Male')
                 OR senior_citizen NOT IN ('0', '1')
                 OR partner           NOT IN ('Yes', 'No')
                 OR dependents        NOT IN ('Yes', 'No')
                 OR phone_service     NOT IN ('Yes', 'No')
                 OR paperless_billing NOT IN ('Yes', 'No')
                 OR churn             NOT IN ('Yes', 'No'))),
    (10, 'Rows breaking: no internet <=> all six add-ons = No internet service',
         0, (SELECT count(*) FROM s
              WHERE (internet_service = 'No') IS DISTINCT FROM
                    (    online_security   = 'No internet service'
                     AND online_backup     = 'No internet service'
                     AND device_protection = 'No internet service'
                     AND tech_support      = 'No internet service'
                     AND streaming_tv      = 'No internet service'
                     AND streaming_movies  = 'No internet service')
                 OR (internet_service <> 'No' AND 'No internet service' IN
                        (online_security, online_backup, device_protection,
                         tech_support, streaming_tv, streaming_movies)))),
    (11, 'Rows breaking: no phone service <=> multiple_lines = No phone service',
         0, (SELECT count(*) FROM s
              WHERE (phone_service = 'No') IS DISTINCT FROM (multiple_lines = 'No phone service'))),
    (12, 'Churn = No',
         5174, (SELECT count(*) FROM s WHERE churn = 'No')),
    (13, 'Churn = Yes',
         1869, (SELECT count(*) FROM s WHERE churn = 'Yes'))
)
SELECT check_no,
       check_name,
       expected,
       actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS result
FROM   checks
ORDER  BY check_no;
