"""Rolling-origin evaluation for TabPFN and cash-flow baselines."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import (
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
)
from sklearn.metrics import mean_absolute_error, precision_score, recall_score
from tabpfn.errors import TabPFNError

from features import TARGET_COLUMNS, build_weekly_feature_tables
from ingestion import load_and_validate_csvs
from model import (
    constant_class_predictions,
    fit_and_predict,
    get_tabpfn_model,
    positive_class_probabilities,
)
from sample_data import load_sample_data

REGRESSION_TARGET = TARGET_COLUMNS[0]
CLASSIFICATION_TARGET = TARGET_COLUMNS[1]
TARGET_MATURITY_DAYS = 30


@dataclass
class BacktestResult:
    """DataFrames, chart, and plain-text summary returned by a backtest."""

    regression_metrics: pd.DataFrame
    classification_metrics: pd.DataFrame
    predictions: pd.DataFrame
    comparison_chart: Any
    summary: str


def rolling_origin_splits(
    data: pd.DataFrame,
    initial_train_weeks: int = 52,
    test_window_weeks: int = 4,
    step_weeks: int = 4,
) -> Iterator[tuple[int, pd.Timestamp, pd.DataFrame, pd.DataFrame]]:
    """Yield expanding training sets with mature labels and strictly later tests."""
    if min(initial_train_weeks, test_window_weeks, step_weeks) < 1:
        raise ValueError("Rolling-origin window sizes must be positive integers.")
    if "week_end" not in data:
        raise ValueError("Weekly data must contain a week_end column.")

    ordered = (
        data.sort_values("week_end").drop_duplicates("week_end").reset_index(drop=True)
    )
    ordered["week_end"] = pd.to_datetime(ordered["week_end"])
    complete = ordered.dropna(subset=TARGET_COLUMNS).reset_index(drop=True)
    for fold, test_start in enumerate(
        range(initial_train_weeks, len(complete), step_weeks), start=1
    ):
        test = complete.iloc[test_start : test_start + test_window_weeks].copy()
        if test.empty:
            continue
        cutoff = complete.iloc[test_start - 1]["week_end"]
        earlier = complete.loc[complete["week_end"] <= cutoff]
        mature = earlier.loc[
            earlier["week_end"] + pd.Timedelta(days=TARGET_MATURITY_DAYS) <= cutoff
        ].copy()
        if mature.empty:
            continue
        yield fold, cutoff, mature.reset_index(drop=True), test.reset_index(drop=True)


def _feature_columns(data: pd.DataFrame) -> list[str]:
    columns = [
        column for column in data.columns if column not in [*TARGET_COLUMNS, "week_end"]
    ]
    non_numeric = [
        column
        for column in columns
        if not pd.api.types.is_numeric_dtype(data[column])
        and not pd.api.types.is_bool_dtype(data[column])
    ]
    if non_numeric:
        raise ValueError(
            "Weekly model features must be numeric: " + ", ".join(non_numeric)
        )
    return columns


def _summary(regression: pd.DataFrame, classification: pd.DataFrame) -> str:
    lines: list[str] = []
    if not regression.empty:
        mae = regression.groupby("model")["mae"].mean().sort_values()
        best = str(mae.index[0])
        trailing = mae.get("Trailing average", np.nan)
        if best == "TabPFN" and pd.notna(trailing) and trailing > 0:
            delta = (trailing - mae.iloc[0]) / trailing * 100
            comparison = f"{delta:.1f}% lower MAE than the trailing-average baseline"
        elif best == "TabPFN" and pd.notna(trailing):
            comparison = "lowest mean MAE among the evaluated models"
        else:
            comparison = f"best mean MAE ({mae.iloc[0]:,.0f})"
        tabpfn_mae = mae.get("TabPFN", np.nan)
        trailing_text = f"{trailing:,.0f}" if pd.notna(trailing) else "not available"
        tabpfn_text = f"{tabpfn_mae:,.0f}" if pd.notna(tabpfn_mae) else "not available"
        lines.append(
            f"Regression: TabPFN MAE {tabpfn_text}; trailing-average MAE "
            f"{trailing_text}; {best} has {comparison}."
        )
        coverage = regression.loc[
            regression["model"] == "TabPFN", "interval_coverage_80"
        ]
        if not coverage.empty and coverage.notna().any():
            lines.append(f"TabPFN 80% interval coverage: {coverage.mean():.1%}.")

    if not classification.empty:
        means = classification.groupby("model")[["precision", "recall"]].mean()
        if "TabPFN" in means.index and "Trailing average" in means.index:
            lines.append(
                "Dip detection (precision/recall): TabPFN "
                f"{means.loc['TabPFN', 'precision']:.2f}/{means.loc['TabPFN', 'recall']:.2f}; "
                "trailing average "
                f"{means.loc['Trailing average', 'precision']:.2f}/"
                f"{means.loc['Trailing average', 'recall']:.2f}."
            )
        else:
            lines.append(
                "Dip detection metrics are available in the classification results."
            )
    return "\n".join(lines) if lines else "No rolling-origin folds were available."


def _comparison_chart(regression: pd.DataFrame, classification: pd.DataFrame) -> Any:
    figure, axes = plt.subplots(1, 3, figsize=(14, 4.5), constrained_layout=True)
    if not regression.empty:
        mae = regression.groupby("model")["mae"].mean().sort_index()
        axes[0].bar(
            mae.index,
            mae.values,
            color=["#287271", "#e09f3e", "#577590"][: len(mae)],
        )
        axes[0].set_title("Next-4-week cash-flow MAE")
        axes[0].set_ylabel("MAE (currency units)")
        axes[0].tick_params(axis="x", rotation=25)
        tabpfn_coverage = regression.loc[
            regression["model"] == "TabPFN", "interval_coverage_80"
        ].dropna()
        if not tabpfn_coverage.empty:
            axes[2].bar(
                ["TabPFN 80% interval"],
                [tabpfn_coverage.mean()],
                color="#287271",
            )
            axes[2].axhline(0.8, color="#b23a48", linestyle="--", linewidth=1)
            axes[2].set_ylim(0, 1)
            axes[2].set_ylabel("Observed coverage")
        axes[2].set_title("Predictive interval coverage")
        axes[2].tick_params(axis="x", rotation=20)
    if not classification.empty:
        scores = classification.groupby("model")[["precision", "recall"]].mean()
        scores.plot(kind="bar", ax=axes[1], color=["#e09f3e", "#577590"])
        axes[1].set_title("30-day dip classification")
        axes[1].set_ylabel("Score")
        axes[1].set_ylim(0, 1)
        axes[1].tick_params(axis="x", rotation=25)
        axes[1].legend(loc="best")
    return figure


def run_rolling_origin_backtest(
    weekly_data: pd.DataFrame,
    initial_train_weeks: int = 52,
    test_window_weeks: int = 4,
    step_weeks: int = 4,
    device: str = "auto",
    model_factory: Callable[[str, str], Any] | None = None,
    chart_path: str | Path | None = None,
) -> BacktestResult:
    """Evaluate TabPFN and two baselines using expanding rolling-origin folds."""
    feature_columns = _feature_columns(weekly_data)
    get_model = model_factory or get_tabpfn_model
    regression_rows: list[dict[str, object]] = []
    classification_rows: list[dict[str, object]] = []
    prediction_rows: list[dict[str, object]] = []
    folds = list(
        rolling_origin_splits(
            weekly_data,
            initial_train_weeks=initial_train_weeks,
            test_window_weeks=test_window_weeks,
            step_weeks=step_weeks,
        )
    )
    if not folds:
        raise ValueError(
            "Not enough target-complete weeks for the requested rolling-origin windows."
        )

    for fold, cutoff, train, test in folds:
        if train["week_end"].max() >= test["week_end"].min():
            raise ValueError(
                "Rolling-origin split contains training data at or after test data."
            )
        train_x = train[feature_columns]
        test_x = test[feature_columns]
        train_y_regression = train[REGRESSION_TARGET].astype(float)
        test_y_regression = test[REGRESSION_TARGET].astype(float).to_numpy()
        tabpfn_mean, tabpfn_lower, tabpfn_upper = fit_and_predict(
            "regression",
            train_x,
            train_y_regression,
            test_x,
            device=device,
            model_factory=get_model,
        )
        trailing_weekly_net = (
            test_x["mean_income_4w"] - test_x["mean_expenses_4w"]
        ).to_numpy()
        trailing_predictions = trailing_weekly_net * 4.0
        gradient_regressor = HistGradientBoostingRegressor(
            max_iter=100, max_leaf_nodes=15, random_state=0
        )
        gradient_regressor.fit(train_x, train_y_regression)
        gradient_predictions = gradient_regressor.predict(test_x)
        regression_predictions = {
            "TabPFN": (tabpfn_mean, tabpfn_lower, tabpfn_upper),
            "Trailing average": (trailing_predictions, None, None),
            "Gradient boosting": (gradient_predictions, None, None),
        }
        for name, (predictions, lower, upper) in regression_predictions.items():
            coverage = (
                float(
                    np.mean((test_y_regression >= lower) & (test_y_regression <= upper))
                )
                if lower is not None and upper is not None
                else np.nan
            )
            regression_rows.append(
                {
                    "fold": fold,
                    "cutoff": cutoff,
                    "test_start": test["week_end"].min(),
                    "test_end": test["week_end"].max(),
                    "model": name,
                    "mae": mean_absolute_error(test_y_regression, predictions),
                    "interval_coverage_80": coverage,
                }
            )
            for index, prediction in enumerate(predictions):
                prediction_rows.append(
                    {
                        "fold": fold,
                        "week_end": test.iloc[index]["week_end"],
                        "task": "regression",
                        "model": name,
                        "actual": test_y_regression[index],
                        "prediction": float(prediction),
                        "probability": np.nan,
                        "lower_80": float(lower[index])
                        if lower is not None
                        else np.nan,
                        "upper_80": float(upper[index])
                        if upper is not None
                        else np.nan,
                    }
                )

        train_y_class = train[CLASSIFICATION_TARGET].astype(int)
        test_y_class = test[CLASSIFICATION_TARGET].astype(int).to_numpy()
        recent_rate = float(train_y_class.tail(13).mean())
        trailing_class_probabilities = np.full(len(test), recent_rate, dtype=float)
        trailing_class_predictions = (trailing_class_probabilities >= 0.5).astype(int)

        if train_y_class.nunique() < 2:
            tabpfn_class_predictions, tabpfn_class_probabilities = (
                constant_class_predictions(train_y_class, len(test))
            )
        else:
            (tabpfn_class_probabilities,) = fit_and_predict(
                "classification",
                train_x,
                train_y_class,
                test_x,
                device=device,
                model_factory=get_model,
            )
            tabpfn_class_predictions = (tabpfn_class_probabilities >= 0.5).astype(int)
        if train_y_class.nunique() < 2:
            gradient_class_predictions, _ = constant_class_predictions(
                train_y_class, len(test)
            )
        else:
            gradient_classifier = HistGradientBoostingClassifier(
                max_iter=100, max_leaf_nodes=15, random_state=0
            )
            gradient_classifier.fit(train_x, train_y_class)
            gradient_probabilities = positive_class_probabilities(
                gradient_classifier, test_x
            )
            gradient_class_predictions = (gradient_probabilities >= 0.5).astype(int)
        classification_predictions = {
            "TabPFN": (tabpfn_class_predictions, tabpfn_class_probabilities),
            "Trailing average": (
                trailing_class_predictions,
                trailing_class_probabilities,
            ),
            "Gradient boosting": (
                gradient_class_predictions,
                gradient_probabilities
                if train_y_class.nunique() >= 2
                else gradient_class_predictions.astype(float),
            ),
        }
        for name, (predictions, probabilities) in classification_predictions.items():
            classification_rows.append(
                {
                    "fold": fold,
                    "cutoff": cutoff,
                    "test_start": test["week_end"].min(),
                    "test_end": test["week_end"].max(),
                    "model": name,
                    "precision": precision_score(
                        test_y_class, predictions, zero_division=0
                    ),
                    "recall": recall_score(test_y_class, predictions, zero_division=0),
                }
            )
            for index, prediction in enumerate(predictions):
                prediction_rows.append(
                    {
                        "fold": fold,
                        "week_end": test.iloc[index]["week_end"],
                        "task": "classification",
                        "model": name,
                        "actual": test_y_class[index],
                        "prediction": int(prediction),
                        "probability": float(probabilities[index]),
                        "lower_80": np.nan,
                        "upper_80": np.nan,
                    }
                )
    regression_frame = pd.DataFrame(regression_rows)
    classification_frame = pd.DataFrame(classification_rows)
    predictions_frame = pd.DataFrame(prediction_rows)
    chart = _comparison_chart(regression_frame, classification_frame)
    if chart_path is not None:
        path = Path(chart_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        chart.savefig(path, dpi=150)
    return BacktestResult(
        regression_metrics=regression_frame,
        classification_metrics=classification_frame,
        predictions=predictions_frame,
        comparison_chart=chart,
        summary=_summary(regression_frame, classification_frame),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--device", default="auto", help="TabPFN device: auto, cpu, or cuda"
    )
    parser.add_argument(
        "--chart", type=Path, default=Path("reports/backtest_comparison.png")
    )
    parser.add_argument("--initial-train-weeks", type=int, default=52)
    args = parser.parse_args()
    raw_transactions, raw_invoices = load_sample_data()
    cleaned = load_and_validate_csvs(raw_transactions, raw_invoices)
    if isinstance(cleaned, list):
        print("Sample data validation failed: " + " ".join(cleaned))
        return 1
    training_data, _ = build_weekly_feature_tables(*cleaned)
    try:
        result = run_rolling_origin_backtest(
            training_data,
            initial_train_weeks=args.initial_train_weeks,
            device=args.device,
            chart_path=args.chart,
        )
    except (ImportError, OSError, RuntimeError, TabPFNError, ValueError) as error:
        print(f"Backtest could not complete: {error}")
        return 1
    print(result.summary)
    print(f"Comparison chart saved to {args.chart}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
