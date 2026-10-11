import pandas as pd

from features import build_weekly_feature_tables
from generate_sample_data import (
    DEFAULT_AS_OF,
    DEFAULT_BUFFER_AMOUNT,
    OPENING_BALANCE,
    generate_sample_data,
)
from sample_data import load_sample_data


def test_generated_data_is_seeded_and_has_required_columns() -> None:
    first = generate_sample_data(seed=17)
    second = generate_sample_data(seed=17, as_of=DEFAULT_AS_OF)
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
    transactions, invoices, _ = generate_sample_data()
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

    expected = OPENING_BALANCE + transactions["amount"].cumsum()
    assert (transactions["balance_after"].round(2) == expected.round(2)).all()


def test_anomaly_labels_point_to_valid_transactions() -> None:
    transactions, invoices, ground_truth = generate_sample_data()
    assert len(ground_truth) == 5
    assert ground_truth["label"].eq("anomaly").all()
    assert ground_truth["transaction_index"].between(0, len(transactions) - 1).all()
    assert ground_truth["transaction_index"].is_unique
    assert ground_truth["anomaly_type"].value_counts().to_dict() == {
        "duplicate_charge": 2,
        "unusually_large_one_off": 2,
        "subscription_price_jump": 1,
    }
    for row in ground_truth.itertuples(index=False):
        transaction = transactions.iloc[row.transaction_index]
        if row.anomaly_type == "duplicate_charge":
            earlier = transactions.iloc[: row.transaction_index]
            assert (
                (earlier["date"] == transaction["date"])
                & (earlier["amount"] == transaction["amount"])
                & (earlier["counterparty"] == transaction["counterparty"])
            ).any()
        elif row.anomaly_type == "subscription_price_jump":
            prior_subscription = transactions.loc[
                (transactions["counterparty"] == transaction["counterparty"])
                & (transactions["date"] < transaction["date"])
                & (transactions["category"] == "Software")
            ]
            assert transaction["amount"] < 0
            assert not prior_subscription.empty
            assert abs(transaction["amount"]) > prior_subscription["amount"].abs().max()
        else:
            assert transaction["category"] in {"Equipment", "Repairs"}
    assert invoices["client"].nunique() == 3


def test_history_spans_at_least_thirty_months() -> None:
    transactions, _, _ = generate_sample_data()
    dates = pd.to_datetime(transactions["date"])
    assert dates.max() <= DEFAULT_AS_OF
    assert (DEFAULT_AS_OF - dates.max()).days <= 7
    assert (dates.max() - dates.min()).days >= 900


def test_default_cashflow_stays_solvent_and_demonstrates_dips() -> None:
    transactions, invoices, _ = generate_sample_data()
    training, _ = build_weekly_feature_tables(
        transactions, invoices, buffer_amount=DEFAULT_BUFFER_AMOUNT
    )
    balance = transactions["balance_after"]
    below_buffer = balance < DEFAULT_BUFFER_AMOUNT
    dip_episodes = int((below_buffer & ~below_buffer.shift(fill_value=False)).sum())
    dip_rate = float(training["balance_below_buffer_within_30d"].mean())

    assert 1500 <= balance.min() <= 2000
    assert balance.max() <= 20000
    assert balance.min() >= 0
    assert dip_episodes >= 3
    assert 0.15 <= dip_rate <= 0.30


def test_app_loader_returns_only_transactions_and_invoices() -> None:
    transactions, invoices = load_sample_data()
    assert len(transactions) > 0
    assert len(invoices) > 0
    assert "anomaly_type" not in transactions.columns
    assert "anomaly_type" not in invoices.columns
