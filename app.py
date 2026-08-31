"""Macro dashboard UI.

This module is presentation only: layout, widgets, and chart assembly. Every
value it renders comes from ``data.py``.
"""

from __future__ import annotations

from datetime import date, timedelta

import plotly.graph_objects as go
import streamlit as st

from data import FredAPIError, fetch_treasury_yields, latest_spread

st.set_page_config(page_title="Macro Dashboard", page_icon="📈", layout="wide")

st.title("📈 Macro Dashboard")
st.caption("US Treasury constant-maturity yields and the 2s10s spread. Source: FRED.")

# --- Date range selector (defaults to the trailing 2 years) --------------------
today = date.today()
default_start = today - timedelta(days=365 * 2)

selection = st.date_input(
    "Date range",
    value=(default_start, today),
    max_value=today,
    help="Defaults to the last 2 years.",
)

# While the user is picking the second date, date_input returns a 1-tuple.
if not isinstance(selection, (list, tuple)) or len(selection) != 2:
    st.info("Pick an end date to load the chart.")
    st.stop()

start_date, end_date = selection
if start_date > end_date:
    st.error("Start date must be on or before end date.")
    st.stop()

# --- Fetch -------------------------------------------------------------------
try:
    df = fetch_treasury_yields(start_date.isoformat(), end_date.isoformat())
except FredAPIError as exc:
    st.error(str(exc))
    st.stop()

if df.dropna(how="all").empty:
    st.warning("FRED returned no observations for this date range.")
    st.stop()

# --- 2s10s spread metric (above the chart) ---------------------------------
try:
    as_of, spread_value, spread_change = latest_spread(df)
except FredAPIError as exc:
    st.error(str(exc))
    st.stop()

metric_col, _ = st.columns([1, 3])
metric_col.metric(
    label=f"2s10s spread — as of {as_of:%b %d, %Y}",
    value=f"{spread_value:.2f}%",
    delta=None if spread_change is None else f"{spread_change * 100:+.0f} bps vs prior day",
    delta_color="normal",
)

# --- Yield chart ----------------------------------------------------------
fig = go.Figure()
fig.add_trace(
    go.Scatter(x=df.index, y=df["DGS2"], name="2Y (DGS2)", mode="lines")
)
fig.add_trace(
    go.Scatter(x=df.index, y=df["DGS10"], name="10Y (DGS10)", mode="lines")
)
fig.update_layout(
    height=480,
    margin=dict(l=0, r=0, t=10, b=0),
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    yaxis=dict(title="Percent", ticksuffix="%"),
    xaxis=dict(title=None),
)
st.plotly_chart(fig, use_container_width=True)

with st.expander("Data"):
    st.dataframe(df.sort_index(ascending=False), use_container_width=True)
