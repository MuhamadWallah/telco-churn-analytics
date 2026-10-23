"""At-risk customers - every customer ranked by churn risk, read from dw.churn_scores."""

from __future__ import annotations

import streamlit as st

import common

scores = common.read("SELECT * FROM olap.v_customer_risk ORDER BY risk_rank")
if scores.empty:
    common.page_header("At-risk customers", "dw.churn_scores")
    st.warning("dw.churn_scores is empty. Run `python model/score.py` (or the monthly workflow) first.",
               icon=":material/hourglass_empty:")
    st.stop()

version = scores["model_version"].iloc[0]
common.page_header("At-risk customers",
                   f"Every customer ranked by churn probability · table dw.churn_scores · model {version} · "
                   f"scored {common.utc(scores['scored_at'].max())}")

f1, f2, f3, f4 = st.columns([1.2, 1.6, 1.1, 1.1])
labels = [x for x in ["High", "Medium", "Low"] if x in set(scores["risk_label"])]
chosen_labels = f1.multiselect("Risk label", labels, default=labels[:1])
contracts = sorted(scores["contract_type"].unique())
chosen_contracts = f2.multiselect("Contract", contracts, default=contracts)
active_only = f3.toggle("Only customers who have not left", value=True,
                        help="Customers with Churn = No: the ones a retention offer could still keep.")
search = f4.text_input("Customer ID contains", "")

view = scores[scores["risk_label"].isin(chosen_labels) & scores["contract_type"].isin(chosen_contracts)]
if active_only:
    view = view[view["actual_churn"] == "No"]
if search.strip():
    view = view[view["customer_id"].str.contains(search.strip(), case=False, regex=False)]

m1, m2, m3 = st.columns(3)
m1.metric("Customers in this list", f"{len(view):,}", border=True)
m2.metric("Their monthly revenue", common.money(view["monthly_charges"].sum()), border=True,
          help="Sum of the monthly charges of the customers in the list: the revenue at risk.")
m3.metric("Average churn probability", f"{view['churn_probability'].mean():.1%}" if len(view) else "-",
          border=True)

table = view.assign(probability_pct=view["churn_probability"] * 100)[[
    "risk_rank", "customer_id", "probability_pct", "risk_label", "actual_churn", "contract_type",
    "payment_method", "internet_service", "tech_support", "tenure", "monthly_charges"]]
st.dataframe(table, hide_index=True, width="stretch", height=460, column_config={
    "risk_rank": st.column_config.NumberColumn("Rank", format="%d", width="small",
                                               help="Position among ALL customers; 1 = highest churn probability"),
    "customer_id": st.column_config.TextColumn("Customer ID"),
    "probability_pct": st.column_config.ProgressColumn("Churn probability", min_value=0, max_value=100,
                                                       format="%.1f%%"),
    "risk_label": st.column_config.TextColumn("Risk", width="small"),
    "actual_churn": st.column_config.TextColumn("Left?", width="small"),
    "contract_type": st.column_config.TextColumn("Contract"),
    "payment_method": st.column_config.TextColumn("Payment method"),
    "internet_service": st.column_config.TextColumn("Internet", width="small"),
    "tech_support": st.column_config.TextColumn("Tech support", width="small"),
    "tenure": st.column_config.NumberColumn("Tenure (months)", format="%d", width="small"),
    "monthly_charges": st.column_config.NumberColumn("Monthly charge", format="%.2f", width="small"),
})
st.download_button("Download this list (CSV)", view.drop(columns=["scored_at"]).to_csv(index=False).encode("utf-8"),
                   file_name=f"at_risk_customers_{version}.csv", mime="text/csv", icon=":material/download:")

st.caption("Scores are recalculated by the monthly job (model/score.py). The model was trained on 80% of these "
           "customers, so their scores are more confident than scores for brand-new customers would be. "
           + common.CAVEAT)
