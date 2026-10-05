"""Altair charts themed with the shared noFumble design tokens."""

from __future__ import annotations

import altair as alt
import pandas as pd

from forecast import ForecastResult
from ui.theme import TOKENS

alt.data_transformers.disable_max_rows()


def cashflow_chart(result: ForecastResult) -> alt.LayerChart:
    """Plot observed balances and one four-week forecast interval, not a path."""
    history = result.recent_balance_history.copy()
    history["series"] = "Observed balance"
    projection_date = result.as_of_week + pd.Timedelta(days=28)
    forecast = pd.DataFrame(
        [
            {
                "week_end": projection_date,
                "balance": result.projected_balance_4w,
                "lower": result.projected_balance_p10,
                "upper": result.projected_balance_p90,
                "series": "Four-week estimate",
            }
        ]
    )
    history_chart = (
        alt.Chart(history)
        .mark_line(color=TOKENS["ink"], strokeWidth=2.5)
        .encode(
            x=alt.X("week_end:T", title=None),
            y=alt.Y("current_balance:Q", title="Account balance"),
            tooltip=[
                alt.Tooltip("week_end:T", title="Week ending"),
                alt.Tooltip("current_balance:Q", title="Balance", format=",.0f"),
            ],
        )
    )
    interval = (
        alt.Chart(forecast)
        .mark_rule(color=TOKENS["accent"], strokeWidth=5, strokeCap="round")
        .encode(
            x=alt.X("week_end:T"),
            y=alt.Y("lower:Q"),
            y2="upper:Q",
            tooltip=[
                alt.Tooltip("lower:Q", title="10th percentile", format=",.0f"),
                alt.Tooltip("upper:Q", title="90th percentile", format=",.0f"),
            ],
        )
    )
    point = (
        alt.Chart(forecast)
        .mark_point(color=TOKENS["accent"], filled=True, size=100)
        .encode(
            x=alt.X("week_end:T"),
            y=alt.Y("balance:Q"),
            tooltip=[
                alt.Tooltip("week_end:T", title="Four weeks from now"),
                alt.Tooltip("balance:Q", title="Expected balance", format=",.0f"),
            ],
        )
    )
    buffer_rule = (
        alt.Chart(pd.DataFrame({"buffer": [result.buffer_amount]}))
        .mark_rule(color=TOKENS["warning"], strokeDash=[5, 4])
        .encode(y="buffer:Q")
    )
    return (
        (history_chart + interval + point + buffer_rule)
        .properties(height=330)
        .configure_view(stroke=None)
        .configure_axis(
            gridColor=TOKENS["line"],
            labelColor=TOKENS["muted"],
            titleColor=TOKENS["muted"],
        )
        .configure_legend(labelColor=TOKENS["muted"])
    )


def track_record_chart(track_record: pd.DataFrame) -> alt.Chart:
    """Compare honest baseline predictions with held-out synthetic outcomes."""
    long = track_record.melt(
        id_vars="week_end",
        value_vars=["actual", "prediction"],
        var_name="series",
        value_name="net_flow",
    )
    return (
        alt.Chart(long)
        .mark_line(point=True)
        .encode(
            x=alt.X("week_end:T", title=None),
            y=alt.Y("net_flow:Q", title="Next-four-week net flow"),
            color=alt.Color(
                "series:N",
                title=None,
                scale=alt.Scale(
                    domain=["actual", "prediction"],
                    range=[TOKENS["ink"], TOKENS["accent"]],
                ),
            ),
            tooltip=[
                alt.Tooltip("week_end:T", title="Forecast week"),
                alt.Tooltip("series:N", title="Series"),
                alt.Tooltip("net_flow:Q", title="Net flow", format=",.0f"),
            ],
        )
        .properties(height=250)
        .configure_view(stroke=None)
        .configure_axis(
            gridColor=TOKENS["line"],
            labelColor=TOKENS["muted"],
            titleColor=TOKENS["muted"],
        )
    )
