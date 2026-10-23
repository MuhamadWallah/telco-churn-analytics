"""Predict churn - a form for one customer: probability, risk label and the top reasons."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import charts
import common

pipe, metrics = common.model_or_stop()
settings, schema = metrics["settings"], metrics["input_schema"]
threshold, medium_from = settings["decision_threshold"], settings["medium_risk_from"]
features = metrics["data"]["numeric_features"] + metrics["data"]["categorical_features"]

common.page_header(
    "Predict churn for one customer",
    f"Model: {metrics['model_name']} · version {metrics['model_version']} · trained "
    f"{pd.Timestamp(metrics['trained_at']):%d %b %Y} · decision threshold {threshold:.2f}")

# Friendly names for the form (display text only; the allowed values come from metrics.json).
LABELS = {
    "gender": "Gender", "SeniorCitizen": "Senior citizen", "Partner": "Has a partner",
    "Dependents": "Has dependents", "tenure": "Tenure (months with the company)",
    "Contract": "Contract", "PaperlessBilling": "Paperless billing", "PaymentMethod": "Payment method",
    "MonthlyCharges": "Monthly charges", "TotalCharges": "Total charges so far",
    "PhoneService": "Phone service", "MultipleLines": "Multiple lines", "InternetService": "Internet service",
    "OnlineSecurity": "Online security", "OnlineBackup": "Online backup", "DeviceProtection": "Device protection",
    "TechSupport": "Tech support", "StreamingTV": "Streaming TV", "StreamingMovies": "Streaming movies",
}
SECTIONS = {
    "Customer": ["gender", "SeniorCitizen", "Partner", "Dependents"],
    "Account": ["tenure", "Contract", "PaperlessBilling", "PaymentMethod", "MonthlyCharges", "TotalCharges"],
    "Services": ["PhoneService", "MultipleLines", "InternetService", "OnlineSecurity", "OnlineBackup",
                 "DeviceProtection", "TechSupport", "StreamingTV", "StreamingMovies"],
}
listed = {c for cols in SECTIONS.values() for c in cols}
SECTIONS["Other"] = [c for c in features if c not in listed]      # any new feature still gets a field
NO_INTERNET, NO_PHONE = "No internet service", "No phone service"
INTERNET_ADDONS = ["OnlineSecurity", "OnlineBackup", "DeviceProtection", "TechSupport",
                   "StreamingTV", "StreamingMovies"]


def field(column: str):
    """One input widget, built from the training data's allowed values and ranges."""
    label = LABELS.get(column, column)
    if column in schema["categorical"]:
        info = schema["categorical"][column]
        return st.selectbox(label, info["values"], index=info["values"].index(info["most_frequent"]))
    info = schema["numeric"][column]
    if info["integer"] and info["min"] == 0 and info["max"] == 1:           # a 0/1 flag
        return 1 if st.selectbox(label, ["No", "Yes"]) == "Yes" else 0
    if info["integer"]:
        return int(st.number_input(label, min_value=int(info["min"]), max_value=int(info["max"]),
                                   value=int(info["median"]), step=1))
    return float(st.number_input(label, min_value=0.0, max_value=round(info["max"] * 2, 2),
                                 value=round(info["median"], 2), step=1.0, format="%.2f"))


def make_consistent(customer: dict) -> list[str]:
    """Fix impossible combinations (e.g. streaming TV without internet). Returns what was changed."""
    notes = []
    if "InternetService" in customer:
        no_internet = customer["InternetService"] == "No"
        for col in INTERNET_ADDONS:
            values = schema["categorical"].get(col, {}).get("values", [])
            if col in customer and NO_INTERNET in values:
                wanted = NO_INTERNET if no_internet else ("No" if customer[col] == NO_INTERNET else customer[col])
                if customer[col] != wanted:
                    notes.append(f"{LABELS.get(col, col)} set to '{wanted}'")
                    customer[col] = wanted
    if "PhoneService" in customer and "MultipleLines" in customer:
        wanted = NO_PHONE if customer["PhoneService"] == "No" else (
            "No" if customer["MultipleLines"] == NO_PHONE else customer["MultipleLines"])
        if customer["MultipleLines"] != wanted:
            notes.append(f"Multiple lines set to '{wanted}'")
            customer["MultipleLines"] = wanted
    return notes


