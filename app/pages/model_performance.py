"""Model performance - comparison table, confusion matrix, feature importance, run history."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import charts
import common

metrics = common.metrics_or_stop()
settings = metrics["settings"]
threshold = settings["decision_threshold"]
names = [m["name"] for m in metrics["models"]]
final = common.final_model(metrics)

common.page_header(
    "Model performance",
    f"From model/metrics.json · final model {metrics['model_name']} · version {metrics['model_version']} · "
    f"trained {common.utc(metrics['trained_at'])} · scikit-learn "
    f"{metrics['sklearn_version']} · {metrics['data']['train_rows']:,} training / "
    f"{metrics['data']['test_rows']:,} test customers")

st.markdown(f"**Why {metrics['model_name']}?** {metrics['selection_reason']}")

# ----------------------------------------------------------------------------- comparison table
st.subheader("Comparison of the four models")
st.caption(f"Test = the untouched 20% test set. CV = {metrics['data']['cv_folds']}-fold cross-validation on the "
           f"training data (used to choose the model). All at decision threshold {threshold:.2f}.")
NAMES = {"accuracy": "Accuracy", "precision": "Precision", "recall": "Recall", "f1": "F1", "roc_auc": "ROC-AUC"}
rows = []
for m in metrics["models"]:
    rows.append({"Model": m["name"] + ("  (final)" if m["is_final"] else ""),
                 **{f"Test {NAMES[k]}": m["test"][k] for k in ("accuracy", "precision", "recall", "f1", "roc_auc")},
                 **{f"CV {NAMES[k]}": m["cv"][k] for k in ("precision", "recall", "roc_auc")}})
table = pd.DataFrame(rows)
st.dataframe(table, hide_index=True, width="stretch",
             column_config={c: st.column_config.NumberColumn(c, format="%.3f") for c in table.columns if c != "Model"})

long = pd.DataFrame([{"model": m["name"], "metric": label, "value": m["test"][key]}
                     for m in metrics["models"]
                     for key, label in [("accuracy", "Accuracy"), ("precision", "Precision"), ("recall", "Recall"),
                                        ("f1", "F1-score"), ("roc_auc", "ROC-AUC")]])
common.show(charts.model_dots(long, names, f"Score on the test set (threshold {threshold:.2f})"))

# ----------------------------------------------------------------------------- confusion + importance
left, right = st.columns(2, gap="large")
with left:
    st.subheader("Confusion matrix (test set)")
    chosen = st.selectbox("Model", names, index=names.index(final["name"]))
    t = next(m for m in metrics["models"] if m["name"] == chosen)["test"]
    common.show(charts.confusion(t["tn"], t["fp"], t["fn"], t["tp"]))
    st.caption(f"Of {t['tp'] + t['fn']:,} customers who left, the model caught {t['tp']:,} (recall "
               f"{t['recall']:.1%}) and missed {t['fn']:,}. It also flagged {t['fp']:,} customers who stayed "
               f"(wasted offers); {t['precision']:.1%} of its warnings were correct (precision).")
with right:
    fi = metrics["feature_importance"]
    st.subheader(f"Feature importance ({fi['model']})")
    common.show(charts.importance_bars(pd.DataFrame(fi["rows"]), "Decrease in test ROC-AUC when shuffled"))
    st.caption(fi["method"][:1].upper() + fi["method"][1:] + ". A large value means the model relies heavily on that input; it "
               "does not mean the input causes churn.")

# ----------------------------------------------------------------------------- threshold
tt = metrics.get("threshold_table")
if tt:
    st.subheader("Choosing the decision threshold")
    rows = pd.DataFrame(tt["rows"])
    long = rows.melt(id_vars="threshold", value_vars=["precision", "recall", "f1"], var_name="metric")
    long["metric"] = long["metric"].map({"precision": "Precision", "recall": "Recall", "f1": "F1-score"})
    common.show(charts.threshold_lines(long, threshold))
    st.caption(f"{tt['model']}, {tt['source']}. The black line is the current threshold ({threshold:.2f}). "
               "Moving it left catches more churners (recall up) but sends more unneeded offers (precision down). "
               "To change it, edit DECISION_THRESHOLD in model/config.py and run model/train.py again.")
    with st.expander("Show the numbers"):
        st.dataframe(rows, hide_index=True, width="stretch", column_config={
            "threshold": st.column_config.NumberColumn("Threshold", format="%.2f"),
            "precision": st.column_config.NumberColumn("Precision", format="%.3f"),
            "recall": st.column_config.NumberColumn("Recall", format="%.3f"),
            "f1": st.column_config.NumberColumn("F1", format="%.3f"),
            "flagged_pct": st.column_config.NumberColumn("Customers flagged %", format="%.1f"),
        })

# ----------------------------------------------------------------------------- history
st.subheader("Performance over time (dw.model_runs)")
runs = common.read("""
    SELECT run_at, run_type, model_version, customers_scored, high_risk,
           current_recall, current_roc_auc, test_recall, test_roc_auc, psi_probability, drift_status
    FROM   olap.v_model_runs
    ORDER  BY run_at
