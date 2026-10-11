"""Pure adapters for mapping common bank exports to noFumble's CSV schema."""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from ingestion import INVOICE_COLUMNS, TRANSACTION_COLUMNS

TRANSACTION_ALIASES = {
    "date": (
        "date",
        "transaction date",
        "posted date",
        "posted",
        "posting date",
        "booking date",
    ),
    "description": (
        "description",
        "transaction description",
        "details",
        "memo",
        "narrative",
        "merchant",
    ),
    "amount": ("amount", "transaction amount", "signed amount", "value", "total"),
    "category": ("category", "transaction category", "type"),
    "counterparty": ("counterparty", "payee", "merchant", "recipient", "vendor"),
    "is_recurring": ("is recurring", "recurring", "repeat"),
    "balance_after": ("balance after", "running balance", "balance"),
    "debit": ("debit", "withdrawal", "money out", "outflow"),
    "credit": ("credit", "deposit", "money in", "inflow"),
}
INVOICE_ALIASES = {
    "invoice_id": ("invoice id", "invoice number", "number", "id"),
    "client": ("client", "customer", "bill to"),
    "issue_date": ("issue date", "invoice date", "date issued"),
    "due_date": ("due date", "payment due"),
    "paid_date": ("paid date", "payment date", "date paid"),
    "amount": ("amount", "total", "invoice amount"),
    "project_type": ("project type", "service", "project", "type"),
}


def _normalize_column(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name).casefold()).strip()


def _guess_mapping(
    columns: list[str], aliases: dict[str, tuple[str, ...]]
) -> dict[str, str | None]:
    normalized = {_normalize_column(column): column for column in columns}
    mapping: dict[str, str | None] = {}
    for canonical, names in aliases.items():
        mapping[canonical] = next(
            (
                normalized[normalized_name]
                for alias in names
                if (normalized_name := _normalize_column(alias)) in normalized
            ),
            None,
        )
    return mapping


def guess_transaction_mapping(columns: list[str]) -> dict[str, str | None]:
    """Suggest bank-column mappings without modifying the input data."""
    mapping = _guess_mapping(columns, TRANSACTION_ALIASES)
    if mapping["debit"] or mapping["credit"]:
        mapping["amount"] = None
    return mapping


def guess_invoice_mapping(columns: list[str]) -> dict[str, str | None]:
    """Suggest invoice-column mappings from common exported headings."""
    return _guess_mapping(columns, INVOICE_ALIASES)


def empty_invoices() -> pd.DataFrame:
    """Return a schema-correct empty invoice table."""
    return pd.DataFrame(columns=INVOICE_COLUMNS)


def _parse_amount(values: pd.Series) -> pd.Series:
    text = values.astype("string").str.strip()
    accounting_negative = text.str.match(r"^\(.*\)$", na=False)
    cleaned = text.str.replace(r"[$,]", "", regex=True).str.replace(
        r"^\((.*)\)$", r"\1", regex=True
    )
    amounts = pd.to_numeric(cleaned, errors="coerce")
    return amounts.where(~accounting_negative, -amounts.abs())


