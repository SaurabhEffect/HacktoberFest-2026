"""Cash-flow forecast service and deterministic risk-signal helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

import numpy as np
import pandas as pd

from features import (
    TARGET_COLUMNS,
    build_weekly_feature_core,
    compute_dip_labels,
)
from generate_sample_data import DEFAULT_BUFFER_AMOUNT
from model import (
    constant_class_predictions,
    fit_and_predict,
)

MINIMUM_TRAINING_WEEKS = 26
LOW_CONFIDENCE_WEEKS = 52
RISK_THRESHOLDS = {"watch": 0.30, "high": 0.60}
RiskLevel = Literal["low", "watch", "high"]


class ForecastUnavailableError(ValueError):
    """Raised when the supplied data cannot support a forecast."""


@dataclass
class ForecastResult:
    """Forecast values, uncertainty, risk signals, and human-readable context."""

    as_of_week: pd.Timestamp
    current_balance: float
    buffer_amount: float
    expected_net_flow_4w: float
    net_flow_p10: float
    net_flow_p90: float
    projected_balance_4w: float
    projected_balance_p10: float
    projected_balance_p90: float
    dip_probability_30d: float
    model_dip_probability_30d: float
    projected_balance_below_buffer_probability: float
    classifier_fallback: str | None
    runway_weeks: float | None
    recent_balance_history: pd.DataFrame
    risk_level: RiskLevel
    risk_signals: list[str]
    summary: str
    warnings: list[str]
    upcoming_expense: float = 0.0


def default_buffer_amount(transactions: pd.DataFrame) -> float:
    """Estimate about one month of typical expenses from calendar-month totals."""
    expenses = transactions.loc[transactions["amount"] < 0].copy()
    if expenses.empty:
        return DEFAULT_BUFFER_AMOUNT
    expenses["month"] = pd.to_datetime(expenses["date"]).dt.to_period("M")
    monthly_totals = -expenses.groupby("month")["amount"].sum()
    typical = float(monthly_totals.median())
    return (
        max(1.0, round(typical, 2)) if np.isfinite(typical) else DEFAULT_BUFFER_AMOUNT
    )


def _probability_below(
    threshold: float, lower: float, upper: float, mean: float
) -> float:
    """Linearly interpolate a CDF through the 10th/90th percentile anchors."""
    spread = upper - lower
    if not np.isfinite(spread) or spread <= 1e-9:
        return float(threshold > mean)
    tail_width = spread / 8.0
    probability = np.interp(
        threshold,
        [lower - tail_width, lower, upper, upper + tail_width],
        [0.0, 0.1, 0.9, 1.0],
    )
    return float(np.clip(probability, 0.0, 1.0))


def _risk_level(probability: float) -> RiskLevel:
    if probability >= RISK_THRESHOLDS["high"]:
        return "high"
    if probability >= RISK_THRESHOLDS["watch"]:
        return "watch"
    return "low"


def _risk_signals(row: pd.Series, buffer_amount: float) -> list[str]:
    def amount(name: str) -> float:
        value = row.get(name, 0.0)
        return float(value) if pd.notna(value) else 0.0

    signals: list[str] = []
    past_due = amount("past_due_receivables")
    if past_due > 0:
        signals.append(f"${past_due:,.0f} in invoices is past due.")
    concentration = amount("top_client_income_share_13w")
    if concentration >= 0.65:
        signals.append(f"{concentration:.0%} of recent income came from one client.")
    weeks_since_payment = row.get("weeks_since_last_payment")
    if pd.notna(weeks_since_payment) and float(weeks_since_payment) >= 4:
        signals.append(
            f"No client payment has arrived in about {weeks_since_payment:.0f} weeks."
        )
    lumpy_next_week = int(amount("known_obligation_next_7d"))
    expected_lumpy = amount("expected_lumpy_outflow_next_28d")
    if lumpy_next_week and expected_lumpy > 0:
        signals.append(
            f"A known quarterly or annual expense of about ${expected_lumpy:,.0f} "
            "is due within a week."
        )
    elif expected_lumpy > 0:
        signals.append(
            f"About ${expected_lumpy:,.0f} in known lumpy expenses is due within "
            "four weeks."
        )
    if len(signals) < 4:
        expected_income = amount("expected_income_this_month")
        typical_expenses = amount("mean_expenses_4w") * 4.0
        if typical_expenses > 0 and expected_income < typical_expenses:
            gap = typical_expenses - expected_income
            signals.append(
                f"Expected invoices are about ${gap:,.0f} below a typical month "
                "of expenses."
            )
    if not signals and float(row.get("current_balance", 0.0)) < buffer_amount:
        signals.append("The current balance is already below your selected buffer.")
    return signals[:4]


def apply_upcoming_expense(
    result: ForecastResult, upcoming_expense: float
) -> ForecastResult:
    """Apply a deterministic expense scenario without fitting either model again."""
    if not np.isfinite(upcoming_expense) or upcoming_expense < 0:
        raise ValueError("upcoming_expense must be a finite, nonnegative amount.")
    shift = float(upcoming_expense)
    projected = result.projected_balance_4w - shift
    lower = result.projected_balance_p10 - shift
    upper = result.projected_balance_p90 - shift
    analytic_probability = _probability_below(
        result.buffer_amount, lower, upper, projected
    )
    dip_probability = max(result.model_dip_probability_30d, analytic_probability)
    risk_level = _risk_level(dip_probability)
    signals = list(result.risk_signals)
    if shift > 0:
        scenario_signal = (
            f"What-if: a ${shift:,.0f} expense would lower the four-week estimate "
            "by the same amount."
        )
        signals = (
            [*signals[:3], scenario_signal]
            if len(signals) >= 4
            else [*signals, scenario_signal]
        )
    summary = (
        f"Estimated balance in four weeks is ${projected:,.0f}, with an 80% "
        f"range of ${lower:,.0f} to ${upper:,.0f}. The chance of dipping below "
        f"your ${result.buffer_amount:,.0f} buffer within 30 days is "
        f"{dip_probability:.0%} ({risk_level} risk). This is a probabilistic "
        "estimate, not a guarantee."
    )
    if result.classifier_fallback == "no_dips":
        summary += " There have been no below-buffer periods in the training history."
    elif result.classifier_fallback == "always_below_buffer":
        summary += " The training history was always below the selected buffer."
    return replace(
        result,
        projected_balance_4w=projected,
        projected_balance_p10=lower,
        projected_balance_p90=upper,
        projected_balance_below_buffer_probability=analytic_probability,
        dip_probability_30d=dip_probability,
        risk_level=risk_level,
        risk_signals=signals[:4],
        summary=summary,
        upcoming_expense=shift,
    )


def run_forecast(
    transactions: pd.DataFrame,
    invoices: pd.DataFrame,
    buffer_amount: float = DEFAULT_BUFFER_AMOUNT,
    device: str = "auto",
    upcoming_expense: float = 0.0,
    model_factory: Any | None = None,
    core_features: tuple[pd.DataFrame, pd.DataFrame] | None = None,
    regression_predictions: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> ForecastResult:
    """Fit weekly TabPFN models and return a 30-day probabilistic risk forecast."""
    if not np.isfinite(buffer_amount) or buffer_amount < 0:
        raise ValueError("buffer_amount must be a finite, nonnegative amount.")
    training, inference = core_features or build_weekly_feature_core(
        transactions, invoices
    )
    training = training.copy()
    training["balance_below_buffer_within_30d"] = compute_dip_labels(
        transactions, training, buffer_amount
    )
    if len(training) < MINIMUM_TRAINING_WEEKS:
        raise ForecastUnavailableError(
            "There is not enough history to make a forecast yet. "
            "At least 26 complete weeks are needed."
        )
    if inference.empty:
        raise ForecastUnavailableError(
            "The latest week is incomplete, so a forecast is not available yet."
        )
    if training.empty:
        raise ForecastUnavailableError("No complete forecast weeks were available.")

    latest = inference.iloc[-1]
    features = [
        column
        for column in training.columns
        if column not in (*TARGET_COLUMNS, "week_end")
    ]
    train_x = training[features]
    latest_x = inference.loc[[inference.index[-1]], features]
    regression = regression_predictions or fit_and_predict(
        "regression",
        train_x,
        training["next_4w_net_cash_flow"].astype(float),
        latest_x,
        device=device,
        model_factory=model_factory,
    )
    mean_flow, lower_flow, upper_flow = (float(values[0]) for values in regression)

    labels = training["balance_below_buffer_within_30d"].astype(int)
    fallback: str | None = None
    if labels.nunique() < 2:
        predictions, probabilities = constant_class_predictions(labels, 1)
        model_probability = float(probabilities[0])
        fallback = "no_dips" if int(predictions[0]) == 0 else "always_below_buffer"
    else:
        (dip_probabilities,) = fit_and_predict(
            "classification",
            train_x,
            labels,
            latest_x,
            device=device,
            model_factory=model_factory,
        )
        model_probability = float(np.clip(dip_probabilities[0], 0.0, 1.0))

    current_balance = float(latest["current_balance"])
    projected_balance = current_balance + mean_flow
    projected_lower = current_balance + lower_flow
    projected_upper = current_balance + upper_flow
    end_probability = _probability_below(
        buffer_amount, projected_lower, projected_upper, projected_balance
    )
    warnings: list[str] = []
    if len(training) < LOW_CONFIDENCE_WEEKS:
        warnings.append(
            "Confidence is limited because fewer than 52 complete weeks are available."
        )
    history = pd.concat([training, inference], ignore_index=True)
    history = (
        history.loc[:, ["week_end", "current_balance"]]
        .drop_duplicates("week_end")
        .sort_values("week_end")
        .tail(26)
        .reset_index(drop=True)
    )
    runway_value = latest.get("runway_weeks_at_current_burn")
    runway = float(runway_value) if pd.notna(runway_value) else None
    row_signals = _risk_signals(latest, buffer_amount)
    result = ForecastResult(
        as_of_week=pd.Timestamp(latest["week_end"]),
        current_balance=current_balance,
        buffer_amount=float(buffer_amount),
        expected_net_flow_4w=mean_flow,
        net_flow_p10=lower_flow,
        net_flow_p90=upper_flow,
        projected_balance_4w=projected_balance,
        projected_balance_p10=projected_lower,
        projected_balance_p90=projected_upper,
        dip_probability_30d=max(model_probability, end_probability),
        model_dip_probability_30d=model_probability,
        projected_balance_below_buffer_probability=end_probability,
        classifier_fallback=fallback,
        runway_weeks=runway,
        recent_balance_history=history,
        risk_level=_risk_level(max(model_probability, end_probability)),
        risk_signals=row_signals,
        summary="",
        warnings=warnings,
    )
    return apply_upcoming_expense(result, upcoming_expense)
