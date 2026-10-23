"""
charts.py - Altair chart builders for the dashboard.

Design rules (applied everywhere):
  - one validated colour order for categories (blue, orange, aqua, yellow);
    a single blue for one-series charts; a one-hue blue scale for heat maps
  - thin bars with a rounded end (square at the baseline), 2px lines,
    8px points, hairline grid lines, no second y-axis
  - every mark has a hover tooltip; text uses ink colours, never the series colour
"""

from __future__ import annotations

import altair as alt
import pandas as pd

INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
MODEL_COLORS = {
    "Logistic Regression": BLUE,
    "Decision Tree": ORANGE,
    "Random Forest": AQUA,
    "Gradient Boosting": YELLOW,
}
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
BAR_SIZE = 18


def finish(chart, height: int = 300):
    """Apply the common look to a top-level chart."""
    return (
        chart.properties(height=height, background="transparent")
        .configure(font=FONT)
        .configure_axis(gridColor=GRID, gridWidth=1, domainColor=BASELINE, tickColor=BASELINE,
                        labelColor=INK_2, titleColor=INK_2, labelFontSize=12, titleFontSize=12,
                        titleFontWeight=400, labelLimit=280)
        .configure_legend(labelColor=INK_2, titleColor=INK_2, labelFontSize=12, orient="top",
                          symbolType="circle", title=None)
        .configure_axisQuantitative(tickCount=6)          # fewer, rounder axis numbers
        .configure_view(strokeWidth=0)
        .configure_title(color=INK, fontSize=14, fontWeight=600, anchor="start")
    )


def _average_rule(avg: float, axis: str = "x"):
    """Thin grey reference line for the company-average churn rate (explained in the page caption)."""
    data = pd.DataFrame({"avg": [avg]})
    enc = {"x": alt.X("avg:Q")} if axis == "x" else {"y": alt.Y("avg:Q")}
    return alt.Chart(data).mark_rule(color=MUTED, strokeWidth=1, strokeDash=[]).encode(
        **enc, tooltip=[alt.Tooltip("avg:Q", title="All customers: churn rate %", format=".2f")])


RATE_TOOLTIP = [
    alt.Tooltip("customers:Q", title="Customers", format=","),
    alt.Tooltip("churned:Q", title="Churned", format=","),
    alt.Tooltip("churn_rate_pct:Q", title="Churn rate %", format=".2f"),
]


