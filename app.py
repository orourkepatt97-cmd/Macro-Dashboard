"""Macro dashboard UI.

Presentation only: layout, widgets, and chart assembly. Every value rendered
here comes from ``data.py``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import feeds
import markets
import theme
from data import (
    CREDIT_SERIES,
    FredAPIError,
    INFLATION_SERIES,
    TREASURY_SERIES,
    changes_bps,
    fetch_change_window,
    fetch_credit_spreads,
    fetch_credit_window,
    fetch_curve_snapshot,
    fetch_inflation,
    fetch_treasury_yields,
    latest_change,
    latest_observations,
    movement_table,
    release_overdue,
    spread,
)

# Sign colouring shared by the Rate Change and Credit tables.
_GREEN, _RED = "#3fb950", "#f85149"


def _sign_color(value: float) -> str | None:
    if pd.isna(value) or value == 0:
        return None
    return f"color: {_GREEN if value > 0 else _RED}"


# Series specs for the pill selectors that replace the Plotly legends. The
# colours here must stay in the same order as theme._PILL_TINTS so the pill
# underline matches its line.
_CREDIT_SPEC = [                      # (label, colour, on secondary axis)
    ("HY OAS", "#cf9038", False),
    ("IG OAS", "#d0d0d0", True),
    ("HY-IG", "#e05252", False),
    ("2s10s", "#7f8fbf", True),
]
_INFLATION_SPEC = [                   # (FRED id, label, colour)
    ("CPIAUCSL", "CPI", "#cf9038"),
    ("PCEPILFE", "Core PCE", "#d0d0d0"),
    ("T10YIE", "10Y breakeven", "#7f8fbf"),
]


def _padded_range(series_list, pad_frac: float = 0.06):
    """A fixed [lo, hi] with a little headroom, spanning every series passed —
    so toggling one off in the pills doesn't rescale the axis. None if empty."""
    frames = [s for s in series_list if s is not None and not s.empty]
    if not frames:
        return None
    values = pd.concat(frames)
    lo, hi = float(values.min()), float(values.max())
    span = hi - lo
    pad = span * pad_frac if span else max(abs(hi) * 0.05, 1.0)
    return [lo - pad, hi + pad]

