#!/usr/bin/env python3
"""
churn_mining.py
===============
Data mining (supervised binary classification) for the project
"Development of a Data Warehouse and Application of Data Mining Techniques for
Customer Churn Analysis in the Telecommunications Sector".

Owners and Developers:
MuhammaD_Wallah and the GROUP are the owners and Developers of this model.

__author__ = "MuhammaD_Wallah and the GROUP"

What the script does
--------------------
 1. Reads the modelling data FROM THE POSTGRESQL WAREHOUSE
    (view olap.v_churn_model_input, created by sql/08_mining_objects.sql),
    not from the CSV file. customerID is not used as a feature.
 2. Splits the data 80/20, stratified on Churn, random_state=42
    -> 5,634 training rows and 1,409 test rows.
 3. Builds a leakage-safe preprocessing pipeline that is fitted on the
    TRAINING data only:
        numeric features     -> median imputation + standard scaling
        categorical features -> most-frequent imputation + one-hot encoding
 4. Trains four models: Logistic Regression, Decision Tree, Random Forest,
    Gradient Boosting.
 5. Reports accuracy, precision, recall, F1-score, ROC-AUC and the confusion
    matrix on the test set, and compares them with the earlier analysis.
 6. Feature importance: Random Forest impurity importance, and permutation
    importance scored on the test ROC-AUC.
 7. 5-fold stratified cross-validation, because a single split is only a
    preliminary estimate.
 8. A threshold-tuning example (the threshold is chosen on training data only).
 9. EXPLAINS the results in plain English (mining/insights.py): what the
    metrics mean, which model and threshold to use and why, the key findings,
    why customers leave (most likely reasons, inferred with model reason
    codes), possible retention solutions (with a what-if simulation by the
    model), and the recommended solution with the reasons it was chosen.
10. Saves charts (PNG), tables (CSV) and the explanation (insights_report.md)
    in mining/outputs/, and writes the results back to the warehouse
    (olap.mining_run, olap.model_metrics, ..., olap.mining_insight,
    olap.retention_solution, olap.churn_reason) for the dashboard.

How to run (from the project folder)
------------------------------------
    pip install -r requirements.txt
    $env:PGPASSWORD = "your-postgres-password"     # PowerShell; otherwise you are asked
    python mining/churn_mining.py

Options
    --host localhost --port 5432 --user postgres --dbname telco_churn_dw
    --output-dir mining/outputs
    --no-db-write              do not write results to the warehouse
    --blank-total-as-missing   treat TotalCharges of tenure-0 customers as
                               missing (the earlier analysis did this) instead
                               of the warehouse value 0.00

Reminder: model results describe ASSOCIATIONS in this sample. Feature
importance shows what the model relies on, not what causes churn.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import matplotlib

matplotlib.use("Agg")  # draw straight to files; no window is needed
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize

import sklearn
import sqlalchemy as sa
from sqlalchemy.engine import URL
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
    roc_curve,
)
from sklearn.model_selection import (
    StratifiedKFold,
    cross_val_predict,
    cross_validate,
    train_test_split,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.tree import DecisionTreeClassifier

import insights   # mining/insights.py: plain-English explanation and recommended solution

# =============================================================================
# 1. Settings
# =============================================================================
RANDOM_STATE = 42
TEST_SIZE = 0.20
CV_FOLDS = 5
PERMUTATION_REPEATS = 10

TARGET = "Churn"
NUMERIC_FEATURES = ["SeniorCitizen", "tenure", "MonthlyCharges", "TotalCharges"]
CATEGORICAL_FEATURES = [
    "gender", "Partner", "Dependents", "PhoneService", "MultipleLines",
    "InternetService", "OnlineSecurity", "OnlineBackup", "DeviceProtection",
    "TechSupport", "StreamingTV", "StreamingMovies", "Contract",
    "PaperlessBilling", "PaymentMethod",
]

# Illustrative costs for the threshold example: missing a churner is assumed
# to cost 5 times as much as an unnecessary retention offer.
COST_MISSED_CHURNER = 5.0   # false negative
COST_WASTED_OFFER = 1.0     # false positive

# Results of the earlier (MySQL-era) analysis, for comparison:
# accuracy, precision, recall, F1, ROC-AUC
PREVIOUS_RESULTS = {
    "Logistic Regression": (0.8055, 0.6572, 0.5588, 0.6040, 0.8419),
    "Decision Tree":       (0.7984, 0.6347, 0.5668, 0.5989, 0.8282),
    "Random Forest":       (0.7587, 0.5311, 0.7754, 0.6304, 0.8410),
    "Gradient Boosting":   (0.8055, 0.6761, 0.5134, 0.5836, 0.8455),
}
METRICS = ["accuracy", "precision", "recall", "f1", "roc_auc"]
METRIC_LABELS = ["Accuracy", "Precision", "Recall", "F1-score", "ROC-AUC"]

# Chart colours (validated categorical order: blue, orange, aqua, yellow)
SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE = "#e1e0d9", "#c3c2b7"
MODEL_COLORS = {
    "Logistic Regression": "#2a78d6",
    "Decision Tree":       "#eb6834",
    "Random Forest":       "#1baf7a",
    "Gradient Boosting":   "#eda100",
}
BLUE = "#2a78d6"
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


# =============================================================================
# 2. Small helpers
# =============================================================================
REPORT_LINES: list[str] = []


def say(text: str = "") -> None:
    """Print a line and keep it for results_summary.txt."""
    print(text)
    REPORT_LINES.append(text)


def heading(title: str) -> None:
    say()
    say("=" * 78)
    say(title)
    say("=" * 78)


def table(df: pd.DataFrame, float_format: str = "{:.4f}") -> None:
    say(df.to_string(index=False, float_format=float_format.format))


# =============================================================================
# 3. Database connection and data loading
# =============================================================================
def connect(args: argparse.Namespace) -> sa.Engine:
    """Create an SQLAlchemy engine (driver: psycopg2) and test the connection.

    The password comes from the PGPASSWORD environment variable (or the
    PostgreSQL password file). If neither works and we are in an interactive
    window, the user is asked for it.
    """
    def make_engine(password: str | None) -> sa.Engine:
        url = URL.create(
            "postgresql+psycopg2",
            username=args.user,
            password=password,
            host=args.host,
            port=args.port,
            database=args.dbname,
        )
        return sa.create_engine(url)

    def attempt(engine: sa.Engine) -> str | None:
        """None if the connection works, otherwise the driver's error message."""
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return None
        except sa.exc.OperationalError as exc:
            return str(getattr(exc, "orig", exc)).strip()

    engine = make_engine(os.environ.get("PGPASSWORD"))
    problem = attempt(engine)
    # Ask only in a real console window (on Windows the "NUL" device also reports
    # itself as a terminal, so both input and output are checked).
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if problem and "no password supplied" in problem.lower() and interactive:
        engine = make_engine(getpass.getpass(f"Password for PostgreSQL user '{args.user}': "))
        problem = attempt(engine)
    if problem is None:
        return engine
    sys.exit(explain_connection_problem(problem, args))


