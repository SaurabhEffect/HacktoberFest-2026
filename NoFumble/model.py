"""Cached TabPFN estimator factories for regression and classification."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from threading import RLock
from typing import Any, Literal

import numpy as np
import pandas as pd

Task = Literal["regression", "classification"]
TABPFN_VERSION = "9.1.0"
TABPFN_DOCUMENTED_ROW_LIMIT = 1_000_000
TABPFN_UNIVERSAL_FEATURE_LIMIT: int | None = None
REGRESSION_INTERVAL_QUANTILES = (0.1, 0.9)
TABPFN_MODEL_LOCK = RLock()


class ModelWeightsUnavailableError(RuntimeError):
    """Raised when TabPFN cannot authenticate or load its local checkpoint."""


@lru_cache(maxsize=4)
def get_tabpfn_model(task: Task, device: str = "auto") -> Any:
    """Return one cached TabPFN estimator for the requested task and device."""
    try:
        from tabpfn import TabPFNClassifier, TabPFNRegressor
    except ImportError as error:
        raise RuntimeError(
            "TabPFN is unavailable. Install the pinned project requirements first."
        ) from error
    estimator_class = {
        "regression": TabPFNRegressor,
        "classification": TabPFNClassifier,
    }.get(task)
    if estimator_class is None:
        raise ValueError("task must be 'regression' or 'classification'.")
    return estimator_class(device=device, random_state=0, show_progress_bar=False)


def warm_tabpfn_weights(device: str = "cpu") -> None:
    """Load the shared TabPFN checkpoint using disposable synthetic rows."""
    try:
        from tabpfn.errors import TabPFNError
    except ImportError as error:
        raise ModelWeightsUnavailableError(
            "TabPFN is unavailable. Install the pinned project requirements."
        ) from error
    try:
        features = pd.DataFrame({"warmup_feature": [0.0, 1.0, 2.0, 3.0]})
        target = pd.Series([0.0, 1.0, 0.0, 1.0])
        with TABPFN_MODEL_LOCK:
            get_tabpfn_model("regression", device).fit(features, target)
    except (TabPFNError, OSError, RuntimeError, ValueError) as error:
        raise ModelWeightsUnavailableError(
            "TabPFN weights could not be loaded. Accept the model license and set "
            "TABPFN_TOKEN for headless use."
        ) from error


def fit_tabpfn_regressor(
    features: pd.DataFrame, target: pd.Series, device: str = "auto"
) -> Any:
    """Fit the cached TabPFN regressor without scaling or imputing features."""
    return get_tabpfn_model("regression", device).fit(features, target)


def fit_tabpfn_classifier(
    features: pd.DataFrame, target: pd.Series, device: str = "auto"
) -> Any:
    """Fit the cached TabPFN classifier without scaling or imputing features."""
    return get_tabpfn_model("classification", device).fit(features, target)


def predict_regression_with_interval(
    estimator: Any,
    features: pd.DataFrame,
    quantiles: tuple[float, ...] | list[float] = REGRESSION_INTERVAL_QUANTILES,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return mean and the first/last requested predictive quantiles."""
    requested = list(quantiles)
    if len(requested) < 2 or any(not 0 < value < 1 for value in requested):
        raise ValueError("At least two quantiles between 0 and 1 are required.")
    if requested != sorted(requested):
        raise ValueError("Quantiles must be in ascending order.")

    output = estimator.predict(features, output_type="main", quantiles=requested)
    if isinstance(output, dict):
        mean = np.asarray(output["mean"], dtype=float).reshape(-1)
        raw_quantiles = output["quantiles"]
        lower = np.asarray(raw_quantiles[0], dtype=float).reshape(-1)
        upper = np.asarray(raw_quantiles[-1], dtype=float).reshape(-1)
    else:
        mean = np.asarray(
            estimator.predict(features, output_type="mean"), dtype=float
        ).reshape(-1)
        raw_quantiles = estimator.predict(
            features, output_type="quantiles", quantiles=requested
        )
        if isinstance(raw_quantiles, (list, tuple)):
            lower = np.asarray(raw_quantiles[0], dtype=float).reshape(-1)
            upper = np.asarray(raw_quantiles[-1], dtype=float).reshape(-1)
        else:
            quantile_array = np.asarray(raw_quantiles, dtype=float)
            if quantile_array.shape == (len(features), len(requested)):
                lower, upper = quantile_array[:, 0], quantile_array[:, -1]
            else:
                lower, upper = quantile_array[0], quantile_array[-1]
    if any(len(values) != len(features) for values in (mean, lower, upper)):
        raise ValueError("TabPFN returned predictions with an unexpected shape.")
    return mean, lower, upper


def constant_class_predictions(
    target: pd.Series, size: int
) -> tuple[np.ndarray, np.ndarray]:
    """Return class predictions and positive-class probabilities for one-class data."""
    if target.empty or target.nunique() != 1:
        raise ValueError("A non-empty, single-class target is required.")
    label = int(target.iloc[0])
    return np.full(size, label, dtype=int), np.full(size, float(label), dtype=float)


def positive_class_probabilities(estimator: Any, features: pd.DataFrame) -> np.ndarray:
    """Return probabilities for class 1, even when the fitted class order varies."""
    probabilities = np.asarray(estimator.predict_proba(features), dtype=float)
    classes = list(estimator.classes_)
    if 1 not in classes:
        return np.zeros(len(features), dtype=float)
    return probabilities[:, classes.index(1)]


def fit_and_predict(
    task: Task,
    train_features: pd.DataFrame,
    train_target: pd.Series,
    prediction_features: pd.DataFrame,
    *,
    device: str = "auto",
    model_factory: Callable[[str, str], Any] | None = None,
    quantiles: tuple[float, ...] | list[float] = REGRESSION_INTERVAL_QUANTILES,
) -> tuple[np.ndarray, ...]:
    """Fit a shared TabPFN estimator and predict atomically."""
    factory = model_factory or get_tabpfn_model
    with TABPFN_MODEL_LOCK:
        estimator = factory(task, device)
        estimator.fit(train_features, train_target)
        if task == "regression":
            return predict_regression_with_interval(
                estimator, prediction_features, quantiles
            )
        if task == "classification":
            return (positive_class_probabilities(estimator, prediction_features),)
        raise ValueError("task must be 'regression' or 'classification'.")


def clear_tabpfn_model_cache() -> None:
    """Clear cached estimator objects, primarily for isolated tests."""
    get_tabpfn_model.cache_clear()
