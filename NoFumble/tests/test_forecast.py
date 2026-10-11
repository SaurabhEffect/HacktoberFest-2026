from __future__ import annotations

import os

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from adapters import empty_invoices
from features import build_weekly_feature_core
from forecast import (
    ForecastUnavailableError,
    apply_upcoming_expense,
    default_buffer_amount,
    run_forecast,
)
from ingestion import load_and_validate_csvs
from sample_data import load_sample_data
from tests.fake_models import fake_model_factory


@pytest.fixture(scope="module")
def sample_core():
    cleaned = load_and_validate_csvs(*load_sample_data())
    assert not isinstance(cleaned, list)
    transactions, invoices = cleaned
    return transactions, invoices, build_weekly_feature_core(transactions, invoices)


def test_forecast_result_shape_and_low_confidence_warning(sample_core) -> None:
    transactions, invoices, core = sample_core
    result = run_forecast(
        transactions,
        invoices,
        buffer_amount=3000,
        model_factory=fake_model_factory,
        core_features=core,
    )
    assert result.as_of_week == core[1].iloc[-1]["week_end"]
    assert result.net_flow_p10 <= result.expected_net_flow_4w <= result.net_flow_p90
    assert result.projected_balance_p10 <= result.projected_balance_4w
    assert 0 <= result.dip_probability_30d <= 1
    assert isinstance(result.recent_balance_history, pd.DataFrame)
    assert {"week_end", "current_balance"} == set(result.recent_balance_history)
    assert result.warnings == []
    assert len(result.risk_signals) <= 4


def test_forecast_refuses_under_26_complete_weeks(sample_core) -> None:
    transactions, invoices, (training, inference) = sample_core
    with pytest.raises(ForecastUnavailableError, match="26 complete weeks"):
        run_forecast(
            transactions,
            invoices,
            model_factory=fake_model_factory,
            core_features=(training.head(25), inference),
        )


def test_forecast_warns_between_26_and_51_weeks(sample_core) -> None:
    transactions, invoices, (training, inference) = sample_core
    result = run_forecast(
        transactions,
        invoices,
        model_factory=fake_model_factory,
        core_features=(training.head(40), inference),
    )
    assert any("fewer than 52" in warning for warning in result.warnings)


def test_buffer_changes_only_the_classifier_and_what_if_never_refits(
    sample_core,
) -> None:
    transactions, invoices, core = sample_core
    calls: list[str] = []

    def factory(task: str, device: str):
        calls.append(task)
        return fake_model_factory(task, device)

    forecast = run_forecast(
        transactions,
        invoices,
        model_factory=factory,
        core_features=core,
    )
    assert calls == ["regression", "classification"]
    changed = apply_upcoming_expense(forecast, 900)
    assert changed.projected_balance_4w == forecast.projected_balance_4w - 900
    assert changed.projected_balance_p10 == forecast.projected_balance_p10 - 900
    assert changed.dip_probability_30d >= forecast.dip_probability_30d
    assert calls == ["regression", "classification"]


def test_single_class_fallback_and_uploaded_buffer_default(sample_core) -> None:
    transactions, invoices, (training, inference) = sample_core
    no_dip = training.copy()
    no_dip["balance_below_buffer_within_30d"] = pd.array(
        [0] * len(no_dip), dtype="Int64"
    )
    result = run_forecast(
        transactions,
        invoices,
        buffer_amount=0,
        model_factory=fake_model_factory,
        core_features=(no_dip, inference),
    )
    assert result.classifier_fallback == "no_dips"
    assert result.model_dip_probability_30d == 0
    assert default_buffer_amount(transactions) > 0
    always_below = run_forecast(
        transactions,
        invoices,
        buffer_amount=100_000,
        model_factory=fake_model_factory,
        core_features=(training, inference),
    )
    assert always_below.classifier_fallback == "always_below_buffer"


def test_empty_invoice_data_is_supported(sample_core) -> None:
    transactions, _, _ = sample_core
    training, inference = build_weekly_feature_core(transactions, empty_invoices())
    assert not training.empty
    assert not inference.empty


def test_streamlit_sample_flow_with_fake_model(monkeypatch) -> None:
    monkeypatch.setenv("NOFUMBLE_TEST_MODEL", "1")
    app = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "app.py"))
    app.run(timeout=60)
    assert not app.exception
    app.button(key="load_sample").click().run(timeout=60)
    assert not app.exception
    app.button(key="run_forecast").click().run(timeout=60)
    assert not app.exception
    assert any("Your cash-flow outlook" in item.value for item in app.markdown)


def test_streamlit_refusal_state(monkeypatch, sample_core) -> None:
    monkeypatch.setenv("NOFUMBLE_TEST_MODEL", "1")
    transactions, invoices, _ = sample_core
    short_transactions = transactions.tail(30)
    first_date = short_transactions["date"].min()
    short_invoices = invoices.loc[invoices["issue_date"] >= first_date]
    app = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "app.py"))
    app.session_state["nf_transactions"] = short_transactions
    app.session_state["nf_invoices"] = short_invoices
    app.session_state["nf_mode"] = "upload"
    app.session_state["nf_buffer"] = 3000.0
    app.run(timeout=60)
    app.button(key="run_forecast").click().run(timeout=60)
    assert not app.exception
    assert any("At least 26 complete weeks" in item.value for item in app.warning)


@pytest.mark.skipif(
    os.getenv("NOFUMBLE_REAL_TABPFN") != "1",
    reason="Set NOFUMBLE_REAL_TABPFN=1 to download/fit real TabPFN weights.",
)
def test_real_tabpfn_forecast_smoke(sample_core) -> None:
    transactions, invoices, core = sample_core
    result = run_forecast(transactions, invoices, device="cpu", core_features=core)
    assert 0 <= result.dip_probability_30d <= 1
