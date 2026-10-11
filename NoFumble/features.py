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
    "known_obligation_next_7d": "1 when a quarterly or annual expense is expected in the next seven days based only on history.",
    "expected_committed_outflow_next_28d": "Median amount of all observed periodic expenses expected in the next 28 days.",
    "expected_lumpy_outflow_next_28d": "Median amount of quarterly and annual expenses expected in the next 28 days.",
    "current_balance": "Balance after all transactions dated on or before week_end.",
    "runway_weeks_at_current_burn": "Current nonnegative balance divided by positive four-week average net burn; missing when burn is not positive.",
}


def _week_end(dates: pd.Series) -> pd.Series:
    days_until_sunday = 6 - dates.dt.weekday
    return (dates + pd.to_timedelta(days_until_sunday, unit="D")).dt.normalize()


def infer_periodic_expenses(
    transactions: pd.DataFrame, as_of: pd.Timestamp
) -> pd.DataFrame:
    """Infer known expense cadence, typical amount, and next date from prior records."""
    known = transactions.loc[
        (transactions["date"] <= as_of) & (transactions["amount"] < 0)
    ]
    cadence_options = (
        ("weekly", 7, 1, 0),
        ("monthly", 30, 4, 1),
        ("quarterly", 91, 8, 3),
        ("annual", 365, 15, 12),
    )
    records: list[dict[str, object]] = []
    for counterparty, group in known.groupby("counterparty", dropna=True):
        dated = group.sort_values("date")
        dates = dated["date"].drop_duplicates()
        intervals = dates.diff().dropna().dt.days.to_numpy(dtype=float)
        if len(dates) < 2:
            continue
        median_interval = float(np.median(intervals))
        if not np.all(np.abs(intervals - median_interval) <= 8):
            continue
        cadence = next(
            (
                (name, months)
                for name, days, tolerance, months in cadence_options
                if abs(median_interval - days) <= tolerance
            ),
            None,
        )
        if cadence is None:
            continue
        cadence_name, months = cadence
        last_date = dates.iloc[-1]
        if cadence_name == "weekly":
            next_date = last_date + pd.Timedelta(days=7)
        else:
            next_date = last_date + pd.DateOffset(months=months)
        records.append(
            {
                "counterparty": counterparty,
                "cadence": cadence_name,
                "expected_amount": float(-dated["amount"].median()),
                "next_expected_date": pd.Timestamp(next_date),
                "transaction_indices": tuple(group.index),
            }
        )
    return pd.DataFrame(
        records,
        columns=[
            "counterparty",
            "cadence",
            "expected_amount",
            "next_expected_date",
            "transaction_indices",
        ],
    )


def periodic_expense_indices(transactions: pd.DataFrame) -> pd.Index:
    """Return indices of expenses with a supported periodic cadence."""
    if transactions.empty:
        return pd.Index([])
    schedules = infer_periodic_expenses(transactions, transactions["date"].max())
    indices = {
        index
        for row in schedules.itertuples(index=False)
        for index in row.transaction_indices
    }
    return pd.Index(list(indices))


def _obligations_for_week(
    transactions: pd.DataFrame, week_end: pd.Timestamp
) -> tuple[int, float, float]:
    schedules = infer_periodic_expenses(transactions, week_end)
    if schedules.empty:
        return 0, 0.0, 0.0
    next_7d = schedules.loc[
        (schedules["next_expected_date"] > week_end)
        & (schedules["next_expected_date"] <= week_end + pd.Timedelta(days=7))
        & schedules["cadence"].isin(["quarterly", "annual"])
    ]
    next_28d = schedules.loc[
        (schedules["next_expected_date"] > week_end)
        & (schedules["next_expected_date"] <= week_end + pd.Timedelta(days=28))
    ]
    lumpy = next_28d.loc[next_28d["cadence"].isin(["quarterly", "annual"])]
    return (
        int(not next_7d.empty),
        float(next_28d["expected_amount"].sum()),
        float(lumpy["expected_amount"].sum()),
    )