def rate_bars(df: pd.DataFrame, category: str, label: str, avg: float | None = None,
              order: list[str] | None = None, highlight: str | None = None,
              extra_tooltip: list | None = None, x_max: float | None = None,
              height: int | None = None):
    """Horizontal bars of churn rate per category (values at the bar tips)."""
    df = df.copy()
    df["is_selected"] = True if highlight is None else (df[category] == highlight)
    sort = order if order is not None else "-x"
    x_max = x_max or max(70.0, float(df["churn_rate_pct"].max()) * 1.18)
    base = alt.Chart(df).encode(
        y=alt.Y(f"{category}:N", sort=sort, title=None, axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X("churn_rate_pct:Q", title="Churn rate (%)", scale=alt.Scale(domain=[0, x_max])),
        tooltip=[alt.Tooltip(f"{category}:N", title=label), *RATE_TOOLTIP, *(extra_tooltip or [])],
    )
    bars = base.mark_bar(color=BLUE, cornerRadiusEnd=4, size=BAR_SIZE).encode(
        opacity=alt.condition("datum.is_selected", alt.value(1.0), alt.value(0.35)))
    text = base.mark_text(align="left", dx=5, color=INK_2, fontSize=12).encode(
        text=alt.Text("churn_rate_pct:Q", format=".1f"))
    layers = [bars, text]
    if avg is not None:
        layers.append(_average_rule(avg, "x"))
    return finish(alt.layer(*layers), height=height or max(110, 36 * len(df) + 40))


def click_rate_bars(df: pd.DataFrame, category: str, label: str, param: str,
                    selected: str | None, order: list[str] | None = None):
    """Like rate_bars, but clicking a bar selects it (used for drill-down).

    Streamlit only supports click selections on single-layer charts, so the
    value is written into the axis label ("0-12 months  ·  47.4%") instead of
    a separate text layer.
    """
    df = df.copy()
    df["is_selected"] = True if selected is None else (df[category] == selected)
    df["bar_label"] = df[category] + "  ·  " + df["churn_rate_pct"].map("{:.1f}%".format)
    label_order = [df.loc[df[category] == v, "bar_label"].iloc[0] for v in (order or df[category])]
    pick = alt.selection_point(name=param, fields=[category], on="click", clear=False)
    x_max = max(70.0, float(df["churn_rate_pct"].max()) * 1.1)
    chart = alt.Chart(df).mark_bar(color=BLUE, cornerRadiusEnd=4, size=BAR_SIZE + 4, cursor="pointer").encode(
        y=alt.Y("bar_label:N", sort=label_order, title=None, axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X("churn_rate_pct:Q", title="Churn rate (%)", scale=alt.Scale(domain=[0, x_max])),
        opacity=alt.condition("datum.is_selected", alt.value(1.0), alt.value(0.35)),
        tooltip=[alt.Tooltip(f"{category}:N", title=label), *RATE_TOOLTIP,
                 alt.Tooltip("avg_monthly_charge:Q", title="Avg monthly charge", format=",.2f")],
    ).add_params(pick)
    return finish(chart, height=max(100, 40 * len(df) + 30))


def month_line(df: pd.DataFrame, avg: float | None = None):
    """Churn rate per tenure month (line + points)."""
    base = alt.Chart(df).encode(
        x=alt.X("tenure_months:Q", title="Tenure (months)", axis=alt.Axis(tickMinStep=1, format="d")),
        y=alt.Y("churn_rate_pct:Q", title="Churn rate (%)", scale=alt.Scale(domain=[0, 100])),
        tooltip=[alt.Tooltip("tenure_months:Q", title="Tenure (months)"), *RATE_TOOLTIP],
    )
    line = base.mark_line(color=BLUE, strokeWidth=2)
    points = base.mark_point(color=BLUE, filled=True, size=64, stroke=SURFACE, strokeWidth=2)
    layers = [line, points]
    if avg is not None:
        layers.append(_average_rule(avg, "y"))
    return finish(alt.layer(*layers), height=320)


def heatmap(df: pd.DataFrame, row: str, col: str, row_title: str, col_title: str):
    """Pivot table as a heat map of churn rate (one blue scale)."""
    vmax = max(1.0, float(df["churn_rate_pct"].max()))
    base = alt.Chart(df).encode(
        y=alt.Y(f"{row}:N", title=row_title, axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X(f"{col}:N", title=col_title, axis=alt.Axis(ticks=False, domain=False, labelAngle=0,
                                                          orient="top")),
        tooltip=[alt.Tooltip(f"{row}:N", title=row_title), alt.Tooltip(f"{col}:N", title=col_title),
                 *RATE_TOOLTIP],
    )
    cells = base.mark_rect(cornerRadius=3, stroke=SURFACE, strokeWidth=2).encode(
        color=alt.Color("churn_rate_pct:Q", title="Churn rate %",
                        scale=alt.Scale(range=BLUE_RAMP, domain=[0, vmax]), legend=None))
    text = base.mark_text(fontSize=13).encode(
        text=alt.Text("churn_rate_pct:Q", format=".1f"),
        color=alt.condition(f"datum.churn_rate_pct > {vmax * 0.55:.2f}", alt.value("white"), alt.value(INK)))
    return finish(cells + text, height=60 * df[row].nunique() + 40)


def ranking_bars(df: pd.DataFrame):
    """Segments ranked by monthly revenue lost to churn."""
    base = alt.Chart(df).encode(
        y=alt.Y("segment:N", sort=alt.EncodingSortField("rank_no", order="ascending"), title=None,
                axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X("monthly_revenue_lost:Q", title="Monthly revenue lost to churn",
                scale=alt.Scale(domain=[0, float(df["monthly_revenue_lost"].max()) * 1.15])),
        tooltip=[alt.Tooltip("rank_no:Q", title="Rank"), alt.Tooltip("segment:N", title="Segment"),
                 alt.Tooltip("customers:Q", title="Customers", format=","),
                 alt.Tooltip("churn_rate_pct:Q", title="Churn rate %", format=".2f"),
                 alt.Tooltip("monthly_revenue_lost:Q", title="Revenue lost", format=",.2f"),
                 alt.Tooltip("share_of_lost_pct:Q", title="Share of all lost %", format=".2f"),
                 alt.Tooltip("cumulative_share_pct:Q", title="Cumulative share %", format=".2f")],
    )
    bars = base.mark_bar(color=BLUE, cornerRadiusEnd=4, size=BAR_SIZE)
    text = base.mark_text(align="left", dx=5, color=INK_2, fontSize=12).encode(
        text=alt.Text("monthly_revenue_lost:Q", format=",.0f"))
    return finish(bars + text, height=max(140, 34 * len(df) + 30))


def dumbbell(df: pd.DataFrame):
    """Churn rate with vs without each add-on service (two points joined by a line)."""
    long = df.melt(id_vars=["addon"], value_vars=["With the add-on", "Without the add-on"],
                   var_name="status", value_name="churn_rate_pct")
    order = df.sort_values("gap", ascending=False)["addon"].tolist()
    rule = alt.Chart(df).mark_rule(color=BASELINE, strokeWidth=2).encode(
        y=alt.Y("addon:N", sort=order, title=None),
        x="With the add-on:Q", x2="Without the add-on:Q")
    points = alt.Chart(long).mark_point(filled=True, size=110, stroke=SURFACE, strokeWidth=2, opacity=1).encode(
        y=alt.Y("addon:N", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X("churn_rate_pct:Q", title="Churn rate (%)", scale=alt.Scale(domain=[0, 50])),
        color=alt.Color("status:N", scale=alt.Scale(domain=["With the add-on", "Without the add-on"],
                                                    range=[BLUE, ORANGE])),
        tooltip=[alt.Tooltip("addon:N", title="Add-on"), alt.Tooltip("status:N", title="Status"),
                 alt.Tooltip("churn_rate_pct:Q", title="Churn rate %", format=".2f")],
    )
    return finish(rule + points, height=44 * len(df) + 50)


def model_dots(long: pd.DataFrame):
    """Test-set metrics: one row per metric, one dot per model."""
    metric_order = ["Accuracy", "Precision", "Recall", "F1-score", "ROC-AUC"]
    return finish(
        alt.Chart(long).mark_point(filled=True, size=90, stroke=SURFACE, strokeWidth=2, opacity=1).encode(
            y=alt.Y("metric:N", sort=metric_order, title=None, axis=alt.Axis(ticks=False, domain=False)),
            yOffset=alt.YOffset("model:N", sort=list(MODEL_COLORS)),
            x=alt.X("value:Q", title="Score on the test set (threshold 0.5)", scale=alt.Scale(domain=[0.45, 0.9])),
            color=alt.Color("model:N", scale=alt.Scale(domain=list(MODEL_COLORS), range=list(MODEL_COLORS.values()))),
            tooltip=[alt.Tooltip("model:N", title="Model"), alt.Tooltip("metric:N", title="Metric"),
                     alt.Tooltip("value:Q", title="Value", format=".4f")],
        ),
        height=320,
    )


def roc_lines(roc: pd.DataFrame):
    """ROC curves of the four models on the test set."""
    diagonal = alt.Chart(pd.DataFrame({"x": [0, 1], "y": [0, 1]})).mark_line(
        color=BASELINE, strokeWidth=1).encode(x="x:Q", y="y:Q")
    lines = alt.Chart(roc).mark_line(strokeWidth=2, interpolate="step-after").encode(
        x=alt.X("fpr:Q", title="False positive rate (staying customers wrongly flagged)",
                scale=alt.Scale(domain=[0, 1])),
        y=alt.Y("tpr:Q", title="True positive rate = recall", scale=alt.Scale(domain=[0, 1])),
        color=alt.Color("label:N", scale=alt.Scale(domain=roc["label"].unique().tolist(),
                                                   range=list(MODEL_COLORS.values())),
                        legend=alt.Legend(orient="bottom", columns=2, labelLimit=320, symbolType="stroke")),
        detail="model:N",
        tooltip=[alt.Tooltip("label:N", title="Model")],
    )
    return finish(diagonal + lines, height=420)


def confusion(tn: int, fp: int, fn: int, tp: int):
    """2 x 2 confusion matrix as a small heat map."""
    df = pd.DataFrame({
        "actual": ["No churn", "No churn", "Churn", "Churn"],
        "predicted": ["No churn", "Churn", "No churn", "Churn"],
        "count": [tn, fp, fn, tp],
        "name": ["True negative", "False positive (wasted offer)",
                 "False negative (missed churner)", "True positive (churner caught)"],
        "short": ["TN", "FP", "FN", "TP"],
    })
    vmax = max(1, int(df["count"].max()))
    df["text"] = df["count"].map("{:,}".format) + "  " + df["short"]
    base = alt.Chart(df).encode(
        x=alt.X("predicted:N", title="Predicted", sort=["No churn", "Churn"],
                axis=alt.Axis(orient="top", labelAngle=0, ticks=False, domain=False)),
        y=alt.Y("actual:N", title="Actual", sort=["No churn", "Churn"], axis=alt.Axis(ticks=False, domain=False)),
        tooltip=[alt.Tooltip("name:N", title="Cell"), alt.Tooltip("count:Q", title="Customers", format=",")],
    )
    cells = base.mark_rect(cornerRadius=3, stroke=SURFACE, strokeWidth=2).encode(
        color=alt.Color("count:Q", scale=alt.Scale(range=BLUE_RAMP, domain=[0, vmax]), legend=None))
    text = base.mark_text(fontSize=15).encode(
        text="text:N",
        color=alt.condition(f"datum.count > {vmax * 0.55:.1f}", alt.value("white"), alt.value(INK)))
    return finish(cells + text, height=220)


def importance_bars(df: pd.DataFrame, value_title: str, with_error: bool = False):
    """Ranked feature importance (largest at the top).

    The order is passed as an explicit list: with two layers (bars + error
    lines) a computed sort would conflict and Vega-Lite would sort alphabetically.
    """
    order = df.sort_values("importance", ascending=False)["feature"].tolist()
    y = alt.Y("feature:N", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False))
    tooltip = [alt.Tooltip("feature:N", title="Feature"),
               alt.Tooltip("importance:Q", title="Importance", format=".5f"),
               *([alt.Tooltip("importance_std:Q", title="Std (repeats)", format=".5f")] if with_error else [])]
    bars = alt.Chart(df).mark_bar(color=BLUE, cornerRadiusEnd=4, size=14).encode(
        y=y, x=alt.X("importance:Q", title=value_title), tooltip=tooltip)
    layers = [bars]
    if with_error:
        err = df.assign(low=df["importance"] - df["importance_std"], high=df["importance"] + df["importance_std"])
        layers.append(alt.Chart(err).mark_rule(color=INK_2, strokeWidth=1).encode(
            y=y, x="low:Q", x2="high:Q", tooltip=tooltip))
    return finish(alt.layer(*layers), height=max(160, 26 * len(df) + 40))


def cost_curve(df: pd.DataFrame, current: float):
    """Total cost of errors at every threshold, with the chosen threshold marked."""
    line = alt.Chart(df).mark_line(color=BLUE, strokeWidth=2).encode(
        x=alt.X("threshold:Q", title="Decision threshold", scale=alt.Scale(domain=[0.05, 0.95])),
        y=alt.Y("cost:Q", title="Total cost of errors (test set)"),
        tooltip=[alt.Tooltip("threshold:Q", format=".2f"), alt.Tooltip("cost:Q", title="Cost", format=",.0f"),
                 alt.Tooltip("missed:Q", title="Missed churners"), alt.Tooltip("wasted:Q", title="Wasted offers")])
    marker = alt.Chart(pd.DataFrame({"threshold": [current]})).mark_rule(color=INK, strokeWidth=1).encode(x="threshold:Q")
    return finish(line + marker, height=260)


def solution_bars(df: pd.DataFrame):
    """Expected churners prevented per simulated solution (largest at the top).

    The recommended solution is drawn solid and named in its label; the other
    solutions are faded, so the choice is never shown by colour alone.
    """
    order = df.sort_values("churners_prevented", ascending=False)["label"].tolist()
    base = alt.Chart(df).encode(
        y=alt.Y("label:N", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X("churners_prevented:Q", title="Expected churners prevented among active customers (what-if)"),
        tooltip=[alt.Tooltip("solution_name:N", title="Solution"),
                 alt.Tooltip("target_customers:Q", title="Active customers targeted", format=","),
                 alt.Tooltip("expected_churners_before:Q", title="Expected churners before", format=",.0f"),
                 alt.Tooltip("expected_churners_after:Q", title="Expected churners after", format=",.0f"),
                 alt.Tooltip("churners_prevented:Q", title="Prevented", format=",.0f"),
                 alt.Tooltip("prevented_per_100:Q", title="Prevented per 100 contacted", format=".1f"),
                 alt.Tooltip("revenue_protected:Q", title="Monthly revenue protected", format=",.0f")],
    )
    bars = base.mark_bar(color=BLUE, cornerRadiusEnd=4, size=BAR_SIZE).encode(
        opacity=alt.condition("datum.is_recommended", alt.value(1.0), alt.value(0.35)))
    text = base.mark_text(align="left", dx=5, color=INK_2, fontSize=12).encode(
        text=alt.Text("churners_prevented:Q", format=",.0f"))
    return finish(alt.layer(bars, text), height=max(140, 44 * len(df) + 40))


def reason_bars(df: pd.DataFrame):
    """Share of the customers who left for whom each factor is a reason (largest at the top)."""
    order = df.sort_values("churners_contributing_pct", ascending=False)["reason_name"].tolist()
    base = alt.Chart(df).encode(
        y=alt.Y("reason_name:N", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X("churners_contributing_pct:Q", title="% of leavers for whom it is a reason",
                scale=alt.Scale(domain=[0, 100])),
        tooltip=[alt.Tooltip("reason_name:N", title="Reason"),
                 alt.Tooltip("churners_contributing_pct:Q", title="A reason for % of leavers", format=".1f"),
                 alt.Tooltip("churners_main_pct:Q", title="Main reason for % of leavers", format=".1f"),
                 alt.Tooltip("at_risk_contributing_pct:Q", title="A reason for % of active High-risk", format=".1f"),
                 alt.Tooltip("evidence_strength:N", title="Evidence")],
    )
    bars = base.mark_bar(color=BLUE, cornerRadiusEnd=4, size=BAR_SIZE)
    text = base.mark_text(align="left", dx=5, color=INK_2, fontSize=12).encode(
        text=alt.Text("churners_contributing_pct:Q", format=".0f"))
    return finish(alt.layer(bars, text), height=max(140, 36 * len(df) + 40))
