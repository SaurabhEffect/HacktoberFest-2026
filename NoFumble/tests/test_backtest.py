import numpy as np
import pandas as pd
import pytest

from backtest import rolling_origin_splits, run_rolling_origin_backtest
from features import build_weekly_feature_tables
from ingestion import load_and_validate_csvs
from model import (
    clear_tabpfn_model_cache,
    fit_and_predict,
    get_tabpfn_model,
    predict_regression_with_interval,
)
from sample_data import load_sample_data


def _fake_model_factory(task: str, device: str):
    del device

    class FakeModel:
        def fit(self, features: pd.DataFrame, target: pd.Series):
            self.value = float(target.mean())
            self.classes_ = np.unique(target)
            return self

        def predict(
            self, features: pd.DataFrame, output_type: str = "mean", quantiles=None
        ):
            values = np.full(len(features), self.value)
            if output_type == "quantiles":
                return [values - 100.0, values + 100.0]
            return values

        def predict_proba(self, features: pd.DataFrame):
            positive = np.full(len(features), min(max(self.value, 0.0), 1.0))
            return np.column_stack([1.0 - positive, positive])

    return FakeModel()


def _weekly_sample() -> pd.DataFrame:
    raw = load_sample_data()
    cleaned = load_and_validate_csvs(*raw)
    assert not isinstance(cleaned, list)
    training, _ = build_weekly_feature_tables(*cleaned, buffer_amount=1000)
    return training


def test_rolling_origin_splits_keep_training_before_test_and_labels_mature() -> None:
    data = _weekly_sample()
    splits = list(
        rolling_origin_splits(
            data, initial_train_weeks=60, test_window_weeks=4, step_weeks=4
        )
    )
    assert splits
    for _, cutoff, train, test in splits:
        assert train["week_end"].max() < test["week_end"].min()
        assert cutoff < test["week_end"].min()
        assert (train["week_end"] + pd.Timedelta(days=30) <= cutoff).all()
        assert (train["week_end"] <= cutoff).all()


def test_small_end_to_end_backtest_on_sample_data(tmp_path) -> None:
    data = _weekly_sample()
    chart_path = tmp_path / "comparison.png"
    result = run_rolling_origin_backtest(
        data,
        initial_train_weeks=60,
        test_window_weeks=4,
        step_weeks=4,
        model_factory=_fake_model_factory,
        chart_path=chart_path,
    )
    assert not result.regression_metrics.empty
    assert not result.classification_metrics.empty
    assert set(result.regression_metrics["model"]) == {
        "TabPFN",
        "Trailing average",
        "Gradient boosting",
    }
    assert set(result.classification_metrics["model"]) == {
        "TabPFN",
        "Trailing average",
        "Gradient boosting",
    }
    assert result.predictions["fold"].nunique() > 0
    assert result.comparison_chart is not None
    assert chart_path.is_file()
    assert "Regression:" in result.summary


def test_split_rejects_invalid_window_sizes() -> None:
    with pytest.raises(ValueError, match="positive integers"):
        list(rolling_origin_splits(pd.DataFrame(), initial_train_weeks=0))


def test_tabpfn_models_are_cached_by_task_and_device() -> None:
    clear_tabpfn_model_cache()
    first = get_tabpfn_model("regression", "cpu")
    second = get_tabpfn_model("regression", "cpu")
    classifier = get_tabpfn_model("classification", "cpu")
    assert first is second
    assert first is not classifier
    clear_tabpfn_model_cache()


def test_regression_wrapper_returns_mean_and_requested_interval() -> None:
    class QuantileModel:
        def predict(self, features, output_type, quantiles=None):
            assert output_type == "main"
            assert quantiles == [0.1, 0.9]
            return {
                "mean": np.array([10.0, 20.0]),
                "quantiles": [
                    np.array([2.0, 12.0]),
                    np.array([18.0, 28.0]),
                ],
            }

    mean, lower, upper = predict_regression_with_interval(
        QuantileModel(), pd.DataFrame({"feature": [1.0, 2.0]})
    )
    np.testing.assert_array_equal(mean, [10.0, 20.0])
    np.testing.assert_array_equal(lower, [2.0, 12.0])
    np.testing.assert_array_equal(upper, [18.0, 28.0])


def test_regression_wrapper_uses_a_single_main_prediction_call() -> None:
    class OneCallModel:
        calls = 0

        def predict(self, features, output_type, quantiles):
            self.calls += 1
            assert output_type == "main"
            return {
                "mean": np.array([3.0]),
                "quantiles": [np.array([1.0]), np.array([5.0])],
            }

    model = OneCallModel()
    predict_regression_with_interval(model, pd.DataFrame({"x": [1]}))
    assert model.calls == 1


def test_fit_and_predict_holds_the_shared_lock(monkeypatch) -> None:
    from importlib import import_module

    model_module = import_module("model")
    events: list[str] = []

    class RecordingLock:
        def __enter__(self):
            events.append("lock-enter")

        def __exit__(self, exc_type, exc, traceback):
            events.append("lock-exit")

    class Estimator:
        def fit(self, features, target):
            events.append("fit")
            self.value = float(target.mean())
            return self

        def predict(self, features, output_type, quantiles=None):
            events.append("predict")
            values = np.full(len(features), self.value)
            return {
                "mean": values,
                "quantiles": [values - 1, values + 1],
            }

    monkeypatch.setattr(model_module, "TABPFN_MODEL_LOCK", RecordingLock())
    predictions = fit_and_predict(
        "regression",
        pd.DataFrame({"feature": [1.0]}),
        pd.Series([2.0]),
        pd.DataFrame({"feature": [2.0]}),
        model_factory=lambda task, device: Estimator(),
    )
    assert events == ["lock-enter", "fit", "predict", "lock-exit"]
    assert predictions[0].tolist() == [2.0]
