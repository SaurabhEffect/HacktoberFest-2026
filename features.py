"""Leakage-aware weekly cash-flow features and forward targets."""

from __future__ import annotations

import numpy as np
import pandas as pd

TARGET_COLUMNS = [
    "next_4w_net_cash_flow",
    "balance_below_buffer_within_30d",
]
LAG_WEEKS = (1, 2, 4, 8)
ROLLING_WEEKS = (4, 8, 13)

# Each output column has a one-line definition for model and evaluation users.
FEATURE_DESCRIPTIONS = {
    "week_end": "Sunday that closes the feature week and defines its information cutoff.",
    "next_4w_net_cash_flow": "Signed transaction total in the 28 days after week_end; unavailable without full coverage.",
    "balance_below_buffer_within_30d": "1 if balance is or becomes lower than buffer in the next 30 days; otherwise 0.",
    "net_flow_lag_1w": "Net cash flow from the immediately preceding week.",
    "net_flow_lag_2w": "Net cash flow from two weeks before week_end.",
    "net_flow_lag_4w": "Net cash flow from four weeks before week_end.",
    "net_flow_lag_8w": "Net cash flow from eight weeks before week_end.",
    "mean_income_4w": "Mean positive transaction total per week over the latest four weeks, including the current week.",
    "mean_expenses_4w": "Mean expense magnitude per week over the latest four weeks, including the current week.",
    "income_volatility_4w": "Population standard deviation of weekly income over the latest four weeks.",
    "mean_income_8w": "Mean positive transaction total per week over the latest eight weeks, including the current week.",
    "mean_expenses_8w": "Mean expense magnitude per week over the latest eight weeks, including the current week.",
    "income_volatility_8w": "Population standard deviation of weekly income over the latest eight weeks.",
    "mean_income_13w": "Mean positive transaction total per week over the latest thirteen weeks, including the current week.",
    "mean_expenses_13w": "Mean expense magnitude per week over the latest thirteen weeks, including the current week.",
    "income_volatility_13w": "Population standard deviation of weekly income over the latest thirteen weeks.",
    "outstanding_receivables": "Sum of invoices issued by week_end and not yet paid as of week_end.",
    "past_due_receivables": "Outstanding invoice total whose due_date is on or before week_end.",
    "expected_income_this_month": "Open invoice amounts due by month-end after shifting due dates by the client's historical mean days late.",
    "top_client_income_share_13w": "Largest client's share of positive transaction income in the latest thirteen weeks.",
    "active_clients_13w": "Distinct counterparties with positive income in the latest thirteen weeks.",
    "weeks_since_last_payment": "Elapsed weeks since the latest positive transaction available by week_end.",
    "weeks_since_last_invoice": "Elapsed weeks since the latest invoice issue_date available by week_end.",
    "week_of_year": "ISO week number for week_end.",
    "month": "Calendar month number for week_end.",
    "weeks_to_month_end": "Days from week_end to that month's last day, expressed in weeks.",
    "known_obligation_next_7d": "1 when a previously observed recurring expense schedule falls in the next seven days.",
    "current_balance": "Balance after all transactions dated on or before week_end.",
    "runway_weeks_at_current_burn": "Current nonnegative balance divided by positive four-week average net burn; missing when burn is not positive.",
}


def _week_end(dates: pd.Series) -> pd.Series:
    days_until_sunday = 6 - dates.dt.weekday
    return (dates + pd.to_timedelta(days_until_sunday, unit="D")).dt.normalize()


def _known_obligation_in_next_week(
    transactions: pd.DataFrame, week_end: pd.Timestamp
) -> int:
    recurring = transactions.loc[
        (transactions["date"] <= week_end)
        & (transactions["amount"] < 0)
        & transactions["is_recurring"]
    ]
    expected_days: list[int] = []
    for _, group in recurring.groupby("counterparty", dropna=True):
        days = group["date"].dt.day
        if len(days) >= 2:
            expected_days.append(int(days.mode().iloc[0]))

    window_end = week_end + pd.Timedelta(days=7)
    month_starts = {
        week_end.replace(day=1),
        window_end.replace(day=1),
    }
    for month_start in month_starts:
        month_last_day = (month_start + pd.offsets.MonthEnd(0)).day
        for expected_day in expected_days:
            due_date = month_start.replace(day=min(expected_day, month_last_day))
            if week_end < due_date <= window_end:
                return 1
    return 0


