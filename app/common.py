"""
common.py - helpers shared by every page of the public web app.

Where the data comes from
  - Charts and numbers: the cloud warehouse (Neon), through the READ-ONLY role
    churn_app_reader (sql/11_app_reader_role.sql).
  - Model results: model/churn_model.joblib and model/metrics.json (made by
    model/train.py). Nothing is typed into the code by hand.

The connection string is a "secret": a password the code needs but must never
contain. The app looks for it in this order:
    1. Streamlit secrets  [neon] url   (.streamlit/secrets.toml on your PC,
                                        App settings > Secrets on Streamlit Cloud)
    2. the environment variable DATABASE_URL
    3. DATABASE_URL in .env.local (or .env), written by `neon deploy` / `neon env pull`
Files 1 and 3 are listed in .gitignore and never committed. On Streamlit Cloud
use the READ-ONLY role, in this form:
    [neon]
    url = "postgresql://churn_app_reader:PASSWORD@HOST/telco_churn_dw?sslmode=require"
The DATABASE_URL that Neon writes is the POOLED connection (host ends in -pooler),
which Neon recommends for web-app traffic. It logs in as the database owner, so
get_engine() opens every transaction as READ ONLY: the app can never change data.

Caching ("remembering" results so the work is not repeated for every visitor):
    st.cache_resource  ONE shared object for all visitors: the database engine, the model
    st.cache_data      query RESULTS, kept for 1 hour (ttl), so Neon is asked rarely
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import joblib
import pandas as pd
import sklearn
import sqlalchemy as sa
import streamlit as st
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "model" / "churn_model.joblib"
METRICS_PATH = ROOT / "model" / "metrics.json"
CACHE_SECONDS = 3600

CAVEAT = ("Results describe **associations** in a public sample (IBM Telco Customer Churn, one point in time). "
          "They show where churn is concentrated and which customers look risky; they do **not** prove what "
          "causes a customer to leave.")

# OLAP cube dimensions (olap.mv_churn_cube) and the label of their "all" member.
ALL = {
    "contract_type": "All contracts",
    "payment_method": "All payment methods",
    "internet_service": "All internet types",
    "tenure_band": "All tenure bands",
}
DIM_LABELS = {
    "contract_type": "Contract",
    "payment_method": "Payment method",
    "internet_service": "Internet service",
    "tenure_band": "Tenure band",
}


# =============================================================================
# Page look
# =============================================================================
def apply_style() -> None:
    st.markdown("""
    <style>
      .block-container { padding-top: 2.2rem; padding-bottom: 3rem; }
      [data-testid="stMetricValue"] { font-size: 1.65rem; }
      [data-testid="stMetricLabel"] p { color: #52514e; }
      h1 { font-size: 1.9rem !important; }
      h3 { font-size: 1.1rem !important; margin-top: 0.6rem; }
    </style>
    """, unsafe_allow_html=True)


def page_header(title: str, subtitle: str) -> None:
    st.title(title)
    st.caption(subtitle)


def show(chart) -> None:
    st.altair_chart(chart, theme=None, width="stretch")


def money(value: float) -> str:
    return f"{value:,.0f}"


def utc(value) -> str:
    """A database or metrics.json time as 'dd Mon yyyy, hh:mm UTC' (whatever the server time zone)."""
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return f"{ts:%d %b %Y, %H:%M} UTC"


# =============================================================================
# Database
# =============================================================================
def _neon_env_file() -> dict:
    """The variables in .env.local (or .env) written by `neon deploy`; {} if there is no file."""
    for name in (".env.local", ".env"):
        if (ROOT / name).is_file():
            return {k: v for k, v in dotenv_values(ROOT / name).items() if v}
    return {}


def database_url() -> str | None:
    try:
        url = st.secrets["neon"]["url"]
    except Exception:                      # no secrets file, or no [neon] section
        url = None
    return url or os.environ.get("DATABASE_URL") or _neon_env_file().get("DATABASE_URL")


def _sqlalchemy_url(url: str) -> str:
    """Neon gives 'postgresql://...'; SQLAlchemy must be told to use psycopg (version 3)."""
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@st.cache_resource(show_spinner=False)
def get_engine() -> sa.Engine:
    """One small connection pool shared by all visitors."""
    return sa.create_engine(
        _sqlalchemy_url(database_url()),
        pool_size=2, max_overflow=3,
        pool_pre_ping=True,               # test a pooled connection before use
        pool_recycle=300,                 # Neon closes idle connections when it sleeps
        connect_args={"connect_timeout": 15, "application_name": "telco-churn-app",
                      "prepare_threshold": None},   # no server-side prepared statements (Neon's pooler)
        execution_options={"postgresql_readonly": True},   # every transaction starts BEGIN READ ONLY
    )


@st.cache_data(ttl=CACHE_SECONDS, show_spinner="Reading the warehouse ...")
def _cached_query(sql: str, params: dict | None = None) -> pd.DataFrame:
    error = None
    for attempt in range(3):              # a sleeping Neon database can need a few seconds to wake up
        try:
            with get_engine().connect() as conn:
                return pd.read_sql(sa.text(sql), conn, params=params)
        except sa.exc.OperationalError as exc:
            error = exc
            time.sleep(2 * (attempt + 1))
    raise error


def read(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Run a read-only query (cached for 1 hour). Shows a clear message instead of a crash."""
    if not database_url():
        st.error("No database connection string was found.", icon=":material/key_off:")
        st.markdown("On your PC, run `neon deploy` (or `neon env pull`) in the project folder to write "
                    "`.env.local`. On Streamlit Community Cloud, add it to **App settings > Secrets**:")
        st.code('[neon]\nurl = "postgresql://churn_app_reader:PASSWORD@HOST/telco_churn_dw?sslmode=require"',
                language="toml")
        st.stop()
    try:
        return _cached_query(sql, params)
    except sa.exc.OperationalError as exc:
        st.error("The database did not answer. Neon may still be waking up: wait 10 seconds and press "
                 "**R** (rerun).", icon=":material/cloud_off:")
        st.code(str(getattr(exc, "orig", exc)).strip().splitlines()[0], language=None)
        st.stop()
    except sa.exc.ProgrammingError as exc:
        st.error("The query was refused or a warehouse object is missing.", icon=":material/database:")
        st.code(str(getattr(exc, "orig", exc)).strip().splitlines()[0], language=None)
        st.markdown("Check that `sql/10_cloud_ml_tables.sql` and `sql/11_app_reader_role.sql` were run, and "
                    "that `model/score.py` has filled `dw.churn_scores`.")
        st.stop()


# =============================================================================
# OLAP cube helpers (olap.mv_churn_cube)
# =============================================================================
def cube() -> pd.DataFrame:
    return read("SELECT * FROM olap.mv_churn_cube")


def cube_cell(c: pd.DataFrame, **chosen) -> pd.Series:
    """The one cube row for the chosen members (dimensions not chosen = 'All ...')."""
    mask = pd.Series(True, index=c.index)
    for dim, all_label in ALL.items():
        mask &= c[dim] == chosen.get(dim, all_label)
    return c[mask].iloc[0]


def cube_breakdown(c: pd.DataFrame, by: str, **chosen) -> pd.DataFrame:
    """Cube rows showing every member of `by`, with the other dimensions as chosen."""
    mask = c[by] != ALL[by]
    for dim, all_label in ALL.items():
        if dim != by:
            mask &= c[dim] == chosen.get(dim, all_label)
    return c[mask].copy()


def member_order(c: pd.DataFrame, dim: str) -> list[str]:
    """Members of a dimension in their natural order (tenure bands by their sort column)."""
    members = c.loc[c[dim] != ALL[dim]]
    if dim == "tenure_band":
        return members.drop_duplicates(dim).sort_values("tenure_band_order")[dim].tolist()
    return sorted(members[dim].unique().tolist())


# =============================================================================
# Model (files made by model/train.py)
# =============================================================================
@st.cache_data(show_spinner=False)
def load_metrics() -> dict:
    return json.loads(METRICS_PATH.read_text(encoding="utf-8"))


@st.cache_resource(show_spinner="Loading the model ...")
def load_model():
    return joblib.load(MODEL_PATH)


def metrics_or_stop() -> dict:
    if not METRICS_PATH.exists():
        st.error("model/metrics.json is missing. Run `python model/train.py` and commit the model/ folder.")
        st.stop()
    metrics = load_metrics()
    if metrics["sklearn_version"] != sklearn.__version__:
        st.warning(f"The model was trained with scikit-learn {metrics['sklearn_version']}, but this app runs "
                   f"{sklearn.__version__}. Use the same version in requirements.txt (or retrain).",
                   icon=":material/warning:")
    return metrics


def model_or_stop() -> tuple[object, dict]:
    metrics = metrics_or_stop()
    if not MODEL_PATH.exists():
        st.error("model/churn_model.joblib is missing. Run `python model/train.py` and commit the model/ folder.")
        st.stop()
    try:
        return load_model(), metrics
    except Exception as exc:              # e.g. saved by a different scikit-learn version
        st.error(f"The model file could not be loaded: {type(exc).__name__}: {exc}")
        st.stop()


def risk_label(probability: float, settings: dict) -> str:
    if probability >= settings["decision_threshold"]:
        return "High"
    if probability >= settings["medium_risk_from"]:
        return "Medium"
    return "Low"


def final_model(metrics: dict) -> dict:
    return next(m for m in metrics["models"] if m["is_final"])
