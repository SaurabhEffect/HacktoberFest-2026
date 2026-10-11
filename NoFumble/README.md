# noFumble

[![Tests](https://github.com/SaurabhEffect/Hacktoberfest-2026/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/SaurabhEffect/Hacktoberfest-2026/actions/workflows/tests.yml)

**Never fumble your cash flow.** noFumble helps freelancers with irregular income
spot tight weeks using a local TabPFN forecast, a 30-day buffer-risk estimate, and
explainable expense checks. The visual language is original and takes inspiration
from Hacktoberfest's optimistic, open-source energy; no event logo or endorsement
is implied.

## Run locally on Windows

Install Python 3.11.9 and Git, then run these commands in PowerShell, in order:

```powershell
git clone https://github.com/SaurabhEffect/Hacktoberfest-2026.git
Set-Location Hacktoberfest-2026\NoFumble
py -3.11 --version
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m streamlit run app.py
```

Open the local URL printed by Streamlit (usually `http://localhost:8501`) and
select **Try with sample data** to explore. To stop the app, press `Ctrl+C` in
the PowerShell window.

If PowerShell blocks virtual-environment activation, either allow it for your
user with `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`,
or run the environment's Python directly instead of activating:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

The built-in sample can be explored without a TabPFN token. The first real
forecast downloads the separately licensed TabPFN weights; accept the model
license and configure `TABPFN_TOKEN` in the same PowerShell session if required:

```powershell
$env:TABPFN_TOKEN = "your-token"
python -m streamlit run app.py
```

Do not commit or share your token. Uploaded transactions and invoices stay in the
app session; no data is sent to a hosted AI API.

### macOS or Linux

```shell
git clone https://github.com/SaurabhEffect/Hacktoberfest-2026.git
cd Hacktoberfest-2026/NoFumble
python3.11 --version
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m streamlit run app.py
```

Run the quality checks:

```shell
python -m ruff format --check .
python -m ruff check .
python -m pytest
```

## Data and forecasting

Users can upload a bank transactions CSV and optionally an invoices CSV. The
intake adapter maps common column names, supports separate debit/credit amounts,
and reports assumptions before strict validation. Missing balances are
reconstructed from an opening balance. Files are processed in memory and are not
written to app storage or logs.

`features.py` builds leakage-aware weekly tabular rows, a buffer-independent core,
and a fast buffer-dependent dip label. The model estimates signed net cash flow
over the next four weeks and the likelihood of falling below a user-set buffer
within 30 days. A TabPFN 10th-to-90th percentile range is shown as uncertainty,
not as an interpolated balance path. The separate four-week balance probability
uses linear interpolation between the 10th and 90th percentile anchors, with
short linear tails to probabilities zero and one. A what-if expense shifts that
distribution analytically and does not refit either model.

With fewer than 26 complete weeks, no forecast is shown. Under 52 complete weeks,
the dashboard warns that confidence is limited. If the history has only one dip
class, the classifier uses a clearly labelled constant-class fallback.

Risk signals (past-due invoices, client concentration, payment recency, upcoming
lumpy expenses, and expected income versus usual expenses) are deterministic
indicators, not model explanations.

`anomalies.py` flags likely duplicates, unusual recurring-charge changes, and
large one-off expenses. Its sample evaluation is explicitly described as
**synthetic injected anomalies**; real bank exports are messier, and a flag is not
proof of fraud. Ground-truth labels stay separate from the app's sample loader.

The sample transactions, invoices, and anomaly labels are deterministic and
fictional. Regenerate them with `python generate_sample_data.py`. Regenerate the
sample baseline chart data with `python scripts/generate_track_record.py`. The
track record is an honest trailing-average baseline comparison, not a TabPFN
accuracy claim.

## Evaluation

`backtest.py` uses expanding rolling-origin folds only. It compares TabPFN with a
trailing-average baseline and histogram gradient boosting; no shuffled split is
used. To run the evaluation and save its chart:

```shell
python backtest.py --device auto
```

## Deploy to Render

The repository-root [render.yaml](../render.yaml) defines a Python web service, the
`/_stcore/health` health check, a persistent disk for model weights, and
environment variables. Set the `TABPFN_TOKEN` secret in the Render dashboard
after accepting the TabPFN license. The model cache lives at `/var/data/tabpfn`;
the attached 5 GB disk preserves downloaded weights across restarts and deploys.
The disk is a paid Render feature. Without it, weights are downloaded again when
the ephemeral filesystem is replaced. Build-time download avoids first-start
network work but makes builds slower and requires the license token during build;
the persistent disk is the chosen option.

Render's `PYTHON_VERSION` is pinned explicitly to 3.11.9 in the Blueprint; the
`.python-version` file is used by local tooling and CI. The start command binds
Streamlit to `0.0.0.0` and Render's `$PORT`, and enables headless mode. CORS and
XSRF protections remain enabled; the deployment does not disable either flag.
Usage-stat collection is disabled in `.streamlit/config.toml`.

TabPFN weights are licensed separately from the Python package. Local CPU
inference avoids sending uploaded data to a third-party AI API, but loading the
checkpoint can require outbound access for the initial download. A paid Render
instance with enough memory for PyTorch and the model is required; this project
does not claim a measured peak-memory figure until a real model warm-up has been
run on the target instance. Review current Render service and disk pricing before
deploying.

Post-deploy checklist:

1. Confirm startup completes and the Render health check is green.
2. Run the built-in sample flow from a cold start.
3. Upload a messy transactions CSV and confirm the assumptions/preview.
4. Confirm a short-history file receives the friendly refusal message.
5. Restart the service and confirm weights persist under `/var/data`.

## Development

Runtime dependencies (including the CPU PyTorch wheel) are pinned in
`requirements.txt`; pytest and Ruff are in `requirements-dev.txt`. CI continues to
use fake model factories and never downloads TabPFN weights. The real-model smoke
test is opt-in with `NOFUMBLE_REAL_TABPFN=1`.