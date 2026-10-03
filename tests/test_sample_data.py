import pandas as pd

from generate_sample_data import generate_sample_data
from sample_data import load_sample_data


def test_generated_data_is_seeded_and_has_required_columns() -> None:
    first = generate_sample_data(seed=17, as_of=pd.Timestamp("2026-10-03"))
    second = generate_sample_data(seed=17, as_of=pd.Timestamp("2026-10-03"))
    assert first[0].equals(second[0])
    assert first[1].equals(second[1])
    assert first[2].equals(second[2])
    assert list(first[0].columns) == [
        "date",
        "description",
        "amount",
        "category",
        "counterparty",
        "is_recurring",
        "balance_after",
    ]
    assert list(first[1].columns) == [
        "invoice_id",
        "client",
        "issue_date",
        "due_date",
        "paid_date",
        "amount",
        "project_type",
    ]


def test_income_reconciles_and_balances_are_cumulative() -> None:
    transactions, invoices, _ = generate_sample_data(as_of=pd.Timestamp("2026-10-03"))
    income = transactions.loc[transactions["amount"] > 0]
    paid_invoices = invoices.loc[invoices["paid_date"] != ""]
    assert len(income) == len(paid_invoices)
    assert set(income["counterparty"]) == set(invoices["client"])
    for _, transaction in income.iterrows():
        matching = paid_invoices.loc[
            (paid_invoices["client"] == transaction["counterparty"])
            & (paid_invoices["paid_date"] == transaction["date"])
            & (paid_invoices["amount"] == transaction["amount"])
        ]
        assert not matching.empty

    expected = 8200 + transactions["amount"].cumsum()
    assert (transactions["balance_after"].round(2) == expected.round(2)).all()


def test_anomaly_labels_point_to_valid_transactions() -> None:
    transactions, invoices, ground_truth = generate_sample_data(as_of=pd.Timestamp("2026-10-03"))
    assert 5 <= len(ground_truth) <= 8
    assert ground_truth["transaction_index"].between(0, len(transactions) - 1).all()
    assert set(ground_truth["anomaly_type"]) >= {
        "duplicate_charge", "subscription_price_jump", "unusually_large_one_off"
    }
    assert (invoices["client"].nunique() == 3)


def test_history_spans_at_least_thirty_months() -> None:
    transactions, _, _ = generate_sample_data(as_of=pd.Timestamp("2026-10-03"))
    dates = pd.to_datetime(transactions["date"])
    assert dates.max() <= pd.Timestamp("2026-10-03")
    assert (pd.Timestamp("2026-10-03") - dates.max()).days <= 7
    assert (dates.max() - dates.min()).days >= 900


def test_app_loader_returns_only_transactions_and_invoices() -> None:
    transactions, invoices = load_sample_data()
    assert len(transactions) > 0
    assert len(invoices) > 0
    assert "anomaly_type" not in transactions.columns
    assert "anomaly_type" not in invoices.columns