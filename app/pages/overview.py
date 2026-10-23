"""Overview - headline KPIs and churn by the four main dimensions (olap materialized views)."""

from __future__ import annotations

import streamlit as st

import charts
import common

k = common.read("SELECT * FROM olap.mv_kpi_overview").iloc[0]
common.page_header(
    "Customer churn overview",
    f"Live from the cloud warehouse (olap.mv_kpi_overview, olap.mv_churn_cube) · "
    f"reporting layer refreshed {common.utc(k.refreshed_at)}")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Customers", f"{int(k.customers):,}", border=True)
c2.metric("Churn rate", f"{k.churn_rate_pct:.2f}%", f"{int(k.churned):,} customers left",
          delta_color="off", delta_arrow="off", border=True)
c3.metric("Monthly revenue lost to churn", common.money(k.monthly_revenue_lost),
          f"{k.monthly_revenue_lost_pct:.1f}% of monthly revenue", delta_color="off", delta_arrow="off",
          border=True, help=f"Sum of the monthly charges of the customers who left, out of "
                            f"{common.money(k.monthly_revenue)} monthly revenue (currency of the data set).")
c4.metric("Average tenure", f"{k.avg_tenure_months:.1f} months", border=True)

cube = common.cube()
avg = float(k.churn_rate_pct)
st.caption(f"Each bar is the churn rate of one group; the thin grey line marks all customers ({avg:.2f}%). "
           "Hover a bar for the counts.")

for pair in (("contract_type", "payment_method"), ("tenure_band", "internet_service")):
    breakdowns = {dim: common.cube_breakdown(cube, dim) for dim in pair}
    height = max(110, 36 * max(len(b) for b in breakdowns.values()) + 40)   # same height side by side
    for column, dim in zip(st.columns(2, gap="large"), pair):
        with column:
            st.subheader(f"Churn rate by {common.DIM_LABELS[dim].lower()}")
            order = common.member_order(cube, dim) if dim in ("contract_type", "tenure_band") else None
            common.show(charts.rate_bars(breakdowns[dim], dim, common.DIM_LABELS[dim], avg, order, height))

st.caption(common.CAVEAT)
