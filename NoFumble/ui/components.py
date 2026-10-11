"""Small, accessible Streamlit components used by the dashboard."""

from __future__ import annotations

from html import escape

import streamlit as st

from forecast import ForecastResult
from ui.theme import RISK_COLORS


def render_hero() -> None:
    """Show the product promise and its local-data privacy boundary."""
    st.markdown(
        """
        <header class="hero" data-testid="hero">
          <div class="hero-copy">
            <p class="eyebrow">Cash-flow clarity for independent work</p>
            <h1>Never fumble<br><span class="hero-highlight">your cash flow.</span></h1>
            <p class="hero-lede">See the tight weeks coming. Make your next move
            with a little more confidence.</p>
            <p class="privacy-note"><span aria-hidden="true">●</span>
            Your files stay in this session. Forecasts run locally; no third-party
            AI API sees your transactions, and the app does not store uploads.</p>
          </div>
          <div class="hero-art" aria-hidden="true">
            <span class="pixel-grid"></span>
            <span class="pixel-block block-one"></span>
            <span class="pixel-block block-two"></span>
            <span class="pixel-block block-three"></span>
            <span class="art-spark">✳</span>
            <span class="art-caption">PLAN<br>AHEAD</span>
          </div>
        </header>
        """,
        unsafe_allow_html=True,
    )


def section_heading(title: str, caption: str | None = None) -> None:
    """Render a consistent heading above a dashboard section."""
    subtitle = f'<p class="section-caption">{escape(caption)}</p>' if caption else ""
    st.markdown(
        f'<div class="section-heading"><h2>{escape(title)}</h2>{subtitle}</div>',
        unsafe_allow_html=True,
    )


def metric_card(
    label: str,
    value: str,
    detail: str,
    *,
    tone: str = "neutral",
    badge: str | None = None,
) -> None:
    """Display a bordered KPI card with a text label independent of color."""
    badge_markup = f'<span class="metric-badge">{escape(badge)}</span>' if badge else ""
    st.markdown(
        f"""
        <article class="metric-card tone-{escape(tone)}" data-testid="metric-card">
          <div class="metric-top"><span>{escape(label)}</span>{badge_markup}</div>
          <p class="metric-value">{escape(value)}</p>
          <p class="metric-detail">{escape(detail)}</p>
        </article>
        """,
        unsafe_allow_html=True,
    )


def risk_badge(level: str, probability: float) -> str:
    """Return a labelled risk pill for reuse in accessible explanatory text."""
    color = RISK_COLORS[level]
    return (
        f'<span class="risk-badge" style="--risk-color:{color}">'
        f"{escape(level.title())} risk · {probability:.0%}</span>"
    )


def render_forecast_summary(result: ForecastResult) -> None:
    """Show forecast interpretation separately from deterministic risk signals."""
    st.markdown(
        f"""
        <div class="summary-card" data-testid="forecast-summary">
          <p class="eyebrow">Your forecast</p>
          <p>{escape(result.summary)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