def explain_connection_problem(message: str, args: argparse.Namespace) -> str:
    """Turn a driver error into one clear instruction (no traceback)."""
    text = message.lower()
    where = f"{args.host}:{args.port}"
    if "does not exist" in text and "database" in text:
        advice = (f"The server at {where} is running and the password is correct, but the database "
                  f"'{args.dbname}' has not been built yet.\nBuild the warehouse first (scripts 01-08), "
                  "for example:  powershell -ExecutionPolicy Bypass -File .\\build_warehouse.ps1")
    elif "password authentication failed" in text:
        advice = f"Wrong password for PostgreSQL user '{args.user}'. Try again, or set $env:PGPASSWORD."
    elif "no password supplied" in text:
        advice = "No password was given. Set it first:  $env:PGPASSWORD = \"your-postgres-password\""
    elif "refused" in text or "could not connect" in text or "timeout" in text:
        advice = (f"No PostgreSQL server is answering at {where}. Start the PostgreSQL service "
                  "(services.msc, 'postgresql-x64-18'), or pass the right --host / --port.")
    else:
        advice = "Check --host, --port, --user and --dbname, and the PGPASSWORD setting."
    first_line = message.splitlines()[0] if message else "unknown error"
    return f"\nCannot connect to PostgreSQL: {first_line}\n\n{advice}\n"


def load_data(engine: sa.Engine) -> pd.DataFrame:
    """Read the modelling data from the warehouse view.

    Rows are ordered as in the original CSV file (source_row_no) so that the
    random split selects the same customers as the earlier analysis.
    """
    query = sa.text("""
        SELECT *
        FROM   olap.v_churn_model_input
        ORDER  BY source_row_no NULLS LAST, customer_id
    """)
    try:
        df = pd.read_sql(query, engine)
    except sa.exc.ProgrammingError as exc:          # e.g. the view has not been created yet
        sys.exit(f"\nCannot read olap.v_churn_model_input: {str(getattr(exc, 'orig', exc)).splitlines()[0]}\n\n"
                 "Run the warehouse scripts first (01-08; the view is created by sql/08_mining_objects.sql).\n")

    missing =[c for c in [TARGET, *NUMERIC_FEATURES, *CATEGORICAL_FEATURES] if c not in df.columns]
    if missing:
        sys.exit(f"Columns missing from olap.v_churn_model_input: {missing}. Re-run sql/08_mining_objects.sql.")
    if df["source_row_no"].isna().any():
        say("WARNING: staging.telco_raw is empty, so rows are ordered by customer_id. "
            "The split will differ from the earlier analysis.")
    return df


# =============================================================================
# 4. Pipeline and models
# =============================================================================
def build_preprocessor() -> ColumnTransformer:
    """Numeric: median + scaling. Categorical: most frequent + one-hot.

    Inside a Pipeline, these steps learn their medians, means, scales and
    categories from the training data only, so no information from the test
    set leaks into the model.
    """
    numeric = Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
    ])
    categorical = Pipeline([
        ("impute", SimpleImputer(strategy="most_frequent")),
        ("onehot", OneHotEncoder(handle_unknown="ignore")),
    ])
    return ColumnTransformer([
        ("num", numeric, NUMERIC_FEATURES),
        ("cat", categorical, CATEGORICAL_FEATURES),
    ])


