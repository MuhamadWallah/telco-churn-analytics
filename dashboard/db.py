"""
db.py - database access for the dashboard.

The dashboard connects as the LOW-PRIVILEGE role churn_dashboard (created by
sql/09_dashboard_access.sql). That role can only read the olap schema and call
a few app.* functions, so even a bug in the dashboard cannot change or expose
the warehouse tables or the password hashes.

Connection settings are read from .streamlit/secrets.toml ([postgres] section)
or, if that file does not exist, from environment variables
DASHBOARD_DB_HOST, DASHBOARD_DB_PORT, DASHBOARD_DB_NAME, DASHBOARD_DB_USER,
DASHBOARD_DB_PASSWORD.
"""

from __future__ import annotations

import os

import pandas as pd
import sqlalchemy as sa
import streamlit as st
from sqlalchemy.engine import URL


def _settings() -> dict:
    try:
        cfg = dict(st.secrets["postgres"])
    except Exception:                      # no secrets.toml or no [postgres] section
        cfg = {}
    env = os.environ.get
    return {
        "host": cfg.get("host", env("DASHBOARD_DB_HOST", "localhost")),
        "port": int(cfg.get("port", env("DASHBOARD_DB_PORT", "5432"))),
        "dbname": cfg.get("dbname", env("DASHBOARD_DB_NAME", "telco_churn_dw")),
        "user": cfg.get("user", env("DASHBOARD_DB_USER", "churn_dashboard")),
        "password": cfg.get("password", env("DASHBOARD_DB_PASSWORD")),
    }


@st.cache_resource(show_spinner=False)
def get_engine() -> sa.Engine:
    """One connection pool shared by all users of the dashboard."""
    s = _settings()
    url = URL.create("postgresql+psycopg2", username=s["user"], password=s["password"],
                     host=s["host"], port=s["port"], database=s["dbname"])
    return sa.create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5,
                            connect_args={"application_name": "telco_churn_dashboard",
                                          "connect_timeout": 5})


def connection_problem() -> str | None:
    """None if the database can be reached, otherwise the error message."""
    try:
        with get_engine().connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return None
    except Exception as exc:  # noqa: BLE001 - show any connection error to the user
        return str(getattr(exc, "orig", exc)).strip()


@st.cache_data(ttl=600, show_spinner=False)
def query(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Run a read-only query. Results are cached for 10 minutes."""
    with get_engine().connect() as conn:
        return pd.read_sql(sa.text(sql), conn, params=params)


def fresh(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Run a read-only query without caching (admin and audit pages)."""
    with get_engine().connect() as conn:
        return pd.read_sql(sa.text(sql), conn, params=params)


def check_login(username: str, password: str) -> dict | None:
    """Ask PostgreSQL to verify the password (app.check_login).

    engine.begin() COMMITS at the end, so the attempt is stored in
    app.login_audit (needed for the lock-out after 5 failures).
    """
    with get_engine().begin() as conn:
        row = conn.execute(
            sa.text("SELECT user_name, display_name, app_role FROM app.check_login(:u, :p)"),
            {"u": username, "p": password},
        ).mappings().first()
    return dict(row) if row else None


def refresh_reporting() -> pd.DataFrame:
    """Refresh every olap materialized view (CONCURRENTLY) and clear the cache."""
    with get_engine().begin() as conn:
        timings = pd.read_sql(sa.text("SELECT * FROM app.refresh_reporting()"), conn)
    query.clear()
    return timings


def latest_run_id() -> int | None:
    run = query("SELECT max(run_id) AS run_id FROM olap.mining_run")
    value = run.iloc[0]["run_id"]
    return None if pd.isna(value) else int(value)
