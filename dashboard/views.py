"""
views.py - the dashboard pages. Each function draws one page.

All data comes from the olap schema (materialized views and mining results)
through the read-only role churn_dashboard; see db.py.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import streamlit as st
from sklearn.metrics import roc_curve

import charts
import db

ALL = {
    "contract_type": "All contracts",
    "payment_method": "All payment methods",
    "internet_service": "All internet types",
    "tenure_band": "All tenure bands",
}
DIM_LABELS = {
    "contract_type": "Contract",
    "payment_method": "Payment method",
    "internet_service": "Internet service",
    "tenure_band": "Tenure band",
}
CAVEAT = ("Figures are **associations** in the IBM Telco public sample (7,043 customers, one point in time). "
          "They show where churn is concentrated, not why customers leave.")


# =============================================================================
# Small helpers
# =============================================================================
def money(value: float) -> str:
    return f"{value:,.0f}"


def page_header(title: str, subtitle: str) -> None:
    st.title(title)
    st.caption(subtitle)


def kpi() -> pd.Series:
    return db.query("SELECT * FROM olap.mv_kpi_overview").iloc[0]


def cube() -> pd.DataFrame:
    return db.query("SELECT * FROM olap.mv_churn_cube")


def cube_cell(c: pd.DataFrame, **chosen) -> pd.Series:
    """The single cube row for the chosen values (unchosen dimensions = 'All ...')."""
    mask = pd.Series(True, index=c.index)
    for dim, all_label in ALL.items():
        mask &= c[dim] == chosen.get(dim, all_label)
    return c[mask].iloc[0]


def cube_breakdown(c: pd.DataFrame, by: str, **chosen) -> pd.DataFrame:
    """Cube rows that show every value of `by`, with the other dimensions as chosen."""
    mask = c[by] != ALL[by]
    for dim, all_label in ALL.items():
        if dim != by:
            mask &= c[dim] == chosen.get(dim, all_label)
    return c[mask].copy()


def tenure_band_order() -> list[str]:
    return ["0-12 months", "13-24 months", "25-48 months", "49-72 months"]


def local_times(df: pd.DataFrame) -> pd.DataFrame:
    """Show time-zone-aware timestamps in this computer's local time, without the offset."""
    df = df.copy()
    for col in df.columns:
        if isinstance(df[col].dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.tz_convert(datetime.now().astimezone().tzinfo).dt.strftime("%Y-%m-%d %H:%M:%S")
    return df


def show(chart, **kwargs):
    return st.altair_chart(chart, theme=None, width="stretch", **kwargs)


# =============================================================================
# 1. Overview
# =============================================================================
def overview() -> None:
    k = kpi()
    page_header("Customer churn overview",
                f"IBM Telco sample · {int(k.customers):,} customers · reporting layer refreshed "
                f"{pd.Timestamp(k.refreshed_at).strftime('%d %b %Y, %H:%M')}")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Customers", f"{int(k.customers):,}", border=True)
    c2.metric("Churn rate", f"{k.churn_rate_pct:.2f}%", f"{int(k.churned):,} customers left",
              delta_color="off", delta_arrow="off", border=True)
    c3.metric("Monthly revenue", money(k.monthly_revenue), border=True,
              help="Sum of current monthly charges (currency units of the data set).")
    c4.metric("Monthly revenue lost to churn", money(k.monthly_revenue_lost),
              f"{k.monthly_revenue_lost_pct:.1f}% of monthly revenue", delta_color="off",
              delta_arrow="off", border=True)
    c5.metric("Average tenure", f"{k.avg_tenure_months:.1f} months", border=True)

    c = cube()
    avg = float(k.churn_rate_pct)
    st.caption(f"Bars show the churn rate of each group; the thin grey line marks all customers ({avg:.2f}%). "
               "Hover a bar for counts.")
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Churn rate by contract type")
        show(charts.rate_bars(cube_breakdown(c, "contract_type"), "contract_type", "Contract", avg,
                              order=["Month-to-month", "One year", "Two year"], height=190))
        st.subheader("Churn rate by internet service")
        show(charts.rate_bars(cube_breakdown(c, "internet_service"), "internet_service", "Internet service", avg,
                              height=190))
    with right:
        st.subheader("Churn rate by payment method")
        show(charts.rate_bars(cube_breakdown(c, "payment_method"), "payment_method", "Payment method", avg,
                              height=190))
        st.subheader("Churn rate by tenure band")
        show(charts.rate_bars(cube_breakdown(c, "tenure_band"), "tenure_band", "Tenure band", avg,
                              order=tenure_band_order(), height=190))

    m2m = cube_cell(c, contract_type="Month-to-month")
    echeck = cube_cell(c, payment_method="Electronic check")
    new = cube_cell(c, tenure_band="0-12 months")
    both = cube_cell(c, contract_type="Month-to-month", payment_method="Electronic check")
    st.subheader("Key findings")
    st.markdown(
        f"- **Month-to-month contracts:** {int(m2m.churned):,} of {int(m2m.customers):,} customers churned "
        f"(**{m2m.churn_rate_pct:.2f}%**), against {cube_cell(c, contract_type='Two year').churn_rate_pct:.2f}% "
        f"on two-year contracts.\n"
        f"- **First year (0-12 months):** {int(new.churned):,} of {int(new.customers):,} churned "
        f"(**{new.churn_rate_pct:.2f}%**) - early tenure is the riskiest period.\n"
        f"- **Electronic check:** {int(echeck.churned):,} of {int(echeck.customers):,} churned "
        f"(**{echeck.churn_rate_pct:.2f}%**).\n"
        f"- **Month-to-month + electronic check:** {int(both.churned):,} of {int(both.customers):,} churned "
        f"(**{both.churn_rate_pct:.2f}%**)."
    )
    st.info(CAVEAT, icon=":material/info:")


# =============================================================================
# 2. OLAP explorer: slice, dice, roll-up and pivot on the data cube
# =============================================================================
def olap_explorer() -> None:
    page_header("Slice & dice explorer",
                "Every combination of contract, payment method, internet service and tenure band "
                "is pre-computed in the materialized view olap.mv_churn_cube (387 rows).")
    c = cube()
    k = kpi()

    cols = st.columns(4)
    chosen = {}
    for col, dim in zip(cols, ALL):
        values = c.loc[c[dim] != ALL[dim], dim].unique().tolist()
        values = tenure_band_order() if dim == "tenure_band" else sorted(values)
        chosen[dim] = col.selectbox(DIM_LABELS[dim], [ALL[dim], *values], key=f"olap_{dim}")

    fixed = [DIM_LABELS[d] for d, v in chosen.items() if v != ALL[d]]
    operation = ("Roll-up to all customers" if not fixed else
                 f"Slice on {fixed[0]}" if len(fixed) == 1 else f"Dice on {', '.join(fixed)}")
    cell = cube_cell(c, **chosen)

    st.markdown(f"**Operation:** {operation}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Customers in selection", f"{int(cell.customers):,}",
              f"{100 * cell.customers / k.customers:.1f}% of all customers", delta_color="off",
              delta_arrow="off", border=True)
    m2.metric("Churned", f"{int(cell.churned):,}", border=True)
    gap = float(cell.churn_rate_pct - k.churn_rate_pct)
    m3.metric("Churn rate", f"{cell.churn_rate_pct:.2f}%",
              f"{gap:+.2f} points vs all customers" if fixed else None,
              delta_color="inverse", border=True)          # red = worse (higher churn) than average
    m4.metric("Monthly revenue lost", money(cell.monthly_revenue_lost),
              f"{100 * cell.monthly_revenue_lost / k.monthly_revenue_lost:.1f}% of all revenue lost",
              delta_color="off", delta_arrow="off", border=True)

    st.divider()
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        by = st.radio("Break the selection down by", list(ALL), index=1, horizontal=True,
                      format_func=lambda d: DIM_LABELS[d], key="olap_breakdown")
        rows = cube_breakdown(c, by, **chosen)
        st.caption(f"Churn rate per {DIM_LABELS[by].lower()} for the other selections "
                   "(the highlighted bar is your current choice; the grey line is the company average).")
        show(charts.rate_bars(rows, by, DIM_LABELS[by], float(k.churn_rate_pct),
                              order=tenure_band_order() if by == "tenure_band" else None,
                              highlight=None if chosen[by] == ALL[by] else chosen[by],
                              extra_tooltip=[charts.alt.Tooltip("monthly_revenue_lost:Q",
                                                                title="Revenue lost", format=",.0f")]))
    with right:
        st.markdown("**Pivot: contract type × payment method** (churn rate %)")
        pivot = c[(c.contract_type != ALL["contract_type"]) & (c.payment_method != ALL["payment_method"])
                  & (c.internet_service == chosen["internet_service"]) & (c.tenure_band == chosen["tenure_band"])]
        st.caption(f"For internet = {chosen['internet_service']}, tenure = {chosen['tenure_band']}. "
                   "Hover a cell to see how many customers it holds.")
        show(charts.heatmap(pivot.assign(payment_method=pivot.payment_method.str.replace(" (automatic)", "", regex=False)),
                            "contract_type", "payment_method", "Contract", "Payment method"))

    with st.expander("Table view of the breakdown"):
        st.dataframe(rows[[by, "customers", "churned", "churn_rate_pct", "monthly_revenue", "monthly_revenue_lost"]]
                     .rename(columns={by: DIM_LABELS[by]}), hide_index=True)
    st.caption(CAVEAT)


# =============================================================================
# 3. Tenure drill-down (click a bar to go one level deeper)
# =============================================================================
def _clicked(event, param: str, field: str) -> str | None:
    try:
        points = event["selection"][param]
        if points:
            return points[0][field]
    except (KeyError, IndexError, TypeError):
        pass
    return None


def tenure_drilldown() -> None:
    page_header("Tenure drill-down",
                "Hierarchy of dim_tenure: tenure group → tenure band → tenure month "
                "(tenure replaces a calendar time dimension, which the source data does not have).")
    dd = db.query("SELECT * FROM olap.mv_tenure_drilldown")
    avg = float(dd.loc[dd.drill_level == "total", "churn_rate_pct"].iloc[0])

    st.subheader("Level 1 · tenure group")
    groups = dd[dd.drill_level == "group"].sort_values("tenure_group_order")
    selected_group = st.session_state.get("drill_group_value", groups.iloc[0].tenure_group)
    event = show(charts.click_rate_bars(groups, "tenure_group", "Tenure group", "pick_group",
                                        selected_group, order=groups.tenure_group.tolist()),
                 key="drill_group", on_select="rerun", selection_mode="pick_group")
    selected_group = _clicked(event, "pick_group", "tenure_group") or selected_group
    st.session_state["drill_group_value"] = selected_group
    st.caption(f"Click a bar to drill down. Company average churn rate: {avg:.2f}%.")

    st.subheader(f"Level 2 · tenure bands in {selected_group}")
    bands = dd[(dd.drill_level == "band") & (dd.tenure_group == selected_group)].sort_values("tenure_band_order")
    default_band = st.session_state.get("drill_band_value")
    if default_band not in bands.tenure_band.tolist():
        default_band = bands.iloc[0].tenure_band
    event = show(charts.click_rate_bars(bands, "tenure_band", "Tenure band", "pick_band", default_band,
                                        order=bands.tenure_band.tolist()),
                 key=f"drill_band_{selected_group}", on_select="rerun", selection_mode="pick_band")
    selected_band = _clicked(event, "pick_band", "tenure_band") or default_band
    st.session_state["drill_band_value"] = selected_band

    st.subheader(f"Level 3 · individual months in {selected_band}")
    months = dd[(dd.drill_level == "month") & (dd.tenure_band == selected_band)].sort_values("tenure_months")
    show(charts.month_line(months, avg))
    worst = months.loc[months.churn_rate_pct.idxmax()]
    st.markdown(f"Highest churn in this band: **month {int(worst.tenure_months)}** — "
                f"{int(worst.churned):,} of {int(worst.customers):,} customers "
                f"(**{worst.churn_rate_pct:.2f}%**).")
    with st.expander("Table view"):
        st.dataframe(months[["tenure_months", "customers", "churned", "churn_rate_pct", "avg_monthly_charge"]],
                     hide_index=True)
    st.caption(CAVEAT)


# =============================================================================
# 4. Revenue at risk (ranking)
# =============================================================================
def revenue_risk() -> None:
    page_header("Revenue at risk",
                "Contract × payment method × internet service segments ranked by the monthly revenue "
                "lost to churn (RANK() OVER, materialized view olap.mv_segment_revenue_risk).")
    seg = db.query("SELECT * FROM olap.mv_segment_revenue_risk ORDER BY rank_no")
    seg["segment"] = (seg.contract_type + " · " + seg.payment_method.str.replace(" (automatic)", "", regex=False)
                      + " · " + seg.internet_service.replace({"No": "No internet"}))
    top_n = st.slider("Number of segments", 5, len(seg), 10, key="risk_top_n")
    top = seg.head(top_n)
    share = float(top.share_of_lost_pct.sum())
    c1, c2, c3 = st.columns(3)
    c1.metric(f"Top {top_n} segments: share of revenue lost", f"{share:.1f}%", border=True)
    c2.metric("Their customers", f"{int(top.customers.sum()):,}",
              f"{100 * top.customers.sum() / seg.customers.sum():.1f}% of all customers",
              delta_color="off", delta_arrow="off", border=True)
    c3.metric("Monthly revenue they lost", money(top.monthly_revenue_lost.sum()), border=True)
    show(charts.ranking_bars(top))
    st.dataframe(
        top[["rank_no", "segment", "customers", "churned", "churn_rate_pct", "monthly_revenue_lost",
             "share_of_lost_pct", "cumulative_share_pct"]],
        hide_index=True,
        column_config={
            "rank_no": st.column_config.NumberColumn("Rank"),
            "segment": st.column_config.TextColumn("Segment", width="large"),
            "customers": st.column_config.NumberColumn("Customers", format="%d"),
            "churned": st.column_config.NumberColumn("Churned", format="%d"),
            "churn_rate_pct": st.column_config.NumberColumn("Churn %", format="%.2f"),
            "monthly_revenue_lost": st.column_config.NumberColumn("Revenue lost", format="%.2f"),
            "share_of_lost_pct": st.column_config.ProgressColumn("Share of lost %", min_value=0, max_value=100,
                                                                 format="%.2f"),
            "cumulative_share_pct": st.column_config.ProgressColumn("Cumulative %", min_value=0, max_value=100,
                                                                    format="%.2f"),
        },
    )
    st.caption(CAVEAT)


# =============================================================================
# 5. Add-on services
# =============================================================================
def services() -> None:
    page_header("Add-on services and churn",
                "Churn rate of customers with and without each internet add-on (olap.mv_addon_churn).")
    a = db.query("SELECT * FROM olap.mv_addon_churn")
    wide = a.pivot(index="addon", columns="status", values="churn_rate_pct").reset_index()
    wide = wide.rename(columns={"Yes": "With the add-on", "No": "Without the add-on",
                                "No internet service": "No internet at all"})
    wide["gap"] = wide["Without the add-on"] - wide["With the add-on"]
    show(charts.dumbbell(wide))
    best = wide.sort_values("gap", ascending=False).iloc[0]
    st.markdown(
        f"Customers **without {best.addon.lower()}** churn at **{best['Without the add-on']:.2f}%**, against "
        f"{best['With the add-on']:.2f}% with it ({best.gap:.1f} percentage points). Security and support "
        "add-ons show the largest gaps; streaming shows almost none. Customers with no internet churn at "
        f"{wide['No internet at all'].iloc[0]:.2f}%.")
    table = wide.sort_values("gap", ascending=False)[
        ["addon", "With the add-on", "Without the add-on", "gap", "No internet at all"]]
    st.dataframe(table, hide_index=True, column_config={
        "addon": "Add-on service",
        "With the add-on": st.column_config.NumberColumn("Churn % with", format="%.2f"),
        "Without the add-on": st.column_config.NumberColumn("Churn % without", format="%.2f"),
        "gap": st.column_config.NumberColumn("Gap (points)", format="%.2f"),
        "No internet at all": st.column_config.NumberColumn("Churn % no internet", format="%.2f")})
    st.info("This supports investigating support and security needs in high-risk groups. It is an association: "
            "customers who buy these services may also differ in other ways.", icon=":material/info:")


# =============================================================================
# 6. Model results
# =============================================================================
def models() -> None:
    run_id = db.latest_run_id()
    if run_id is None:
        page_header("Model results", "No mining run found.")
        st.warning("Run  python mining/churn_mining.py  to train the models and store their results.")
        return
    run = db.query("SELECT * FROM olap.mining_run WHERE run_id = :r", {"r": run_id}).iloc[0]
    page_header("Churn prediction models",
                f"Mining run {run_id} · {pd.Timestamp(run.run_at).strftime('%d %b %Y, %H:%M')} · "
                f"{run.train_rows:,} training / {run.test_rows:,} test customers · scikit-learn {run.sklearn_version}")
    st.info("MuhammaD_Wallah and the GROUP are the owners and Developers of this model.",
            icon=":material/verified:")
    metrics = db.query("SELECT * FROM olap.model_metrics WHERE run_id = :r", {"r": run_id})
    order = list(charts.MODEL_COLORS)
    metrics["order"] = metrics.model_name.map(order.index)
    metrics = metrics.sort_values("order")

    tab1, tab2, tab3 = st.tabs(["Model comparison", "ROC curves & decision threshold", "Feature importance"])

    with tab1:
        long = metrics.melt(id_vars="model_name",
                            value_vars=["accuracy", "precision_score", "recall_score", "f1_score", "roc_auc"],
                            var_name="metric", value_name="value").rename(columns={"model_name": "model"})
        long["metric"] = long.metric.map({"accuracy": "Accuracy", "precision_score": "Precision",
                                          "recall_score": "Recall", "f1_score": "F1-score", "roc_auc": "ROC-AUC"})
        long["value"] = long.value.astype(float)
        show(charts.model_dots(long))
        table = metrics[["model_name", "accuracy", "precision_score", "recall_score", "f1_score", "roc_auc",
                         "tn", "fp", "fn", "tp", "cv_roc_auc_mean", "cv_roc_auc_std", "hyperparameters"]]
        st.dataframe(table, hide_index=True, column_config={
            "model_name": "Model", "accuracy": "Accuracy", "precision_score": "Precision", "recall_score": "Recall",
            "tn": "TN", "fp": "FP", "fn": "FN", "tp": "TP",
            "f1_score": "F1", "roc_auc": "ROC-AUC (test)", "cv_roc_auc_mean": "ROC-AUC (5-fold CV mean)",
            "cv_roc_auc_std": "CV std", "hyperparameters": st.column_config.TextColumn("Settings", width="large")})
        st.markdown(
            "**Reading the results.** Random Forest (with class weighting) catches the most churners "
            "(highest **recall**) but flags more customers who would have stayed. Gradient Boosting and "
            "Logistic Regression are more **precise** but miss about half of the churners. Their ROC-AUC "
            "values differ by less than the variation between cross-validation folds, so the choice "
            "depends on business costs: favour recall when a missed churner is expensive, precision when "
            "retention offers are expensive.")

    preds = db.query("SELECT model_name, actual_churn, churn_probability::float AS p "
                     "FROM olap.model_test_prediction WHERE run_id = :r", {"r": run_id})
    with tab2:
        roc_parts = []
        for name in order:
            part = preds[preds.model_name == name]
            fpr, tpr, _ = roc_curve(part.actual_churn, part.p)
            auc = float(metrics.loc[metrics.model_name == name, "roc_auc"].iloc[0])
            roc_parts.append(pd.DataFrame({"fpr": fpr, "tpr": tpr, "model": name,
                                           "label": f"{name} (AUC {auc:.3f})"}))
        left, right = st.columns([1, 1.05], gap="large")
        with left:
            st.markdown("**ROC curves (test set)**")
            show(charts.roc_lines(pd.concat(roc_parts)))
        with right:
            st.markdown("**Try a decision threshold**")
            name = st.selectbox("Model", order, index=order.index("Gradient Boosting"), key="thr_model")
            threshold = st.slider("Predict churn when probability ≥", 0.05, 0.95, 0.50, 0.05, key="thr_value")
            k1, k2 = st.columns(2)
            cost_fn = k1.number_input("Cost of a missed churner", 0.0, 1000.0, 5.0, 1.0, key="thr_cost_fn")
            cost_fp = k2.number_input("Cost of a wasted offer", 0.0, 1000.0, 1.0, 1.0, key="thr_cost_fp")
            part = preds[preds.model_name == name]
            y, p = part.actual_churn.to_numpy(), part.p.to_numpy()
            pred = (p >= threshold).astype(int)
            tp = int(((pred == 1) & (y == 1)).sum()); fp = int(((pred == 1) & (y == 0)).sum())
            fn = int(((pred == 0) & (y == 1)).sum()); tn = int(((pred == 0) & (y == 0)).sum())
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / (tp + fn) if tp + fn else 0.0
            show(charts.confusion(tn, fp, fn, tp))
            s1, s2, s3 = st.columns(3)
            s1.metric("Recall", f"{recall:.3f}", border=True)
            s2.metric("Precision", f"{precision:.3f}", border=True)
            s3.metric("Cost of errors", f"{cost_fn * fn + cost_fp * fp:,.0f}", border=True)
            grid = np.round(np.arange(0.05, 0.951, 0.05), 2)
            curve = pd.DataFrame({"threshold": grid,
                                  "missed": [int(((p < t) & (y == 1)).sum()) for t in grid],
                                  "wasted": [int(((p >= t) & (y == 0)).sum()) for t in grid]})
            curve["cost"] = cost_fn * curve.missed + cost_fp * curve.wasted
            best = curve.loc[curve.cost.idxmin()]
            st.markdown(f"Lowest cost on the test set at threshold **{best.threshold:.2f}** "
                        f"({int(best.missed)} missed churners, {int(best.wasted)} wasted offers).")
            show(charts.cost_curve(curve, threshold))
        st.caption("For teaching only: a real threshold must be chosen on training or validation data "
                   "(as churn_mining.py does), then confirmed on the test set.")

    with tab3:
        imp = db.query("SELECT method, feature, importance::float AS importance, "
                       "importance_std::float AS importance_std, rank_no "
                       "FROM olap.model_feature_importance WHERE run_id = :r", {"r": run_id})
        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("**Random Forest – impurity importance** (top 15 one-hot features)")
            show(charts.importance_bars(imp[imp.method == "impurity"].nsmallest(15, "rank_no"),
                                        "Mean decrease in impurity"))
        with right:
            st.markdown("**Random Forest – permutation importance** (original columns)")
            show(charts.importance_bars(imp[imp.method == "permutation"], "Mean decrease in test ROC-AUC",
                                        with_error=True))
        st.info("Importance shows what the model relies on, not what causes churn. Correlated features "
                "(e.g. tenure and TotalCharges) share importance.", icon=":material/info:")


# =============================================================================
# 7. At-risk customers
# =============================================================================
def at_risk() -> None:
    run_id = db.latest_run_id()
    if run_id is None:
        page_header("At-risk customers", "No mining run found.")
        st.warning("Run  python mining/churn_mining.py  first.")
        return
    page_header("At-risk customers",
                "Customers who have NOT churned, ranked by their predicted churn probability "
                "(out-of-fold scores: each customer was scored by a model that did not see them).")
    scores = db.query("""
        SELECT s.customer_id, s.churn_probability::float AS churn_probability, s.risk_band,
               v.contract_type, v.payment_method, v.internet_service, v.tenure,
               v.monthly_charges::float AS monthly_charges,
               v.online_security::text AS online_security, v.tech_support::text AS tech_support
        FROM   olap.customer_churn_score AS s
        JOIN   olap.v_customer_churn     AS v USING (customer_id)
        WHERE  s.run_id = :r AND s.actual_churn = 0
        ORDER  BY s.churn_probability DESC
    """, {"r": run_id})

    f1, f2 = st.columns(2)
    bands = f1.pills("Risk band", ["High", "Medium", "Low"], selection_mode="multi", default=["High"],
                     key="risk_bands") or ["High"]
    contracts = f2.multiselect("Contract", sorted(scores.contract_type.unique()), key="risk_contracts")
    view = scores[scores.risk_band.isin(bands)]
    if contracts:
        view = view[view.contract_type.isin(contracts)]

    c1, c2, c3 = st.columns(3)
    c1.metric("Customers in the list", f"{len(view):,}", border=True)
    c2.metric("Their monthly revenue", money(view.monthly_charges.sum()), border=True)
    c3.metric("Average predicted churn probability",
              f"{view.churn_probability.mean():.2f}" if len(view) else "–", border=True)

    st.dataframe(view, hide_index=True, height=440, column_config={
        "customer_id": "Customer", "churn_probability": st.column_config.ProgressColumn(
            "Churn probability", min_value=0.0, max_value=1.0, format="%.2f"),
        "risk_band": "Band", "contract_type": "Contract", "payment_method": "Payment",
        "internet_service": "Internet", "tenure": st.column_config.NumberColumn("Tenure (months)"),
        "monthly_charges": st.column_config.NumberColumn("Monthly charge", format="%.2f"),
        "online_security": "Online security", "tech_support": "Tech support"})
    st.download_button("Download this list (CSV)", view.to_csv(index=False).encode("utf-8"),
                       file_name="at_risk_customers.csv", mime="text/csv", icon=":material/download:")
    st.info("Use this list to design **controlled retention experiments** (offer vs no offer, randomly "
            "assigned), not for automatic decisions. A real deployment needs authorised data, privacy "
            "safeguards and a check for unfair bias between customer groups.", icon=":material/info:")


# =============================================================================
# 8. Insights & recommendations (explanation written by mining/insights.py)
# =============================================================================
def insights() -> None:
    run_id = db.latest_run_id()
    ready = bool(db.query("SELECT to_regclass('olap.mining_insight') IS NOT NULL "
                          "AND to_regclass('olap.retention_solution') IS NOT NULL AS ok").iloc[0]["ok"])
    sections = (db.query("SELECT section_no, section_title, body_markdown FROM olap.mining_insight "
                         "WHERE run_id = :r ORDER BY section_no", {"r": run_id})
                if ready and run_id is not None else pd.DataFrame())
    if sections.empty:
        page_header("Insights & recommendations", "No explanation found for the latest mining run.")
        st.warning("Run  sql/08_mining_objects.sql  once (it adds the tables olap.mining_insight and "
                   "olap.retention_solution), then run  python mining/churn_mining.py  again.")
        return
    page_header("Insights & recommendations",
                f"Mining run {run_id} · plain-English explanation built by rules from this run's numbers "
                "(mining/insights.py) · what-if effects estimated by the scoring model, out-of-fold")
    sol = db.query("SELECT * FROM olap.retention_solution WHERE run_id = :r", {"r": run_id})
    for col in ["expected_churners_before", "expected_churners_after", "churners_prevented",
                "prevented_per_100", "revenue_protected", "net_revenue_change"]:
        sol[col] = sol[col].astype(float)

    rec = sol[sol.is_recommended]
    if len(rec):
        r = rec.iloc[0]
        st.success(f"**Recommended solution: {r.solution_name}.** {r.action}", icon=":material/lightbulb:")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active customers targeted", f"{int(r.target_customers):,}",
                  f"{int(r.high_risk_customers):,} in the High risk band", delta_color="off",
                  delta_arrow="off", border=True)
        c2.metric("Expected churners prevented", f"{r.churners_prevented:,.0f}",
                  f"{r.churners_prevented / r.expected_churners_before:.0%} fewer among the targets",
                  delta_color="off", delta_arrow="off", border=True)
        c3.metric("Monthly revenue protected", money(r.revenue_protected), border=True,
                  help="Monthly bills of the customers the model expects to keep (before the cost of the offer).")
        c4.metric("Prevented per 100 contacted", f"{r.prevented_per_100:.1f}", border=True)

    why_customers_leave(run_id)

    sim = sol[sol.simulated].sort_values("rank_no").copy()
    sim["label"] = np.where(sim.is_recommended, sim.solution_name + " (recommended)", sim.solution_name)
    st.subheader("Possible solutions compared")
    st.caption("Expected churners = sum of the predicted churn probabilities of the targeted active customers. "
               "The what-if assumes every targeted customer accepts; target groups overlap, so effects do not add up.")
    show(charts.solution_bars(sim))
    shared = {
        "solution_name": st.column_config.TextColumn("Solution", width="medium"),
        "target_rule": st.column_config.TextColumn("Who is targeted", width="medium"),
        "target_customers": st.column_config.NumberColumn("Targeted", format="%d"),
        "expected_churners_before": st.column_config.NumberColumn("Expected churners", format="%.0f"),
        "reasons": st.column_config.TextColumn("Why (not) chosen", width="large"),
    }
    # net_revenue_change is only known for the discount; its value is quoted in that row's reasons
    st.dataframe(sim[["rank_no", "solution_name", "target_rule", "target_customers", "expected_churners_before",
                      "expected_churners_after", "churners_prevented", "prevented_per_100", "revenue_protected",
                      "reasons"]], hide_index=True, column_config={
        **shared,
        "rank_no": st.column_config.NumberColumn("Rank"),
        "expected_churners_after": st.column_config.NumberColumn("After the action", format="%.0f"),
        "churners_prevented": st.column_config.NumberColumn("Prevented", format="%.0f"),
        "prevented_per_100": st.column_config.NumberColumn("Per 100 contacted", format="%.1f"),
        "revenue_protected": st.column_config.NumberColumn("Revenue protected / month", format="%.0f",
                                                           help="Monthly bills of the customers the model expects "
                                                                "to keep, before the cost of the offer"),
    })
    support = sol[~sol.simulated]
    if len(support):
        st.markdown("**Supporting actions** (the model cannot simulate their effect)")
        st.dataframe(support[["solution_name", "target_rule", "target_customers", "high_risk_customers",
                              "expected_churners_before", "reasons"]], hide_index=True, column_config={
            **shared, "high_risk_customers": st.column_config.NumberColumn("In the High band", format="%d")})

    st.subheader("Full explanation")
    for row in sections.itertuples():
        expanded = row.section_title.startswith(("In short", "Why customers leave", "Recommended"))
        with st.expander(row.section_title, expanded=expanded):
            st.markdown(row.body_markdown)
    report = "\n\n".join(f"## {row.section_no}. {row.section_title}\n\n{row.body_markdown}"
                         for row in sections.itertuples())
    st.download_button("Download the explanation (Markdown)", report.encode("utf-8"),
                       file_name=f"churn_insights_run_{run_id}.md", mime="text/markdown",
                       icon=":material/download:")
    st.info("The recommendation is a model-based starting point for a **controlled pilot** (offer vs no offer, "
            "randomly assigned), not proof of what an offer would achieve. " + CAVEAT + " The section **Why "
            "customers leave** gives the most likely reasons that the data and the model support, and how to "
            "confirm each one.", icon=":material/info:")