def _invoice_features(
    invoices: pd.DataFrame, week_end: pd.Timestamp
) -> tuple[float, float, float]:
    issued = invoices.loc[invoices["issue_date"] <= week_end]
    paid_by_week_end = issued["paid_date"].notna() & (issued["paid_date"] <= week_end)
    historical = issued.loc[paid_by_week_end].copy()
    if historical.empty:
        average_late: dict[str, float] = {}
    else:
        historical["days_late"] = (
            historical["paid_date"] - historical["due_date"]
        ).dt.days
        average_late = historical.groupby("client")["days_late"].mean().to_dict()
    open_invoices = issued.loc[~paid_by_week_end].copy()
    outstanding = float(open_invoices["amount"].sum())
    past_due = float(
        open_invoices.loc[open_invoices["due_date"] <= week_end, "amount"].sum()
    )
    month_end = week_end + pd.offsets.MonthEnd(0)
    expected_income = 0.0
    for invoice in open_invoices.itertuples(index=False):
        average_days_late = average_late.get(invoice.client, 0.0)
        expected_date = invoice.due_date + pd.Timedelta(days=round(average_days_late))
        expected_date = max(expected_date, week_end)
        if expected_date <= month_end:
            expected_income += float(invoice.amount)
    return outstanding, past_due, expected_income


