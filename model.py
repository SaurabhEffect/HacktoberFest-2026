"""Cached TabPFN estimator factories for regression and classification."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

import numpy as np
import pandas as pd

Task = Literal["regression", "classification"]
TABPFN_VERSION = "9.1.0"
TABPFN_DOCUMENTED_ROW_LIMIT = 1_000_000
TABPFN_UNIVERSAL_FEATURE_LIMIT: int | None = None
REGRESSION_INTERVAL_QUANTILES = (0.1, 0.9)


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
    quantiles: tuple[float, float] = REGRESSION_INTERVAL_QUANTILES,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return mean predictions and the requested TabPFN predictive quantiles."""
    mean = np.asarray(estimator.predict(features, output_type="mean"), dtype=float)
    raw_quantiles = estimator.predict(
        features,
        output_type="quantiles",
        quantiles=list(quantiles),
    )
    if isinstance(raw_quantiles, (list, tuple)):
        lower, upper = (
            np.asarray(values, dtype=float).reshape(-1) for values in raw_quantiles
        )
    else:
        quantile_array = np.asarray(raw_quantiles, dtype=float)
        if quantile_array.shape == (len(features), len(quantiles)):
            lower, upper = quantile_array[:, 0], quantile_array[:, 1]
        else:
            lower, upper = quantile_array[0], quantile_array[1]
    return mean.reshape(-1), lower.reshape(-1), upper.reshape(-1)


def clear_tabpfn_model_cache() -> None:
    """Clear cached estimator objects, primarily for isolated tests."""
    get_tabpfn_model.cache_clear()
