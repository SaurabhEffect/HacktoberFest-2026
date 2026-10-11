"""Read and validate transaction and invoice CSV inputs."""

from __future__ import annotations

from io import IOBase
from pathlib import Path

import numpy as np
import pandas as pd

TRANSACTION_COLUMNS = [
    "date",
    "description",
    "amount",
    "category",
    "counterparty",
    "is_recurring",
    "balance_after",
]
INVOICE_COLUMNS = [
    "invoice_id",
    "client",
    "issue_date",
    "due_date",
    "paid_date",
    "amount",
    "project_type",
]
CSVSource = str | Path | IOBase | pd.DataFrame
CleanData = tuple[pd.DataFrame, pd.DataFrame]


def _read_csv(
    source: CSVSource, name: str, problems: list[str], invoice_file: bool = False
) -> pd.DataFrame | None:
    try:
        if isinstance(source, pd.DataFrame):
            return source.copy()
        dtype = {"invoice_id": "string"} if invoice_file else None
        return pd.read_csv(source, dtype=dtype)
    except Exception:  # noqa: BLE001
        problems.append(
            f"Could not read the {name} CSV; check that it is a valid CSV file."
        )
        return None


def _normalize_dates(
    frame: pd.DataFrame,
    columns: list[str],
    name: str,
    problems: list[str],
) -> None:
    for column in columns:
        original = frame[column]
        blank = original.isna() | original.astype("string").str.strip().eq("")
        parsed = (
            pd.to_datetime(original, errors="coerce", format="mixed", utc=True)
            .dt.tz_convert(None)
            .dt.normalize()
        )
        invalid = parsed.isna() & (~blank if column == "paid_date" else True)
        if invalid.any():
            problems.append(
                f"The {name} CSV has {int(invalid.sum())} unparseable value(s) in {column}."
            )
        frame[column] = parsed


def _normalize_amount(frame: pd.DataFrame, name: str, problems: list[str]) -> None:
    amount = pd.to_numeric(frame["amount"], errors="coerce")
    invalid = amount.isna() | (amount.notna() & ~np.isfinite(amount))
    if invalid.any():
        problems.append(
            f"The {name} CSV has {int(invalid.sum())} missing or invalid amount value(s)."
        )
    frame["amount"] = amount.astype(float)


def load_and_validate_csvs(
    transactions_csv: CSVSource, invoices_csv: CSVSource
) -> CleanData | list[str]:
    """Return normalized transaction/invoice frames, or plain-language problems."""
    problems: list[str] = []
    transactions = _read_csv(transactions_csv, "transactions", problems)
    invoices = _read_csv(invoices_csv, "invoices", problems, invoice_file=True)
    if transactions is None or invoices is None:
        return problems
    missing_transactions = [
        column for column in TRANSACTION_COLUMNS if column not in transactions.columns
    ]
    missing_invoices = [
        column for column in INVOICE_COLUMNS if column not in invoices.columns
    ]
    if missing_transactions:
        problems.append(
            "Transactions CSV is missing required columns: "
            + ", ".join(missing_transactions)
            + "."
        )
    if missing_invoices:
        problems.append(
            "Invoices CSV is missing required columns: "
            + ", ".join(missing_invoices)
            + "."
        )
    if problems:
        return problems

    try:
        transactions = transactions.loc[:, TRANSACTION_COLUMNS].copy()
        invoices = invoices.loc[:, INVOICE_COLUMNS].copy()
        _normalize_dates(transactions, ["date"], "transactions", problems)
        _normalize_dates(
            invoices, ["issue_date", "due_date", "paid_date"], "invoices", problems
        )
        _normalize_amount(transactions, "transactions", problems)
        _normalize_amount(invoices, "invoices", problems)
        balance = pd.to_numeric(transactions["balance_after"], errors="coerce")
        invalid_balance = balance.isna() | (balance.notna() & ~np.isfinite(balance))
        if invalid_balance.any():
            problems.append(
                "The transactions CSV has "
                f"{int(invalid_balance.sum())} missing or invalid balance_after value(s)."
            )
        transactions["balance_after"] = balance.astype(float)
        for column in ["description", "category", "counterparty"]:
            transactions[column] = transactions[column].astype("string").str.strip()
        for column in ["invoice_id", "client", "project_type"]:
            invoices[column] = invoices[column].astype("string").str.strip()
        recurring_text = (
            transactions["is_recurring"].astype("string").str.strip().str.lower()
        )
        recurring_values = recurring_text.map(
            {
                "true": True,
                "1": True,
                "yes": True,
                "y": True,
                "false": False,
                "0": False,
                "no": False,
                "n": False,
            }
        )
        if recurring_values.isna().any():
            problems.append(
                "The transactions CSV has missing or invalid is_recurring value(s); "
                "use true/false, yes/no, or 1/0."
            )
        else:
            transactions["is_recurring"] = recurring_values.astype(bool)
        for frame, columns, name in (
            (
                transactions,
                ["description", "category", "counterparty"],
                "Transactions",
            ),
            (invoices, ["invoice_id", "client", "project_type"], "Invoices"),
        ):
            for column in columns:
                blank = frame[column].isna() | frame[column].eq("")
                if blank.any():
                    problems.append(
                        f"{name} CSV has {int(blank.sum())} blank value(s) in {column}."
                    )

        if (invoices["amount"].notna() & (invoices["amount"] <= 0)).any():
            problems.append("Invoice amounts must be positive numbers.")
        income = transactions["category"].str.casefold().eq("income")
        amount_valid = transactions["amount"].notna()
        confused_sign = amount_valid & (
            (income & (transactions["amount"] <= 0))
            | (~income & (transactions["amount"] >= 0))
        )
        if confused_sign.any():
            problems.append(
                "Transaction signs do not match category: income must be positive and "
                "expenses must be negative."
            )

        duplicates = invoices["invoice_id"].duplicated(keep=False)
        if duplicates.any():
            duplicate_ids = sorted(
                invoices.loc[duplicates, "invoice_id"].unique().tolist()
            )
            problems.append(
                "Invoices CSV contains duplicate invoice_id value(s): "
                + ", ".join(duplicate_ids)
                + "."
            )
    except Exception:  # noqa: BLE001
        return [
            "Could not validate the CSV data; check that both files use the expected columns and formats."
        ]
    if problems:
        return problems
    return transactions, invoices
