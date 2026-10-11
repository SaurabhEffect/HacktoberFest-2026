from __future__ import annotations

import pandas as pd
import pytest

from adapters import adapt_transactions, guess_transaction_mapping
from anomalies import detect_anomalies, evaluate_synthetic_anomalies
from forecast import run_forecast
from generate_sample_data import generate_sample_data
from ingestion import load_and_validate_csvs
from tests.fake_models import fake_model_factory


@pytest.mark.parametrize("seed", [7, 19, 42])
def test_synthetic_injected_anomaly_detection(seed: int) -> None:
    transactions, _, ground_truth = generate_sample_data(seed=seed)
    cleaned = load_and_validate_csvs(transactions, generate_sample_data(seed=seed)[1])
    assert not isinstance(cleaned, list)
    detected = detect_anomalies(cleaned[0])
    expected = set(ground_truth["transaction_index"])
    actual = set(detected["transaction_index"])
    assert len(expected & actual) >= 4, f"Missed labels: {sorted(expected - actual)}"
    assert len(actual - expected) <= 5, f"False positives: {sorted(actual - expected)}"


def test_anomaly_evaluation_reports_synthetic_caveat(capsys) -> None:
    transactions, _, ground_truth = generate_sample_data()
    metrics = evaluate_synthetic_anomalies(transactions, ground_truth)
    assert metrics["recall"] >= 0.8
    assert "Synthetic injected anomalies" in capsys.readouterr().out


def test_messy_debit_credit_export_flows_through_forecast() -> None:
    transactions, invoices, _ = generate_sample_data()
    export = pd.DataFrame(
        {
            "Posted": transactions["date"],
            "Memo": transactions["description"],
            "Money Out": (-transactions["amount"]).clip(lower=0),
            "Money In": transactions["amount"].clip(lower=0),
        }
    )
    mapping = guess_transaction_mapping(list(export.columns))
    canonical, assumptions = adapt_transactions(export, mapping, opening_balance=16850)
    assert mapping["date"] == "Posted"
    assert mapping["debit"] == "Money Out"
    assert mapping["credit"] == "Money In"
    assert any("Balances were reconstructed" in item for item in assumptions)
    cleaned = load_and_validate_csvs(canonical, invoices)
    assert not isinstance(cleaned, list)
    result = run_forecast(
        *cleaned,
        model_factory=fake_model_factory,
    )
    assert result.as_of_week is not None


def test_adapter_handles_sign_flip_and_missing_date() -> None:
    export = pd.DataFrame({"Date": ["2025-01-01"], "Amount": [-30]})
    mapping = guess_transaction_mapping(list(export.columns))
    flipped, _ = adapt_transactions(export, mapping, flip_sign=True)
    assert flipped.iloc[0]["amount"] == 30
    export.loc[0, "Date"] = "not a date"
    invalid, _ = adapt_transactions(export, mapping)
    assert pd.isna(invalid.iloc[0]["date"])


def test_adapter_parses_currency_and_accounting_negative() -> None:
    export = pd.DataFrame(
        {
            "Date": ["2025-01-01", "2025-01-02"],
            "Amount": ["$1,250.50", "($32.75)"],
        }
    )
    adapted, _ = adapt_transactions(
        export,
        guess_transaction_mapping(list(export.columns)),
    )
    assert adapted["amount"].tolist() == [1250.5, -32.75]


def test_adapter_rejects_empty_transactions() -> None:
    with pytest.raises(ValueError, match="file is empty"):
        adapt_transactions(pd.DataFrame(columns=["Date", "Amount"]))
