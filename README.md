# noFumble

noFumble is a cash-flow forecasting project for freelancers with irregular income. The current repository contains a deterministic synthetic-data generator, CSV ingestion and validation, leakage-aware weekly feature engineering, a TabPFN model wrapper, rolling-origin evaluation, sample data, and tests. The Streamlit interface is not implemented yet.

## Requirements

- Python 3.11
- Git
- Internet access and TabPFN model-license acceptance on first fit to download pretrained weights

## Quick Start

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On macOS or Linux, activate it with `source .venv/bin/activate`.

Install the pinned development dependencies, generate the sample data, and run the checks:

```shell
python -m pip install -r requirements-dev.txt
python generate_sample_data.py
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

The generator accepts `--seed` and `--output-dir` options. For example:

```shell
python generate_sample_data.py --seed 7 --output-dir data
```

## Sample Data

The `data/` directory contains synthetic freelancer transactions and invoices. Running the generator refreshes `transactions.csv`, `invoices.csv`, and `anomaly_ground_truth.csv`. The evaluation labels are intentionally kept out of `load_sample_data()`, which returns only transactions and invoices.

All sample records are fictional. Do not place real customer uploads, credentials, or private data in the repository. Local upload and secret paths are excluded by `.gitignore`.

## Data Modules

`ingestion.py` reads both CSV inputs, normalizes dates and numeric/boolean columns, and returns either clean DataFrames or plain-language validation problems. `features.py` builds weekly training and inference tables; target-incomplete weeks are retained separately, and feature columns are documented in `FEATURE_DESCRIPTIONS`. Feature cutoffs are Sunday week-ends, and input records after each cutoff are excluded from that week's features.

## Model Evaluation

`model.py` wraps TabPFN 9.1.0 regression and classification estimators, cached per task and device. The regression estimator supports predictive quantiles; the project reports the 10th-to-90th percentile interval. TabPFN documents support for up to 1,000,000 rows, subject to checkpoint, feature-count, and memory constraints; its package documentation does not specify a universal feature cap. The `device` option accepts `"auto"`, explicit device strings or `torch.device` instances, and device sequences. On systems without an accelerator, `"auto"` selects CPU. Inputs are not scaled or imputed.

`backtest.py` runs expanding rolling-origin folds, compares TabPFN with a trailing-average baseline and histogram gradient boosting, prints a plain summary, and saves a comparison chart:

```shell
python backtest.py --device auto
```

The default chart path is `reports/backtest_comparison.png`. Training labels must have matured by each fold cutoff, and every test week follows the training weeks. TabPFN's first fit may open a browser for model-license acceptance and local weight access. No input data is sent to a hosted inference API.

## Development

`requirements.txt` pins the runtime dependencies; `requirements-dev.txt` adds the pinned lint tool. GitHub Actions runs formatting, lint, and tests on pushes and pull requests, and supports manual dispatch. CI instantiates TabPFN estimators but does not fit them, so it does not require credentials or download model weights.