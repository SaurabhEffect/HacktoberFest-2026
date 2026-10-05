"""Small deterministic estimators for UI and forecast tests."""

from __future__ import annotations

import numpy as np
import pandas as pd


def fake_model_factory(task: str, device: str):
    """Create a mean-predicting estimator with a scikit-learn-like interface."""
    del task, device

    class FakeModel:
        def fit(self, features: pd.DataFrame, target: pd.Series):
            del features
            self.value = float(target.mean())
            self.classes_ = np.unique(target)
            return self

        def predict(self, features, output_type="mean", quantiles=None):
            values = np.full(len(features), self.value, dtype=float)
            if output_type == "main":
                requested = quantiles or [0.1, 0.9]
                width = 100.0
                return {
                    "mean": values,
                    "quantiles": [
                        values + (quantile - 0.5) * 2 * width for quantile in requested
                    ],
                }
            if output_type == "quantiles":
                return [values + (quantile - 0.5) * 200.0 for quantile in quantiles]
            return values

        def predict_proba(self, features: pd.DataFrame):
            positive = np.full(
                len(features), min(max(self.value, 0.0), 1.0), dtype=float
            )
            if list(self.classes_) == [0]:
                return np.ones((len(features), 1), dtype=float)
            if list(self.classes_) == [1]:
                return np.ones((len(features), 1), dtype=float)
            return np.column_stack([1.0 - positive, positive])

    return FakeModel()
