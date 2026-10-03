"""Load the built-in sample transactions and invoices."""

from pathlib import Path

import pandas as pd


def load_sample_data(
    data_dir: str | Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load sample transactions and invoices without exposing evaluation labels."""
    directory = Path(data_dir) if data_dir is not None else Path(__file__).parent / "data"
    transactions_path = directory / "transactions.csv"
    invoices_path = directory / "invoices.csv"
    if not transactions_path.is_file() or not invoices_path.is_file():
        raise FileNotFoundError("Sample CSVs are missing. Run generate_sample_data.py first.")
    return pd.read_csv(transactions_path), pd.read_csv(invoices_path)