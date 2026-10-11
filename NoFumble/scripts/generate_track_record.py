"""Generate a reproducible sample comparison against a trailing-average baseline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def generate_track_record(output_path: str | Path) -> pd.DataFrame:
    """Write held-out sample outcomes and trailing-average predictions."""
    sys.path.insert(0, str(ROOT))
    from features import build_weekly_feature_tables
    from generate_sample_data import DEFAULT_BUFFER_AMOUNT
    from ingestion import load_and_validate_csvs
    from sample_data import load_sample_data

    cleaned = load_and_validate_csvs(*load_sample_data())
    if isinstance(cleaned, list):
        raise ValueError("Sample data failed validation: " + " ".join(cleaned))
    transactions, invoices = cleaned
    training, _ = build_weekly_feature_tables(
        transactions, invoices, buffer_amount=DEFAULT_BUFFER_AMOUNT
    )
    record = training.tail(68).iloc[::4].tail(16).copy()
    output = pd.DataFrame(
        {
            "week_end": record["week_end"],
            "actual": record["next_4w_net_cash_flow"],
            "prediction": (record["mean_income_4w"] - record["mean_expenses_4w"]) * 4,
        }
    )
    output.to_csv(output_path, index=False, date_format="%Y-%m-%d")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "sample_track_record.csv",
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output = generate_track_record(args.output)
    print(f"Wrote {len(output)} trailing-average backtest windows to {args.output}")


if __name__ == "__main__":
    main()