def _obligations_by_week(
    transactions: pd.DataFrame, week_ends: pd.DatetimeIndex
) -> list[tuple[int, float, float]]:
    """Precompute cadence-based obligation signals for every feature cutoff."""
    cadence_options = (
        ("weekly", 7, 1, 1),
        ("monthly", 30, 4, 1),
        ("quarterly", 91, 8, 3),
        ("annual", 365, 15, 12),
    )
    result = [[0, 0.0, 0.0] for _ in week_ends]
    expenses = transactions.loc[transactions["amount"] < 0]
    for _, group in expenses.groupby("counterparty", dropna=True):
        daily = group.groupby("date", as_index=False, sort=True)["amount"].median()
        dates = daily["date"].to_numpy(dtype="datetime64[ns]")
        amounts = daily["amount"].to_numpy(dtype=float)
        for index, week_end in enumerate(week_ends):
            count = int(np.searchsorted(dates, week_end.to_datetime64(), side="right"))
            if count < 2:
                continue
            history_dates = dates[:count]
            intervals = np.diff(history_dates).astype("timedelta64[D]").astype(float)
            median_interval = float(np.median(intervals))
            if not np.all(np.abs(intervals - median_interval) <= 8):
                continue
            cadence = next(
                (
                    (name, months)
                    for name, days, tolerance, months in cadence_options
                    if abs(median_interval - days) <= tolerance
                ),
                None,
            )
            if cadence is None:
                continue
            cadence_name, months = cadence
            last_date = pd.Timestamp(history_dates[-1])
            next_date = (
                last_date + pd.Timedelta(days=7)
                if cadence_name == "weekly"
                else last_date + pd.DateOffset(months=months)
            )
            if next_date <= week_end or next_date > week_end + pd.Timedelta(days=28):
                continue
            amount = float(-np.median(amounts[:count]))
            result[index][1] += amount
            if cadence_name in ("quarterly", "annual"):
                result[index][2] += amount
                if next_date <= week_end + pd.Timedelta(days=7):
                    result[index][0] = 1
    return [(flag, committed, lumpy) for flag, committed, lumpy in result]


def _known_obligation_in_next_week(
    transactions: pd.DataFrame, week_end: pd.Timestamp
) -> int:
    """Retain the original helper name for callers while using lumpy obligations."""
    return _obligations_for_week(transactions, week_end)[0]


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


def build_weekly_feature_core(
    transactions: pd.DataFrame,
    invoices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build buffer-independent weekly features and regression targets."""
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

    obligation_features = _obligations_by_week(transactions, week_ends)
    rows: list[dict[str, object]] = []
    for week_index, week_end in enumerate(week_ends):
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
        (
            row["known_obligation_next_7d"],
            row["expected_committed_outflow_next_28d"],
            row["expected_lumpy_outflow_next_28d"],
        ) = obligation_features[week_index]
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
            row["next_4w_net_cash_flow"] = float(future["amount"].sum())
            row["balance_below_buffer_within_30d"] = pd.NA
        else:
            row["next_4w_net_cash_flow"] = np.nan
            row["balance_below_buffer_within_30d"] = pd.NA
        rows.append(row)
    result = pd.DataFrame(rows)
    result["balance_below_buffer_within_30d"] = pd.array(
        result["balance_below_buffer_within_30d"], dtype="Int64"
    )
    target_complete = result["next_4w_net_cash_flow"].notna()
    training = result.loc[target_complete].reset_index(drop=True)
    inference = result.loc[~target_complete].reset_index(drop=True)
    return training, inference


def compute_dip_labels(
    transactions: pd.DataFrame, weekly_features: pd.DataFrame, buffer_amount: float
) -> pd.Series:
    """Compute buffer-dependent 30-day labels from a prebuilt balance path."""
    if not np.isfinite(buffer_amount):
        raise ValueError("buffer_amount must be a finite number.")
    if "week_end" not in weekly_features or "current_balance" not in weekly_features:
        raise ValueError("Weekly features must contain week_end and current_balance.")
    if transactions.empty:
        return pd.Series(pd.array([], dtype="Int64"), index=weekly_features.index)

    ordered = transactions.sort_values("date")
    transaction_dates = pd.to_datetime(ordered["date"]).to_numpy(dtype="datetime64[ns]")
    below_buffer = (
        pd.to_numeric(ordered["balance_after"], errors="coerce").to_numpy()
        < buffer_amount
    )
    prefix_count = np.concatenate(([0], np.cumsum(below_buffer, dtype=np.int64)))
    week_ends = pd.to_datetime(weekly_features["week_end"]).to_numpy(
        dtype="datetime64[ns]"
    )
    start = np.searchsorted(transaction_dates, week_ends, side="right")
    end = np.searchsorted(
        transaction_dates, week_ends + np.timedelta64(30, "D"), side="right"
    )
    below_now = (
        pd.to_numeric(weekly_features["current_balance"], errors="coerce").to_numpy()
        < buffer_amount
    )
    labels = below_now | ((prefix_count[end] - prefix_count[start]) > 0)
    return pd.Series(
        pd.array(labels.astype(int), dtype="Int64"), index=weekly_features.index
    )


def build_weekly_feature_tables(
    transactions: pd.DataFrame,
    invoices: pd.DataFrame,
    buffer_amount: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return training and inference weeks with labels for the requested buffer."""
    training, inference = build_weekly_feature_core(transactions, invoices)
    training["balance_below_buffer_within_30d"] = compute_dip_labels(
        transactions, training, buffer_amount
    )
    inference["balance_below_buffer_within_30d"] = pd.array(
        [pd.NA] * len(inference), dtype="Int64"
    )
    return training, inference