def reason_points(x: pd.DataFrame) -> pd.DataFrame:
    """For each factor: how many percentage points this customer's values add to the churn
    probability, compared with the values of real training customers (saved in metrics.json).

    For every factor we copy the customer once per reference customer, replace only that factor's
    columns with the reference customer's values, and average the predictions. The difference
    from the customer's own prediction is the factor's effect for THIS customer.
    """
    ref = pd.DataFrame(metrics["reference_rows"])[x.columns]
    own = pipe.predict_proba(x)[0, 1]
    has_internet = x.iloc[0].get("InternetService", "Yes") != "No"
    copies, owners, shown = [], [], {}
    for group in metrics["reason_groups"]:
        cols = [c for c in group["columns"] if c in x.columns]
        if not cols or (group.get("needs_internet") and not has_internet):
            continue
        rows = ref[ref["InternetService"] != "No"] if group.get("needs_internet") else ref
        variant = pd.concat([x] * len(rows), ignore_index=True)
        for col in cols:                                  # column by column keeps the data types
            variant[col] = rows[col].to_numpy()
        copies.append(variant)
        owners += [group["label"]] * len(rows)
        values = [f"{LABELS.get(c, c)}: {x.iloc[0][c]}" for c in cols[:3]]
        shown[group["label"]] = ", ".join(values) + (" ..." if len(cols) > 3 else "")
    probs = pipe.predict_proba(pd.concat(copies, ignore_index=True))[:, 1]
    mean = pd.DataFrame({"label": owners, "p": probs}).groupby("label", sort=False)["p"].mean()
    out = pd.DataFrame({"label": mean.index, "points": (own - mean.to_numpy()) * 100})
    out["value"] = out["label"].map(shown)
    return out.sort_values("points", ascending=False, ignore_index=True)


# ----------------------------------------------------------------------------- the form
with st.form("customer_form"):
    customer = {}
    for section, columns in SECTIONS.items():
        columns = [c for c in columns if c in features]
        if not columns:
            continue
        st.markdown(f"**{section}**")
        grid = st.columns(3)
        for i, col in enumerate(columns):
            with grid[i % 3]:
                customer[col] = field(col)
    auto_total = st.checkbox("Calculate total charges as tenure × monthly charges", value=True)
    submitted = st.form_submit_button("Predict churn", type="primary", icon=":material/online_prediction:")

if submitted:
    if auto_total and {"TotalCharges", "tenure", "MonthlyCharges"} <= customer.keys():
        customer["TotalCharges"] = round(customer["tenure"] * customer["MonthlyCharges"], 2)
    st.session_state["customer"] = customer
    st.session_state["notes"] = make_consistent(customer)

if "customer" not in st.session_state:
    st.info("Fill in the customer's details and press **Predict churn**.", icon=":material/edit_note:")
    st.caption(common.CAVEAT)
    st.stop()

# ----------------------------------------------------------------------------- the prediction
x = pd.DataFrame([st.session_state["customer"]])[features]
probability = float(pipe.predict_proba(x)[0, 1])
label = common.risk_label(probability, settings)
if st.session_state["notes"]:
    st.caption("Adjusted to a possible combination: " + "; ".join(st.session_state["notes"]) + ".")

st.subheader("Result")
r1, r2, r3 = st.columns(3)
r1.metric("Churn probability", f"{probability:.1%}", border=True)
r2.metric("Risk label", label, border=True,
          help=f"High: probability ≥ {threshold:.2f} · Medium: {medium_from:.2f} to {threshold:.2f} · "
               f"Low: below {medium_from:.2f}")
r3.metric("Prediction", "Likely to churn" if probability >= threshold else "Likely to stay", border=True,
          help=f"'Likely to churn' when the probability is at least the decision threshold ({threshold:.2f}).")
st.progress(min(max(probability, 0.0), 1.0))

# ----------------------------------------------------------------------------- the reasons
st.subheader("Top reasons for this customer")
points = reason_points(x)
raising = points[points["points"] >= 1.0].head(3)
if raising.empty:
    st.markdown("No single factor raises this customer's risk by 1 point or more.")
for row in raising.itertuples():
    st.markdown(f"- **{row.label}** ({row.value}) raises the churn probability by **{row.points:.1f} "
                "percentage points** compared with typical customers.")
lowering = points[points["points"] <= -1.0].tail(2)
for row in lowering.iloc[::-1].itertuples():
    st.markdown(f"- **{row.label}** ({row.value}) *lowers* it by **{-row.points:.1f} points**.")
common.show(charts.reason_bars(points))

importance = pd.DataFrame(metrics["feature_importance"]["rows"]).head(5)
st.caption("Most important inputs for the model overall (permutation feature importance on the test set): "
           + ", ".join(f"{i + 1}. {LABELS.get(f, f)}" for i, f in enumerate(importance["feature"])) + ".")
with st.expander("How are the reasons worked out?"):
    st.markdown(
        "Feature importance tells which inputs matter **for the model in general**. For **one** customer we "
        "ask, for each factor: *what would the model predict if this customer had the values that real "
        f"customers have for this factor?* We try {len(metrics['reference_rows'])} real training customers' "
        "values and average the predictions. The difference from this customer's own prediction is the "
        "number of percentage points the factor adds (orange, raises the risk) or removes (blue).")
st.caption(common.CAVEAT)