def build_weekly_feature_tables(
    transactions: pd.DataFrame,
    invoices: pd.DataFrame,
    buffer_amount: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return target-complete training weeks and target-incomplete inference weeks."""
    required_transaction_columns = {
        "date",
        "amount",
        "counterparty",
        "is_recurring",
        "balance_after",
    }
    required_invoice_columns = {
        "client",
        "issue_date",
        "due_date",
        "paid_date",
        "amount",
    }
    missing_transactions = sorted(
        required_transaction_columns - set(transactions.columns)
    )
    missing_invoices = sorted(required_invoice_columns - set(invoices.columns))
    if missing_transactions or missing_invoices:
        raise ValueError(
            "Provide cleaned data with the required transaction and invoice columns."
        )
    if not np.isfinite(buffer_amount):
        raise ValueError("buffer_amount must be a finite number.")

    transactions = transactions.copy()
    invoices = invoices.copy()
    transactions["date"] = pd.to_datetime(transactions["date"]).dt.normalize()
    invoices["issue_date"] = pd.to_datetime(invoices["issue_date"]).dt.normalize()
    invoices["due_date"] = pd.to_datetime(invoices["due_date"]).dt.normalize()
    invoices["paid_date"] = pd.to_datetime(invoices["paid_date"]).dt.normalize()
    event_dates = pd.concat(
        [transactions["date"], invoices["issue_date"]], ignore_index=True
    ).dropna()
    if event_dates.empty:
        raise ValueError("At least one dated transaction or invoice is required.")

    first_week = event_dates.min().to_period("W-SUN")
    last_week = event_dates.max().to_period("W-SUN")
    periods = pd.period_range(first_week, last_week, freq="W-SUN")
    week_ends = pd.DatetimeIndex([period.end_time.normalize() for period in periods])
    transactions = transactions.sort_values("date").reset_index(drop=True)
    transactions["week_end"] = _week_end(transactions["date"])
    weekly = (
        transactions.assign(
            income=transactions["amount"].clip(lower=0),
            expenses=-transactions["amount"].clip(upper=0),
        )
        .groupby("week_end")
        .agg(
            net_flow=("amount", "sum"),
            income=("income", "sum"),
            expenses=("expenses", "sum"),
        )
    )
    weekly = weekly.reindex(week_ends, fill_value=0.0)
    current_net_flow = weekly["net_flow"]
    if transactions.empty:
        current_balance = pd.Series(np.nan, index=week_ends, dtype=float)
        transaction_data_end = None
    else:
        first_transaction = transactions.iloc[0]
        opening_balance = float(first_transaction["balance_after"]) - float(
            first_transaction["amount"]
        )
        current_balance = opening_balance + current_net_flow.cumsum()
        transaction_data_end = transactions["date"].max()

    rolling: dict[str, pd.Series] = {}
    for window in ROLLING_WEEKS:
        rolling[f"mean_income_{window}w"] = (
            weekly["income"].rolling(window, min_periods=1).mean()
        )
        rolling[f"mean_expenses_{window}w"] = (
            weekly["expenses"].rolling(window, min_periods=1).mean()
        )
        rolling[f"income_volatility_{window}w"] = (
            weekly["income"].rolling(window, min_periods=1).std(ddof=0)
        )

    rows: list[dict[str, object]] = []
    for week_end in week_ends:
        row: dict[str, object] = {"week_end": week_end}
        for lag in LAG_WEEKS:
            lagged_flow = current_net_flow.shift(lag).loc[week_end]
            row[f"net_flow_lag_{lag}w"] = (
                float(lagged_flow) if pd.notna(lagged_flow) else np.nan
            )
        for name, values in rolling.items():
            row[name] = float(values.loc[week_end])
        outstanding, past_due, expected_income = _invoice_features(invoices, week_end)
        row["outstanding_receivables"] = outstanding
        row["past_due_receivables"] = past_due
        row["expected_income_this_month"] = expected_income
        trailing_start = week_end - pd.Timedelta(weeks=13)
        recent_income = transactions.loc[
            (transactions["date"] > trailing_start)
            & (transactions["date"] <= week_end)
            & (transactions["amount"] > 0)
        ]
        client_income = recent_income.groupby("counterparty")["amount"].sum()
        total_income = float(client_income.sum())
        row["top_client_income_share_13w"] = (
            float(client_income.max() / total_income) if total_income > 0 else 0.0
        )
        row["active_clients_13w"] = int(client_income.index.nunique())
        available_payments = transactions.loc[
            (transactions["date"] <= week_end) & (transactions["amount"] > 0), "date"
        ]
        available_invoices = invoices.loc[
            invoices["issue_date"] <= week_end, "issue_date"
        ]
        row["weeks_since_last_payment"] = (
            (week_end - available_payments.max()).days / 7.0
            if not available_payments.empty
            else np.nan
        )
        row["weeks_since_last_invoice"] = (
            (week_end - available_invoices.max()).days / 7.0
            if not available_invoices.empty
            else np.nan
        )
        row["week_of_year"] = int(week_end.isocalendar().week)
        row["month"] = int(week_end.month)
        month_end = week_end + pd.offsets.MonthEnd(0)
        row["weeks_to_month_end"] = (month_end - week_end).days / 7.0
        row["known_obligation_next_7d"] = _known_obligation_in_next_week(
            transactions, week_end
        )
        balance = float(current_balance.loc[week_end])
        row["current_balance"] = balance
        current_burn = float(rolling["mean_expenses_4w"].loc[week_end]) - float(
            rolling["mean_income_4w"].loc[week_end]
        )
        row["runway_weeks_at_current_burn"] = (
            max(balance, 0.0) / current_burn if current_burn > 0 else np.nan
        )
        target_end = week_end + pd.Timedelta(days=30)
        targets_available = (
            transaction_data_end is not None and target_end <= transaction_data_end
        )
        if targets_available:
            future = transactions.loc[
                (transactions["date"] > week_end)
                & (transactions["date"] <= week_end + pd.Timedelta(days=28))
            ]
            future_dip = transactions.loc[
                (transactions["date"] > week_end)
                & (transactions["date"] <= target_end)
                & (transactions["balance_after"] < buffer_amount)
            ]
            row["next_4w_net_cash_flow"] = float(future["amount"].sum())
            row["balance_below_buffer_within_30d"] = int(
                balance < buffer_amount or not future_dip.empty
            )
        else:
            row["next_4w_net_cash_flow"] = np.nan
            row["balance_below_buffer_within_30d"] = pd.NA
        rows.append(row)
    result = pd.DataFrame(rows)
    result["balance_below_buffer_within_30d"] = pd.array(
        result["balance_below_buffer_within_30d"], dtype="Int64"
    )
    target_complete = result[TARGET_COLUMNS].notna().all(axis=1)
    training = result.loc[target_complete].reset_index(drop=True)
    inference = result.loc[~target_complete].reset_index(drop=True)
    return training, inference