def build_models() -> dict[str, tuple[object, str]]:
    """The four classifiers with their hyperparameters (and a readable summary).

    These settings reproduce the earlier analysis on the same split.
    """
    return {
        "Logistic Regression": (
            LogisticRegression(max_iter=1000),
            "max_iter=1000 (other settings default: L2 penalty, C=1.0)",
        ),
        "Decision Tree": (
            DecisionTreeClassifier(max_depth=5, min_samples_leaf=20, random_state=RANDOM_STATE),
            "max_depth=5, min_samples_leaf=20",
        ),
        "Random Forest": (
            RandomForestClassifier(n_estimators=200, max_depth=8, min_samples_leaf=5,
                                   class_weight="balanced_subsample",
                                   random_state=RANDOM_STATE, n_jobs=-1),
            "n_estimators=200, max_depth=8, min_samples_leaf=5, class_weight=balanced_subsample",
        ),
        "Gradient Boosting": (
            GradientBoostingClassifier(n_estimators=150, learning_rate=0.05, max_depth=3,
                                       random_state=RANDOM_STATE),
            "n_estimators=150, learning_rate=0.05, max_depth=3",
        ),
    }


def make_pipeline(model) -> Pipeline:
    """A fresh (unfitted) preprocessing + model pipeline."""
    return Pipeline([("prep", build_preprocessor()), ("model", clone(model))])


def evaluate(y_true, y_prob, threshold: float = 0.5) -> dict:
    """Classification metrics for predicted probabilities at a threshold."""
    y_pred = (y_prob >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()
    return {
        "accuracy": accuracy_score(y_true, y_pred),
        "precision": precision_score(y_true, y_pred, zero_division=0),
        "recall": recall_score(y_true, y_pred, zero_division=0),
        "f1": f1_score(y_true, y_pred, zero_division=0),
        "roc_auc": roc_auc_score(y_true, y_prob),
        "tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp),
    }


def readable_feature_names(names) -> list[str]:
    """'num__tenure' -> 'tenure'; 'cat__Contract_Month-to-month' -> 'Contract=Month-to-month'."""
    result = []
    for name in names:
        name = name.split("__", 1)[1]
        for column in sorted(CATEGORICAL_FEATURES, key=len, reverse=True):
            if name.startswith(column + "_"):
                name = f"{column}={name[len(column) + 1:]}"
                break
        result.append(name)
    return result


# =============================================================================
# 5. Charts
# =============================================================================
def chart_style() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.family": "sans-serif", "font.sans-serif": ["Segoe UI", "DejaVu Sans", "Arial"],
        "font.size": 10, "text.color": INK,
        "axes.edgecolor": BASELINE, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "axes.titlesize": 12, "axes.titleweight": "semibold", "axes.titlelocation": "left",
        "axes.spines.top": False, "axes.spines.right": False,
        "xtick.color": BASELINE, "ytick.color": BASELINE,
        "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
        "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
        "legend.frameon": False, "legend.labelcolor": INK_2,
        "lines.linewidth": 2, "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
    })


def save(fig, path: Path) -> None:
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    say(f"  saved {path.as_posix()}")


def chart_model_comparison(test_metrics: dict, path: Path) -> None:
    """Dot plot: one row per metric, one dot per model (values are close, so
    dots show the differences better than bars starting at zero)."""
    fig, ax = plt.subplots(figsize=(8.5, 4.4))
    offsets = np.linspace(-0.24, 0.24, len(MODEL_COLORS))
    for offset, (name, color) in zip(offsets, MODEL_COLORS.items()):
        values = [test_metrics[name][m] for m in METRICS]
        ax.scatter(values, np.arange(len(METRICS)) + offset, s=64, color=color,
                   edgecolors=SURFACE, linewidths=2, zorder=3, label=name)
    ax.set_yticks(range(len(METRICS)), METRIC_LABELS)
    ax.invert_yaxis()
    ax.set_xlim(0.45, 0.90)
    ax.xaxis.grid(True)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("Score on the test set (1,409 customers, threshold 0.5)")
    ax.set_title("Model comparison on the test set", pad=30)
    ax.legend(ncol=4, loc="lower left", bbox_to_anchor=(0, 1.0), handletextpad=0.2, columnspacing=1.4)
    save(fig, path)


def chart_roc(y_test, test_probs: dict, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(6.2, 6.2))
    ax.plot([0, 1], [0, 1], color=BASELINE, linewidth=1)
    ax.text(0.62, 0.55, "random guess", color=MUTED, fontsize=8, rotation=45)
    for name, color in MODEL_COLORS.items():
        fpr, tpr, _ = roc_curve(y_test, test_probs[name])
        auc = roc_auc_score(y_test, test_probs[name])
        ax.plot(fpr, tpr, color=color, linewidth=2, label=f"{name}  (AUC {auc:.3f})")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.01)
    ax.set_aspect("equal")
    ax.grid(True)
    ax.set_axisbelow(True)
    ax.set_xlabel("False positive rate (staying customers wrongly flagged)")
    ax.set_ylabel("True positive rate = recall (churners caught)")
    ax.set_title("ROC curves on the test set")
    ax.legend(loc="lower right")
    save(fig, path)


