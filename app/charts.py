"""
charts.py - Altair chart builders for the web app (same look as dashboard/charts.py).

Design rules used everywhere
  - one validated colour order for categories (blue, orange, aqua, yellow);
    a single blue for one-series charts; a one-hue blue scale for heat maps
  - thin bars with a rounded end (square at the baseline), 2px lines, 8px points
  - hairline grid, no second y-axis, every mark has a hover tooltip
  - text uses ink colours, never the series colour
"""

from __future__ import annotations

import altair as alt
import pandas as pd

INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
CATEGORICAL = [BLUE, ORANGE, AQUA, YELLOW]
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
BAR_SIZE = 18

RATE_TOOLTIP = [
    alt.Tooltip("customers:Q", title="Customers", format=","),
    alt.Tooltip("churned:Q", title="Churned", format=","),
    alt.Tooltip("churn_rate_pct:Q", title="Churn rate %", format=".2f"),
]


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
        .configure_axisQuantitative(tickCount=6)
        .configure_view(strokeWidth=0)
    )


def _average_rule(avg: float, axis: str = "x"):
    """Thin grey reference line for the all-customers churn rate (explained in the page caption)."""
    data = pd.DataFrame({"avg": [avg]})
    enc = {"x": alt.X("avg:Q")} if axis == "x" else {"y": alt.Y("avg:Q")}
    return alt.Chart(data).mark_rule(color=MUTED, strokeWidth=1).encode(
        **enc, tooltip=[alt.Tooltip("avg:Q", title="All customers: churn rate %", format=".2f")])


