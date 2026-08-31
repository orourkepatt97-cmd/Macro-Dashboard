"""FRED data access for the macro dashboard.

All network calls to FRED live here. Fetch functions are wrapped in
``st.cache_data`` so repeated interactions (changing the date range, reruns)
don't hammer the API. ``app.py`` should import from this module and never
talk to FRED directly.
"""

from __future__ import annotations

import os

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

FRED_API_KEY = os.getenv("FRED_API_KEY")
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# One hour: FRED daily series update once per business day, so this is plenty.
_CACHE_TTL = 60 * 60


class FredAPIError(RuntimeError):
    """Raised when a FRED request fails or the API key is missing."""


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def fetch_series(series_id: str, start_date: str, end_date: str) -> pd.Series:
    """Fetch a single FRED series as a date-indexed float Series.

    ``start_date`` and ``end_date`` are ISO strings (``YYYY-MM-DD``) and are
    passed straight through to the API as the inclusive observation window.
    Missing observations (FRED encodes them as ``"."``) are dropped.
    """
    if not FRED_API_KEY:
        raise FredAPIError(
            "FRED_API_KEY is not set. Create a .env file in the project root "
            "with FRED_API_KEY=your_key (see .env.example)."
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
        # FRED returns a JSON body with an "error_message" on failures.
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
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    series = frame.set_index("date")["value"].dropna()
    series.name = series_id
    return series


@st.cache_data(ttl=_CACHE_TTL, show_spinner="Fetching Treasury yields from FRED…")
def fetch_treasury_yields(start_date: str, end_date: str) -> pd.DataFrame:
    """Return DGS2, DGS10 and the derived 2s10s spread over the window.

    Columns: ``DGS2``, ``DGS10`` (percent), ``2s10s`` (DGS10 - DGS2, percentage
    points). Rows are the union of both series' observation dates.
    """
    dgs2 = fetch_series("DGS2", start_date, end_date)
    dgs10 = fetch_series("DGS10", start_date, end_date)

    df = pd.concat([dgs2, dgs10], axis=1)
    df.columns = ["DGS2", "DGS10"]
    df.index.name = "date"
    df = df.sort_index()
    df["2s10s"] = df["DGS10"] - df["DGS2"]
    return df


def latest_spread(df: pd.DataFrame) -> tuple[pd.Timestamp, float, float | None]:
    """Latest 2s10s observation as ``(date, value, change_vs_previous)``.

    ``change_vs_previous`` is in percentage points and is ``None`` when there is
    only a single observation. Raises ``FredAPIError`` if the window has no data.
    """
    spread = df["2s10s"].dropna()
    if spread.empty:
        raise FredAPIError("No 2s10s observations in the selected date range.")

    latest_value = float(spread.iloc[-1])
    change = float(spread.iloc[-1] - spread.iloc[-2]) if len(spread) > 1 else None
    return spread.index[-1], latest_value, change
