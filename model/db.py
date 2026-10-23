"""
db.py - one database connection helper for train.py, score.py and promote.py.

The connection string is NEVER written in the code. It is read from, in order:
  1. the environment variable DATABASE_URL
       - in GitHub Actions:       from the Actions secret NEON_DATABASE_URL (see monthly_refresh.yml)
       - on your PC (PowerShell): $env:DATABASE_URL = "postgresql://...neon.tech/telco_churn_dw?sslmode=require"
  2. the file .env.local (or .env) in the project folder, written by `neon deploy`
     (or `neon env pull`). It is listed in .gitignore, never committed. These jobs
     use its DATABASE_URL_UNPOOLED: a DIRECT connection, which Neon recommends for
     schema changes and batch writes. The pooled DATABASE_URL is for the web app.

Neon "scales to zero": after a few idle minutes the database computer is
switched off and the first connection has to wake it up (usually under a few
seconds). wait_for_database() retries a few times so a sleeping database does
not make a job fail.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import sqlalchemy as sa
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def neon_env_file() -> dict:
    """The variables in .env.local (or .env) written by `neon deploy`; {} if there is no file."""
    for name in (".env.local", ".env"):
        if (ROOT / name).is_file():
            return {k: v for k, v in dotenv_values(ROOT / name).items() if v}
    return {}


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        env = neon_env_file()
        url = env.get("DATABASE_URL_UNPOOLED") or env.get("DATABASE_URL") or ""
    if not url:
        sys.exit(
            "\nNo database connection string was found.\n"
            "Run  neon deploy  (or  neon env pull) in the project folder to write .env.local, or set\n"
            "PowerShell:  $env:DATABASE_URL = \"postgresql://USER:PASSWORD@HOST/telco_churn_dw?sslmode=require\"\n")
    return url


def sqlalchemy_url(url: str) -> str:
    """Neon gives 'postgresql://...'; SQLAlchemy must be told to use psycopg (version 3)."""
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str | None = None, app_name: str = "telco-churn-model") -> sa.Engine:
    return sa.create_engine(
        sqlalchemy_url(url or database_url()),
        pool_pre_ping=True,          # test a pooled connection before using it
        pool_recycle=300,            # Neon closes idle connections when it sleeps
        connect_args={
            "connect_timeout": 15,   # seconds; enough for Neon to wake up
            "application_name": app_name,
            "prepare_threshold": None,   # no server-side prepared statements (safe with Neon's pooler)
        },
    )


def wait_for_database(engine: sa.Engine, attempts: int = 6, pause: float = 5.0) -> None:
    """Connect, retrying while Neon wakes up. Stops with a clear message if it never answers."""
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return
        except sa.exc.OperationalError as exc:
            message = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
            if "password authentication failed" in message or "does not exist" in message:
                sys.exit(f"\nCannot connect: {message}\nCheck the user, password and database name in DATABASE_URL.\n")
            print(f"  database not answering yet (attempt {attempt}/{attempts}): {message}")
            if attempt == attempts:
                sys.exit("\nThe database did not answer. Is the Neon project active, and is DATABASE_URL correct?\n")
            time.sleep(pause)
