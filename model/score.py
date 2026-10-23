#!/usr/bin/env python3
"""
score.py - score EVERY customer with the saved model and write the results to
the warehouse.

What it does
  1. Loads model/churn_model.joblib and model/metrics.json, and stops if the
     installed scikit-learn version differs from the one used for training.
  2. Reads all customers from olap.v_churn_model_input.
  3. Predicts each customer's churn probability and risk label
     (High / Medium / Low, using the threshold saved in metrics.json).
  4. In ONE transaction (all or nothing):
       - logs the run in dw.model_runs: the model's metrics on today's data and
         simple data-drift measures (PSI, mean tenure, ...);
       - replaces the rows of dw.churn_scores (one row per customer, with
         model_version and scored_at).
     Readers (the web app) keep seeing the old scores until the new ones are
     committed.

How to run (PowerShell, from the project folder):
    $env:DATABASE_URL = "postgresql://OWNER:PASSWORD@HOST/telco_churn_dw?sslmode=require"
    python model/score.py
Options
    --run-type monthly|manual|retrain|local   label stored in dw.model_runs (default: local)
    --dry-run                                 score and print, but write nothing
    --summary-file model/last_refresh.json    also save a short JSON summary (used by the workflow)

It needs a database role that may WRITE dw.churn_scores and dw.model_runs
(the Neon owner role), not the web app's read-only role.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
import sqlalchemy as sa

import config
import drift
from db import make_engine, wait_for_database
from train import FEATURES, metrics_at


def load_model() -> tuple[object, dict]:
    metrics_path = config.MODEL_DIR / config.METRICS_FILE
    model_path = config.MODEL_DIR / config.MODEL_FILE
    if not model_path.exists() or not metrics_path.exists():
        sys.exit("No saved model. Run  python model/train.py  first.")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if metrics["sklearn_version"] != sklearn.__version__:
        sys.exit(f"The model was saved with scikit-learn {metrics['sklearn_version']}, but "
                 f"{sklearn.__version__} is installed.\nInstall the pinned versions "
                 "(pip install -r requirements.txt) or retrain the model.")
    return joblib.load(model_path), metrics


def risk_labels(probability: np.ndarray, threshold: float, medium_from: float) -> np.ndarray:
    return np.select([probability >= threshold, probability >= medium_from], ["High", "Medium"], "Low")


def main() -> None:
    parser = argparse.ArgumentParser(description="Score all customers and save the results in the warehouse.")
    parser.add_argument("--run-type", default="local", choices=["monthly", "manual", "retrain", "local"])
    parser.add_argument("--dry-run", action="store_true", help="do not write to the database")
    parser.add_argument("--summary-file", type=Path, help="also write a JSON summary to this file")
    args = parser.parse_args()

    pipe, metrics = load_model()
    settings = metrics["settings"]
    threshold, medium_from = settings["decision_threshold"], settings["medium_risk_from"]
    print(f"Model {metrics['model_version']} ({metrics['model_name']}), threshold {threshold:.2f}")

    engine = make_engine(app_name="telco-churn-score")
    wait_for_database(engine)
    with engine.connect() as conn:
        df = pd.read_sql(sa.text(f"SELECT * FROM {config.SOURCE_VIEW} ORDER BY {config.ID_COLUMN}"), conn)
    X, y = df[FEATURES], df[config.TARGET].astype(int)

    probability = pipe.predict_proba(X)[:, 1]
    scores = pd.DataFrame({
        "customer_id": df[config.ID_COLUMN],
        "churn_probability": probability.round(6),
        "risk_label": risk_labels(probability, threshold, medium_from),
        "predicted_churn": probability >= threshold,
    })
    scores["risk_rank"] = scores["churn_probability"].rank(ascending=False, method="first").astype(int)

    # How is the model doing today, and do today's customers still look like the training data?
    # (Today's data includes the customers the model was trained on, so these
    #  numbers are optimistic: watch how they CHANGE from month to month.)
    today = metrics_at(y, probability, threshold)
    profile = drift.profile(X, y, probability, threshold, medium_from)
    reference = metrics["drift_reference"]
    psi_value = drift.psi(reference["bin_shares"], drift.bin_shares(probability, reference["bin_edges"]))
    drift_status = drift.status(psi_value, settings["psi_watch"], settings["psi_act"])
    test = next(m for m in metrics["models"] if m["is_final"])["test"]
    counts = scores["risk_label"].value_counts()

    run = {
        "run_type": args.run_type,
        "model_version": metrics["model_version"],
        "model_name": metrics["model_name"],
        "trained_at": metrics["trained_at"],
        "sklearn_version": sklearn.__version__,
        "decision_threshold": threshold,
        "customers_scored": int(len(scores)),
        "high_risk": int(counts.get("High", 0)),
        "medium_risk": int(counts.get("Medium", 0)),
        "low_risk": int(counts.get("Low", 0)),
        "mean_probability": profile["mean_probability"],
        "actual_churn_rate": profile["churn_rate"],
        "test_recall": test["recall"], "test_precision": test["precision"],
        "test_f1": test["f1"], "test_roc_auc": test["roc_auc"],
        "current_accuracy": today["accuracy"], "current_recall": today["recall"],
        "current_precision": today["precision"], "current_f1": today["f1"], "current_roc_auc": today["roc_auc"],
        "psi_probability": psi_value,
        "drift_status": drift_status,
        "mean_tenure": profile["mean_tenure"],
        "mean_monthly_charges": profile["mean_monthly_charges"],
        "share_month_to_month": profile["share_month_to_month"],
        "git_commit": os.environ.get("GITHUB_SHA"),
    }

    print(f"Scored {len(scores):,} customers: High {run['high_risk']:,}, Medium {run['medium_risk']:,}, "
          f"Low {run['low_risk']:,}")
    print(f"Today: recall {today['recall']:.3f}, ROC-AUC {today['roc_auc']:.3f} | test set at training: "
          f"recall {test['recall']:.3f}, ROC-AUC {test['roc_auc']:.3f}")
    print(f"Drift: PSI {psi_value:.4f} -> {drift_status}  (reference mean tenure "
          f"{reference['mean_tenure']}, today {profile['mean_tenure']})")

    if args.dry_run:
        print("Dry run: nothing written.")
        return

    insert_run = sa.text(f"""
        INSERT INTO dw.model_runs ({", ".join(run)})
        VALUES ({", ".join(":" + k for k in run)})
        RETURNING run_id, run_at
    """)
    try:
        with engine.begin() as conn:                     # one transaction: all or nothing
            run_id, run_at = conn.execute(insert_run, run).one()
            conn.execute(sa.text("DELETE FROM dw.churn_scores"))
            scores.assign(run_id=run_id, model_version=metrics["model_version"]).to_sql(
                "churn_scores", conn, schema="dw", if_exists="append", index=False,
                method="multi", chunksize=1000)
    except sa.exc.ProgrammingError as exc:
        sys.exit(f"\n{str(getattr(exc, 'orig', exc)).splitlines()[0]}\n"
                 "Run sql/10_cloud_ml_tables.sql first, and connect as the owner role (not the read-only role).\n")
    run_at = run_at.astimezone(timezone.utc)
    print(f"Saved dw.churn_scores ({len(scores):,} rows) and dw.model_runs run_id {run_id} at {run_at:%Y-%m-%d %H:%M} UTC")

    summary = {"run_id": int(run_id), "run_at": run_at.isoformat(), **{k: v for k, v in run.items() if k != "git_commit"}}
    if args.summary_file:
        args.summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):             # a table on the GitHub Actions run page
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### Churn scores refreshed\n\n| Item | Value |\n|---|---|\n")
            for key in ("model_version", "customers_scored", "high_risk", "medium_risk", "low_risk",
                        "current_recall", "current_roc_auc", "psi_probability", "drift_status"):
                fh.write(f"| {key} | {summary[key]} |\n")


if __name__ == "__main__":
    main()