def _clean_counterparty(description: object) -> str:
    value = str(description).strip()
    value = re.sub(
        r"\b(?:ACH|POS|CARD|DEBIT|CREDIT|PAYMENT|PURCHASE)\b", " ", value, flags=re.I
    )
    value = re.sub(
        r"\b(?:REF|ID|TERM|CONF|WEB)\b[:#\s-]*[\w-]+", " ", value, flags=re.I
    )
    value = re.sub(r"\b\d[\w-]*\b", " ", value)
    value = re.sub(r"[^A-Za-z&' ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value.title() or "Unknown counterparty"


def _infer_recurring(
    dates: pd.Series, amounts: pd.Series, counterparties: pd.Series
) -> pd.Series:
    recurring = pd.Series(False, index=dates.index, dtype=bool)
    for _, indices in counterparties.groupby(counterparties).groups.items():
        valid_dates = pd.to_datetime(dates.loc[indices], errors="coerce")
        group_amounts = amounts.loc[indices].abs()
        ordered = (
            pd.DataFrame({"date": valid_dates, "amount": group_amounts}, index=indices)
            .dropna()
            .sort_values("date")
        )
        if len(ordered) < 3:
            continue
        intervals = ordered["date"].diff().dropna().dt.days.to_numpy(dtype=float)
        typical_interval = float(np.median(intervals))
        typical_amount = float(ordered["amount"].median())
        similar_amounts = (
            (ordered["amount"] - typical_amount).abs()
            <= max(typical_amount * 0.15, 1.0)
        ).all()
        monthly = 24 <= typical_interval <= 38 and np.all(
            np.abs(intervals - typical_interval) <= 8
        )
        if monthly and similar_amounts:
            recurring.loc[ordered.index] = True
    return recurring


def adapt_transactions(
    raw: pd.DataFrame,
    mapping: dict[str, str | None] | None = None,
    *,
    opening_balance: float = 0.0,
    flip_sign: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """Map a bank export into canonical transactions and report assumptions."""
    if raw.empty:
        raise ValueError("The transactions file is empty. Upload a CSV with rows.")
    selected = mapping or guess_transaction_mapping(list(raw.columns))
    assumptions: list[str] = []

    date_column = selected.get("date")
    if not date_column or date_column not in raw:
        raise ValueError("Select a transaction-date column to continue.")
    dates = pd.to_datetime(raw[date_column], errors="coerce")

    amount_column = selected.get("amount")
    debit_column = selected.get("debit")
    credit_column = selected.get("credit")
    if amount_column and amount_column in raw:
        amounts = _parse_amount(raw[amount_column])
    elif debit_column or credit_column:
        credits = (
            _parse_amount(raw[credit_column]).fillna(0) if credit_column in raw else 0
        )
        debits = (
            _parse_amount(raw[debit_column]).fillna(0) if debit_column in raw else 0
        )
        amounts = credits - debits
        assumptions.append(
            "Signed amounts were built from separate debit and credit columns."
        )
    else:
        raise ValueError("Select an amount column, or map debit and/or credit columns.")
    if flip_sign:
        amounts = -amounts
        assumptions.append("The transaction sign convention was flipped as requested.")

    description_column = selected.get("description")
    counterparty_column = selected.get("counterparty")
    if description_column and description_column in raw:
        descriptions = raw[description_column].fillna("").astype(str).str.strip()
    elif counterparty_column and counterparty_column in raw:
        descriptions = raw[counterparty_column].fillna("").astype(str).str.strip()
        assumptions.append("Descriptions were copied from the counterparty column.")
    else:
        descriptions = pd.Series("Bank transaction", index=raw.index)
        assumptions.append("A generic description was added.")

    if counterparty_column and counterparty_column in raw:
        counterparties = raw[counterparty_column].fillna("").astype(str).str.strip()
        empty = counterparties.eq("")
        counterparties.loc[empty] = descriptions.loc[empty].map(_clean_counterparty)
    else:
        counterparties = descriptions.map(_clean_counterparty)
        assumptions.append(
            "Counterparties were derived from the transaction descriptions."
        )

    category_column = selected.get("category")
    if category_column and category_column in raw:
        categories = raw[category_column].fillna("Other").astype(str).str.strip()
    else:
        categories = pd.Series("Other", index=raw.index)
        assumptions.append(
            "Missing categories were set to Income or Other from the amount sign."
        )
    categories = categories.where(amounts <= 0, "Income")
    categories = categories.mask(
        categories.str.casefold().eq("income") & (amounts < 0), "Other"
    )

    recurring_column = selected.get("is_recurring")
    if recurring_column and recurring_column in raw:
        recurring = raw[recurring_column]
    else:
        recurring = _infer_recurring(dates, amounts, counterparties)
        assumptions.append(
            "Recurring monthly charges were inferred from repeated merchants and similar amounts."
        )

    balance_column = selected.get("balance_after")
    if balance_column and balance_column in raw:
        balance = _parse_amount(raw[balance_column])
    else:
        if not np.isfinite(opening_balance):
            raise ValueError("Opening balance must be a finite amount.")
        order = pd.DataFrame(
            {"date": dates, "amount": amounts}, index=raw.index
        ).sort_values("date", kind="stable")
        balance = opening_balance + order["amount"].cumsum()
        balance = balance.reindex(raw.index)
        assumptions.append(
            f"Balances were reconstructed from the ${opening_balance:,.2f} opening balance."
        )

    canonical = pd.DataFrame(
        {
            "date": dates,
            "description": descriptions,
            "amount": amounts,
            "category": categories,
            "counterparty": counterparties,
            "is_recurring": recurring,
            "balance_after": balance,
        },
        index=raw.index,
    )
    return canonical.loc[:, TRANSACTION_COLUMNS], assumptions


def adapt_invoices(
    raw: pd.DataFrame | None,
    mapping: dict[str, str | None] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Map optional invoice data or return a valid empty invoice table."""
    if raw is None:
        return empty_invoices(), [
            "No invoices were uploaded; receivables signals are unavailable."
        ]
    if raw.empty:
        return empty_invoices(), [
            "The invoice file is empty; receivables signals are unavailable."
        ]
    selected = mapping or guess_invoice_mapping(list(raw.columns))
    missing = [column for column in INVOICE_COLUMNS if not selected.get(column)]
    if missing:
        raise ValueError(
            "The invoice file is missing mapped fields: " + ", ".join(missing) + "."
        )
    invoices = pd.DataFrame(
        {
            canonical: raw[source]
            for canonical, source in selected.items()
            if canonical in INVOICE_COLUMNS
        },
        index=raw.index,
    )
    invoices["paid_date"] = invoices["paid_date"].fillna("")
    return invoices.loc[:, INVOICE_COLUMNS], []
