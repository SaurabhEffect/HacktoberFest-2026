# noFumble

noFumble is a planned cash-flow forecasting tool for freelancers with irregular income. The current starter repository contains a deterministic synthetic-data generator, sample CSVs, a sample-data loader, and consistency tests. Forecasting and the Streamlit interface are not implemented yet.

## Requirements

- Python 3.11
- Git

## Quick Start

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On macOS or Linux, activate it with `source .venv/bin/activate`.

Install the pinned dependencies, generate the sample data, and run the tests:

```shell
python -m pip install -r requirements.txt
python generate_sample_data.py
python -m pytest
```

The generator accepts `--seed` and `--output-dir` options. For example:

```shell
python generate_sample_data.py --seed 7 --output-dir data
```

## Sample Data

The `data/` directory contains synthetic freelancer transactions and invoices. Running the generator refreshes `transactions.csv`, `invoices.csv`, and `anomaly_ground_truth.csv`. The evaluation labels are intentionally kept out of `load_sample_data()`, which returns only transactions and invoices.

All sample records are fictional. Do not place real customer uploads, credentials, or private data in the repository. Local upload and secret paths are excluded by `.gitignore`.

## Development

Tests are discovered from `tests/` and run both locally and in GitHub Actions on pushes and pull requests. The runtime requirements currently cover the generator and tests; add and pin the app/model dependencies when those modules are introduced and their compatibility is verified.