""")
if runs.empty:
    st.info("No scoring runs yet. They appear after `python model/score.py` or the monthly workflow.")
else:
    hist = runs.melt(id_vars=["run_at", "model_version"], value_vars=["current_recall", "current_roc_auc"],
                     var_name="metric", value_name="value")
    hist["metric"] = hist["metric"].map({"current_recall": "Recall (today's data)",
                                         "current_roc_auc": "ROC-AUC (today's data)"})
    common.show(charts.history_lines(hist))
    latest = runs.iloc[-1]
    st.caption(f"Latest run: data drift **{latest.drift_status}** (PSI {latest.psi_probability:.3f}; below "
               f"{settings['psi_watch']:.2f} = OK, {settings['psi_watch']:.2f}-{settings['psi_act']:.2f} = Watch, "
               f"above {settings['psi_act']:.2f} = Act). Today's data includes the training customers, so watch the "
               "trend rather than the level.")
    recent = runs.tail(12).iloc[::-1][["run_at", "run_type", "model_version", "high_risk", "current_recall",
                                       "current_roc_auc", "psi_probability", "drift_status"]]
    st.dataframe(recent, hide_index=True, width="stretch", column_config={
        "run_at": st.column_config.DatetimeColumn("Run (UTC)", format="D MMM YYYY, HH:mm", timezone="UTC"),
        "run_type": st.column_config.TextColumn("Type"),
        "model_version": st.column_config.TextColumn("Model version"),
        "high_risk": st.column_config.NumberColumn("High-risk customers", format="%d"),
        "current_recall": st.column_config.NumberColumn("Recall today", format="%.3f"),
        "current_roc_auc": st.column_config.NumberColumn("ROC-AUC today", format="%.3f"),
        "psi_probability": st.column_config.NumberColumn("PSI", format="%.3f"),
        "drift_status": st.column_config.TextColumn("Drift"),
    })

with st.expander("What do these metrics mean?"):
    st.markdown(f"""
- **Recall**: of the customers who really left, the share the model flagged. High recall = few churners missed.
- **Precision**: of the customers the model flagged, the share who really left. High precision = few wasted offers.
- **F1-score**: one number that balances precision and recall.
- **ROC-AUC**: how well the model ranks leavers above stayers, whatever the threshold (0.5 = guessing, 1 = perfect).
- **Accuracy**: share of all customers classified correctly (can look good even for a weak model, because most customers stay).
- **Decision threshold ({threshold:.2f})**: a customer is predicted to churn when the probability is at least this
  value. A lower threshold catches more churners (recall up) but sends more unnecessary offers (precision down).
""")
st.caption(common.CAVEAT)