def chart_confusion_matrices(test_metrics: dict, path: Path) -> None:
    """2 x 2 small multiples, one confusion matrix per model, one blue scale."""
    cmap = LinearSegmentedColormap.from_list("blues", BLUE_RAMP)
    vmax = max(max(m["tn"], m["fp"], m["fn"], m["tp"]) for m in test_metrics.values())
    norm = Normalize(vmin=0, vmax=vmax)
    fig, axes = plt.subplots(2, 2, figsize=(8.4, 7.6))
    for ax, name in zip(axes.ravel(), MODEL_COLORS):
        m = test_metrics[name]
        cells = np.array([[m["tn"], m["fp"]], [m["fn"], m["tp"]]])
        labels = np.array([["TN", "FP"], ["FN", "TP"]])
        ax.imshow(cells, cmap=cmap, norm=norm)
        for r in range(2):
            for c in range(2):
                red, green, blue, _ = cmap(norm(cells[r, c]))
                luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
                ax.text(c, r, f"{cells[r, c]:,}\n{labels[r, c]}", ha="center", va="center",
                        fontsize=12, color="white" if luminance < 0.5 else INK)
        ax.set_xticks([0, 1], ["No churn", "Churn"])
        ax.set_yticks([0, 1], ["No churn", "Churn"])
        ax.set_xticks([0.5], minor=True)
        ax.set_yticks([0.5], minor=True)
        ax.grid(which="minor", color=SURFACE, linewidth=2)   # 2px surface gap between cells
        ax.tick_params(which="both", length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_xlabel("Predicted")
        ax.set_ylabel("Actual")
        ax.set_title(f"{name}\nrecall {m['recall']:.3f}  |  precision {m['precision']:.3f}", fontsize=10)
    fig.suptitle("Confusion matrices on the test set (threshold 0.5)", x=0.02, ha="left",
                 fontsize=12, fontweight="semibold", color=INK)
    fig.tight_layout()
    save(fig, path)


def chart_horizontal_bars(labels, values, errors, title: str, xlabel: str, path: Path,
                          label_top: int) -> None:
    """Single-series ranked bar chart (largest at the top)."""
    fig, ax = plt.subplots(figsize=(8.0, 0.36 * len(labels) + 1.3))
    y = np.arange(len(labels))
    ax.barh(y, values, height=0.55, color=BLUE, xerr=errors,
            error_kw={"ecolor": INK_2, "elinewidth": 1, "capsize": 0})
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.axvline(0, color=BASELINE, linewidth=1)
    ax.xaxis.grid(True)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    span = max(values) - min(0, min(values))
    for i in range(min(label_top, len(values))):          # label only the leading bars
        tip = values[i] + (errors[i] if errors is not None else 0)
        ax.text(tip + span * 0.01, i, f"{values[i]:.4f}", va="center", fontsize=8, color=INK_2)
    ax.set_xlabel(xlabel)
    ax.set_title(title)
    save(fig, path)


def chart_threshold(tuning: pd.DataFrame, chosen: float, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8.0, 4.6))
    lines = [("precision", "Precision", MODEL_COLORS["Logistic Regression"]),
             ("recall", "Recall", MODEL_COLORS["Decision Tree"]),
             ("f1", "F1-score", MODEL_COLORS["Random Forest"])]
    for column, label, color in lines:
        ax.plot(tuning["threshold"], tuning[column], color=color, linewidth=2, label=label)
        ax.text(tuning["threshold"].iloc[-1] + 0.01, tuning[column].iloc[-1], label,
                va="center", fontsize=9, color=INK_2)
    ax.axvline(0.5, color=MUTED, linewidth=1)
    ax.text(0.505, 0.03, "default 0.50", color=MUTED, fontsize=8)
    ax.axvline(chosen, color=INK, linewidth=1)
    ax.text(chosen + 0.005, 0.10, f"best F1 {chosen:.2f}", color=INK, fontsize=8)
    ax.set_xlim(tuning["threshold"].min(), tuning["threshold"].max() + 0.08)
    ax.set_ylim(0, 1)
    ax.grid(True)
    ax.set_axisbelow(True)
    ax.set_xlabel("Decision threshold (predict churn when probability >= threshold)")
    ax.set_ylabel("Score (training data, out-of-fold)")
    ax.set_title("Gradient Boosting: precision, recall and F1 at different thresholds", pad=26)
    ax.legend(ncol=3, loc="lower left", bbox_to_anchor=(0, 1.0))
    save(fig, path)


def chart_solutions(solutions: pd.DataFrame, path: Path) -> None:
    """Ranked bars: expected churners prevented per simulated solution.
    The recommended solution is drawn solid and named in its label."""
    sim = solutions[solutions["simulated"]].sort_values("churners_prevented", ascending=False)
    labels = [f"{n}  (recommended)" if r else n for n, r in zip(sim["solution_name"], sim["is_recommended"])]
    values = sim["churners_prevented"].to_numpy()
    fig, ax = plt.subplots(figsize=(8.0, 0.5 * len(sim) + 1.4))
    y = np.arange(len(sim))
    ax.barh(y, values, height=0.55, color=[BLUE if r else BLUE_RAMP[1] for r in sim["is_recommended"]])
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.axvline(0, color=BASELINE, linewidth=1)
    ax.xaxis.grid(True)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    span = max(values.max(), 0) - min(values.min(), 0)
    for i, value in enumerate(values):
        ax.text(max(value, 0) + span * 0.01, i, f"{value:,.0f}", va="center", fontsize=9, color=INK_2)
    ax.set_xlabel("Expected churners prevented among active customers (model what-if, out-of-fold)")
    ax.set_title("Possible solutions: what-if effect estimated by the scoring model")
    save(fig, path)


