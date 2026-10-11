"""Print reproducibility, feature, backtest, buffer, and timing diagnostics."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT))
SAMPLE_FILES = (
    "transactions.csv",
    "invoices.csv",
    "anomaly_ground_truth.csv",
)


def check_determinism() -> None:
    """Regenerate twice with generator defaults and compare exact CSV bytes."""
    generator = ROOT / "generate_sample_data.py"
    with tempfile.TemporaryDirectory(prefix="nofumble-sample-check-") as temporary:
        root = Path(temporary)
        output_dirs = [root / "first", root / "second"]
        for output_dir in output_dirs:
            subprocess.run(
                [
                    sys.executable,
                    str(generator),
                    "--output-dir",
                    str(output_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        print("1. Determinism (default seed/as-of):")
        for filename in SAMPLE_FILES:
            first = (output_dirs[0] / filename).read_bytes()
            second = (output_dirs[1] / filename).read_bytes()
            committed = (DATA_DIR / filename).read_bytes()
            matches = first == second == committed
            print(f"   {filename}: {'PASS' if matches else 'FAIL'}")
            if not matches:
                raise AssertionError(
                    f"{filename} differs between default regenerations or committed data"
                )


def print_obligation_frequencies(
    transactions: pd.DataFrame, training_weeks: pd.Series
) -> None:
    """Count weeks each known obligation group is forecast in the next seven days."""
    from features import _known_obligation_in_next_week

    groups = {
        "monthly recurring bills": transactions["is_recurring"],
        "weekly groceries": transactions["counterparty"].eq("Neighborhood Market"),
        "quarterly taxes": transactions["counterparty"].eq("IRS EFTPS"),
        "annual insurance": transactions["counterparty"].eq("Pioneer Mutual"),
    }
    counts = {
        name: sum(
            _known_obligation_in_next_week(transactions.loc[mask], week_end)
            for week_end in training_weeks
        )
        for name, mask in groups.items()
    }
    print("   obligation-type weeks flagged:")
    for name, count in sorted(counts.items(), key=lambda item: item[1], reverse=True):
        print(f"     {name}: {count}")


def main() -> None:
    from backtest import rolling_origin_splits
    from features import build_weekly_feature_tables
    from generate_sample_data import DEFAULT_BUFFER_AMOUNT
    from ingestion import load_and_validate_csvs
    from sample_data import load_sample_data

    check_determinism()

    loaded = load_and_validate_csvs(*load_sample_data())
    if isinstance(loaded, list):
        raise ValueError("Committed sample data failed validation: " + " ".join(loaded))
    transactions, invoices = loaded

    feature_start = time.perf_counter()
    training, _ = build_weekly_feature_tables(
        transactions, invoices, buffer_amount=DEFAULT_BUFFER_AMOUNT
    )
    feature_seconds = time.perf_counter() - feature_start

    print("\n2. Feature health:")
    obligation_mean = float(training["known_obligation_next_7d"].mean())
    print(f"   known_obligation_next_7d mean (training): {obligation_mean:.3f}")
    if obligation_mean > 0.8 or obligation_mean < 0.1:
        print("   assessment: nearly constant; carries little signal at this cadence")
    else:
        print("   assessment: within the requested signal range")
    print_obligation_frequencies(transactions, training["week_end"])
    for window in (8, 13):
        volatility = training[f"income_volatility_{window}w"]
        print(
            f"   income_volatility_{window}w: mean={volatility.mean():,.2f}, "
            f"std={volatility.std(ddof=0):,.2f}"
        )
    print(f"   feature-build time (default buffer): {feature_seconds:.3f}s")

    print("\n3. Backtest realism (buffer $3,000, initial_train_weeks=52):")
    folds = list(
        rolling_origin_splits(
            training,
            initial_train_weeks=52,
            test_window_weeks=4,
            step_weeks=4,
        )
    )
    if not folds:
        raise ValueError("No rolling-origin folds are available for the sample data")
    initial_52_weeks = training.iloc[:52]
    first_train = folds[0][2]
    test_region = pd.concat([test for _, _, _, test in folds], ignore_index=True)
    print(
        "   nominal initial 52-week window: "
        f"{int(initial_52_weeks['balance_below_buffer_within_30d'].sum())}/52 positive"
    )
    print(
        "   first-fold matured training window: "
        f"{int(first_train['balance_below_buffer_within_30d'].sum())}/"
        f"{len(first_train)} positive"
    )
    print(
        "   test region: "
        f"{int(test_region['balance_below_buffer_within_30d'].sum())}/"
        f"{len(test_region)} positive"
    )
    folds_with_positive_test = 0
    for fold, cutoff, train_fold, test_fold in folds:
        positives = int(test_fold["balance_below_buffer_within_30d"].sum())
        folds_with_positive_test += positives > 0
        print(
            f"   fold {fold:02d} (cutoff {cutoff.date()}): "
            f"{positives}/{len(test_fold)} positive test weeks; "
            f"{len(train_fold)} matured train weeks"
        )
    print(
        "   folds with at least one positive test week: "
        f"{folds_with_positive_test}/{len(folds)}"
    )
    first_year_rate = float(training.head(52)["balance_below_buffer_within_30d"].mean())
    last_year_rate = float(training.tail(52)["balance_below_buffer_within_30d"].mean())
    print(
        f"   first-year dip rate: {first_year_rate:.1%}; "
        f"last-year dip rate: {last_year_rate:.1%}"
    )

    print("\n4. Buffer alignment:")
    excluded_categories = {"Taxes", "Equipment", "Repairs", "Insurance"}
    ordinary_expenses = transactions.loc[
        (transactions["amount"] < 0)
        & ~transactions["category"].isin(excluded_categories)
    ].copy()
    ordinary_expenses["month"] = ordinary_expenses["date"].dt.to_period("M")
    monthly_expenses = -ordinary_expenses.groupby("month")["amount"].sum()
    typical_monthly_expenses = float(monthly_expenses.median())
    print(
        "   typical monthly expenses (median; excludes taxes, insurance, "
        f"equipment, and repairs): ${typical_monthly_expenses:,.2f}"
    )
    for buffer_amount in (2000, 3000, 4000, 5000):
        buffer_training, _ = build_weekly_feature_tables(
            transactions, invoices, buffer_amount=buffer_amount
        )
        rate = float(buffer_training["balance_below_buffer_within_30d"].mean())
        print(f"   dip rate at ${buffer_amount:,}: {rate:.1%}")


if __name__ == "__main__":
    main()
