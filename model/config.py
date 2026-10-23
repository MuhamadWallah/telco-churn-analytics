"""
config.py - the settings shared by train.py, score.py and promote.py.

Change a setting HERE, then run   python model/train.py   again. train.py copies
every setting into model/metrics.json, and score.py and the web app read them
from that file, so all parts of the project always use the same numbers.
"""

from __future__ import annotations

from pathlib import Path

# -----------------------------------------------------------------------------
# Files
# -----------------------------------------------------------------------------
MODEL_DIR = Path(__file__).resolve().parent
MODEL_FILE = "churn_model.joblib"              # the whole fitted Pipeline
METRICS_FILE = "metrics.json"                  # all metrics + settings + input form schema
CANDIDATE_DIR = MODEL_DIR / "candidate"        # a retrained model waiting for comparison
ARCHIVE_DIR = MODEL_DIR / "archive"            # older models, kept for roll-back
MAX_MODEL_MB = 25                              # commit the model to GitHub only below this size

# -----------------------------------------------------------------------------
# Data: always read from the warehouse view (created by sql/08_mining_objects.sql),
# never from the CSV file. customer_id is an identifier, NOT a feature.
# -----------------------------------------------------------------------------
SOURCE_VIEW = "olap.v_churn_model_input"
ID_COLUMN = "customer_id"
TARGET = "Churn"                               # 1 = Yes, 0 = No
NUMERIC_FEATURES = ["SeniorCitizen", "tenure", "MonthlyCharges", "TotalCharges"]
CATEGORICAL_FEATURES = [
    "gender", "Partner", "Dependents", "PhoneService", "MultipleLines",
    "InternetService", "OnlineSecurity", "OnlineBackup", "DeviceProtection",
    "TechSupport", "StreamingTV", "StreamingMovies", "Contract",
    "PaperlessBilling", "PaymentMethod",
]

# -----------------------------------------------------------------------------
# Experiment design (same as mining/churn_mining.py, so the results match)
# -----------------------------------------------------------------------------
RANDOM_STATE = 42
TEST_SIZE = 0.20                               # 80/20 stratified split
CV_FOLDS = 5                                   # cross-validation on the TRAINING part only
PERMUTATION_REPEATS = 5                        # feature-importance repeats

# -----------------------------------------------------------------------------
# THE DECISION THRESHOLD - the one setting to change.
# A customer is predicted to churn when  probability >= DECISION_THRESHOLD.
#   lower it (e.g. 0.35) -> more churners caught (higher recall), more wasted offers
#   raise it (e.g. 0.65) -> fewer wasted offers (higher precision), more churners missed
# Risk labels:  High   = probability >= DECISION_THRESHOLD
#               Medium = MEDIUM_RISK_FROM <= probability < DECISION_THRESHOLD
#               Low    = probability < MEDIUM_RISK_FROM
# -----------------------------------------------------------------------------
DECISION_THRESHOLD = 0.50
MEDIUM_RISK_FROM = 0.25

# -----------------------------------------------------------------------------
# How the final model is chosen (on cross-validation of the TRAINING data, so
# the test set stays an untouched, fair final exam):
#   1. drop models whose precision is below MIN_PRECISION
#      (0.50 = at most one wasted offer for every churner caught);
#   2. of the rest, take the highest SELECTION_METRIC;
#   3. a tie is broken by ROC-AUC.
# For churn, missing a leaver usually costs more than an unneeded offer, so
# recall is the main metric. Use "precision", "f1" or "roc_auc" to change it.
# -----------------------------------------------------------------------------
SELECTION_METRIC = "recall"
MIN_PRECISION = 0.50

# -----------------------------------------------------------------------------
# Retraining rule (promote.py): a retrained model replaces the current one only
# if neither recall nor ROC-AUC gets worse on the same test customers.
# -----------------------------------------------------------------------------
PROMOTION_METRICS = ["recall", "roc_auc"]
PROMOTION_TOLERANCE = 0.0                      # 0.0 = "must not get worse at all"

# -----------------------------------------------------------------------------
# Data drift (score.py). PSI = Population Stability Index: how much the spread
# of churn probabilities today differs from the spread when the model was trained.
# -----------------------------------------------------------------------------
PSI_WATCH = 0.10                               # below 0.10: OK (no real change)
PSI_ACT = 0.25                                 # 0.10-0.25: Watch; above 0.25: Act (retrain)

# -----------------------------------------------------------------------------
# "Top reasons" on the Predict page. Each group of columns is one reason; the
# app measures how much this customer's values for the group raise or lower the
# churn probability compared with real customers' values (see app/pages/predict_churn.py).
# needs_internet: only compared with customers who also have internet service.
# -----------------------------------------------------------------------------
REASON_GROUPS = [
    {"label": "Contract type", "columns": ["Contract"]},
    {"label": "Time with the company (tenure)", "columns": ["tenure", "TotalCharges"]},
    {"label": "Monthly charges", "columns": ["MonthlyCharges"]},
    {"label": "Internet service type",
     "columns": ["InternetService", "OnlineSecurity", "OnlineBackup", "DeviceProtection",
                 "TechSupport", "StreamingTV", "StreamingMovies"]},
    {"label": "Online security and tech support", "columns": ["OnlineSecurity", "TechSupport"],
     "needs_internet": True},
    {"label": "Online backup and device protection", "columns": ["OnlineBackup", "DeviceProtection"],
     "needs_internet": True},
    {"label": "Streaming TV and movies", "columns": ["StreamingTV", "StreamingMovies"],
     "needs_internet": True},
    {"label": "Payment method", "columns": ["PaymentMethod"]},
    {"label": "Paperless billing", "columns": ["PaperlessBilling"]},
    {"label": "Phone lines", "columns": ["PhoneService", "MultipleLines"]},
    {"label": "Household (partner, dependents)", "columns": ["Partner", "Dependents"]},
    {"label": "Senior citizen", "columns": ["SeniorCitizen"]},
    {"label": "Gender", "columns": ["gender"]},
]
REFERENCE_ROWS = 200                           # real training customers kept for the comparison
