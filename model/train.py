#!/usr/bin/env python3
"""
train.py - train four churn models on the WAREHOUSE data and save the final one.

What it does
  1. Reads the modelling data from the warehouse view olap.v_churn_model_input
     with SQLAlchemy + psycopg (not from the CSV). customer_id is not a feature.
  2. Splits the data 80/20, stratified on Churn, random_state=42.
  3. Puts each model inside ONE Pipeline, so the preprocessing is learned from
     the training data only and is saved together with the model:
        numeric      -> median imputation + StandardScaler
        categorical  -> most-frequent imputation + OneHotEncoder(handle_unknown="ignore")
  4. Trains Logistic Regression, Decision Tree, Random Forest and Gradient Boosting.
  5. Chooses the final model by 5-fold cross-validation on the TRAINING data
     (rule in config.py: highest recall among models with precision >= 0.50).
  6. Scores all four once on the untouched test set (the comparison table).
  7. Saves the final fitted Pipeline -> model/churn_model.joblib
     and all metrics and settings    -> model/metrics.json
     (training date, scikit-learn version, decision threshold, confusion
     matrices, feature importance, a threshold trade-off table, the input
     form for the web app).

This script only READS from the database. score.py writes the predictions.

How to run (PowerShell, from the project folder):
    $env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST/telco_churn_dw?sslmode=require"
    python model/train.py               # train and REPLACE the current model
    python model/train.py --candidate   # save into model/candidate/ instead and compare it with
                                        # the current model (used by the quarterly retraining;
                                        # promote.py then decides whether to replace it)

Reminder: the results are ASSOCIATIONS in this sample, not causes of churn.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import time
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
import sklearn
import sqlalchemy as sa
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_val_predict, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.tree import DecisionTreeClassifier

import config
import drift
from db import make_engine, wait_for_database

FEATURES = config.NUMERIC_FEATURES + config.CATEGORICAL_FEATURES


# =============================================================================
# Data
# =============================================================================
def load_data(engine: sa.Engine) -> pd.DataFrame:
    """One row per customer, read from the warehouse.

    Ordered like the original CSV file (source_row_no), so the random split
    selects the same customers as mining/churn_mining.py.
    """
    sql = sa.text(f"SELECT * FROM {config.SOURCE_VIEW} "
                  f"ORDER BY source_row_no NULLS LAST, {config.ID_COLUMN}")
    try:
        with engine.connect() as conn:
            df = pd.read_sql(sql, conn)
    except sa.exc.ProgrammingError as exc:
        sys.exit(f"\nCannot read {config.SOURCE_VIEW}: {str(getattr(exc, 'orig', exc)).splitlines()[0]}\n"
                 "Build the warehouse first (sql/01 ... sql/08).\n")
    missing = [c for c in [config.ID_COLUMN, config.TARGET, *FEATURES] if c not in df.columns]
    if missing:
        sys.exit(f"Columns missing from {config.SOURCE_VIEW}: {missing}. Re-run sql/08_mining_objects.sql.")
    if df.empty:
        sys.exit(f"{config.SOURCE_VIEW} is empty. Load the warehouse first (CALL dw.run_etl();).")
    if df["source_row_no"].isna().any():
        print("WARNING: staging.telco_raw is empty, so rows are ordered by customer_id and the "
              "split differs from mining/churn_mining.py. (On Neon: run sql/10_cloud_ml_tables.sql.)")
    return df


# =============================================================================
# Pipeline and models (same settings as mining/churn_mining.py)
# =============================================================================
def build_preprocessor() -> ColumnTransformer:
    numeric = Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
    ])
    categorical = Pipeline([
        ("impute", SimpleImputer(strategy="most_frequent")),
        ("onehot", OneHotEncoder(handle_unknown="ignore")),
    ])
    return ColumnTransformer([
        ("num", numeric, config.NUMERIC_FEATURES),
        ("cat", categorical, config.CATEGORICAL_FEATURES),
    ])


def build_models() -> dict[str, tuple[object, str]]:
    rs = config.RANDOM_STATE
    return {
        "Logistic Regression": (LogisticRegression(max_iter=1000), "max_iter=1000"),
        "Decision Tree": (DecisionTreeClassifier(max_depth=5, min_samples_leaf=20, random_state=rs),
                          "max_depth=5, min_samples_leaf=20"),
        "Random Forest": (RandomForestClassifier(n_estimators=200, max_depth=8, min_samples_leaf=5,
                                                 class_weight="balanced_subsample", random_state=rs, n_jobs=-1),
                          "n_estimators=200, max_depth=8, min_samples_leaf=5, class_weight=balanced_subsample"),
        "Gradient Boosting": (GradientBoostingClassifier(n_estimators=150, learning_rate=0.05, max_depth=3,
                                                         random_state=rs),
                              "n_estimators=150, learning_rate=0.05, max_depth=3"),
    }


def make_pipeline(model) -> Pipeline:
    return Pipeline([("prep", build_preprocessor()), ("model", clone(model))])


def metrics_at(y_true, probability, threshold: float) -> dict:
    """Metrics when 'churn' is predicted for probability >= threshold."""
    predicted = (np.asarray(probability) >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, predicted, labels=[0, 1]).ravel()
    return {
        "accuracy": round(float(accuracy_score(y_true, predicted)), 4),
        "precision": round(float(precision_score(y_true, predicted, zero_division=0)), 4),
        "recall": round(float(recall_score(y_true, predicted, zero_division=0)), 4),
        "f1": round(float(f1_score(y_true, predicted, zero_division=0)), 4),
        "roc_auc": round(float(roc_auc_score(y_true, probability)), 4),
        "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
    }


def choose_final_model(cv: dict[str, dict]) -> tuple[str, str]:
    """Apply the rule in config.py to the cross-validation results."""
    metric, floor = config.SELECTION_METRIC, config.MIN_PRECISION
    eligible = [name for name, m in cv.items() if m["precision"] >= floor]
    pool = eligible or list(cv)
    best = max(pool, key=lambda name: (cv[name][metric], cv[name]["roc_auc"]))
    if not eligible:
        return best, (f"No model reached precision {floor:.2f}, so {best} was chosen for the highest "
                      f"cross-validated {metric} ({cv[best][metric]:.3f}).")
    reason = (f"{best} has the highest cross-validated {metric} ({cv[best][metric]:.3f}) of the models "
              f"whose precision is at least {floor:.2f} (threshold {config.DECISION_THRESHOLD:.2f}).")
    dropped = [name for name in cv if name not in eligible]
    if dropped:
        reason += f" Excluded for precision below {floor:.2f}: {', '.join(dropped)}."
    return best, reason


# =============================================================================
# Information the web app needs (so nothing is hard-coded there)
# =============================================================================
def input_schema(X_train: pd.DataFrame) -> dict:
    """Allowed values for the Predict form, learned from the training data."""
    schema = {"numeric": {}, "categorical": {}}
    for col in config.NUMERIC_FEATURES:
        s = X_train[col].dropna()
        schema["numeric"][col] = {"min": float(s.min()), "max": float(s.max()),
                                  "median": float(s.median()), "integer": bool((s % 1 == 0).all())}
    for col in config.CATEGORICAL_FEATURES:
        counts = X_train[col].value_counts()
        schema["categorical"][col] = {"values": counts.index.tolist(), "most_frequent": counts.index[0]}
    return schema


def as_json_rows(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records"))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# =============================================================================
# Main
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Train the churn models on the warehouse data.")
    parser.add_argument("--candidate", action="store_true",
                        help="save into model/candidate/ and compare with the current model")
    args = parser.parse_args()
    started = time.time()
    threshold = config.DECISION_THRESHOLD

    # ---------------------------------------------------------------- data
    print("1. Reading the modelling data from the warehouse ...")
    engine = make_engine(app_name="telco-churn-train")
    wait_for_database(engine)
    df = load_data(engine)
    X = df[FEATURES].copy()
    y = df[config.TARGET].astype(int)
    print(f"   {len(df):,} customers, {len(FEATURES)} features, churn rate {100 * y.mean():.2f}%")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=config.TEST_SIZE, stratify=y, random_state=config.RANDOM_STATE)
    print(f"   training {len(X_train):,} rows, test {len(X_test):,} rows (stratified, random_state="
          f"{config.RANDOM_STATE})")

    # ---------------------------------------------------------------- models
    print(f"2. Cross-validation on the training data ({config.CV_FOLDS} folds) and the test set ...")
    folds = StratifiedKFold(n_splits=config.CV_FOLDS, shuffle=True, random_state=config.RANDOM_STATE)
    models = build_models()
    fitted: dict[str, Pipeline] = {}
    results: dict[str, dict] = {}
    out_of_fold: dict[str, np.ndarray] = {}
    for name, (model, params) in models.items():
        oof = cross_val_predict(make_pipeline(model), X_train, y_train, cv=folds, method="predict_proba")[:, 1]
        out_of_fold[name] = oof
        pipe = make_pipeline(model).fit(X_train, y_train)
        fitted[name] = pipe
        results[name] = {
            "hyperparameters": params,
            "cv": metrics_at(y_train, oof, threshold),
            "test": metrics_at(y_test, pipe.predict_proba(X_test)[:, 1], threshold),
        }
        print(f"   {name:<20} CV recall {results[name]['cv']['recall']:.3f}  precision "
              f"{results[name]['cv']['precision']:.3f} | TEST recall {results[name]['test']['recall']:.3f}  "
              f"precision {results[name]['test']['precision']:.3f}  F1 {results[name]['test']['f1']:.3f}  "
              f"ROC-AUC {results[name]['test']['roc_auc']:.3f}")

    final_name, reason = choose_final_model({name: r["cv"] for name, r in results.items()})
    final = fitted[final_name]
    if "n_jobs" in final.named_steps["model"].get_params():
        final.named_steps["model"].set_params(n_jobs=1)    # one customer at a time in the app: no thread pool
    print(f"3. Final model: {final_name}\n   {reason}")

    # How the decision threshold changes the results (training data, out-of-fold, so the
    # test set is not used to choose the threshold). Change it in config.DECISION_THRESHOLD.
    threshold_rows = []
    for t in np.round(np.arange(0.20, 0.801, 0.05), 2):
        m = metrics_at(y_train, out_of_fold[final_name], float(t))
        threshold_rows.append({"threshold": float(t), "precision": m["precision"], "recall": m["recall"],
                               "f1": m["f1"], "flagged_pct": round(100 * (m["tp"] + m["fp"]) / len(y_train), 1)})
    print(f"   Threshold trade-off for {final_name} (cross-validation on the training data):")
    print("   threshold  precision  recall   F1   flagged%")
    for r in threshold_rows:
        mark = "  <- DECISION_THRESHOLD" if abs(r["threshold"] - threshold) < 1e-9 else ""
        print(f"     {r['threshold']:.2f}      {r['precision']:.3f}    {r['recall']:.3f}  {r['f1']:.3f}  "
              f"{r['flagged_pct']:5.1f}{mark}")

    # ---------------------------------------------------------------- importance
    print(f"4. Permutation feature importance of {final_name} on the test set ...")
    perm = permutation_importance(final, X_test, y_test, scoring="roc_auc",
                                  n_repeats=config.PERMUTATION_REPEATS, random_state=config.RANDOM_STATE)
    importance = (pd.DataFrame({"feature": X_test.columns, "importance": perm.importances_mean.round(5),
                                "importance_std": perm.importances_std.round(5)})
                  .sort_values("importance", ascending=False, ignore_index=True))
    print("   top 5: " + ", ".join(importance["feature"].head(5)))

    # ---------------------------------------------------------------- drift reference
    all_probs = final.predict_proba(X)[:, 1]
    edges = drift.bin_edges(all_probs)
    reference = drift.profile(X, y, all_probs, threshold, config.MEDIUM_RISK_FROM)
    reference.update({"bin_edges": edges, "bin_shares": drift.bin_shares(all_probs, edges)})

    # ---------------------------------------------------------------- metrics.json
    trained_at = datetime.now(timezone.utc).replace(microsecond=0)
    version = f"{trained_at:%Y%m%d-%H%M}-{slug(final_name)}"
    metrics = {
        "model_version": version,
        "model_name": final_name,
        "trained_at": trained_at.isoformat(),
        "selection_reason": reason,
        "sklearn_version": sklearn.__version__,
        "environment": {"python": platform.python_version(), "scikit-learn": sklearn.__version__,
                        "numpy": np.__version__, "pandas": pd.__version__, "joblib": joblib.__version__},
        "data": {
            "source": config.SOURCE_VIEW, "rows": int(len(df)), "train_rows": int(len(X_train)),
            "test_rows": int(len(X_test)), "churn_rate": round(float(y.mean()), 4),
            "random_state": config.RANDOM_STATE, "test_size": config.TEST_SIZE, "cv_folds": config.CV_FOLDS,
            "numeric_features": config.NUMERIC_FEATURES, "categorical_features": config.CATEGORICAL_FEATURES,
        },
        "settings": {
            "decision_threshold": threshold, "medium_risk_from": config.MEDIUM_RISK_FROM,
            "selection_metric": config.SELECTION_METRIC, "min_precision": config.MIN_PRECISION,
            "promotion_metrics": config.PROMOTION_METRICS, "promotion_tolerance": config.PROMOTION_TOLERANCE,
            "psi_watch": config.PSI_WATCH, "psi_act": config.PSI_ACT,
        },
        "models": [{"name": name, "is_final": name == final_name, **r} for name, r in results.items()],
        "threshold_table": {
            "model": final_name,
            "source": f"{config.CV_FOLDS}-fold cross-validation on the training data (out-of-fold probabilities)",
            "rows": threshold_rows,
        },
        "feature_importance": {
            "model": final_name,
            "method": f"permutation importance: mean decrease in test ROC-AUC when the column is shuffled "
                      f"({config.PERMUTATION_REPEATS} repeats)",
            "rows": as_json_rows(importance),
        },
        "input_schema": input_schema(X_train),
        "reason_groups": config.REASON_GROUPS,
        "reference_rows": as_json_rows(X_train.sample(n=min(config.REFERENCE_ROWS, len(X_train)),
                                                      random_state=config.RANDOM_STATE)),
        "drift_reference": reference,
    }

    # ---------------------------------------------------------------- candidate vs current
    out_dir = config.CANDIDATE_DIR if args.candidate else config.MODEL_DIR
    current_file = config.MODEL_DIR / config.MODEL_FILE
    if args.candidate:
        comparison = {"test_customers": int(len(y_test)), "threshold": threshold,
                      "candidate": {k: results[final_name]["test"][k] for k in ("recall", "precision", "f1", "roc_auc")}}
        if current_file.exists():
            current_info = json.loads((config.MODEL_DIR / config.METRICS_FILE).read_text(encoding="utf-8"))
            comparison["current_version"] = current_info.get("model_version")
            try:
                current_test = metrics_at(y_test, joblib.load(current_file).predict_proba(X_test)[:, 1], threshold)
                comparison["current"] = {k: current_test[k] for k in ("recall", "precision", "f1", "roc_auc")}
            except Exception as exc:          # e.g. saved by another scikit-learn version
                comparison["current_error"] = f"{type(exc).__name__}: {exc}"
        metrics["comparison_with_current"] = comparison

    # ---------------------------------------------------------------- save
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / config.MODEL_FILE
    joblib.dump(final, model_path, compress=3)
    (out_dir / config.METRICS_FILE).write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    size_mb = model_path.stat().st_size / 1_048_576
    print(f"5. Saved {model_path.relative_to(config.MODEL_DIR.parent).as_posix()} ({size_mb:.2f} MB) and "
          f"{(out_dir / config.METRICS_FILE).relative_to(config.MODEL_DIR.parent).as_posix()}")
    print(f"   model_version {version}, scikit-learn {sklearn.__version__}, Python {platform.python_version()}")
    if size_mb > config.MAX_MODEL_MB:
        print(f"   WARNING: the model is larger than {config.MAX_MODEL_MB} MB. Do not commit it; reduce "
              "n_estimators / max_depth in build_models() and train again.")
    print(f"Finished in {time.time() - started:.0f} s. Results are associations in this sample, not causes.")


if __name__ == "__main__":
    main()
