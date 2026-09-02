"""Kalshi Fed-decision prediction markets.

Public market data only — the ``api.elections.kalshi.com`` REST API needs no
auth for reads. One 15-minute cached call. Any failure (network, HTTP error,
unexpected JSON) returns an empty frame plus a short note, so the UI degrades
gracefully instead of raising.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_TIMEOUT = 10
_CACHE_TTL = 5 * 60
_HEADERS = {"Accept": "application/json", "User-Agent": "macro-dashboard/1.0 (+streamlit)"}

# The series with one event per FOMC meeting and Hike / Cut / Maintain outcomes.
_KNOWN_FED_SERIES = ("KXFEDDECISION", "KXFED")

COLUMNS = ["Meeting", "Outcome", "Implied prob", "Volume"]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def _get(url: str, **params) -> dict:
    resp = requests.get(url, params=params or None, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _implied_prob(market: dict) -> float:
    """Kalshi ``*_dollars`` prices are already 0-1 probabilities. Prefer the last
    trade, fall back to the yes bid/ask midpoint, then the legacy cent fields."""
    last = _to_float(market.get("last_price_dollars"))
    if last:
        return last
    bid = _to_float(market.get("yes_bid_dollars"))
    ask = _to_float(market.get("yes_ask_dollars"))
    quotes = [q for q in (bid, ask) if q is not None]
    if quotes:
        return sum(quotes) / len(quotes)
    cents = market.get("last_price")  # legacy schema: price in cents
    if cents:
        return cents / 100.0
    return 0.0


def _volume(market: dict) -> int:
    vol = _to_float(market.get("volume_fp"))
    if vol is None:
        vol = _to_float(market.get("volume"))
    return int(round(vol)) if vol is not None else 0


def _fed_series_ticker() -> str | None:
    """Locate the Fed-decision series — try the known tickers, then discover it
    by title from the Economics category."""
    for ticker in _KNOWN_FED_SERIES:
        try:
            if _get(f"{_BASE}/series/{ticker}").get("series"):
                return ticker
        except requests.RequestException:
            continue
    try:
        for series in _get(f"{_BASE}/series", category="Economics").get("series", []):
            title = (series.get("title") or "").lower()
            if "fed" in title and any(word in title for word in ("meeting", "decision")):
                return series.get("ticker")
    except requests.RequestException:
        pass
    return None


def _all_markets(series_ticker: str) -> list[dict]:
    markets: list[dict] = []
    cursor: str | None = None
    for _ in range(15):  # hard cap on pagination
        page = _get(
            f"{_BASE}/markets",
            series_ticker=series_ticker,
            status="open",
            limit=200,
            **({"cursor": cursor} if cursor else {}),
        )
        markets.extend(page.get("markets", []))
        cursor = page.get("cursor")
        if not cursor:
            break
    return markets


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def fed_decision_markets(meetings: int = 2) -> tuple[pd.DataFrame, str | None, datetime]:
    """Implied probabilities for the next ``meetings`` FOMC decisions from Kalshi.

    Returns ``(frame, note, fetched_at)``. ``frame`` columns: Meeting (ISO date),
    Outcome, ``Implied prob`` (0-1), Volume — one row per outcome, grouped by
    meeting (soonest first) then ordered most-likely first. ``note`` is ``None``
    on success, otherwise a short string explaining why data is missing/partial
    and ``frame`` is empty. ``fetched_at`` is the UTC time this ran (cached with
    the result, so it reflects the last real fetch, not the cache read).
    """
    fetched_at = datetime.now(timezone.utc)
    try:
        series = _fed_series_ticker()
        if not series:
            return _empty(), "Kalshi: couldn't locate the Fed-decision series.", fetched_at
        markets = _all_markets(series)
    except requests.RequestException as exc:
        return _empty(), f"Kalshi API unavailable ({type(exc).__name__}).", fetched_at
    except ValueError:
        return _empty(), "Kalshi API returned an unexpected response.", fetched_at
    except Exception as exc:  # noqa: BLE001 - market data must never break the page
        return _empty(), f"Kalshi data error ({type(exc).__name__}).", fetched_at

    if not markets:
        return _empty(), "Kalshi: no open Fed-decision markets right now.", fetched_at

    now = fetched_at
    by_event: dict[str, list[dict]] = {}
    for market in markets:
        by_event.setdefault(market.get("event_ticker", ""), []).append(market)

    def event_close(group: list[dict]) -> datetime:
        times = [t for t in (_parse_dt(m.get("close_time")) for m in group) if t]
        return min(times) if times else datetime.max.replace(tzinfo=timezone.utc)

    upcoming = sorted(
        ((etk, grp) for etk, grp in by_event.items() if event_close(grp) >= now),
        key=lambda item: event_close(item[1]),
    )[:meetings]

    if not upcoming:
        return _empty(), "Kalshi: no upcoming Fed-decision meetings found.", fetched_at

    rows = []
    for _, group in upcoming:
        meeting = event_close(group).date().isoformat()
        for market in group:
            rows.append(
                {
                    "Meeting": meeting,
                    "Outcome": (market.get("yes_sub_title") or market.get("subtitle") or "").strip(),
                    "Implied prob": _implied_prob(market),
                    "Volume": _volume(market),
                }
            )

    frame = (
        pd.DataFrame(rows)
        .sort_values(["Meeting", "Implied prob"], ascending=[True, False])
        .reset_index(drop=True)[COLUMNS]
    )
    return frame, None, fetched_at
