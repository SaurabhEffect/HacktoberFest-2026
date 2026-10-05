"""Robust, explainable expense checks for transaction histories."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from features import periodic_expense_indices

ANOMALY_COLUMNS = [
    "transaction_index",
    "date",
    "description",
    "amount",
    "reason",
    "severity",
]
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def detect_anomalies(transactions: pd.DataFrame) -> pd.DataFrame:
    """Flag likely duplicate, changed recurring, and large one-off expenses."""
    required = {
        "date",
        "description",
        "amount",
        "counterparty",
        "is_recurring",
    }
    missing = sorted(required - set(transactions.columns))
    if missing:
        raise ValueError(
            "Transactions are missing required columns: " + ", ".join(missing)
        )

    frame = transactions.copy().reset_index(drop=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    flags: dict[int, list[tuple[str, str]]] = {}

    def flag(index: int, reason: str, severity: str) -> None:
        flags.setdefault(index, []).append((reason, severity))

    expenses = frame.loc[frame["amount"] < 0]
    ordered = expenses.assign(_index=expenses.index).sort_values("date", kind="stable")
    for _, group in ordered.groupby(
        ["counterparty", "amount"], dropna=True, sort=False
    ):
        seen: list[tuple[pd.Timestamp, int]] = []
        for transaction_date, original_index in group[["date", "_index"]].itertuples(
            index=False, name=None
        ):
            transaction_date = pd.Timestamp(transaction_date)
            original_index = int(original_index)
            if any(
                abs((transaction_date - old_date).total_seconds()) <= 86400
                for old_date, _ in seen
            ):
                flag(
                    original_index,
                    "Likely duplicate: same merchant and amount within one day.",
                    "high",
                )
            seen.append((transaction_date, original_index))

    recurring = expenses.loc[expenses["is_recurring"].astype(bool)]
    for _, group in recurring.groupby("counterparty", dropna=True, sort=False):
        ordered_group = group.sort_values("date")
        prior_amounts: list[float] = []
        for index, transaction in ordered_group.iterrows():
            amount = abs(float(transaction["amount"]))
            if len(prior_amounts) >= 2:
                usual = float(np.median(prior_amounts))
                mad = float(np.median(np.abs(np.asarray(prior_amounts) - usual)))
                deviation = abs(amount - usual)
                threshold = max(6.0 * 1.4826 * mad, usual * 0.5, 5.0)
                if deviation > threshold:
                    flag(
                        int(index),
                        f"Recurring charge changed from a usual ${usual:,.0f}.",
                        "medium" if amount < usual else "high",
                    )
                    prior_amounts = [amount]
                    continue
            prior_amounts.append(amount)

    periodic_indices = set(periodic_expense_indices(frame))
    one_offs = expenses.loc[
        ~expenses["is_recurring"].astype(bool) & ~expenses.index.isin(periodic_indices)
    ]
    magnitudes = one_offs["amount"].abs().to_numpy(dtype=float)
    if len(magnitudes):
        median = float(np.median(magnitudes))
        mad = float(np.median(np.abs(magnitudes - median)))
        threshold = max(median * 4.0, median + 6.0 * 1.4826 * mad, 500.0)
        for index, transaction in one_offs.iterrows():
            amount = abs(float(transaction["amount"]))
            if amount > threshold:
                flag(
                    int(index),
                    f"One-off expense of ${amount:,.0f} is unusually large "
                    f"compared with other non-periodic expenses.",
                    "high",
                )

    records: list[dict[str, object]] = []
    for index, reasons in flags.items():
        reasons = list(dict.fromkeys(reason for reason, _ in reasons))
        severity = max(
            (level for _, level in flags[index]), key=SEVERITY_RANK.__getitem__
        )
        transaction = frame.iloc[index]
        records.append(
            {
                "transaction_index": index,
                "date": transaction["date"],
                "description": transaction["description"],
                "amount": float(transaction["amount"]),
                "reason": " ".join(reasons),
                "severity": severity,
            }
        )
    return pd.DataFrame(records, columns=ANOMALY_COLUMNS).sort_values(
        "transaction_index", ignore_index=True
    )


def evaluate_synthetic_anomalies(
    transactions: pd.DataFrame,
    ground_truth: pd.DataFrame | str | Path,
) -> dict[str, float]:
    """Print precision and recall for synthetic injected anomaly labels."""
    labels = (
        pd.read_csv(ground_truth)
        if isinstance(ground_truth, (str, Path))
        else ground_truth.copy()
    )
    if "transaction_index" not in labels:
        raise ValueError("Ground-truth data must contain transaction_index.")
    expected = set(labels["transaction_index"].astype(int))
    detected = set(detect_anomalies(transactions)["transaction_index"].astype(int))
    true_positive = len(expected & detected)
    precision = true_positive / len(detected) if detected else 0.0
    recall = true_positive / len(expected) if expected else 0.0
    print(
        "Synthetic injected anomalies — "
        f"precision: {precision:.1%}; recall: {recall:.1%} "
        f"({true_positive}/{len(expected)} labels found)."
    )
    return {"precision": precision, "recall": recall}