def why_customers_leave(run_id: int) -> None:
    """The reason-code summary (olap.churn_reason) on the Insights page."""
    st.subheader("Why customers leave")
    st.caption("The IBM sample records THAT a customer left, not WHY, so these reasons are inferred. For every "
               "customer the model measured how much each factor raises the churn probability compared with typical "
               "values of that factor; a factor that adds at least 5 points is one of the customer's reasons.")
    ready = bool(db.query("SELECT to_regclass('olap.churn_reason') IS NOT NULL AS ok").iloc[0]["ok"])
    reasons = (db.query("SELECT * FROM olap.churn_reason WHERE run_id = :r", {"r": run_id})
               if ready else pd.DataFrame())
    if reasons.empty:
        st.info("Run  sql/08_mining_objects.sql  once more (it adds olap.churn_reason), then  python "
                "mining/churn_mining.py. Until then the reasons are only in the full explanation below.",
                icon=":material/info:")
        return
    for col in ["churners_main_pct", "churners_contributing_pct", "avg_points_churners", "at_risk_main_pct",
                "at_risk_contributing_pct", "data_gap_ratio"]:
        reasons[col] = reasons[col].astype(float)
    factors = reasons[reasons.reason_key != "none"].sort_values("churners_contributing_pct", ascending=False)
    none = reasons[reasons.reason_key == "none"]
    top = factors.iloc[0]
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        show(charts.reason_bars(factors))
        if len(none):
            st.caption(f"{none.iloc[0].churners_main_pct:.0f}% of the customers who left have no single clear driver "
                       "(no factor adds 5 points or more).")
    with right:
        st.markdown(f"**Most common reason: {top.reason_name}** - a reason for {top.churners_contributing_pct:.0f}% "
                    f"of the customers who left (the main reason for {top.churners_main_pct:.0f}%).")
        st.markdown(f"**Likely explanation** (interpretation, not measured): {top.likely_explanation}")
        st.markdown(f"**How to confirm:** {top.how_to_confirm[0].upper() + top.how_to_confirm[1:]}")
    st.dataframe(factors[["reason_name", "churners_contributing_pct", "churners_main_pct", "at_risk_contributing_pct",
                          "evidence_strength", "likely_explanation", "how_to_confirm", "linked_solution"]],
                 hide_index=True, column_config={
                     "reason_name": st.column_config.TextColumn("Reason", width="medium"),
                     "churners_contributing_pct": st.column_config.ProgressColumn(
                         "A reason for (% of leavers)", min_value=0, max_value=100, format="%.0f%%"),
                     "churners_main_pct": st.column_config.NumberColumn("Main reason for (%)", format="%.1f"),
                     "at_risk_contributing_pct": st.column_config.NumberColumn(
                         "A reason for (% of active High-risk)", format="%.0f",
                         help="Customers who have not left yet but are in the High risk band"),
                     "evidence_strength": st.column_config.TextColumn(
                         "Evidence", help="Strong = churn-rate gap of at least 2x in the data AND a reason for at "
                                          "least 25% of leavers; Moderate = one of the two; Weak = neither"),
                     "likely_explanation": st.column_config.TextColumn("Likely explanation", width="large"),
                     "how_to_confirm": st.column_config.TextColumn("How to confirm", width="large"),
                     "linked_solution": st.column_config.TextColumn("Linked solution", width="medium"),
                 })