st.set_page_config(
    page_title="Macro Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)
theme.apply()

# --- Header: title on the left, period selector top-right --------------
today = date.today()
_PERIOD_DAYS = {"1M": 30, "3M": 91, "6M": 182, "1Y": 365, "2Y": 730, "5Y": 1825}
_DEFAULT_PERIOD = "2Y"

head_l, head_r = st.columns([5, 4], vertical_alignment="top")
with head_l:
    st.title("Macro Dashboard")
    st.caption(
        "Treasury rates & curve, credit spreads, inflation, Fed-decision pricing, "
        "and market news."
    )
with head_r:
    period = st.pills(
        "PERIOD",
        options=[*_PERIOD_DAYS, "Custom"],
        selection_mode="single",
        default=_DEFAULT_PERIOD,
        key="period",
    )
    custom_range = None
    if period == "Custom":
        with st.popover("Custom range", use_container_width=True):
            custom_range = st.date_input(
                "Date range",
                value=(today - timedelta(days=_PERIOD_DAYS[_DEFAULT_PERIOD]), today),
                max_value=today,
                key="custom_range",
            )

if period == "Custom":
    # date_input returns a 1-tuple while the user is mid-selection.
    if not isinstance(custom_range, (list, tuple)) or len(custom_range) != 2:
        st.info("Pick a start and end date in the Custom range picker.")
        st.stop()
    start_date, end_date = custom_range
    if start_date > end_date:
        st.error("Start date must be on or before end date.")
        st.stop()
else:
    _span = _PERIOD_DAYS.get(period or _DEFAULT_PERIOD, _PERIOD_DAYS[_DEFAULT_PERIOD])
    start_date, end_date = today - timedelta(days=_span), today


# --- "As of" data-freshness line + refresh ----------------------------
def _asof_line(asof: dict) -> str:
    """One short line: the treasury tenors share a release so they collapse to a
    single "Rates as of <date>", followed by the CPI and PCE data months. A
    " (late)" suffix appears only when that series' next release is overdue."""
    parts: list[str] = []

    rate_dates = {s: asof[s] for s in (*TREASURY_SERIES, "T10YIE") if asof.get(s)}
    if rate_dates:
        rep = min(rate_dates, key=rate_dates.get)  # oldest of the rate series
        when = rate_dates[rep]
        parts.append(
            f"Rates as of {when:%b %d}" + (" (late)" if release_overdue(rep, when) else "")
        )

    for series_id, name in (("CPIAUCSL", "CPI"), ("PCEPILFE", "PCE")):
        when = asof.get(series_id)
        if when is None:
            parts.append(f"{name} n/a")
        else:
            parts.append(
                f"{name} {when:%b %Y}"
                + (" (late)" if release_overdue(series_id, when) else "")
            )

    return "  ·  ".join(parts) if parts else "data unavailable"


asof_col, refresh_col = st.columns([13, 1], vertical_alignment="center")
asof_col.caption(_asof_line(latest_observations()))
if refresh_col.button("↻", help="Clear the FRED and news cache, then reload", key="refresh_data"):
    st.cache_data.clear()
    st.rerun()

rates_tab, change_tab, credit_tab, inflation_tab, news_tab = st.tabs(
    ["Rates", "Rate Change", "Credit", "Inflation", "News"]
)

# ========================================================================
# Rates tab
# ========================================================================
with rates_tab:
    try:
        df = fetch_treasury_yields(start_date.isoformat(), end_date.isoformat())
    except FredAPIError as exc:
        st.error(str(exc))
        st.stop()

    if df.dropna(how="all").empty:
        st.warning("FRED returned no yield observations for this date range.")
        st.stop()

    # --- Curve-spread metrics --------------------------------------------
    _SPREAD_HELP = (
        "1-day change in the spread itself. 0 bps means the two yields moved by "
        "the same amount (a parallel shift) — the spread was flat that day."
    )

    def spread_metric(col, short_id: str, long_id: str) -> None:
        name = f"{TREASURY_SERIES[short_id]}{TREASURY_SERIES[long_id]}"
        try:
            as_of, level, change_bps = latest_change(spread(df, short_id, long_id))
        except FredAPIError as exc:
            col.metric(f"{name} spread", "n/a", help=str(exc))
            return
        col.metric(
            label=f"{name} spread — {as_of:%b %d, %Y}",
            value=f"{level * 100:+.0f} bps",
            delta=None if change_bps is None else f"{change_bps:+.1f} bps 1-day",
            delta_color="normal",
            help=_SPREAD_HELP,
        )

    left, right = st.columns(2)
    spread_metric(left, "DGS2", "DGS10")
    spread_metric(right, "DGS5", "DGS30")

    # --- Yields over time ----------------------------------------------
    st.subheader("Yields over time")
    _tenor_labels = list(TREASURY_SERIES.values())
    picked_tenors = st.pills(
        "Tenors",
        options=_tenor_labels,
        selection_mode="multi",
        default=_tenor_labels,
        key="tenors",
        label_visibility="collapsed",
    )
    selected_ids = [
        sid for sid, lbl in TREASURY_SERIES.items() if lbl in (picked_tenors or [])
    ]
    if not selected_ids:
        st.info("Pick at least one tenor above.")
    else:
        ts_fig = go.Figure()
        for series_id in selected_ids:
            style = theme.TENOR_LINE[series_id]
            ts_fig.add_trace(
                go.Scatter(
                    x=df.index,
                    y=df[series_id],
                    name=f"{TREASURY_SERIES[series_id]} ({series_id})",
                    mode="lines",
                    line=dict(color=style["color"], width=style["width"]),
                    opacity=style["opacity"],
                )
            )
        ts_fig.update_layout(
            height=460,
            margin=dict(l=52, r=14, t=10, b=28),
            yaxis=dict(title="Percent", ticksuffix="%"),
            xaxis=dict(title=None),
            showlegend=False,  # the colour-tinted tenor pills above are the legend
        )
        theme.render_chart(ts_fig)

    # --- Yield curve: today vs 1M / 1Y ago ---------------------------
    st.subheader("Yield curve — today vs 1 month and 1 year ago")
    try:
        curve = fetch_curve_snapshot()
    except FredAPIError as exc:
        st.warning(str(exc))
    else:
        snapshot_cols = [c for c in curve.columns if c != "tenor_years"]
        curve_fig = go.Figure()
        for col in snapshot_cols:
            curve_fig.add_trace(
                go.Scatter(
                    x=curve["tenor_years"],
                    y=curve[col],
                    name=col,
                    mode="lines+markers",
                )
            )
        curve_fig.update_layout(
            height=420,
            margin=dict(l=52, r=14, t=10, b=28),
            yaxis=dict(title="Yield", ticksuffix="%"),
            xaxis=dict(
                title="Tenor",
                tickvals=list(curve["tenor_years"]),
                ticktext=list(curve.index),
            ),
        )
        theme.render_chart(curve_fig)

    # --- Fed decision odds (Kalshi) ----------------------------------
    st.subheader("Fed decision odds — Kalshi")
    fed_df, fed_note, fed_fetched = markets.fed_decision_markets()
    if fed_note:
        st.caption(f"⚠️ {fed_note}")
    elif fed_df.empty:
        st.caption("No Kalshi Fed-decision markets to show.")
    else:
        fed_show = fed_df.copy()
        fed_show["Meeting"] = fed_show["Meeting"].mask(fed_show["Meeting"].duplicated(), "")
        st.table(
            fed_show.style
            .format({"Implied prob": "{:.0%}", "Volume": "{:,d}"})
            .set_properties(subset=["Implied prob", "Volume"], **{"text-align": "right"})
            .hide(axis="index")
        )
        st.caption(
            "Implied probability from Kalshi market prices for the next two FOMC "
            f"meetings · prices fetched {fed_fetched.astimezone():%b %d, %H:%M %Z}"
        )

    with st.expander("Yield data"):
        st.dataframe(df.sort_index(ascending=False), width="stretch")

# ========================================================================
# Credit tab
# ========================================================================
with credit_tab:
    try:
        credit_hist = fetch_credit_window()
        credit_range = fetch_credit_spreads(start_date.isoformat(), end_date.isoformat())
        curve_yields = fetch_treasury_yields(start_date.isoformat(), end_date.isoformat())
    except FredAPIError as exc:
        st.error(str(exc))
        st.stop()

    if credit_hist.dropna(how="all").empty:
        st.warning("FRED returned no credit-spread data.")
        st.stop()

    # --- Level + change table (fixed window; columns match Rate Change) -----
    _CREDIT_CHANGE_KEYS = ("1D", "1W", "1M", "3M", "YTD", "1Y")
    credit_rows = {}
    for series_id, label in CREDIT_SERIES.items():
        moves = changes_bps(credit_hist[series_id], keys=_CREDIT_CHANGE_KEYS)
        credit_rows[label] = {
            "Level (bps)": moves["Level"] * 100.0,
            **{key: moves[key] for key in _CREDIT_CHANGE_KEYS},
        }
    credit_tbl = pd.DataFrame.from_dict(
        credit_rows, orient="index", columns=["Level (bps)", *_CREDIT_CHANGE_KEYS]
    )
    credit_formats = {"Level (bps)": "{:.0f}"}
    credit_formats.update({key: "{:+.1f}" for key in _CREDIT_CHANGE_KEYS})
    st.table(
        credit_tbl.style
        .format(credit_formats, na_rep="–")
        .set_properties(**{"text-align": "right"})
    )
    st.caption(
        "ICE BofA option-adjusted spreads. Changes in bps vs. the last observation "
        "on or before each lookback date; a positive change is wider."
    )

    # --- Credit vs curve, dual axis -------------------------------
    # Left axis:  HY OAS + the HY-IG quality spread (both a few hundred bp).
    # Right axis: IG OAS + 2s10s (both in the ~40-90bp range, so they share a
    # scale cleanly). The quality spread shows whether stress is broad or
    # confined to the low-quality end.
    st.subheader("Credit vs curve")
    hy_oas = credit_range["BAMLH0A0HYM2"].dropna() * 100.0                        # bps
    ig_oas = credit_range["BAMLC0A0CM"].dropna() * 100.0                          # bps
    quality = (credit_range["BAMLH0A0HYM2"] - credit_range["BAMLC0A0CM"]).dropna() * 100.0
    twos_tens = spread(curve_yields, "DGS2", "DGS10").dropna() * 100.0            # bps
    credit_data = {"HY OAS": hy_oas, "IG OAS": ig_oas, "HY-IG": quality, "2s10s": twos_tens}

    credit_labels = [label for label, _, _ in _CREDIT_SPEC]
    credit_pick = st.pills(
        "Credit series", credit_labels, selection_mode="multi",
        default=credit_labels, key="credit_series", label_visibility="collapsed",
    )

    if hy_oas.empty or twos_tens.empty:
        st.info("Not enough data in the selected range to plot credit against the curve.")
    elif not credit_pick:
        st.caption("Select at least one series above.")
    else:
        left_range = _padded_range([hy_oas, quality])
        right_range = _padded_range([ig_oas, twos_tens])
        credit_fig = make_subplots(specs=[[{"secondary_y": True}]])
        for label, colour, on_secondary in _CREDIT_SPEC:
            if label not in credit_pick:
                continue
            s = credit_data[label]
            credit_fig.add_trace(
                go.Scatter(x=s.index, y=s.values, name=label, mode="lines",
                           line=dict(color=colour, width=2.0 if label == "HY OAS" else 1.6)),
                secondary_y=on_secondary,
            )
        credit_fig.update_layout(
            height=460, margin=dict(l=56, r=56, t=10, b=28), showlegend=False,
        )
        credit_fig.update_xaxes(title=None)
        credit_fig.update_yaxes(
            title_text="HY OAS · HY-IG (bps)", tickformat=".0f", color="#cf9038",
            secondary_y=False, **({"range": left_range} if left_range else {}),
        )
        credit_fig.update_yaxes(
            title_text="IG OAS · 2s10s (bps)", tickformat=".0f", color="#7f8fbf",
            showgrid=False, secondary_y=True, **({"range": right_range} if right_range else {}),
        )
        theme.render_chart(credit_fig)
        st.caption(
            "HY-IG is the quality spread — wide relative to HY OAS means stress is "
            "concentrated in low-quality credit rather than broad-based."
        )

    with st.expander("Credit data"):
        st.dataframe(credit_range.sort_index(ascending=False), width="stretch")

# ========================================================================
# Inflation tab
# ========================================================================
with inflation_tab:
    try:
        infl = fetch_inflation(start_date.isoformat(), end_date.isoformat())
    except FredAPIError as exc:
        st.error(str(exc))
        st.stop()

    if infl.dropna(how="all").empty:
        st.warning("FRED returned no inflation observations for this date range.")
        st.stop()

    cols = st.columns(len(INFLATION_SERIES))
    for col, (series_id, label) in zip(cols, INFLATION_SERIES.items()):
        try:
            as_of, level, change_bps = latest_change(infl[series_id])
        except FredAPIError as exc:
            col.metric(label, "n/a", help=str(exc))
            continue
        col.metric(
            label=f"{label} — {as_of:%b %d, %Y}",
            value=f"{level:.2f}%",
            delta=None if change_bps is None else f"{change_bps / 100:+.2f} pp vs prior obs.",
            delta_color="normal",
        )

    infl_labels = [label for _, label, _ in _INFLATION_SPEC]
    infl_pick = st.pills(
        "Inflation series", infl_labels, selection_mode="multi",
        default=infl_labels, key="inflation_series", label_visibility="collapsed",
    )
    infl_range = _padded_range([infl[sid].dropna() for sid, _, _ in _INFLATION_SPEC])

    infl_fig = go.Figure()
    for series_id, label, colour in _INFLATION_SPEC:
        if label not in (infl_pick or []):
            continue
        # CPI/PCE are monthly; plot their own observation dates so the line
        # isn't broken by NaNs on the daily grid the breakeven series creates.
        s = infl[series_id].dropna()
        infl_fig.add_trace(
            go.Scatter(x=s.index, y=s.values, name=label, mode="lines",
                       line=dict(color=colour))
        )
    infl_yaxis = dict(title="Percent", ticksuffix="%")
    if infl_range:
        infl_yaxis["range"] = infl_range
    infl_fig.update_layout(
        height=480,
        margin=dict(l=52, r=14, t=10, b=28),
        showlegend=False,
        yaxis=infl_yaxis,
        xaxis=dict(title=None),
    )
    theme.render_chart(infl_fig)
    if not infl_pick:
        st.caption("Select at least one series above.")

    with st.expander("Inflation data"):
        st.dataframe(infl.sort_index(ascending=False), width="stretch")

# ========================================================================
# News tab
# ========================================================================
with news_tab:
    news = feeds.fetch_all_news()
    items, counts = news["items"], news["counts"]

    picked_sources = st.pills(
        "News sources",
        options=feeds.SOURCES,
        selection_mode="multi",
        default=feeds.SOURCES,
        key="news_sources",
        label_visibility="collapsed",
    )
    news_sources = picked_sources or []

    dead = [src for src, n in counts.items() if n == 0]
    if dead:
        st.caption("⚠️ No items from: " + ", ".join(dead))

    visible = [it for it in items if it["source"] in news_sources]

    header = st.columns([3, 1])
    header[0].subheader(f"Latest headlines · {len(visible)} shown")
    max_cards = header[1].slider(
        "Max cards", min_value=10, max_value=120, value=40, step=10,
        label_visibility="collapsed",
    )

    if not news_sources:
        st.info("Pick at least one news source above.")
    elif not visible:
        st.info("No headlines available right now. Try again in a few minutes.")

    for item in visible[:max_cards]:
        with st.container(border=True):
            text_col = st
            if item["image"]:
                img_col, text_col = st.columns([1, 4], vertical_alignment="center")
                try:
                    img_col.image(item["image"], width="stretch")
                except Exception:  # noqa: BLE001 — broken image URL must not kill the card
                    img_col.markdown("📄")

            when = (
                item["published"].astimezone().strftime("%b %d, %Y · %H:%M")
                if item["published"]
                else "date unknown"
            )
            text_col.markdown(f"**[{item['title']}]({item['link']})**")
            text_col.caption(f"{item['source']}  ·  {when}")
            if item["summary"]:
                text_col.write(item["summary"])

# ========================================================================
# Change tab
# ========================================================================
with change_tab:
    try:
        hist = fetch_change_window()
    except FredAPIError as exc:
        st.error(str(exc))
        st.stop()

    if hist.dropna(how="all").empty:
        st.warning("FRED returned no yield history for the movement table.")
        st.stop()

    tbl = movement_table(hist)
    change_cols = [c for c in tbl.columns if c != "Level"]

    number_formats = {"Level": "{:.2f}", **{col: "{:+.1f}" for col in change_cols}}
    styled = (
        tbl.style
        .format(number_formats, na_rep="–")
        .map(_sign_color, subset=change_cols)
        .set_properties(**{"text-align": "right"})  # header alignment is in theme.py
    )
    st.table(styled)
    st.caption(
        "Level in percent (2dp) · changes in basis points (1dp) vs. the last "
        "observation on or before each lookback date."
    )
