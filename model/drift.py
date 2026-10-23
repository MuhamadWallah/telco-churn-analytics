"""
drift.py - simple data-drift measures, shared by train.py and score.py.

"Data drift" means: today's customers look different from the customers the
model learned from, so its predictions may become less reliable.

train.py saves a REFERENCE profile (what the data looked like at training time)
in metrics.json; score.py measures the same profile every month, stores it in
dw.model_runs, and compares the two:

  PSI (Population Stability Index) compares how the churn probabilities are
  spread over 10 bins (each bin held 10% of the customers at training time).
      PSI < 0.10        OK     - no real change
      0.10 <= PSI < 0.25 Watch - some change; look at the inputs
      PSI >= 0.25       Act    - big change; retrain and investigate
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config


def bin_edges(probabilities: np.ndarray, bins: int = 10) -> list[float]:
    """The 9 inner cut points that split the training-time probabilities into 10 equal groups."""
    inner = np.quantile(probabilities, np.linspace(0, 1, bins + 1)[1:-1])
    return [round(float(v), 6) for v in np.unique(inner)]


def bin_shares(probabilities: np.ndarray, edges: list[float]) -> list[float]:
    """Share of customers in each bin (bins are [edge_i, edge_i+1))."""
    index = np.searchsorted(np.asarray(edges), probabilities, side="right")
    counts = np.bincount(index, minlength=len(edges) + 1)
    return [round(float(v), 6) for v in counts / max(len(probabilities), 1)]


def psi(expected: list[float], actual: list[float], floor: float = 1e-4) -> float:
    """Population Stability Index: sum of (actual - expected) * ln(actual / expected)."""
    e = np.clip(np.asarray(expected, dtype=float), floor, None)
    a = np.clip(np.asarray(actual, dtype=float), floor, None)
    return round(float(np.sum((a - e) * np.log(a / e))), 4)


def status(psi_value: float, watch: float = config.PSI_WATCH, act: float = config.PSI_ACT) -> str:
    if psi_value >= act:
        return "Act"
    if psi_value >= watch:
        return "Watch"
    return "OK"


def profile(X: pd.DataFrame, y: pd.Series, probabilities: np.ndarray,
            threshold: float, medium_from: float) -> dict:
    """A few easy-to-read numbers that describe the customer base and the predictions."""
    p = np.asarray(probabilities)
    return {
        "customers": int(len(X)),
        "churn_rate": round(float(y.mean()), 4),
        "mean_probability": round(float(p.mean()), 4),
        "share_high": round(float((p >= threshold).mean()), 4),
        "share_medium": round(float(((p >= medium_from) & (p < threshold)).mean()), 4),
        "share_low": round(float((p < medium_from).mean()), 4),
        "mean_tenure": round(float(X["tenure"].mean()), 2),
        "mean_monthly_charges": round(float(X["MonthlyCharges"].mean()), 2),
        "share_month_to_month": round(float((X["Contract"] == "Month-to-month").mean()), 4),
    }