# =============================================================================
# 9. Data quality
# =============================================================================
def data_quality() -> None:
    page_header("Data quality and ETL",
                "The 41 warehouse checks of sql/05_validation_checks.sql, run live through app.data_quality().")
    dq = db.fresh("SELECT * FROM app.data_quality()")
    failed = int((dq.result != "PASS").sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Checks", f"{len(dq)}", border=True)
    c2.metric("Passed", f"{len(dq) - failed}", border=True)
    c3.metric("Failed", f"{failed}", border=True)
    if failed:
        st.error(f"{failed} check(s) failed - see the rows marked FAIL.", icon=":material/error:")
    else:
        st.success("All warehouse checks passed.", icon=":material/check_circle:")
    dq["status"] = np.where(dq.result == "PASS", "✅ PASS", "❌ FAIL")
    st.dataframe(dq[["check_no", "check_group", "check_name", "expected", "actual", "status"]],
                 hide_index=True, height=520,
                 column_config={"check_no": "No.", "check_group": "Group", "check_name": st.column_config.TextColumn(
                     "Check", width="large"), "expected": "Expected", "actual": "Actual", "status": "Result"})
    st.subheader("ETL runs (dw.etl_run_log)")
    st.dataframe(local_times(db.fresh("SELECT * FROM app.etl_runs(20)")), hide_index=True, column_config={
        "run_id": "Run", "status": "Status", "started_at": "Started", "seconds": "Seconds",
        "staging_rows": "Staging rows", "error_message": "Error"})


# =============================================================================
# 10. Administration (admin role only)
# =============================================================================
def admin() -> None:
    if st.session_state.get("user", {}).get("app_role") != "admin":
        st.error("This page is only available to administrators.")
        st.stop()
    page_header("Administration", "Dashboard accounts, sign-in history and the reporting layer.")

    st.subheader("Reporting layer")
    st.write("Recompute every materialized view in the olap schema from the warehouse "
             "(REFRESH MATERIALIZED VIEW CONCURRENTLY - other users can keep working).")
    if st.button("Refresh materialized views", icon=":material/refresh:", type="primary"):
        with st.spinner("Refreshing..."):
            timings = db.refresh_reporting()
        st.success(f"Refreshed {len(timings)} materialized views in {timings.seconds.sum():.2f} s.")
        st.dataframe(timings, hide_index=True)

    st.subheader("Accounts")
    st.dataframe(local_times(db.fresh("SELECT * FROM app.list_users()")), hide_index=True, column_config={
        "username": "Username", "full_name": "Name", "user_role": "Role", "is_active": "Active",
        "created_at": "Created", "password_changed_at": "Password changed", "last_login_at": "Last sign-in"})
    st.caption("Accounts and passwords are managed by the database administrator with "
               "sql/09_dashboard_access.sql (or SELECT app.set_user(...)). Passwords are stored as bcrypt hashes.")

    st.subheader("Recent sign-in attempts")
    logins = local_times(db.fresh("SELECT * FROM app.recent_logins(50)"))
    logins["result"] = np.where(logins.success, "✅ success", "❌ failed")
    st.dataframe(logins[["attempted_at", "username", "result", "note"]], hide_index=True, column_config={
        "attempted_at": "Time", "username": "Username", "result": "Result", "note": "Detail"})
