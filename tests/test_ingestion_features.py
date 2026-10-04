from io import StringIO

import pandas as pd

from features import FEATURE_DESCRIPTIONS, TARGET_COLUMNS, build_weekly_feature_tables
from ingestion import load_and_validate_csvs

TRANSACTIONS_CSV = """date,description,amount,category,counterparty,is_recurring,balance_after
2024-01-03,CLIENT PAYMENT,100,Income,Client A,false,1100
2024-01-05,SOFTWARE,-40,Software,Tools,true,1060
2024-01-10,CLIENT PAYMENT,200,Income,Client A,false,1260
2024-02-01,PHONE,-50,Phone,Telco,true,1210
2024-03-15,CLIENT PAYMENT,900,Income,Client A,false,2110
"""
INVOICES_CSV = """invoice_id,client,issue_date,due_date,paid_date,amount,project_type
INV-1,Client A,2024-01-02,2024-01-10,2024-01-20,100,Design
INV-2,Client A,2024-03-01,2024-03-31,,250,Consulting
"""


def _clean_sample() -> tuple[pd.DataFrame, pd.DataFrame]:
    result = load_and_validate_csvs(StringIO(TRANSACTIONS_CSV), StringIO(INVOICES_CSV))
    assert not isinstance(result, list)
    return result


def test_ingestion_normalizes_types_and_dates() -> None:
    transactions, invoices = _clean_sample()
    assert pd.api.types.is_datetime64_dtype(transactions["date"])
    assert pd.api.types.is_bool_dtype(transactions["is_recurring"])
    assert pd.api.types.is_float_dtype(transactions["amount"])
    assert pd.api.types.is_datetime64_dtype(invoices["paid_date"])
    assert pd.isna(invoices.loc[1, "paid_date"])


def test_ingestion_returns_plain_language_validation_problems() -> None:
    bad_transactions = StringIO(
        "date,description,amount,category,counterparty,is_recurring,balance_after\n"
        "not-a-date,Purchase,20,Software,Shop,maybe,100\n"
        "2024-01-02,Income,-10,Income,Client,false,90\n"
    )
    bad_invoices = StringIO(
        "invoice_id,client,issue_date,due_date,paid_date,amount,project_type\n"
        "INV-1,Client,not-a-date,2024-01-10,,100,Design\n"
        "INV-1,Client,2024-01-02,2024-01-10,,100,Design\n"
    )
    result = load_and_validate_csvs(bad_transactions, bad_invoices)
    assert isinstance(result, list)
    joined = " ".join(result).lower()
    assert "unparseable" in joined
    assert "signs" in joined
    assert "is_recurring" in joined
    assert "duplicate invoice_id" in joined
    assert all(isinstance(problem, str) for problem in result)


def test_ingestion_reports_missing_columns_and_unreadable_files() -> None:
    missing_columns = load_and_validate_csvs(
        StringIO("date,amount\n2024-01-01,3\n"), StringIO(INVOICES_CSV)
    )
    unreadable = load_and_validate_csvs(StringIO(""), StringIO(INVOICES_CSV))
    assert isinstance(missing_columns, list)
    assert any("missing required columns" in problem for problem in missing_columns)
    assert isinstance(unreadable, list)
    assert all(isinstance(problem, str) for problem in unreadable)


def test_weekly_table_separates_complete_targets_from_inference_rows() -> None:
    transactions, invoices = _clean_sample()
    training, inference = build_weekly_feature_tables(
        transactions, invoices, buffer_amount=1000
    )
    combined = pd.concat([training, inference], ignore_index=True)
    assert training[TARGET_COLUMNS].notna().all().all()
    assert inference[TARGET_COLUMNS].isna().all().all()
    assert combined["week_end"].is_unique
    assert len(combined) == len(
        pd.date_range(
            combined["week_end"].min(), combined["week_end"].max(), freq="W-SUN"
        )
    )
    assert "runway_weeks_at_current_burn" in combined


def test_feature_columns_are_documented_one_line_each() -> None:
    transactions, invoices = _clean_sample()
    training, inference = build_weekly_feature_tables(transactions, invoices)
    output_columns = set(training.columns) | set(inference.columns)
    assert output_columns == set(FEATURE_DESCRIPTIONS)
    assert all("\n" not in description for description in FEATURE_DESCRIPTIONS.values())


def test_features_for_week_ignore_all_later_records() -> None:
    transactions, invoices = _clean_sample()
    training, _ = build_weekly_feature_tables(transactions, invoices)
    cutoff = pd.Timestamp("2024-01-07")
    baseline = training.loc[training["week_end"] == cutoff].iloc[0]
    changed_transactions = transactions.copy()
    future_rows = changed_transactions["date"] > cutoff
    changed_transactions.loc[future_rows, "amount"] *= 20
    changed_transactions.loc[future_rows, "counterparty"] = "Future Client"
    changed_invoices = invoices.copy()
    future_invoice_rows = changed_invoices["issue_date"] > cutoff
    changed_invoices.loc[future_invoice_rows, "amount"] *= 20
    changed_invoices.loc[future_invoice_rows, "client"] = "Future Client"
    changed_invoices.loc[changed_invoices["paid_date"] > cutoff, "paid_date"] += (
        pd.Timedelta(days=20)
    )
    changed_training, _ = build_weekly_feature_tables(
        changed_transactions, changed_invoices
    )
    changed = changed_training.loc[changed_training["week_end"] == cutoff].iloc[0]
    feature_columns = [
        column for column in FEATURE_DESCRIPTIONS if column not in TARGET_COLUMNS
    ]
    pd.testing.assert_series_equal(
        baseline[feature_columns], changed[feature_columns], check_names=False
    )
    assert baseline["next_4w_net_cash_flow"] != changed["next_4w_net_cash_flow"]