def chart_reasons(reasons: pd.DataFrame, path: Path) -> None:
    """Ranked bars: share of the customers who left for whom each factor is a
    reason (raises their churn probability by >= 5 points); the label also
    gives the share for whom it is the MAIN reason."""
    ranked = reasons[reasons["reason_key"] != "none"].sort_values("churners_contributing_pct", ascending=False)
    values = ranked["churners_contributing_pct"].to_numpy()
    fig, ax = plt.subplots(figsize=(8.0, 0.45 * len(ranked) + 1.5))
    y = np.arange(len(ranked))
    ax.barh(y, values, height=0.55, color=BLUE)
    ax.set_yticks(y, ranked["reason_name"])
    ax.invert_yaxis()
    ax.axvline(0, color=BASELINE, linewidth=1)
    ax.xaxis.grid(True)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    for i, (value, main) in enumerate(zip(values, ranked["churners_main_pct"])):
        main_text = "<1%" if 0 < main < 1 else f"{main:.0f}%"
        ax.text(value + 1, i, f"{value:.0f}%  (main reason for {main_text})", va="center", fontsize=9, color=INK_2)
    ax.set_xlim(0, 100)
    none = reasons.loc[reasons["reason_key"] == "none", "churners_main_pct"]
    ax.set_xlabel("% of the customers who left for whom the factor is a reason (inferred, not stated by customers)"
                  + (f"\n{none.iloc[0]:.0f}% of them have no single clear driver" if len(none) else ""))
    ax.set_title("Why customers leave: most likely reasons (model reason codes)")
    save(fig, path)


# =============================================================================
# 6. Write results to the warehouse
# =============================================================================
def write_results(engine: sa.Engine, run: dict, frames: dict[str, pd.DataFrame]) -> int:
    """Insert one mining run and its results in ONE transaction (all or nothing).

    frames maps an olap table name to the rows to append (run_id is added here).
    """
    with engine.begin() as conn:
        run_id = conn.execute(sa.text("""
            INSERT INTO olap.mining_run
                   (train_rows, test_rows, random_state, scoring_model,
                    tuned_threshold, sklearn_version, notes)
            VALUES (:train_rows, :test_rows, :random_state, :scoring_model,
                    :tuned_threshold, :sklearn_version, :notes)
            RETURNING run_id
        """), run).scalar_one()
        for table_name, frame in frames.items():
            frame.assign(run_id=run_id).to_sql(table_name, conn, schema="olap",
                                               if_exists="append", index=False,
                                               method="multi", chunksize=1000)
    return int(run_id)


def existing_olap_tables(engine: sa.Engine, names: list[str]) -> set[str]:
    """The olap tables among `names` that exist. Warehouses built before the
    explanation tables were added need sql/08_mining_objects.sql once more."""
    with engine.connect() as conn:
        found = conn.execute(sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'olap' "
                                     "AND tablename = ANY(:names)"), {"names": names}).scalars()
        return set(found)


