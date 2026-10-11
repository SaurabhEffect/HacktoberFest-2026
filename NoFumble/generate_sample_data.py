"""Generate seeded bank transactions and invoices for noFumble."""

from __future__ import annotations

import argparse
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
GROUND_TRUTH_COLUMNS = ["transaction_index", "label", "anomaly_type"]
DEFAULT_AS_OF = pd.Timestamp("2026-10-03")
DEFAULT_BUFFER_AMOUNT = 3000.0
OPENING_BALANCE = 16850.0
INCOME_SCALE = 0.68

CLIENTS = {
    "Northstar Studio": {"payment": "on_time", "low": 1400, "high": 3200},
    "Juniper Health": {"payment": "late", "low": 2800, "high": 5200},
    "Mosaic Works": {"payment": "irregular", "low": 800, "high": 4600},
}


def generate_sample_data(
    seed: int = 42, as_of: pd.Timestamp | str | None = None
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return seeded transactions, invoices, and evaluation-only anomaly labels."""
    rng = np.random.default_rng(seed)
    end_date = pd.Timestamp(DEFAULT_AS_OF if as_of is None else as_of).normalize()
    history_start = end_date - pd.DateOffset(months=33)
    invoice_cutoff = end_date - pd.Timedelta(days=45)
    invoice_start = history_start + pd.Timedelta(days=4)
    invoices: list[dict[str, object]] = []
    anomaly_records: list[dict[str, object]] = []
    income_transactions: list[dict[str, object]] = []
    invoice_months = pd.date_range(
        invoice_start.replace(day=1), invoice_cutoff.replace(day=1), freq="MS"
    )
    invoice_number = 1001
    for month in invoice_months:
        slow_season = month.month in (8, 12)
        for client, behavior in CLIENTS.items():
            attendance = 0.68 if slow_season else 0.94
            if rng.random() > attendance:
                continue
            issue_day = int(rng.integers(3, 19))
            issue_date = month + pd.Timedelta(days=issue_day - 1)
            if issue_date > invoice_cutoff:
                continue
            typical_amount = float(rng.uniform(behavior["low"], behavior["high"]))
            seasonal_factor = float(rng.uniform(0.55, 0.82)) if slow_season else 1.0
            amount = round(typical_amount * seasonal_factor * INCOME_SCALE, 2)
            due_date = issue_date + pd.Timedelta(days=30)
            payment_behavior = behavior["payment"]
            paid_date: pd.Timestamp | pd.NaT
            if payment_behavior == "on_time":
                paid_date = due_date + pd.Timedelta(days=int(rng.integers(-2, 5)))
            elif payment_behavior == "late":
                paid_date = due_date + pd.Timedelta(days=int(rng.integers(15, 41)))
            elif rng.random() < 0.16:
                paid_date = pd.NaT
            else:
                delay = int(rng.integers(-2, 76))
                paid_date = due_date + pd.Timedelta(days=delay)
            if not pd.isna(paid_date) and paid_date > end_date:
                paid_date = pd.NaT
            invoice_id = f"INV-{invoice_number}"
            invoice_number += 1
            project_type = str(
                rng.choice(["Design", "Consulting", "Development", "Retainer"])
            )
            invoices.append(
                {
                    "invoice_id": invoice_id,
                    "client": client,
                    "issue_date": issue_date,
                    "due_date": due_date,
                    "paid_date": paid_date,
                    "amount": amount,
                    "project_type": project_type,
                }
            )
            if not pd.isna(paid_date) and paid_date <= end_date:
                description = str(
                    rng.choice(
                        [
                            f"ACH CREDIT {client.upper()} INV {invoice_id}",
                            f"STRIPE TRANSFER {invoice_id} / {client[:8].upper()}",
                            f"CLIENT PMT REF:{invoice_id} {client.upper()}",
                        ]
                    )
                )
                income_transactions.append(
                    {
                        "date": paid_date,
                        "description": description,
                        "amount": amount,
                        "category": "Income",
                        "counterparty": client,
                        "is_recurring": payment_behavior == "on_time",
                        "balance_after": 0.0,
                        "_invoice_id": invoice_id,
                    }
                )
    expense_transactions: list[dict[str, object]] = []

    def add_expense(
        date: pd.Timestamp,
        description: str,
        amount: float,
        category: str,
        counterparty: str,
        recurring: bool = False,
        anomaly_type: str | None = None,
    ) -> None:
        if date > end_date:
            return
        row: dict[str, object] = {
            "date": date,
            "description": description,
            "amount": -round(amount, 2),
            "category": category,
            "counterparty": counterparty,
            "is_recurring": recurring,
            "balance_after": 0.0,
        }
        expense_transactions.append(row)
        if anomaly_type:
            anomaly_records.append(
                {"_row": row, "label": "anomaly", "anomaly_type": anomaly_type}
            )

    months = pd.date_range(
        history_start.replace(day=1), end_date.replace(day=1), freq="MS"
    )
    software_jump_month = months[int(len(months) * 0.58)]
    duplicate_months = {
        months[int(len(months) * 0.28)],
        months[int(len(months) * 0.76)],
    }
    for month in months:
        rent_day = min(1, (month + pd.offsets.MonthEnd(0)).day)
        add_expense(
            month.replace(day=rent_day),
            "ACH DEBIT / HARBOR LOFT RENT 8F2",
            1650,
            "Housing",
            "Harbor Loft",
            True,
        )
        software_date = month + pd.Timedelta(days=6)
        software_amount = 32.0 if month < software_jump_month else 89.0
        add_expense(
            software_date,
            "CARD PURCHASE CLOUDSUITE*PRO 800-555-0199",
            software_amount,
            "Software",
            "CloudSuite",
            True,
            "subscription_price_jump" if month == software_jump_month else None,
        )
        add_expense(
            month + pd.Timedelta(days=11),
            "AUTOPAY MOBILE / NORTH CELLULAR WEB",
            74.0,
            "Phone",
            "North Cellular",
            True,
        )
        add_expense(
            month + pd.Timedelta(days=2),
            "ONLINE TRANSFER TO PERSONAL CHECKING OWNER DRAW",
            1200,
            "Owner Draw",
            "Owner Transfer",
            True,
        )
        add_expense(
            month + pd.Timedelta(days=14),
            "CITY UTILITIES AUTOPAY WEB PMT",
            200,
            "Living",
            "City Utilities",
            True,
        )
        add_expense(
            month + pd.Timedelta(days=23),
            "TRANSIT PASS AUTO RELOAD METRO",
            150,
            "Living",
            "Metro Transit",
            True,
        )
        if month.month in (3, 6, 9, 12):
            tax_date = month + pd.Timedelta(days=19)
            add_expense(
                tax_date,
                "IRS EFTPS USATAXPYMT CONF 4481",
                float(rng.uniform(2100, 3100)),
                "Taxes",
                "IRS EFTPS",
            )
        if month.month == 2:
            insurance_date = month + pd.Timedelta(days=14)
            add_expense(
                insurance_date,
                "ACH INS PREM / PIONEER MUTUAL POL 77A",
                1180,
                "Insurance",
                "Pioneer Mutual",
            )
        if month in duplicate_months:
            duplicate_date = month + pd.Timedelta(days=22)
            duplicate_amount = float(rng.uniform(45, 125))
            merchant = str(
                rng.choice(["METRO MARKET #184", "RIVER FUEL 0291", "OFFICE DEPOT 441"])
            )
            add_expense(
                duplicate_date,
                f"POS DEBIT {merchant} TERM 09",
                duplicate_amount,
                "Supplies",
                merchant,
            )
            add_expense(
                duplicate_date + pd.Timedelta(hours=2),
                f"POS DEBIT {merchant} TERM 09",
                duplicate_amount,
                "Supplies",
                merchant,
                anomaly_type="duplicate_charge",
            )
    first_grocery_date = history_start.replace(day=1) + pd.Timedelta(days=2)
    for grocery_date in pd.date_range(first_grocery_date, end_date, freq="7D"):
        add_expense(
            grocery_date,
            "POS DEBIT NEIGHBORHOOD MARKET GROCERY",
            150,
            "Living",
            "Neighborhood Market",
        )
    equipment_month = months[int(len(months) * 0.43)]
    equipment_date = equipment_month + pd.Timedelta(days=16)
    add_expense(
        equipment_date,
        "CARD PURCHASE NORTHSTAR COMPUTING / MACBOOK PRO",
        3450,
        "Equipment",
        "Northstar Computing",
        anomaly_type="unusually_large_one_off",
    )
    repair_month = months[int(len(months) * 0.87)]
    add_expense(
        repair_month + pd.Timedelta(days=24),
        "CARD PURCHASE CLEARLINE REPAIR / EMERGENCY WORKSTATION",
        2280,
        "Repairs",
        "Clearline Repair",
        anomaly_type="unusually_large_one_off",
    )
    all_transactions = income_transactions + expense_transactions
    all_transactions.sort(
        key=lambda item: (pd.Timestamp(item["date"]), str(item["description"]))
    )
    running_balance = OPENING_BALANCE
    anomaly_by_identity = {id(record["_row"]): record for record in anomaly_records}
    ground_truth: list[dict[str, object]] = []
    for index, transaction in enumerate(all_transactions):
        running_balance = round(running_balance + float(transaction["amount"]), 2)
        transaction["balance_after"] = running_balance
        anomaly = anomaly_by_identity.get(id(transaction))
        if anomaly:
            ground_truth.append(
                {
                    "transaction_index": index,
                    "label": anomaly["label"],
                    "anomaly_type": anomaly["anomaly_type"],
                }
            )
        transaction.pop("_invoice_id", None)
    transactions_frame = pd.DataFrame(all_transactions, columns=TRANSACTION_COLUMNS)
    invoices_frame = pd.DataFrame(invoices, columns=INVOICE_COLUMNS)
    ground_truth_frame = pd.DataFrame(ground_truth, columns=GROUND_TRUTH_COLUMNS)
    for frame, columns in (
        (transactions_frame, ["date"]),
        (invoices_frame, ["issue_date", "due_date", "paid_date"]),
    ):
        for column in columns:
            frame[column] = (
                pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d").fillna("")
            )

    return transactions_frame, invoices_frame, ground_truth_frame


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed", type=int, default=42, help="Random seed (default: 42)"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).parent / "data"
    )
    parser.add_argument(
        "--as-of",
        type=pd.Timestamp,
        default=DEFAULT_AS_OF,
        help=f"Latest date in the generated history (default: {DEFAULT_AS_OF.date()})",
    )
    args = parser.parse_args()

    transactions, invoices, ground_truth = generate_sample_data(
        seed=args.seed, as_of=args.as_of
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    transactions.to_csv(args.output_dir / "transactions.csv", index=False)
    invoices.to_csv(args.output_dir / "invoices.csv", index=False)
    ground_truth.to_csv(args.output_dir / "anomaly_ground_truth.csv", index=False)
    from features import build_weekly_feature_tables

    training, _ = build_weekly_feature_tables(
        transactions, invoices, buffer_amount=DEFAULT_BUFFER_AMOUNT
    )
    below_buffer = transactions["balance_after"] < DEFAULT_BUFFER_AMOUNT
    dip_episodes = int((below_buffer & ~below_buffer.shift(fill_value=False)).sum())
    dip_rate = float(training["balance_below_buffer_within_30d"].mean())
    monthly_income = transactions.loc[transactions["amount"] > 0].copy()
    monthly_income["month"] = pd.to_datetime(monthly_income["date"]).dt.to_period("M")
    monthly_totals = monthly_income.groupby("month")["amount"].sum()
    volatility = (
        monthly_totals.std(ddof=0) / monthly_totals.mean()
        if len(monthly_totals)
        else 0.0
    )
    print(
        f"Generated {len(transactions):,} transactions and {len(invoices):,} invoices; "
        f"income volatility (monthly CV): {volatility:.2f}; "
        f"labeled anomalies: {len(ground_truth)}."
    )
    print(
        f"Balance min/max: ${transactions['balance_after'].min():,.2f} / "
        f"${transactions['balance_after'].max():,.2f}."
    )
    print(
        f"Dip episodes below ${DEFAULT_BUFFER_AMOUNT:,.0f}: {dip_episodes}; "
        f"training dip-label rate: {dip_rate:.1%}."
    )
    print(
        "Rows per file: "
        f"transactions.csv={len(transactions):,}, "
        f"invoices.csv={len(invoices):,}, "
        f"anomaly_ground_truth.csv={len(ground_truth):,}."
    )


if __name__ == "__main__":
    main()
