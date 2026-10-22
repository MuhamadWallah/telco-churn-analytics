#!/bin/bash
set -e

echo "================================================================="
echo " Telco Churn Analytics - Docker Container"
echo " Owners & Developers: MuhammaD_Wallah and the GROUP"
echo "================================================================="

PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGPASSWORD="${PGPASSWORD:-postgres}"
DBNAME="telco_churn_dw"

export PGPASSWORD

echo "Waiting for PostgreSQL at ${PGHOST}:${PGPORT}..."
until pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PGUSER"; do
  sleep 1
done
echo "PostgreSQL is ready."

# Check if telco_churn_dw exists
DB_EXISTS=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DBNAME'" || true)

if [ "$DB_EXISTS" != "1" ]; then
  echo "Database '$DBNAME' not found. Initializing warehouse schemas and loading data..."
  psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 -f sql/01_create_database_and_schemas.sql

  for script in 02_staging_load 03_star_schema_ddl 04_etl_procedure 05_validation_checks 06_olap_queries 07_materialized_views 08_mining_objects; do
    echo "Running sql/${script}.sql..."
    psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DBNAME" -v ON_ERROR_STOP=1 -P pager=off -f "sql/${script}.sql"
  done
  echo "Warehouse built successfully."
else
  echo "Database '$DBNAME' already exists."
fi

# Configure dashboard security role and accounts if needed
CHURN_DASHBOARD_PASS="${DASHBOARD_DB_PASSWORD:-churn_dashboard_pass}"
ADMIN_PASS="${ADMIN_PASSWORD:-Password123!}"
ANALYST_PASS="${ANALYST_PASSWORD:-Password123!}"

ROLE_EXISTS=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DBNAME" -tAc "SELECT 1 FROM pg_roles WHERE rolname='churn_dashboard'" || true)

if [ "$ROLE_EXISTS" != "1" ]; then
  echo "Configuring dashboard security role and user accounts..."
  psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DBNAME" \
    -v dashboard_db_password="$CHURN_DASHBOARD_PASS" \
    -v admin_password="$ADMIN_PASS" \
    -v analyst_password="$ANALYST_PASS" \
    -P pager=off \
    -f sql/09_dashboard_access.sql
fi

# Run data mining if no run recorded yet
MINING_RUNS=$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$DBNAME" -tAc "SELECT count(*) FROM olap.mining_run" 2>/dev/null || echo "0")

if [ "$MINING_RUNS" = "0" ] || [ -z "$MINING_RUNS" ]; then
  echo "Running data mining pipeline (training models and storing predictions)..."
  python mining/churn_mining.py --host "$PGHOST" --port "$PGPORT" --user "$PGUSER" --dbname "$DBNAME"
  echo "Data mining completed."
fi

if [ $# -gt 0 ]; then
  exec "$@"
fi

echo "Starting Streamlit dashboard on port 8501..."
exec python -m streamlit run dashboard/app.py \
  --server.port=8501 \
  --server.address=0.0.0.0 \
  --server.headless=true
