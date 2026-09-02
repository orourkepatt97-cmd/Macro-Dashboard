"""FRED data access for the macro dashboard.

All network calls to FRED live here; fetch functions are cached with
``st.cache_data``. ``app.py`` imports from this module and never calls FRED
directly.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv


def _load_fred_api_key() -> str | None:
    """The FRED key from ``st.secrets`` when running on Streamlit Community
    Cloud, falling back to a local ``.env`` file or a plain environment
    variable — so the same code runs in both places."""
    try:
        secret = st.secrets.get("FRED_API_KEY")
        if secret:
            return str(secret)
    except Exception:  # noqa: BLE001 - no secrets.toml locally is expected
        pass
    load_dotenv()
    return os.getenv("FRED_API_KEY")


FRED_API_KEY = _load_fred_api_key()
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# One hour: FRED daily series refresh about once per business day.
_CACHE_TTL = 60 * 60

# FRED constant-maturity Treasury series -> short label for the chart legend.
# Dict order is the natural short->long curve order and drives the legend.
TREASURY_SERIES: dict[str, str] = {
    "DGS3MO": "3M",
    "DGS2": "2Y",
    "DGS5": "5Y",
    "DGS10": "10Y",
    "DGS30": "30Y",
}

# Tenor in years, used as the x-axis position on the curve chart.
TENOR_YEARS: dict[str, float] = {
    "DGS3MO": 0.25,
    "DGS2": 2.0,
    "DGS5": 5.0,
    "DGS10": 10.0,
    "DGS30": 30.0,
}

# Inflation series -> label. CPI and core PCE are published as index levels and
# are shown as year-over-year % change; T10YIE is already an annualised rate.
INFLATION_SERIES: dict[str, str] = {
    "CPIAUCSL": "CPI (headline, YoY)",
    "PCEPILFE": "Core PCE (YoY)",
    "T10YIE": "10Y breakeven",
}

# Native release frequency: "D" daily (business days), "M" monthly.
SERIES_FREQ: dict[str, str] = {
    "DGS3MO": "D", "DGS2": "D", "DGS5": "D", "DGS10": "D", "DGS30": "D",
    "CPIAUCSL": "M", "PCEPILFE": "M", "T10YIE": "D",
}

# ICE BofA option-adjusted credit spreads (FRED reports them in percentage
# points; multiply by 100 for basis points).
CREDIT_SERIES: dict[str, str] = {
    "BAMLH0A0HYM2": "HY OAS",
    "BAMLC0A0CM": "IG OAS",
}


class FredAPIError(RuntimeError):
    """Raised when a FRED request fails or the API key is missing."""


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def fetch_series(series_id: str, start_date: str, end_date: str) -> pd.Series:
    """Fetch one FRED series as a date-indexed float Series (missing points dropped).

    ``start_date`` / ``end_date`` are ISO ``YYYY-MM-DD`` strings, passed straight
    through as the inclusive observation window.
    """
    if not FRED_API_KEY:
        raise FredAPIError(
            "FRED_API_KEY is not set. Add it to .streamlit/secrets.toml on "
            "Streamlit Community Cloud, or to a local .env file (see .env.example)."
        )

    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": start_date,
        "observation_end": end_date,
    }

    try:
        response = requests.get(FRED_BASE_URL, params=params, timeout=30)
    except requests.RequestException as exc:  # network error, DNS, timeout
        raise FredAPIError(f"Could not reach FRED: {exc}") from exc

    if response.status_code != 200:
        detail = response.text[:300]
        try:
            detail = response.json().get("error_message", detail)
        except ValueError:
            pass
        raise FredAPIError(
            f"FRED returned HTTP {response.status_code} for {series_id}: {detail}"
        )

    observations = response.json().get("observations", [])
    if not observations:
        return pd.Series(dtype="float64", name=series_id)

    frame = pd.DataFrame(observations)
    frame["date"] = pd.to_datetime(frame["date"])
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")  # "." -> NaN
    series = frame.set_index("date")["value"].dropna()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    series.name = series_id
    return series


@st.cache_data(ttl=_CACHE_TTL, show_spinner="Fetching Treasury yields from FRED…")
def fetch_treasury_yields(start_date: str, end_date: str) -> pd.DataFrame:
    """Constant-maturity yields, one column per :data:`TREASURY_SERIES` id.

    Index is the union of all observation dates, sorted ascending. Column labels
    come from the dict keys, so they never depend on concat column ordering.
    """
    frame = pd.DataFrame(
        {
            series_id: fetch_series(series_id, start_date, end_date)
            for series_id in TREASURY_SERIES
        }
    )
    frame.index.name = "date"
    return frame.sort_index()


@st.cache_data(ttl=_CACHE_TTL, show_spinner="Fetching inflation data from FRED…")
def fetch_inflation(start_date: str, end_date: str) -> pd.DataFrame:
    """CPI & core PCE as YoY %, plus the 10Y breakeven rate (already %).

    CPI/PCE come as index levels, so ~13 extra months are fetched before
    ``start_date`` to seed the trailing-12-month change; the result is then
    trimmed back to ``[start_date, end_date]``. Columns are the FRED ids.
    """
    lookback = (pd.Timestamp(start_date) - pd.DateOffset(months=13)).strftime("%Y-%m-%d")

    cpi = fetch_series("CPIAUCSL", lookback, end_date)
    pce = fetch_series("PCEPILFE", lookback, end_date)
    breakeven = fetch_series("T10YIE", lookback, end_date)

    frame = pd.DataFrame(
        {
            "CPIAUCSL": cpi.pct_change(12) * 100.0,
            "PCEPILFE": pce.pct_change(12) * 100.0,
            "T10YIE": breakeven,
        }
    )
    frame.index.name = "date"
    frame = frame.sort_index()
    return frame.loc[str(start_date):str(end_date)]


def spread(df: pd.DataFrame, short_id: str, long_id: str) -> pd.Series:
    """Yield spread ``long - short`` in percentage points, NaN rows dropped."""
    out = (df[long_id] - df[short_id]).dropna()
    out.name = f"{TREASURY_SERIES[short_id]}{TREASURY_SERIES[long_id]}"
    return out


def curve_snapshots(df: pd.DataFrame) -> pd.DataFrame:
    """Yield-by-tenor for the latest date, ~1 month ago and ~1 year ago.

    Returns a DataFrame indexed by tenor label ("3M".."30Y") with a
    ``tenor_years`` column for the x-axis and one column per snapshot. Each
    snapshot takes the most recent observation on or before its target date, so
    weekends/holidays don't leave gaps. A snapshot older than the available data
    comes back as all-NaN and simply doesn't plot.
    """
    valid = df.dropna(how="all")
    if valid.empty:
        raise FredAPIError("No yield observations available for the curve chart.")

    latest = valid.index.max()
    targets = {
        f"Today ({latest:%b %d, %Y})": latest,
        "1 month ago": latest - pd.DateOffset(months=1),
        "1 year ago": latest - pd.DateOffset(years=1),
    }

    rows: dict[str, dict[str, float]] = {}
    for series_id, label in TREASURY_SERIES.items():
        col = df[series_id].dropna()
        rows[label] = {"tenor_years": TENOR_YEARS[series_id]}
        for snap_label, target in targets.items():
            as_of = col.loc[:target]
            rows[label][snap_label] = float(as_of.iloc[-1]) if not as_of.empty else float("nan")

    out = pd.DataFrame(rows).T
    out.index.name = "tenor"
    return out


CHANGE_COLUMNS = ("Level", "1D", "1W", "1M", "3M", "YTD", "1Y")


def _lookback_date(asof: pd.Timestamp, key: str) -> pd.Timestamp:
    """Calendar date the change window looks back to, given the latest obs date."""
    if key == "1D":
        return asof - pd.Timedelta(days=1)
    if key == "1W":
        return asof - pd.Timedelta(weeks=1)
    if key == "1M":
        return asof - pd.DateOffset(months=1)
    if key == "3M":
        return asof - pd.DateOffset(months=3)
    if key == "YTD":
        return pd.Timestamp(year=asof.year - 1, month=12, day=31)
    if key == "1Y":
        return asof - pd.DateOffset(years=1)
    raise ValueError(f"unknown lookback {key!r}")


def movement_table(yields: pd.DataFrame) -> pd.DataFrame:
    """Level-and-change table for the five tenors and the 2s10s / 5s30s spreads.

    ``yields`` is a date-indexed frame with the :data:`TREASURY_SERIES` columns
    (as returned by :func:`fetch_treasury_yields`), covering at least ~13 months.

    Pure: no I/O. Returns a frame indexed by row label
    ("3M".."30Y", "2s10s", "5s30s") with columns :data:`CHANGE_COLUMNS` —
    ``Level`` in percent, the rest the change **in basis points** from each
    row's latest observation to the last observation on or before the lookback
    date (so weekends and holidays are handled by value, not row offset).
    A change that has no history to compare against is ``NaN``.
    """
    frame = yields.sort_index()
    rows: dict[str, pd.Series] = {
        label: frame[series_id].dropna() for series_id, label in TREASURY_SERIES.items()
    }
    rows["2s10s"] = (frame["DGS10"] - frame["DGS2"]).dropna()
    rows["5s30s"] = (frame["DGS30"] - frame["DGS5"]).dropna()

    records: dict[str, dict[str, float]] = {}
    for label, series in rows.items():
        if series.empty:
            records[label] = {col: float("nan") for col in CHANGE_COLUMNS}
            continue
        asof = series.index[-1]
        latest = float(series.iloc[-1])
        record = {"Level": latest}
        for key in CHANGE_COLUMNS[1:]:
            prior = series.loc[: _lookback_date(asof, key)]
            if prior.empty:
                record[key] = float("nan")
                continue
            change = round((latest - float(prior.iloc[-1])) * 100.0, 1)
            record[key] = change if change != 0 else 0.0  # normalise -0.0
        records[label] = record

    return pd.DataFrame.from_dict(records, orient="index", columns=list(CHANGE_COLUMNS))


def changes_bps(series: pd.Series, keys: tuple[str, ...] = ("1D", "1W", "1M")) -> dict[str, float]:
    """``{"Level": <latest, native units>, key: <bps change>, ...}``.

    Pure. Each change is the latest observation minus the last observation on or
    before that lookback date (see :func:`_lookback_date`), in basis points;
    ``NaN`` when there is no history to compare against.
    """
    s = series.dropna().sort_index()
    if s.empty:
        return {"Level": float("nan"), **{key: float("nan") for key in keys}}

    asof = s.index[-1]
    latest = float(s.iloc[-1])
    out: dict[str, float] = {"Level": latest}
    for key in keys:
        prior = s.loc[: _lookback_date(asof, key)]
        if prior.empty:
            out[key] = float("nan")
        else:
            change = round((latest - float(prior.iloc[-1])) * 100.0, 1)
            out[key] = change if change != 0 else 0.0  # normalise -0.0
    return out


@st.cache_data(ttl=_CACHE_TTL, show_spinner="Fetching credit spreads from FRED…")
def fetch_credit_spreads(start_date: str, end_date: str) -> pd.DataFrame:
    """HY and IG option-adjusted spreads, one column per :data:`CREDIT_SERIES`
    id, in percentage points (multiply by 100 for basis points)."""
    frame = pd.DataFrame(
        {series_id: fetch_series(series_id, start_date, end_date) for series_id in CREDIT_SERIES}
    )
    frame.index.name = "date"
    return frame.sort_index()


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def fetch_credit_window() -> pd.DataFrame:
    """~14 months of credit spreads — enough for every change lookback
    (1D through 1Y, plus YTD) regardless of the selected date range."""
    end = pd.Timestamp.today().normalize()
    start = end - pd.DateOffset(months=14)
    return fetch_credit_spreads(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def fetch_curve_snapshot() -> pd.DataFrame:
    """:func:`curve_snapshots` over a fixed ~400-day window.

    Fetched independently of the dashboard's date-range selection so the
    "1 year ago" line is always available.
    """
    end = pd.Timestamp.today().normalize()
    start = end - pd.DateOffset(days=400)
    df = fetch_treasury_yields(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    return curve_snapshots(df)


@st.cache_data(ttl=_CACHE_TTL, show_spinner="Fetching yield history…")
def fetch_change_window() -> pd.DataFrame:
    """~14 months of daily yields — enough history for every movement-table
    lookback (1D through 1Y, plus YTD) regardless of the selected date range."""
    end = pd.Timestamp.today().normalize()
    start = end - pd.DateOffset(months=14)
    return fetch_treasury_yields(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def latest_observations() -> dict[str, pd.Timestamp | None]:
    """Most recent observation date FRED currently holds for every dashboard series.

    Independent of the selected date range (fetches a short trailing window per
    series) so the header can report data freshness even when the user is looking
    at a historical range. A series that fails to fetch comes back as ``None``.
    """
    end = pd.Timestamp.today().normalize()
    start = (end - pd.DateOffset(days=120)).strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    out: dict[str, pd.Timestamp | None] = {}
    for series_id in (*TREASURY_SERIES, *INFLATION_SERIES):
        try:
            series = fetch_series(series_id, start, end_str)
        except FredAPIError:
            out[series_id] = None
            continue
        out[series_id] = series.index.max() if not series.empty else None
    return out


def release_overdue(
    series_id: str, as_of: pd.Timestamp | None, today: pd.Timestamp | None = None
) -> bool:
    """True only once the *next* scheduled release should have landed and hasn't.

    The normal lag is not staleness. Daily H.15 series publish every business day
    about one business day behind, so a value one or two business days old is
    current — flag only when the latest observation is more than two business
    days old. Monthly series (CPI, core PCE) publish month M's data in the middle
    of month M+1, so month M staying latest is fine until well into month M+2.
    A missing date counts as overdue.
    """
    if as_of is None:
        return True
    today = (pd.Timestamp.today() if today is None else pd.Timestamp(today)).normalize()
    as_of = pd.Timestamp(as_of).normalize()

    if SERIES_FREQ.get(series_id) == "M":
        deadline = as_of + pd.DateOffset(months=2) + pd.Timedelta(days=20)
    else:
        deadline = pd.Timestamp(np.busday_offset(as_of.date(), 2, roll="forward"))
    return today > deadline


def latest_change(series: pd.Series) -> tuple[pd.Timestamp, float, float | None]:
    """``(as_of, level_pct, change_bps)`` over the last two observation dates.

    ``series`` is a yield or spread in percentage points. ``change_bps`` is the
    move between the two most recent *distinct* dates, already converted to basis
    points here (1 pp = 100 bps) so the UI does no unit math; ``None`` when there
    is only one observation.

    Two fixes over the previous version:
      * duplicate trailing dates are collapsed before differencing, so
        ``iloc[-1] - iloc[-2]`` can't silently compare a date with itself and
        report 0;
      * the pp->bps conversion happens here instead of being split with the UI,
        which is where the old "+0 bps" came from when the ``* 100`` was missing.

    A genuine parallel shift of the curve still gives ``change_bps == 0`` for a
    spread -- that is a flat spread, not a missing move.
    """
    series = series.dropna()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    if series.empty:
        raise FredAPIError("No observations in the selected date range.")

    as_of = series.index[-1]
    level = float(series.iloc[-1])
    if len(series) < 2:
        return as_of, level, None

    change_bps = (series.iloc[-1] - series.iloc[-2]) * 100.0
    return as_of, level, round(float(change_bps), 1)
