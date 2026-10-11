"""noFumble's private, local-first cash-flow dashboard."""

from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path
from time import perf_counter

os.environ.setdefault("TABPFN_NO_BROWSER", "true")

import pandas as pd
import streamlit as st

from adapters import (
    adapt_invoices,
    adapt_transactions,
    guess_invoice_mapping,
    guess_transaction_mapping,
)
from anomalies import detect_anomalies
from features import (
    TARGET_COLUMNS,
    build_weekly_feature_core,
    build_weekly_feature_tables,
)
from forecast import (
    ForecastUnavailableError,
    apply_upcoming_expense,
    default_buffer_amount,
    run_forecast,
)
from generate_sample_data import DEFAULT_BUFFER_AMOUNT
from ingestion import load_and_validate_csvs
from model import (
    ModelWeightsUnavailableError,
    fit_and_predict,
    warm_tabpfn_weights,
)
from sample_data import load_sample_data
from ui.charts import cashflow_chart, track_record_chart
from ui.components import (
    metric_card,
    render_forecast_summary,
    render_hero,
    section_heading,
)

LOGGER = logging.getLogger("nofumble")
ROOT = Path(__file__).resolve().parent

st.set_page_config(
    page_title="noFumble — cash-flow clarity",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _load_styles() -> None:
    styles = (ROOT / "ui" / "styles.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{styles}</style>", unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def _cached_feature_core(
    transactions: pd.DataFrame, invoices: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cache features by data content; the buffer does not affect this result."""
    return build_weekly_feature_core(transactions, invoices)


def _content_fingerprint(*frames: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    for frame in frames:
        digest.update(repr(tuple(frame.columns)).encode())
        digest.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
    return digest.hexdigest()


def _clear_session_forecast_cache() -> None:
    for key in (
        "nf_base_forecast",
        "nf_base_buffer",
        "nf_feature_fingerprint",
        "nf_feature_core",
        "nf_regression_fingerprint",
        "nf_regression_predictions",
    ):
        st.session_state.pop(key, None)


def _session_feature_core(
    transactions: pd.DataFrame, invoices: pd.DataFrame, mode: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if mode == "sample":
        return _cached_feature_core(transactions, invoices)
    fingerprint = _content_fingerprint(transactions, invoices)
    if st.session_state.get("nf_feature_fingerprint") != fingerprint:
        st.session_state["nf_feature_core"] = build_weekly_feature_core(
            transactions, invoices
        )
        st.session_state["nf_feature_fingerprint"] = fingerprint
        st.session_state.pop("nf_regression_predictions", None)
    return st.session_state["nf_feature_core"]


@st.cache_data(show_spinner=False)
def _cached_regression_predictions(
    train_features: pd.DataFrame,
    train_target: pd.Series,
    latest_features: pd.DataFrame,
    device: str,
    use_fake_model: bool,
) -> tuple:
    factory = None
    if use_fake_model:
        from tests.fake_models import fake_model_factory

        factory = fake_model_factory
    return fit_and_predict(
        "regression",
        train_features,
        train_target,
        latest_features,
        device=device,
        model_factory=factory,
    )


def _session_regression_predictions(
    train_features: pd.DataFrame,
    train_target: pd.Series,
    latest_features: pd.DataFrame,
    device: str,
    factory,
) -> tuple:
    fingerprint = _content_fingerprint(train_features, latest_features)
    fingerprint += hashlib.sha256(
        pd.util.hash_pandas_object(train_target, index=True).values.tobytes()
    ).hexdigest()
    fingerprint += device
    if st.session_state.get("nf_regression_fingerprint") != fingerprint:
        predictions = fit_and_predict(
            "regression",
            train_features,
            train_target,
            latest_features,
            device=device,
            model_factory=factory,
        )
        st.session_state["nf_regression_predictions"] = predictions
        st.session_state["nf_regression_fingerprint"] = fingerprint
    return st.session_state["nf_regression_predictions"]


@st.cache_resource(show_spinner=False)
def _warm_tabpfn() -> str | None:
    started = perf_counter()
    try:
        warm_tabpfn_weights(device="cpu")
    except ModelWeightsUnavailableError as error:
        LOGGER.warning(
            "TabPFN weights or TABPFN_TOKEN need setup (%s).",
            type(error).__name__,
        )
        return str(error)
    LOGGER.info(
        "TabPFN model weights loaded in %.2f seconds.", perf_counter() - started
    )
    return None


def _select_column(
    label: str,
    options: list[str],
    guessed: str | None,
    key: str,
    *,
    optional: bool = True,
) -> str | None:
    choices: list[str | None] = ([None] if optional else []) + options
    chosen_index = choices.index(guessed) if guessed in choices else 0
    value = st.selectbox(
        label,
        options=choices,
        index=chosen_index,
        key=key,
        format_func=lambda item: "Not available / derive" if item is None else item,
    )
    return value


def _render_intake() -> None:
    st.markdown('<div id="intake"></div>', unsafe_allow_html=True)
    section_heading(
        "Start with your numbers", "Use private CSVs or explore the fictional sample."
    )
    sample_col, upload_col = st.columns([1, 1.35], gap="large")
    with sample_col:
        st.markdown(
            """
            <div class="summary-card">
              <p class="eyebrow">Zero setup</p>
              <p><strong>See how it works</strong><br>
              Load a 33-month fictional freelancer history. No account or real bank
              data needed.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Try with sample data", type="primary", key="load_sample"):
            try:
                raw_transactions, raw_invoices = load_sample_data()
                cleaned = load_and_validate_csvs(raw_transactions, raw_invoices)
                if isinstance(cleaned, list):
                    st.error(
                        "The built-in sample could not be checked. " + " ".join(cleaned)
                    )
                else:
                    (
                        st.session_state["nf_transactions"],
                        st.session_state["nf_invoices"],
                    ) = cleaned
                    st.session_state["nf_mode"] = "sample"
                    st.session_state["nf_assumptions"] = [
                        "All sample transactions and invoices are fictional."
                    ]
                    st.session_state["nf_buffer"] = DEFAULT_BUFFER_AMOUNT
                    _clear_session_forecast_cache()
            except OSError:
                st.error("Sample files are unavailable. Please use CSV upload instead.")

    with upload_col:
        transaction_file = st.file_uploader(
            "Transactions CSV",
            type=["csv"],
            key="transaction_upload",
            help="Bank export. Your file is read in memory for this session only.",
        )
        invoice_file = st.file_uploader(
            "Invoices CSV (optional)",
            type=["csv"],
            key="invoice_upload",
            help="Invoice receivables improve payment-timing signals.",
        )
        if transaction_file is not None:
            try:
                raw_transactions = pd.read_csv(io.BytesIO(transaction_file.getvalue()))
                raw_invoices = (
                    pd.read_csv(io.BytesIO(invoice_file.getvalue()))
                    if invoice_file is not None
                    else None
                )
            except (pd.errors.ParserError, UnicodeDecodeError, ValueError):
                st.error(
                    "We could not read that CSV. Please check its format and try again."
                )
                return

            guessed = guess_transaction_mapping(list(raw_transactions.columns))
            st.markdown("**Match the bank columns**")
            with st.form("transaction_mapping"):
                mapping: dict[str, str | None] = {}
                column_names = list(raw_transactions.columns)
                left, right = st.columns(2)
                with left:
                    for field in ("date", "description", "amount", "category"):
                        mapping[field] = _select_column(
                            field.replace("_", " ").title(),
                            column_names,
                            guessed.get(field),
                            f"map_{field}",
                            optional=field not in ("date",),
                        )
                with right:
                    for field in ("counterparty", "is_recurring", "balance_after"):
                        mapping[field] = _select_column(
                            field.replace("_", " ").title(),
                            column_names,
                            guessed.get(field),
                            f"map_{field}",
                        )
                debit, credit = st.columns(2)
                with debit:
                    mapping["debit"] = _select_column(
                        "Debit / money out",
                        column_names,
                        guessed.get("debit"),
                        "map_debit",
                    )
                with credit:
                    mapping["credit"] = _select_column(
                        "Credit / money in",
                        column_names,
                        guessed.get("credit"),
                        "map_credit",
                    )
                opening_balance = st.number_input(
                    "Opening balance before the first row",
                    value=0.0,
                    step=100.0,
                    help="Used only when no running-balance column is mapped.",
                )
                flip_sign = st.checkbox(
                    "Flip the sign convention",
                    help="Use if credits are negative in this export.",
                )
                preview = raw_transactions.head(5)
                st.caption("Five-row source preview")
                st.dataframe(preview, use_container_width=True, hide_index=True)
                submitted = st.form_submit_button(
                    "Map, validate, and use this data", type="primary"
                )
            if submitted:
                try:
                    canonical_transactions, assumptions = adapt_transactions(
                        raw_transactions,
                        mapping,
                        opening_balance=opening_balance,
                        flip_sign=flip_sign,
                    )
                    canonical_invoices, invoice_assumptions = adapt_invoices(
                        raw_invoices,
                        guess_invoice_mapping(list(raw_invoices.columns))
                        if raw_invoices is not None
                        else None,
                    )
                    cleaned = load_and_validate_csvs(
                        canonical_transactions, canonical_invoices
                    )
                    if isinstance(cleaned, list):
                        st.error("Please fix these data issues: " + " ".join(cleaned))
                    else:
                        (
                            st.session_state["nf_transactions"],
                            st.session_state["nf_invoices"],
                        ) = cleaned
                        st.session_state["nf_mode"] = "upload"
                        st.session_state["nf_assumptions"] = (
                            assumptions + invoice_assumptions
                        )
                        st.session_state["nf_buffer"] = default_buffer_amount(
                            cleaned[0]
                        )
                        _clear_session_forecast_cache()
                except (ValueError, KeyError) as error:
                    st.error(str(error))

    if st.session_state.get("nf_assumptions"):
        with st.expander("Data assumptions", expanded=True):
            for assumption in st.session_state["nf_assumptions"]:
                st.write(f"• {assumption}")


def _risk_signal_list(signals: list[str]) -> None:
    if not signals:
        st.info("No strong risk signals were triggered by the available history.")
        return
    for signal in signals:
        st.markdown(f'<div class="signal-card">{signal}</div>', unsafe_allow_html=True)


def _render_dashboard() -> None:
    transactions = st.session_state["nf_transactions"]
    invoices = st.session_state["nf_invoices"]
    mode = st.session_state["nf_mode"]
    if "nf_buffer" not in st.session_state:
        st.session_state["nf_buffer"] = (
            DEFAULT_BUFFER_AMOUNT
            if mode == "sample"
            else default_buffer_amount(transactions)
        )
    with st.sidebar:
        st.markdown("### Set your safety line")
        buffer_amount = st.number_input(
            "Cash buffer",
            min_value=0.0,
            step=100.0,
            key="nf_buffer",
            help="The balance you want to keep available.",
        )
        upcoming_expense = st.number_input(
            "What-if: upcoming expense",
            min_value=0.0,
            value=0.0,
            step=100.0,
            key="nf_upcoming_expense",
            help="This shifts the projection and risk estimate without refitting the models.",
        )
        run_clicked = st.button("Run forecast", type="primary", key="run_forecast")
        st.caption("A 30-day dip chance and a four-week cash-flow estimate.")

    cached = st.session_state.get("nf_base_forecast")
    if run_clicked:
        try:
            with st.status(
                "Preparing your forecast. The first TabPFN run can take longer while local weights load.",
                expanded=True,
            ) as status:
                core = _session_feature_core(transactions, invoices, mode)
                training, inference = core
                model_features = [
                    column
                    for column in training.columns
                    if column not in (*TARGET_COLUMNS, "week_end")
                ]
                test_model = os.getenv("NOFUMBLE_TEST_MODEL") == "1"
                factory = None
                if test_model:
                    from tests.fake_models import fake_model_factory

                    factory = fake_model_factory
                regression_features = training[model_features]
                target = training["next_4w_net_cash_flow"].astype(float)
                latest_features = inference.loc[[inference.index[-1]], model_features]
                if mode == "sample":
                    regression_predictions = _cached_regression_predictions(
                        regression_features,
                        target,
                        latest_features,
                        "cpu",
                        test_model,
                    )
                else:
                    regression_predictions = _session_regression_predictions(
                        regression_features,
                        target,
                        latest_features,
                        "cpu" if test_model else "auto",
                        factory,
                    )
                base = run_forecast(
                    transactions,
                    invoices,
                    buffer_amount=float(buffer_amount),
                    device="cpu" if test_model else "auto",
                    model_factory=factory,
                    core_features=core,
                    regression_predictions=regression_predictions,
                )
                st.session_state["nf_base_forecast"] = base
                st.session_state["nf_base_buffer"] = float(buffer_amount)
                cached = base
                status.update(label="Forecast ready", state="complete", expanded=False)
        except ForecastUnavailableError as error:
            st.warning(str(error))
            return
        except Exception as error:  # noqa: BLE001
            LOGGER.error(
                "Forecast failed during model or data processing (%s).",
                type(error).__name__,
            )
            st.error(
                "The forecast could not be completed. Check that TabPFN weights are "
                "available and, for a headless deployment, that TABPFN_TOKEN is set."
            )
            return
    if cached is None or st.session_state.get("nf_base_buffer") != float(buffer_amount):
        st.info("Choose your buffer, then run a forecast to see your outlook.")
        return

    result = apply_upcoming_expense(cached, float(upcoming_expense))
    if result.warnings:
        for warning in result.warnings:
            st.warning(warning)
    if result.classifier_fallback == "no_dips":
        st.info("There were no below-buffer weeks in the available training history.")
    elif result.classifier_fallback == "always_below_buffer":
        st.info("The available training history stayed below the selected buffer.")

    st.markdown('<div id="dashboard"></div>', unsafe_allow_html=True)
    section_heading(
        "Your cash-flow outlook", f"As of week ending {result.as_of_week:%b %d, %Y}"
    )
    columns = st.columns(4, gap="medium")
    with columns[0]:
        metric_card(
            "Balance today",
            f"${result.current_balance:,.0f}",
            "Latest observed balance",
        )
    with columns[1]:
        metric_card(
            "Estimated balance · 4 weeks",
            f"${result.projected_balance_4w:,.0f}",
            f"80% range ${result.projected_balance_p10:,.0f} to ${result.projected_balance_p90:,.0f}",
        )
    with columns[2]:
        metric_card(
            "Chance of a 30-day dip",
            f"{result.dip_probability_30d:.0%}",
            f"{result.risk_level.title()} risk · below ${result.buffer_amount:,.0f}",
            tone=result.risk_level,
            badge=result.risk_level.title(),
        )
    with columns[3]:
        runway = (
            f"{result.runway_weeks:.1f} weeks"
            if result.runway_weeks is not None
            else "Not burning cash"
        )
        metric_card("Cash runway", runway, "At the recent average burn rate")

    st.altair_chart(cashflow_chart(result), use_container_width=True)
    st.caption(
        "Observed balance history and one four-week estimate. The interval marker is "
        "the model's 10th-to-90th percentile range; it is not an interpolated path."
    )
    render_forecast_summary(result)
    section_heading(
        "Risk signals",
        "Deterministic indicators from your latest week, not model explanations.",
    )
    _risk_signal_list(result.risk_signals)

    section_heading(
        "Unusual expenses",
        "Rule-based checks for duplicates, changed subscriptions, and unusually large one-offs.",
    )
    anomalies = detect_anomalies(transactions)
    if anomalies.empty:
        st.success("No unusual expenses were flagged in the available history.")
    else:
        st.dataframe(
            anomalies,
            use_container_width=True,
            hide_index=True,
            column_config={
                "transaction_index": st.column_config.NumberColumn("Row"),
                "date": st.column_config.DateColumn("Date"),
                "amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
                "severity": st.column_config.TextColumn("Severity"),
            },
        )
        if mode == "sample":
            st.caption(
                "Evaluation refers to synthetic injected anomalies; real bank data "
                "can be messier and these flags are not proof of fraud."
            )

    if mode == "sample":
        section_heading(
            "Track record", "A held-out comparison using the trailing-average baseline."
        )
        track_path = ROOT / "data" / "sample_track_record.csv"
        if track_path.is_file():
            track_record = pd.read_csv(track_path, parse_dates=["week_end"])
            st.altair_chart(track_record_chart(track_record), use_container_width=True)
            st.caption(
                "Fictional sample outcomes versus a trailing-average baseline; "
                "this is not a TabPFN accuracy claim."
            )
        else:
            st.info("Sample track record is not available yet.")
    else:
        section_heading(
            "Track record", "Optionally evaluate the last four rolling-origin folds."
        )
        if st.button("Run a four-fold backtest", key="run_backtest"):
            if len(training) < 68:
                st.warning("At least 68 complete weeks are needed for four test folds.")
            else:
                from backtest import run_rolling_origin_backtest

                weekly_training, _ = build_weekly_feature_tables(
                    transactions, invoices, buffer_amount=float(buffer_amount)
                )
                with st.status("Running a time-ordered backtest…") as status:
                    result_backtest = run_rolling_origin_backtest(
                        weekly_training.tail(68),
                        initial_train_weeks=52,
                        test_window_weeks=4,
                        step_weeks=4,
                        device="auto",
                    )
                    st.write(result_backtest.summary)
                    status.update(label="Backtest complete", state="complete")


_load_styles()
render_hero()
if (
    os.getenv("NOFUMBLE_WARM_MODEL") == "true"
    and os.getenv("NOFUMBLE_TEST_MODEL") != "1"
):
    with st.status("Preparing the local forecasting model…", expanded=False) as status:
        warm_error = _warm_tabpfn()
        if warm_error:
            status.update(label="Forecast model needs setup", state="error")
            st.warning(warm_error)
        else:
            status.update(label="Forecast model is ready", state="complete")
_render_intake()

if "nf_transactions" in st.session_state and "nf_invoices" in st.session_state:
    _render_dashboard()

st.markdown(
    """
    <footer class="footer-note">
      Forecasts are uncertain and are not financial advice. Cash-flow probabilities
      are estimates from limited history, not guarantees. noFumble is open source;
      uploaded data is processed in this session and is not written to app storage.
    </footer>
    """,
    unsafe_allow_html=True,
)