# =============================================================================
# 7. Main analysis
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Churn classification on the PostgreSQL warehouse.")
    parser.add_argument("--host", default=os.environ.get("PGHOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PGPORT", "5432")))
    parser.add_argument("--user", default=os.environ.get("PGUSER", "postgres"))
    parser.add_argument("--dbname", default=os.environ.get("PGDATABASE", "telco_churn_dw"))
    parser.add_argument("--output-dir", default=str(Path(__file__).resolve().parent / "outputs"))
    parser.add_argument("--no-db-write", action="store_true", help="do not write results to the warehouse")
    parser.add_argument("--blank-total-as-missing", action="store_true",
                        help="treat TotalCharges of tenure-0 customers as missing (earlier analysis)")
    args = parser.parse_args()

    started = time.time()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    chart_style()

    # ---------------------------------------------------------------- data
    heading("1. Data from the warehouse (olap.v_churn_model_input)")
    engine = connect(args)
    df = load_data(engine)
    say(f"Database        : {args.dbname} on {args.host}:{args.port}")
    say("Model Ownership : MuhammaD_Wallah and the GROUP are the owners and Developers of this model")
    say(f"Rows x columns  : {df.shape[0]:,} x {df.shape[1]} "
        f"(identifiers customer_id, source_row_no are NOT used as features)")
    say(f"Features        : {len(NUMERIC_FEATURES)} numeric + {len(CATEGORICAL_FEATURES)} categorical")
    say(f"Churn = 1       : {int(df[TARGET].sum()):,} ({100 * df[TARGET].mean():.2f}%)")
    say(f"scikit-learn    : {sklearn.__version__}")

    X = df[NUMERIC_FEATURES + CATEGORICAL_FEATURES].copy()
    y = df[TARGET].astype(int)
    ids = df["customer_id"]
    if args.blank_total_as_missing:
        X.loc[X["tenure"] == 0, "TotalCharges"] = np.nan
        say("TotalCharges    : tenure-0 customers set to MISSING (median-imputed), as in the earlier analysis")
    else:
        say("TotalCharges    : warehouse value (0.00 for the 11 tenure-0 customers)")

    # ---------------------------------------------------------------- split
    heading("2. Train / test split (80/20, stratified, random_state=42)")
    X_train, X_test, y_train, y_test, ids_train, ids_test = train_test_split(
        X, y, ids, test_size=TEST_SIZE, stratify=y, random_state=RANDOM_STATE)
    say(f"Training rows: {len(X_train):,}  (churn rate {100 * y_train.mean():.2f}%)")
    say(f"Test rows    : {len(X_test):,}  (churn rate {100 * y_test.mean():.2f}%)")

    # ---------------------------------------------------------------- models
    heading("3. Test-set results (threshold 0.5)")
    models = build_models()
    fitted: dict[str, Pipeline] = {}
    test_probs: dict[str, np.ndarray] = {}
    test_metrics: dict[str, dict] = {}
    for name, (model, params) in models.items():
        pipe = make_pipeline(model).fit(X_train, y_train)     # fitted on training data only
        fitted[name] = pipe
        test_probs[name] = pipe.predict_proba(X_test)[:, 1]
        test_metrics[name] = evaluate(y_test, test_probs[name])

    results = pd.DataFrame([{"model": n, **{k: test_metrics[n][k] for k in METRICS + ["tn", "fp", "fn", "tp"]}}
                            for n in models])
    table(results)

    heading("4. Comparison with the earlier analysis (now - previous)")
    rows = []
    for name in models:
        for metric, previous in zip(METRICS, PREVIOUS_RESULTS[name]):
            now = round(test_metrics[name][metric], 4)
            rows.append({"model": name, "metric": metric, "previous": previous,
                         "now": now, "difference": round(now - previous, 4)})
    comparison = pd.DataFrame(rows)
    wide = comparison.pivot(index="model", columns="metric", values="difference")[METRICS].reset_index()
    table(wide, "{:+.4f}")
    say(f"Largest absolute difference: {comparison['difference'].abs().max():.4f}")
    rf = test_metrics["Random Forest"]
    say(f"Random Forest confusion matrix now: TN {rf['tn']}  FP {rf['fp']}  FN {rf['fn']}  TP {rf['tp']}"
        "   (earlier: TN 779  FP 256  FN 84  TP 290)")

    # ---------------------------------------------------------------- importance
    heading("5. Feature importance (Random Forest)")
    rf_pipe = fitted["Random Forest"]
    names = readable_feature_names(rf_pipe.named_steps["prep"].get_feature_names_out())
    impurity = (pd.DataFrame({"feature": names,
                              "importance": rf_pipe.named_steps["model"].feature_importances_})
                .sort_values("importance", ascending=False, ignore_index=True))
    impurity["rank_no"] = np.arange(1, len(impurity) + 1)
    say("5a. Impurity importance (mean decrease in impurity; one-hot columns), top 10:")
    table(impurity.head(10)[["rank_no", "feature", "importance"]])

    perm = permutation_importance(rf_pipe, X_test, y_test, scoring="roc_auc",
                                  n_repeats=PERMUTATION_REPEATS, random_state=RANDOM_STATE)
    permutation = (pd.DataFrame({"feature": X_test.columns,
                                 "importance": perm.importances_mean,
                                 "importance_std": perm.importances_std})
                   .sort_values("importance", ascending=False, ignore_index=True))
    permutation["rank_no"] = np.arange(1, len(permutation) + 1)
    say()
    say(f"5b. Permutation importance (mean decrease in test ROC-AUC, {PERMUTATION_REPEATS} repeats; "
        "original columns), top 10:")
    table(permutation.head(10)[["rank_no", "feature", "importance", "importance_std"]], "{:.5f}")

    # ---------------------------------------------------------------- CV
    heading(f"6. {CV_FOLDS}-fold stratified cross-validation (all 7,043 rows; pipeline refitted per fold)")
    cv = StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=RANDOM_STATE)
    cv_rows = []
    for name, (model, _) in models.items():
        scores = cross_validate(make_pipeline(model), X, y, cv=cv,
                                scoring=["accuracy", "precision", "recall", "f1", "roc_auc"])
        row = {"model": name}
        for metric in METRICS:
            row[f"{metric}_mean"] = scores[f"test_{metric}"].mean()
        row["roc_auc_std"] = scores["test_roc_auc"].std()
        row["roc_auc_folds"] = " ".join(f"{v:.3f}" for v in scores["test_roc_auc"])
        cv_rows.append(row)
    cv_results = pd.DataFrame(cv_rows)
    table(cv_results.drop(columns="roc_auc_folds"))
    say("ROC-AUC per fold:")
    for row in cv_rows:
        say(f"  {row['model']:<20} {row['roc_auc_folds']}")

    # ---------------------------------------------------------------- threshold
    heading("7. Threshold tuning example (Gradient Boosting)")
    say("The threshold is chosen on OUT-OF-FOLD predictions for the TRAINING set (5-fold CV),")
    say("then checked once on the untouched test set. Tuning on the test set would leak.")
    gb_model = models["Gradient Boosting"][0]
    oof_train = cross_val_predict(make_pipeline(gb_model), X_train, y_train, cv=cv,
                                  method="predict_proba")[:, 1]
    tuning_rows = []
    for threshold in np.round(np.arange(0.10, 0.701, 0.05), 2):
        m = evaluate(y_train, oof_train, threshold)
        tuning_rows.append({
            "threshold": threshold, "precision": m["precision"], "recall": m["recall"],
            "f1": m["f1"], "flagged_pct": 100 * (m["tp"] + m["fp"]) / len(y_train),
            "cost": COST_MISSED_CHURNER * m["fn"] + COST_WASTED_OFFER * m["fp"],
        })
    tuning = pd.DataFrame(tuning_rows)
    table(tuning, "{:.4f}")
    best_f1_threshold = float(tuning.loc[tuning["f1"].idxmax(), "threshold"])
    best_cost_threshold = float(tuning.loc[tuning["cost"].idxmin(), "threshold"])
    say()
    say(f"Threshold with the best F1 on training data      : {best_f1_threshold:.2f}")
    say(f"Threshold with the lowest cost (FN = {COST_MISSED_CHURNER:g} x FP)       : {best_cost_threshold:.2f}")

    gb_test = test_probs["Gradient Boosting"]
    check_rows = []
    for label, threshold in [("default", 0.50), ("best F1", best_f1_threshold),
                             ("lowest cost", best_cost_threshold)]:
        m = evaluate(y_test, gb_test, threshold)
        check_rows.append({"rule": label, "threshold": threshold, "precision": m["precision"],
                           "recall": m["recall"], "f1": m["f1"], "missed_churners": m["fn"],
                           "wasted_offers": m["fp"],
                           "flagged_pct": 100 * (m["tp"] + m["fp"]) / len(y_test),
                           "cost": COST_MISSED_CHURNER * m["fn"] + COST_WASTED_OFFER * m["fp"]})
    threshold_check = pd.DataFrame(check_rows)
    say("Effect on the TEST set:")
    table(threshold_check, "{:.4f}")

    # ---------------------------------------------------------------- scores
    heading("8. Churn probability for every customer (out-of-fold)")
    scoring_model = cv_results.sort_values("roc_auc_mean", ascending=False).iloc[0]["model"]
    say(f"Scoring model (highest mean CV ROC-AUC): {scoring_model}")
    oof_all = cross_val_predict(make_pipeline(models[scoring_model][0]), X, y, cv=cv,
                                method="predict_proba")[:, 1]
    scores_df = pd.DataFrame({
        "customer_id": ids.values,
        "churn_probability": np.round(oof_all, 6),
        "risk_band": np.select([oof_all >= 0.50, oof_all >= 0.25], ["High", "Medium"], "Low"),
        "actual_churn": y.values.astype(int),
    })
    band_summary = (scores_df.groupby("risk_band")
                    .agg(customers=("customer_id", "size"), actual_churn_rate=("actual_churn", "mean"))
                    .reindex(["High", "Medium", "Low"]).reset_index())
    band_summary["actual_churn_rate"] *= 100
    table(band_summary, "{:.2f}")
    still_active_high = int(((scores_df["risk_band"] == "High") & (scores_df["actual_churn"] == 0)).sum())
    say(f"Customers who have NOT churned but are in the High band (retention targets): {still_active_high:,}")

    # ---------------------------------------------------------------- explanation
    heading("9. What the results mean: explanation, possible solutions and recommendation")
    say("Built by rules from the numbers above (mining/insights.py); the what-if simulation refits")
    say(f"{scoring_model} once per cross-validation fold.")
    explanation = insights.explain_results(
        X=X, y=y, ids=ids, model_settings={name: params for name, (_, params) in models.items()},
        test_metrics=test_metrics, cv_results=cv_results, comparison=comparison, permutation=permutation,
        threshold_check=threshold_check, tuned_model="Gradient Boosting",
        cost_ratio=COST_MISSED_CHURNER / COST_WASTED_OFFER, scores_df=scores_df,
        scoring_model=scoring_model, scoring_estimator=models[scoring_model][0],
        make_pipeline=make_pipeline, cv=cv)
    for line in explanation.text_lines(prefix="9."):
        say(line)

    # ---------------------------------------------------------------- charts & files
    heading("10. Charts and files")
    chart_model_comparison(test_metrics, out / "01_model_comparison.png")
    chart_roc(y_test, test_probs, out / "02_roc_curves.png")
    chart_confusion_matrices(test_metrics, out / "03_confusion_matrices.png")
    top = impurity.head(15)
    chart_horizontal_bars(top["feature"].tolist(), top["importance"].to_numpy(), None,
                          "Random Forest: impurity importance (top 15 one-hot features)",
                          "Mean decrease in impurity (all features sum to 1)",
                          out / "04_rf_impurity_importance.png", label_top=6)
    chart_horizontal_bars(permutation["feature"].tolist(), permutation["importance"].to_numpy(),
                          permutation["importance_std"].to_numpy(),
                          "Random Forest: permutation importance (original columns)",
                          f"Mean decrease in test ROC-AUC when the column is shuffled ({PERMUTATION_REPEATS} repeats; line = 1 std)",
                          out / "05_permutation_importance.png", label_top=4)
    chart_threshold(tuning, best_f1_threshold, out / "06_threshold_tuning.png")
    chart_solutions(explanation.solutions, out / "07_solution_comparison.png")
    chart_reasons(explanation.reasons, out / "08_churn_reasons.png")

    results.to_csv(out / "test_metrics.csv", index=False)
    comparison.to_csv(out / "comparison_with_previous.csv", index=False)
    cv_results.to_csv(out / "cross_validation.csv", index=False)
    impurity.to_csv(out / "rf_impurity_importance.csv", index=False)
    permutation.to_csv(out / "rf_permutation_importance.csv", index=False)
    tuning.to_csv(out / "threshold_tuning_train_oof.csv", index=False)
    scores_df.to_csv(out / "customer_churn_scores.csv", index=False)
    explanation.solutions.to_csv(out / "retention_solutions.csv", index=False)
    explanation.targets.to_csv(out / "recommended_action_targets.csv", index=False)
    explanation.reasons.to_csv(out / "churn_reasons.csv", index=False)
    explanation.customer_reasons.to_csv(out / "churn_reasons_by_customer.csv", index=False)
    say(f"  saved 11 CSV files in {out.as_posix()}")
    (out / "insights_report.md").write_text(explanation.markdown(), encoding="utf-8")
    say(f"  saved {(out / 'insights_report.md').as_posix()}  (the explanation of section 9)")

    # ---------------------------------------------------------------- database
    heading("11. Results written to the warehouse")
    if args.no_db_write:
        say("Skipped (--no-db-write).")
    else:
        metrics_df = pd.DataFrame([{
            "model_name": name,
            "hyperparameters": models[name][1],
            "accuracy": round(test_metrics[name]["accuracy"], 4),
            "precision_score": round(test_metrics[name]["precision"], 4),
            "recall_score": round(test_metrics[name]["recall"], 4),
            "f1_score": round(test_metrics[name]["f1"], 4),
            "roc_auc": round(test_metrics[name]["roc_auc"], 4),
            "tn": test_metrics[name]["tn"], "fp": test_metrics[name]["fp"],
            "fn": test_metrics[name]["fn"], "tp": test_metrics[name]["tp"],
            "cv_accuracy_mean": round(r["accuracy_mean"], 4),
            "cv_precision_mean": round(r["precision_mean"], 4),
            "cv_recall_mean": round(r["recall_mean"], 4),
            "cv_f1_mean": round(r["f1_mean"], 4),
            "cv_roc_auc_mean": round(r["roc_auc_mean"], 4),
            "cv_roc_auc_std": round(r["roc_auc_std"], 4),
        } for name, r in zip(models, cv_rows)])
        importance_df = pd.concat([
            impurity.assign(model_name="Random Forest", method="impurity", importance_std=np.nan),
            permutation.assign(model_name="Random Forest", method="permutation"),
        ])[["model_name", "method", "feature", "importance", "importance_std", "rank_no"]]
        importance_df["importance"] = importance_df["importance"].round(6)
        importance_df["importance_std"] = importance_df["importance_std"].astype(float).round(6)
        test_pred_df = pd.concat([
            pd.DataFrame({"model_name": name, "customer_id": ids_test.values,
                          "actual_churn": y_test.values.astype(int),
                          "churn_probability": np.round(test_probs[name], 6)})
            for name in models])
        notes = ("TotalCharges blank-as-missing (earlier analysis)" if args.blank_total_as_missing
                 else "TotalCharges = warehouse value (0.00 for tenure 0)")
        frames = {"model_metrics": metrics_df, "model_feature_importance": importance_df,
                  "model_test_prediction": test_pred_df, "customer_churn_score": scores_df}
        insight_frames = {"mining_insight": explanation.section_rows(),
                          "retention_solution": explanation.solutions,
                          "churn_reason": explanation.reasons}
        present = existing_olap_tables(engine, list(insight_frames))
        frames.update({name: frame for name, frame in insight_frames.items() if name in present})
        run_id = write_results(engine, {
            "train_rows": len(X_train), "test_rows": len(X_test), "random_state": RANDOM_STATE,
            "scoring_model": scoring_model, "tuned_threshold": best_f1_threshold,
            "sklearn_version": sklearn.__version__, "notes": notes,
        }, frames)
        say(f"olap.mining_run run_id = {run_id}")
        for table_name, frame in frames.items():
            say(f"  olap.{table_name:<26} {len(frame):>6,} rows")
        missing = [f"olap.{name}" for name in insight_frames if name not in present]
        if missing:
            say(f"  NOTE: {', '.join(missing)} do(es) not exist yet, so that part of the explanation was saved")
            say("  only in mining/outputs. Run sql/08_mining_objects.sql once, then run this script again")
            say("  to show it on the dashboard's 'Insights & recommendations' page.")

    say()
    say(f"Finished in {time.time() - started:.1f} s. Remember: these are associations in this sample, "
        "not causes of churn.")
    say("Full explanation: mining/outputs/insights_report.md")
    (out / "results_summary.txt").write_text("\n".join(REPORT_LINES) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
