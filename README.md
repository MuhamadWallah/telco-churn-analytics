# Telco Churn Analytics & Data Warehouse

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

## 👥 Authors & Attribution
**MuhammaD_Wallah and the GROUP are the owners and Developers of this model.**