def rate_bars(df: pd.DataFrame, category: str, label: str, avg: float | None = None,
              order: list[str] | None = None, height: int | None = None):
    """Horizontal bars of churn rate per member (value at the bar tip).

    Without an `order`, the highest churn rate comes first. The order is always
    passed as an explicit list: in a layered chart a computed sort ("-x") makes
    Vega-Lite fall back to alphabetical order.
    """
    if order is None:
        order = df.sort_values("churn_rate_pct", ascending=False)[category].tolist()
    x_max = max(70.0, float(df["churn_rate_pct"].max()) * 1.18)
    base = alt.Chart(df).encode(
        y=alt.Y(f"{category}:N", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X("churn_rate_pct:Q", title="Churn rate (%)", scale=alt.Scale(domain=[0, x_max])),
        tooltip=[alt.Tooltip(f"{category}:N", title=label), *RATE_TOOLTIP],
    )
    bars = base.mark_bar(color=BLUE, cornerRadiusEnd=4, size=BAR_SIZE)
    text = base.mark_text(align="left", dx=5, color=INK_2, fontSize=12).encode(
        text=alt.Text("churn_rate_pct:Q", format=".1f"))
    layers = [bars, text]
    if avg is not None:
        layers.append(_average_rule(avg, "x"))
    return finish(alt.layer(*layers), height=height or max(110, 36 * len(df) + 40))


def month_line(df: pd.DataFrame, avg: float | None = None):
    """Churn rate per tenure month (line + points)."""
    base = alt.Chart(df).encode(
        x=alt.X("tenure_months:Q", title="Tenure (months)", axis=alt.Axis(tickMinStep=1, format="d")),
        y=alt.Y("churn_rate_pct:Q", title="Churn rate (%)", scale=alt.Scale(domain=[0, 100])),
        tooltip=[alt.Tooltip("tenure_months:Q", title="Tenure (months)"), *RATE_TOOLTIP],
    )
    layers = [base.mark_line(color=BLUE, strokeWidth=2),
              base.mark_point(color=BLUE, filled=True, size=64, stroke=SURFACE, strokeWidth=2, opacity=1)]
    if avg is not None:
        layers.append(_average_rule(avg, "y"))
    return finish(alt.layer(*layers), height=300)


def heatmap(df: pd.DataFrame, row: str, col: str, row_title: str, col_title: str):
    """Two-dimensional slice of the cube as a heat map of churn rate (one blue scale)."""
    vmax = max(1.0, float(df["churn_rate_pct"].max()))
    base = alt.Chart(df).encode(
        y=alt.Y(f"{row}:N", title=row_title, axis=alt.Axis(ticks=False, domain=False)),
        x=alt.X(f"{col}:N", title=col_title, axis=alt.Axis(ticks=False, domain=False, labelAngle=0,
                                                          orient="top", labelLimit=160)),
        tooltip=[alt.Tooltip(f"{row}:N", title=row_title), alt.Tooltip(f"{col}:N", title=col_title),
                 *RATE_TOOLTIP],
    )
    cells = base.mark_rect(cornerRadius=3, stroke=SURFACE, strokeWidth=2).encode(
        color=alt.Color("churn_rate_pct:Q", scale=alt.Scale(range=BLUE_RAMP, domain=[0, vmax]), legend=None))
    text = base.mark_text(fontSize=13).encode(
        text=alt.Text("churn_rate_pct:Q", format=".1f"),
        color=alt.condition(f"datum.churn_rate_pct > {vmax * 0.55:.2f}", alt.value("white"), alt.value(INK)))
    return finish(cells + text, height=60 * df[row].nunique() + 40)


def confusion(tn: int, fp: int, fn: int, tp: int):
    """2 x 2 confusion matrix as a small heat map."""
    df = pd.DataFrame({
        "actual": ["No churn", "No churn", "Churn", "Churn"],
        "predicted": ["No churn", "Churn", "No churn", "Churn"],
        "count": [tn, fp, fn, tp],
        "name": ["True negative (stayer left alone)", "False positive (wasted offer)",
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


def importance_bars(df: pd.DataFrame, value_title: str):
    """Ranked feature importance with a +/- 1 std line (largest at the top)."""
    order = df.sort_values("importance", ascending=False)["feature"].tolist()
    y = alt.Y("feature:N", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False))
    tooltip = [alt.Tooltip("feature:N", title="Feature"),
               alt.Tooltip("importance:Q", title="Importance", format=".4f"),
               alt.Tooltip("importance_std:Q", title="Std (repeats)", format=".4f")]
    bars = alt.Chart(df).mark_bar(color=BLUE, cornerRadiusEnd=4, size=14).encode(
        y=y, x=alt.X("importance:Q", title=value_title), tooltip=tooltip)
    err = df.assign(low=df["importance"] - df["importance_std"], high=df["importance"] + df["importance_std"])
    rule = alt.Chart(err).mark_rule(color=INK_2, strokeWidth=1).encode(y=y, x="low:Q", x2="high:Q", tooltip=tooltip)
    return finish(alt.layer(bars, rule), height=max(160, 26 * len(df) + 40))


def model_dots(long: pd.DataFrame, models: list[str], x_title: str):
    """One row per metric, one dot per model (dots show small differences better than bars)."""
    metric_order = ["Accuracy", "Precision", "Recall", "F1-score", "ROC-AUC"]
    low = max(0.0, float(long["value"].min()) - 0.05)
    return finish(
        alt.Chart(long).mark_point(filled=True, size=90, stroke=SURFACE, strokeWidth=2, opacity=1).encode(
            y=alt.Y("metric:N", sort=metric_order, title=None, axis=alt.Axis(ticks=False, domain=False)),
            yOffset=alt.YOffset("model:N", sort=models),
            x=alt.X("value:Q", title=x_title, scale=alt.Scale(domain=[low, 1.0])),
            color=alt.Color("model:N", scale=alt.Scale(domain=models, range=CATEGORICAL[:len(models)])),
            tooltip=[alt.Tooltip("model:N", title="Model"), alt.Tooltip("metric:N", title="Metric"),
                     alt.Tooltip("value:Q", title="Value", format=".4f")],
        ),
        height=320,
    )


def threshold_lines(long: pd.DataFrame, current: float):
    """Precision, recall and F1 at each decision threshold, with the current threshold marked."""
    series = long["metric"].unique().tolist()
    base = alt.Chart(long).encode(
        x=alt.X("threshold:Q", title="Decision threshold (predict churn when probability ≥ threshold)",
                scale=alt.Scale(domain=[float(long["threshold"].min()), float(long["threshold"].max())])),
        y=alt.Y("value:Q", title="Score (training data, cross-validation)", scale=alt.Scale(domain=[0, 1])),
        color=alt.Color("metric:N", scale=alt.Scale(domain=series, range=CATEGORICAL[:len(series)])),
        tooltip=[alt.Tooltip("threshold:Q", title="Threshold", format=".2f"),
                 alt.Tooltip("metric:N", title="Metric"), alt.Tooltip("value:Q", title="Value", format=".3f")],
    )
    marker = alt.Chart(pd.DataFrame({"t": [current]})).mark_rule(color=INK, strokeWidth=1).encode(
        x="t:Q", tooltip=[alt.Tooltip("t:Q", title="Current threshold", format=".2f")])
    return finish(alt.layer(base.mark_line(strokeWidth=2),
                            base.mark_point(filled=True, size=64, stroke=SURFACE, strokeWidth=2, opacity=1),
                            marker), height=280)


def reason_bars(df: pd.DataFrame):
    """How much each factor raises (right) or lowers (left) this customer's churn probability."""
    df = df.assign(direction=df["points"].map(lambda v: "Raises the risk" if v > 0 else "Lowers the risk"))
    order = df.sort_values("points", ascending=False)["label"].tolist()
    span = max(5.0, float(df["points"].abs().max()) * 1.25)
    base = alt.Chart(df).encode(
        y=alt.Y("label:N", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False, labelLimit=260)),
        x=alt.X("points:Q", title="Change in churn probability (percentage points)",
                scale=alt.Scale(domain=[-span, span])),
        tooltip=[alt.Tooltip("label:N", title="Factor"), alt.Tooltip("value:N", title="This customer"),
                 alt.Tooltip("points:Q", title="Points", format="+.1f")],
    )
    bars = base.mark_bar(cornerRadiusEnd=4, size=14).encode(
        color=alt.Color("direction:N", scale=alt.Scale(domain=["Raises the risk", "Lowers the risk"],
                                                      range=[ORANGE, BLUE])))
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color=BASELINE, strokeWidth=1).encode(x="x:Q")
    return finish(alt.layer(bars, zero), height=max(160, 26 * len(df) + 50))


def history_lines(long: pd.DataFrame):
    """A metric per scoring run over time (one line per metric, same 0-1 scale)."""
    series = long["metric"].unique().tolist()
    base = alt.Chart(long).encode(
        x=alt.X("run_at:T", title="Scoring run"),
        y=alt.Y("value:Q", title="Value", scale=alt.Scale(zero=False)),
        color=alt.Color("metric:N", scale=alt.Scale(domain=series, range=CATEGORICAL[:len(series)])),
        tooltip=[alt.Tooltip("run_at:T", title="Run", format="%d %b %Y %H:%M"),
                 alt.Tooltip("metric:N", title="Metric"), alt.Tooltip("value:Q", title="Value", format=".4f"),
                 alt.Tooltip("model_version:N", title="Model version")],
    )
    return finish(alt.layer(base.mark_line(strokeWidth=2),
                            base.mark_point(filled=True, size=64, stroke=SURFACE, strokeWidth=2, opacity=1)),
                  height=260)
