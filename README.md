# Telco Churn Analytics & Data Warehouse

[![Build, Test & Deploy Model](https://github.com/MuhamadWallah/telco-churn-analytics/actions/workflows/deploy.yml/badge.svg)](https://github.com/MuhamadWallah/telco-churn-analytics/actions/workflows/deploy.yml)
[![GitHub Pages](https://img.shields.io/badge/Live_Showcase-GitHub_Pages-blue?logo=github&style=flat-square)](https://muhamadwallah.github.io/telco-churn-analytics/)

> 🌐 **Live Web Application & Model Predictor**: [https://muhamadwallah.github.io/telco-churn-analytics/](https://muhamadwallah.github.io/telco-churn-analytics/)  
> **Owners & Developers:** MuhammaD_Wallah and the GROUP are the owners and Developers of this model.

An end-to-end Enterprise Data Warehouse, Machine Learning Data Mining pipeline, and Interactive Analytics Dashboard for customer churn prediction in the telecommunications sector.

---

## 📌 Architecture Overview

1. **Layer 1: Staging (`staging`)**
   - Untouched raw ingestion of `data/Telco-Customer-Churn.csv`.
   - Comprehensive data profiling and automated quality checks.

2. **Layer 2: Data Warehouse Star Schema (`dw`)**
   - Fact table: `fact_customer_churn`
   - Dimension tables: `dim_customer`, `dim_contract`, `dim_service`, `dim_payment`, `dim_tenure`, `dim_churn`.
   - Automated ETL stored procedure (`dw.run_etl()`) with full data validation checks.

3. **Layer 3: Analytical & OLAP Layer (`olap`)**
   - Multi-dimensional cubes and materialized views (`mv_churn_cube`, `mv_kpi_overview`, `mv_revenue_risk`, `mv_addon_churn`).
   - Secure database objects for machine learning features (`v_churn_model_input`) and model tracking.

4. **Layer 4: Data Mining & Supervised ML (`mining/churn_mining.py`)**
   - Leakage-safe preprocessing pipeline (StandardScaler + OneHotEncoder).
   - Models trained & evaluated:
     - Logistic Regression
     - Decision Tree
     - Random Forest
     - Gradient Boosting
   - 5-fold Stratified Cross-Validation, ROC-AUC, threshold optimization, and feature importance.
   - Predictions and risk scores stored back into the warehouse.
   - **Explanation & recommendation (`mining/insights.py`)** – every run explains its results in plain English:
     what the metrics mean, which model and threshold to use and why, the key churn findings, **why customers
     leave**, possible retention solutions, and the **recommended solution with the reasons it was chosen**.
     The sample records *that* a customer left, not *why*, so the reasons are inferred: for every customer the
     model measures how much each factor (contract, tenure, fibre, support, price, billing, profile) raises the
     churn probability compared with typical values (a *reason code*). Each reason comes with how often it
     applies, the data pattern behind it, a likely explanation, a strength-of-evidence label and how to confirm it. Solutions are compared
     with an out-of-fold *what-if simulation* by the scoring model (e.g. "if these month-to-month customers were
     on a one-year contract, how many fewer would churn?"). No AI text generator is used: every sentence is
     built by rules from the run's own numbers, so the report is reproducible.
     Output: `mining/outputs/insights_report.md`, `retention_solutions.csv`, `recommended_action_targets.csv`,
     `churn_reasons.csv`, `churn_reasons_by_customer.csv`, `07_solution_comparison.png`, `08_churn_reasons.png`,
     the tables `olap.mining_insight` / `olap.retention_solution` / `olap.churn_reason`, and the dashboard
     page **Insights & recommendations**.

5. **Layer 5: Interactive Web Dashboard (`dashboard/app.py`)**
   - Streamlit application with role-based access control (`admin`, `analyst`).
   - Interactive OLAP slicing & dicing, tenure drill-downs, revenue risk analysis, and real-time decision threshold simulation.

## 🐳 Running with Docker (Recommended)

Run the entire system (PostgreSQL Data Warehouse + Automated ML Pipeline + Streamlit Dashboard) with a single command:

```bash
docker compose up --build
```

- **Dashboard**: Opens at [http://localhost:8501](http://localhost:8501)
- **Sign in**: 
  - Admin: `admin` / `Password123!`
  - Analyst: `analyst` / `Password123!`
- **Database**: PostgreSQL 16 runs containerized and is accessible on port `5433` (or internally via Docker network).

---

## 💻 Local Setup (Without Docker)

### 1. Requirements
- Python 3.10+
- PostgreSQL 15+

Install Python dependencies:
```bash
pip install -r requirements.txt
```

### 2. Build the Warehouse & Run Machine Learning
Run the automated build script (runs SQL scripts `01` through `08` and executes data mining):
```powershell
powershell -ExecutionPolicy Bypass -File .\build_warehouse.ps1
```

> **Warehouse built before the explanation feature?** Run script 08 once more (it only adds the new
> tables; earlier mining runs are kept), then re-run the mining:
> ```powershell
> & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/08_mining_objects.sql
> python mining/churn_mining.py
> ```

### 3. Setup Dashboard Security & Secrets
Run the dashboard security setup:
```powershell
psql -U postgres -h localhost -p 5432 -d telco_churn_dw -f sql/09_dashboard_access.sql
```

Create `.streamlit/secrets.toml`:
```toml
[postgres]
host = "localhost"
port = 5432
dbname = "telco_churn_dw"
user = "churn_dashboard"
password = "YOUR_CHURN_DASHBOARD_PASSWORD"
```

### 4. Launch the Dashboard
```bash
python -m streamlit run dashboard/app.py
```
Open [http://localhost:8501](http://localhost:8501) and sign in.

---

## ☁️ Free Cloud Deployment (Neon + Streamlit Community Cloud + GitHub Actions)

A public version of the project runs entirely on free plans:

| Part | Service | Files |
|---|---|---|
| Cloud data warehouse | Neon (serverless PostgreSQL) | `sql/01`–`08`, `sql/10`–`12` |
| Model training and scoring | Python (your PC or GitHub Actions) | `model/` |
| Public web app | Streamlit Community Cloud | `app/` |
| Monthly maintenance | GitHub Actions | `.github/workflows/monthly_refresh.yml` |

**Live app:** https://muhammad-telco-churn-analytics.streamlit.app/ ·
**Report section (architecture + free-plan limits):** [docs/CLOUD_DEPLOYMENT_REPORT.md](docs/CLOUD_DEPLOYMENT_REPORT.md)

```
app/
  streamlit_app.py            entry point: page set-up, sidebar, navigation
  common.py                   database connection (st.secrets or .env.local, read-only), caching, model loading
  charts.py                   Altair chart builders
  pages/overview.py           KPIs + churn by contract, payment method, tenure band, internet service
  pages/olap_explorer.py      slice & dice, roll-up, pivot and drill-down
  pages/predict_churn.py      one customer -> churn probability, risk label, top reasons
  pages/at_risk_customers.py  every customer ranked by risk (dw.churn_scores) + CSV download
  pages/model_performance.py  model comparison, confusion matrix, feature importance, run history
model/
  config.py                   all model settings (decision threshold, selection rule, drift limits)
  db.py                       database connection (DATABASE_URL, else DATABASE_URL_UNPOOLED in .env.local)
  train.py                    trains the 4 models from the warehouse -> churn_model.joblib + metrics.json
  score.py                    scores every customer -> dw.churn_scores, logs dw.model_runs
  promote.py                  quarterly retraining: replace only if not worse; roll back
  drift.py                    data-drift measures (PSI)
sql/
  10_cloud_ml_tables.sql      dw.churn_scores, dw.model_runs, app views; staging made LOGGED for Neon
  11_app_reader_role.sql      read-only role churn_app_reader used by the web app
  12_monthly_refresh.sql      validation + REFRESH MATERIALIZED VIEW (monthly job)
```

Run the cloud version from your PC:
```powershell
pip install -r requirements.txt
npm i -g neon@latest                    # Neon CLI (once)
neon deploy                             # applies neon.ts, writes DATABASE_URL + DATABASE_URL_UNPOOLED to .env.local (git-ignored)
.\build_warehouse.ps1 -Cloud -SkipMining   # build the warehouse on Neon (direct connection from .env.local)
python model/train.py                   # train the models and save the final one (direct connection)
python model/score.py                   # write dw.churn_scores and dw.model_runs (direct connection)
streamlit run app/streamlit_app.py      # [neon] url from .streamlit/secrets.toml if set, else pooled DATABASE_URL from .env.local (read-only transactions)
```
`$env:DATABASE_URL = "..."` still overrides `.env.local` (GitHub Actions sets it from the secret NEON_DATABASE_URL).

**Keeping it running:** see [docs/MAINTENANCE.md](docs/MAINTENANCE.md): the monthly 5-minute check, data drift and
retraining, free-plan limits, passwords, and backups with `backup_neon.ps1` / `restore_neon.ps1`.

Results are associations in a public sample, not causes of churn.

---

## 👥 Authors & Attribution
**MuhammaD_Wallah and the GROUP are the owners and Developers of this model.**
