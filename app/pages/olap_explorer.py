"""OLAP explorer - slice and dice the cube, plus the roll-up, pivot and drill-down views."""

from __future__ import annotations

import streamlit as st

import charts
import common

common.page_header("OLAP explorer",
                   "Slice and dice the churn cube (olap.mv_churn_cube), then read the roll-up, pivot and "
                   "drill-down materialized views.")
cube = common.cube()
k = common.read("SELECT churn_rate_pct FROM olap.mv_kpi_overview").iloc[0]
avg = float(k.churn_rate_pct)

# ----------------------------------------------------------------------------- slice & dice
st.subheader("Slice and dice")
st.caption("**Slice** = fix one dimension (e.g. Contract = Month-to-month). **Dice** = fix two or more. "
           "Leave a filter on 'All ...' to keep every member of that dimension.")
chosen = {}
for column, dim in zip(st.columns(4), common.ALL):
    chosen[dim] = column.selectbox(common.DIM_LABELS[dim], [common.ALL[dim], *common.member_order(cube, dim)],
                                   key=f"filter_{dim}")

cell = common.cube_cell(cube, **chosen)
fixed = [f"{common.DIM_LABELS[d]} = {v}" for d, v in chosen.items() if v != common.ALL[d]]
st.markdown("**Selection:** " + (" · ".join(fixed) if fixed else "all customers"))
m1, m2, m3, m4 = st.columns(4)
m1.metric("Customers", f"{int(cell.customers):,}", border=True)
m2.metric("Churned", f"{int(cell.churned):,}", border=True)
m3.metric("Churn rate", f"{cell.churn_rate_pct:.2f}%",
          f"{cell.churn_rate_pct - avg:+.2f} points vs all customers" if fixed else None,
          delta_color="inverse", border=True)
m4.metric("Monthly revenue lost", common.money(cell.monthly_revenue_lost), border=True)

free = [d for d in common.ALL if chosen[d] == common.ALL[d]]
if free:
    by = st.radio("Break the selection down by", free, format_func=common.DIM_LABELS.get, horizontal=True)
    rows = common.cube_breakdown(cube, by, **chosen)
    order = common.member_order(cube, by) if by in ("contract_type", "tenure_band") else None
    common.show(charts.rate_bars(rows, by, common.DIM_LABELS[by], avg, order))
    st.caption(f"Grey line: churn rate of all customers ({avg:.2f}%).")
    with st.expander("Show the numbers"):
        st.dataframe(rows[[by, "customers", "churned", "churn_rate_pct", "monthly_revenue", "monthly_revenue_lost"]],
                     hide_index=True, width="stretch")
else:
    st.info("All four dimensions are fixed, so the selection is one single cell of the cube.")

# ----------------------------------------------------------------------------- 2-D dice
st.subheader("Dice: contract type × payment method")
st.caption("Churn rate (%) for every contract and payment combination, within the internet-service and "
           "tenure filters above.")
grid = cube[(cube.contract_type != common.ALL["contract_type"])
            & (cube.payment_method != common.ALL["payment_method"])
            & (cube.internet_service == chosen["internet_service"])
            & (cube.tenure_band == chosen["tenure_band"])]
if grid.empty:
    st.info("No customers in this selection.")
else:
    common.show(charts.heatmap(grid, "contract_type", "payment_method", "Contract", "Payment method"))

# ----------------------------------------------------------------------------- roll-up
st.subheader("Roll-up: contract × payment method → contract → all customers")
rollup = common.read("""
    SELECT level, contract_type, payment_method, customers, churned, churn_rate_pct, monthly_revenue_lost
    FROM   olap.mv_churn_rollup
    ORDER  BY contract_type = 'All contracts', contract_type, grouping_id, payment_method
""")
st.dataframe(rollup, hide_index=True, width="stretch", height=35 * (len(rollup) + 1) + 3, column_config={
    "level": st.column_config.TextColumn("Level"),
    "contract_type": st.column_config.TextColumn("Contract"),
    "payment_method": st.column_config.TextColumn("Payment method"),
    "customers": st.column_config.NumberColumn("Customers", format="%d"),
    "churned": st.column_config.NumberColumn("Churned", format="%d"),
    "churn_rate_pct": st.column_config.NumberColumn("Churn %", format="%.2f"),
    "monthly_revenue_lost": st.column_config.NumberColumn("Monthly revenue lost", format="%.2f"),
})

# ----------------------------------------------------------------------------- pivot
st.subheader("Pivot: contract type × churn")
pivot = common.read("""
    SELECT contract_type, churn_no, churn_yes, total, churn_rate_pct
    FROM   olap.mv_contract_churn_pivot
    ORDER  BY is_total, contract_type
""")
st.dataframe(pivot, hide_index=True, width="stretch", column_config={
    "contract_type": st.column_config.TextColumn("Contract"),
    "churn_no": st.column_config.NumberColumn("Churn = No", format="%d"),
    "churn_yes": st.column_config.NumberColumn("Churn = Yes", format="%d"),
    "total": st.column_config.NumberColumn("Total", format="%d"),
    "churn_rate_pct": st.column_config.NumberColumn("Churn %", format="%.2f"),
})

# ----------------------------------------------------------------------------- drill-down
st.subheader("Drill-down: tenure group → tenure band → month")
drill = common.read("SELECT * FROM olap.mv_tenure_drilldown")
level = st.radio("Level", ["group", "band", "month"], horizontal=True,
                 format_func={"group": "Tenure group", "band": "Tenure band", "month": "Month"}.get)
rows = drill[drill.drill_level == level]
if level == "month":
    common.show(charts.month_line(rows.sort_values("tenure_months"), avg))
else:
    name = "tenure_group" if level == "group" else "tenure_band"
    order = rows.sort_values([f"{name}_order"])[name].tolist()
    common.show(charts.rate_bars(rows, name, "Tenure", avg, order))

st.caption(common.CAVEAT)
