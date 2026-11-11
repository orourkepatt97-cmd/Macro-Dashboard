#!/usr/bin/env python3
"""Standalone data-pipeline validator for the macro dashboard.

Checks the dashboard's data plumbing against the sources themselves, with no
hardcoded expected values. Every threshold here is either a generic policy
keyed off a series' own FRED-reported frequency (release-lag bounds), or
derived from that series' own history (10-year min/max, 5-sigma move) — never
a number specific to one series baked into this script.

    Structural   — per FRED series: does the id resolve, do the observation
                   dates form a complete sequence at the frequency FRED itself
                   reports, is the latest observation within a lag budget
                   derived from that same frequency.
    Transform    — every YoY, spread, rolling-average and realized-vol value
                   data.py/app.py compute is independently recomputed here
                   from raw observations fetched directly (not via
                   data.fetch_series) and compared; any disagreement is an
                   error.
    Shape        — for the non-FRED sources with no second copy (MOVE via
                   Yahoo/yfinance; the FX tab's BIS policy rates and BCRP
                   Peruvian-sol rate): a plausible count of recent observations,
                   fresh latest date, no multi-day silences, values in a sane
                   range — a partial or empty return FAILS. The FX tab's whole
                   carry table is also recomputed from raw fetches of its own.
    Cross-source — FRED's DGS yields against the Fed's own H.15 release CSV;
                   the Cleveland Fed nowcast feed against its own published
                   page (same origin, so this is a reachability/recency check,
                   not independent corroboration — documented at the check);
                   Kalshi FOMC outcome probabilities summing near 100%.
    Sanity       — latest level outside the series' own 10-year min/max
                   (skipped for series whose *level* trends monotonically —
                   CPI/PCE/AHE indices, payroll count — where that check is
                   never meaningful; a move-based check covers those instead),
                   or a single-period move beyond 5 standard deviations of
                   that series' own period-over-period changes.

Prints one PASS/FAIL line per check, with the actual numbers on failure. A
structural gap matching a declared, verified entry in _KNOWN_GAPS (e.g. the
Oct 2025 shutdown gap in the CPI-family series) reports KNOWN instead of
FAIL and doesn't affect the exit code — but every known exception is still
listed, every run, in its own section at the end, so a real permanent
condition stays visible instead of turning into a FAIL you learn to ignore.
Exits 0 if everything passed (KNOWNs don't count against this), 1 otherwise,
so this can run as a scheduled job.

Every run writes its result to validate_status.json next to this script
(skipped only on the fatal early exit when FRED_API_KEY is missing, so a
prior good status isn't clobbered by a run that never really started) — the
dashboard header reads that file to show a small pass/fail status line.

Usage:
    python validate.py            # full report
    python validate.py --quiet    # only failures + the final summary
"""

from __future__ import annotations

import argparse
import calendar
import csv
import inspect
import io
import json
import math
import re
import sys
import time
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import data
import markets

# Written by main() at the end of every completed run; read by data.py's
# validation_status() for the dashboard header. Anchored to this script's own
# directory so the file lands in the same place regardless of the caller's
# cwd (a cron job, a CI runner, or Streamlit itself).
STATUS_FILE = Path(__file__).resolve().parent / "validate_status.json"

# --------------------------------------------------------------------------
# Config: generic policies, not per-series expected values.
# --------------------------------------------------------------------------

_TIMEOUT = 30
_HISTORY_YEARS = 10          # window for min/max + move-distribution checks
_GAP_SCAN_YEARS = 2          # window for the explicit-gap listing (recent health,
                              # not a re-litigation of every old holiday/outage)
_MOVE_SIGMA = 5.0             # "a move beyond N std devs of its own changes"

# Max calendar days FRED's own `last_updated` timestamp for a series may lag
# today, keyed by FRED's own `frequency_short` (D/W/BW/M/Q/SA/A) — i.e. "how
# long since FRED last touched this series' record", not "how old is the
# latest observation's date label" (labeling conventions vary by agency and
# aren't comparable across series; see the comment in audit_series()).
# Monthly/quarterly budgets allow slightly more than one full release cycle
# so a series doesn't read as stale right up until its next release lands.
_LAG_BUDGET_DAYS = {
    "D": 5, "W": 12, "BW": 20, "M": 40, "Q": 100, "SA": 200, "A": 400,
}

# Per-series override of the frequency-class budget above, for a series whose
# own publishing cadence is demonstrably slower than its frequency class
# implies. Every entry carries its evidence — an override with no reason is
# just a loosened check nobody can audit. Anything not listed here uses the
# generic budget.
_LAG_BUDGET_OVERRIDES: dict[str, tuple[int, str]] = {
    "THREEFYTP10": (
        9,  # a 7-day cadence plus 2 days of slack
        "daily observations, but FRED publishes the Kim-Wright term premium in a "
        "weekly batch — every Tuesday, data through the prior Friday (18 "
        "consecutive weekly vintages in FRED's vintage dates), so last_updated is "
        "legitimately up to ~7 days old; this job runs Mondays, when it's ~6",
    ),
}
# The Fed's H.10 (foreign exchange rates) is a weekly release: FRED's vintage
# dates for these series are every Monday (occasionally Tuesday) with the prior
# week's daily rates, so last_updated is legitimately up to ~7 days old and the
# generic daily budget would fail them every Friday. Same evidence as above.
for _pair in data.FX_PAIRS:
    if _pair.spot_id:
        _LAG_BUDGET_OVERRIDES[_pair.spot_id] = (
            9,  # a 7-day cadence plus 2 days of slack
            "daily observations, but FRED publishes the Fed's H.10 exchange rates in a "
            "weekly batch (vintage dates are consecutive Mondays), so last_updated is "
            "legitimately up to ~7 days old",
        )

# MOVE shape check (Yahoo Finance ^MOVE via yfinance — see check_move_shape()).
_MOVE_WINDOW_DAYS = 90          # "recent" = the last 90 calendar days (~61 sessions)
_MOVE_MIN_RECENT_OBS = 50       # fewer than this in the window = a partial/sparse return
_MOVE_MAX_AGE_DAYS = 7          # latest observation no older than this (covers a long weekend)
_MOVE_SANE_RANGE = (40.0, 200.0)  # MOVE has historically lived in roughly 40-200

# Series that are cumulative/monotonic *levels* (a price index or a payroll
# count) rather than a bounded quantity (a rate, a yield, a spread, a claims
# count): their 10-year min is trivially the oldest value and their 10-year
# max is trivially today's, so a min/max check on the level is never
# informative. The move-based check (their period-over-period change) is used
# for these instead; see level_bound_check().
_MONOTONIC_LEVEL_SERIES = {"CPIAUCNS", "CPILFENS", "PCEPILFE", "PAYEMS", "CES0500000003"}

# Known, permanent data gaps — verified against the source, not a pipeline
# bug. A gap whose date matches an entry here reports KNOWN (its own status,
# distinct from PASS/FAIL) instead of FAIL, and does not affect the exit
# code; it's still listed in the "Known exceptions" section every run so it
# stays visible rather than silently disappearing. A gap NOT listed here
# still fails normally — this is an allowlist of specific, already-explained
# dates, not a blanket "ignore gaps in this series."
#
# Only Oct 2025 is actually missing for these four (verified directly against
# FRED: Sep and Nov 2025 both carry real values) — despite the shutdown
# initially being reported as spanning Oct-Nov, Nov's data did land.
_KNOWN_GAPS: dict[str, dict[str, object]] = {
    "CPIAUCNS": {
        "dates": ("2025-10-01",),
        "reason": "Oct 2025 CPI was not published on schedule due to the government "
                  "shutdown; FRED carries no observation for that month.",
    },
    "CPILFENS": {
        "dates": ("2025-10-01",),
        "reason": "Same Oct 2025 shutdown gap as CPIAUCNS (core CPI, NSA).",
    },
    "UNRATE": {
        "dates": ("2025-10-01",),
        "reason": "The October 2025 Employment Situation was not published on "
                  "schedule due to the government shutdown.",
    },
    "CIVPART": {
        "dates": ("2025-10-01",),
        "reason": "Same Oct 2025 shutdown gap as UNRATE (both come from the same "
                  "Employment Situation release).",
    },
}

# FX tab: BIS policy rates + BCRP Peruvian sol (see transform_checks_fx()).
_BIS_MAX_AGE_DAYS = 21          # BIS runs ~a week behind; an area quiet for 3 weeks is broken
_BIS_MAX_SILENCE_DAYS = 7       # longest gap between consecutive observations, last 90d
_BIS_RATE_BAND = (-1.0, 60.0)   # percent: sane range for any policy rate
_PEN_MIN_RECENT_OBS = 15        # of ~21 business days in 30 calendar days
_PEN_MAX_AGE_DAYS = 7
_PEN_MAX_SILENCE_DAYS = 5
_FX_CHANGE_TOL = 0.0051         # the table rounds changes to 2dp, so allow half a unit of the last place
# BIS policy rate vs FRED's monthly call-money series for the same country,
# compared as a month average. They are different measures (a target vs an
# actual overnight rate), so this is a "same ballpark" check that catches a wrong
# country, scale, or stale feed, not an exact tie-out.
_BIS_VS_FRED_RATE = {
    "JP": "IRSTCI01JPM156N", "CA": "IRSTCI01CAM156N", "AU": "IRSTCI01AUM156N",
    "BR": "IRSTCI01BRM156N", "GB": "IRSTCI01GBM156N", "MX": "IRSTCI01MXM156N",
}
_BIS_VS_FRED_TOL = 0.25         # percentage points
# Mexico is checked but reported as a KNOWN exception when it disagrees: as of
# this writing BIS's MX policy rate sits a persistent ~1.3pp above FRED's MX
# call-money series (month averages, every month of 2026), unlike the other five
# countries, which agree within 0.06pp. Unresolved which is right — see CLAUDE.md.
_BIS_VS_FRED_KNOWN_DIFFERENT = {"MX"}

FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES_URL = "https://api.stlouisfed.org/fred/series"

# The Fed's own H.15 "Selected Interest Rates" data-download package. This
# ticker hash is a stable, long-published identifier for the full H.15
# constant-maturity-Treasury package (not something we're guessing at).
_H15_PACKAGE = "bf17364827e38702b42a58cf8eaa3f78"
_H15_URL = "https://www.federalreserve.gov/datadownload/Output.aspx"
# FRED DGS id -> the matching column header in the H.15 CSV.
_H15_COLUMN = {
    "DGS3MO": "RIFLGFCM03_N.B",
    "DGS2": "RIFLGFCY02_N.B",
    "DGS5": "RIFLGFCY05_N.B",
    "DGS10": "RIFLGFCY10_N.B",
    "DGS30": "RIFLGFCY30_N.B",
}

_CLEVELAND_PAGE_URL = "https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting"

_KALSHI_SUM_BAND = (0.85, 1.15)  # sane band for mutually-exclusive outcome prices to sum to


# --------------------------------------------------------------------------
# Result tracking
# --------------------------------------------------------------------------

_PASS = 0
_FAIL = 0
_KNOWN_COUNT = 0
_KNOWN_LOG: list[str] = []
_FAIL_LOG: list[str] = []  # check names only — feeds validate_status.json
_QUIET = False


def check(name: str, ok: bool, detail: str = "") -> bool:
    """Record and print one PASS/FAIL line. Returns ``ok`` for chaining."""
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        if not _QUIET:
            print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        _FAIL += 1
        _FAIL_LOG.append(name)
        print(f"[FAIL] {name} — {detail}")
    return ok


def known(name: str, reason: str) -> None:
    """Record a check outcome that matched a declared, verified exception in
    _KNOWN_GAPS — distinct from both PASS (nothing to see) and FAIL (breaks
    the build): it doesn't touch the exit code. Not printed inline (that
    would just be the same recurring noise this mechanism exists to remove)
    — collected here and printed once, together, in the dedicated "Known
    exceptions" section at the end of the run, so it stays visible as a
    deliberate, scannable list rather than either a scary FAIL or something
    buried in a PASS line's fine print."""
    global _KNOWN_COUNT
    _KNOWN_COUNT += 1
    _KNOWN_LOG.append(f"{name} — {reason}")


def section(title: str) -> None:
    if not _QUIET:
        print(f"\n== {title} ==")


# --------------------------------------------------------------------------
# Independent HTTP fetchers — deliberately NOT data.fetch_series, so a bug in
# that function's request construction or parsing can't hide from this script.
# --------------------------------------------------------------------------

def _fred_get(url: str, params: dict) -> requests.Response:
    """GET against FRED, backing off and retrying on HTTP 429 (its ~120
    requests/minute limit — a full run makes enough calls to brush against it,
    and an unretried 429 would show up as a spurious "no data" failure)."""
    for attempt in range(5):
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        if resp.status_code != 429:
            return resp
        time.sleep(5 * (attempt + 1))
    return resp


def fred_meta(series_id: str) -> dict | None:
    try:
        resp = _fred_get(
            FRED_SERIES_URL,
            {"series_id": series_id, "api_key": data.FRED_API_KEY, "file_type": "json"},
        )
        resp.raise_for_status()
        seriess = resp.json().get("seriess", [])
        return seriess[0] if seriess else None
    except (requests.RequestException, ValueError, IndexError):
        return None


def fred_obs(series_id: str, start: str, end: str) -> pd.Series | None:
    """Raw observations, independently fetched and parsed (own code path)."""
    try:
        resp = _fred_get(
            FRED_OBS_URL,
            {
                "series_id": series_id, "api_key": data.FRED_API_KEY,
                "file_type": "json", "observation_start": start, "observation_end": end,
            },
        )
        resp.raise_for_status()
        rows = resp.json().get("observations", [])
    except (requests.RequestException, ValueError):
        return None
    out: dict[pd.Timestamp, float] = {}
    for row in rows:
        try:
            val = float(row["value"])
        except (TypeError, ValueError):
            continue  # "." = missing
        out[pd.Timestamp(row["date"])] = val
    series = pd.Series(out, dtype="float64").sort_index()
    series.name = series_id
    return series


# --------------------------------------------------------------------------
# Structural checks
# --------------------------------------------------------------------------

def _expected_step(freq_short: str) -> str | pd.Timedelta | None:
    """The calendar step between consecutive observations for a frequency, or
    ``None`` for daily/business-day series (handled by a max-gap heuristic
    below instead, since holidays make an exact step meaningless)."""
    return {"W": pd.Timedelta(days=7), "M": "MS", "Q": "QS"}.get(freq_short)


def _find_gaps(dates: pd.DatetimeIndex, freq_short: str) -> list[dict[str, str]]:
    """Explicit list of gap records, or ``[]`` if none. Each record is
    ``{"key": ..., "detail": ...}`` — ``key`` is the canonical date checked
    against ``_KNOWN_GAPS`` (the missing period itself for monthly/quarterly;
    the date silence resumes on for daily/weekly), ``detail`` is the
    human-readable description used when it's NOT a known exception.

    Daily/business series: any run without an observation for more than the
    lag budget's worth of calendar days flags a gap (tolerates ordinary
    weekends and holidays without listing every one of them individually).
    Weekly: any consecutive step != 7 days. Monthly/quarterly: any missing
    period on a full calendar grid built from the series' own first/last date.
    """
    if len(dates) < 2:
        return []
    step = _expected_step(freq_short)
    if step is None:  # daily / business-day: gap = an unusually long silence
        max_gap = _LAG_BUDGET_DAYS.get(freq_short, 5)
        gaps = []
        diffs = dates.to_series().diff().dt.days.dropna()
        for prev, cur, days in zip(dates[:-1], dates[1:], diffs):
            if days > max_gap:
                gaps.append({"key": str(cur.date()),
                             "detail": f"{prev.date()} -> {cur.date()} ({int(days)}d silence)"})
        return gaps
    if isinstance(step, pd.Timedelta):
        diffs = dates.to_series().diff().dt.days.dropna()
        return [
            {"key": str(cur.date()),
             "detail": f"{prev.date()} -> {cur.date()} ({int(days)}d, expected {step.days}d)"}
            for prev, cur, days in zip(dates[:-1], dates[1:], diffs)
            if days != step.days
        ]
    # "MS" / "QS": build the full calendar grid and diff against what's there.
    full = pd.date_range(dates.min(), dates.max(), freq=step)
    missing = full.difference(dates)
    return [{"key": str(d.date()), "detail": str(d.date())} for d in missing]


def audit_series(series_id: str) -> tuple[pd.Series, dict] | None:
    """Structural checks for one series. Returns ``(10y series, meta)`` for
    reuse by the sanity-bound checks, or ``None`` if the series doesn't
    resolve (callers skip everything downstream for it)."""
    meta = fred_meta(series_id)
    if not check(f"{series_id}: series resolves on FRED", meta is not None,
                 "no matching series id" if meta is None else ""):
        return None

    today = pd.Timestamp.today().normalize()
    start = (today - pd.DateOffset(years=_HISTORY_YEARS)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    series = fred_obs(series_id, start, end)
    series_ok = series is not None and not series.empty
    check(f"{series_id}: observations endpoint returns data", series_ok,
          "empty/failed observations response" if not series_ok else "")
    if not series_ok:
        return None

    freq_short = meta.get("frequency_short", "")

    recent = series[series.index >= today - pd.DateOffset(years=_GAP_SCAN_YEARS)]
    gaps = _find_gaps(recent.index, freq_short)
    known_cfg = _KNOWN_GAPS.get(series_id)
    known_dates = set(known_cfg["dates"]) if known_cfg else set()
    unknown_gaps = [g for g in gaps if g["key"] not in known_dates]
    matched_known = [g for g in gaps if g["key"] in known_dates]

    check(f"{series_id}: no unexpected gaps in last {_GAP_SCAN_YEARS}y at {freq_short or '?'} frequency",
          not unknown_gaps,
          "; ".join(g["detail"] for g in unknown_gaps) if unknown_gaps else "")
    for g in matched_known:
        known(f"{series_id}: gap at {g['key']}", known_cfg["reason"])

    override = _LAG_BUDGET_OVERRIDES.get(series_id)
    lag_budget = override[0] if override else _LAG_BUDGET_DAYS.get(freq_short, 60)
    last_updated_raw = meta.get("last_updated")
    last_updated = pd.Timestamp(last_updated_raw).tz_localize(None) if last_updated_raw else None
    if last_updated is None:
        check(f"{series_id}: FRED metadata has a last_updated timestamp", False, "field missing")
    else:
        # Freshness is judged against *last_updated* (when FRED's own record was
        # touched), not the latest observation's calendar-label age: agencies
        # label a release's reference period differently (e.g. BEA's PCE dates
        # a release by the covered month's 1st despite structurally releasing
        # ~26 days after month-end, and BLS's continuing-claims print carries
        # an extra week of built-in lag vs. initial claims) — so "age of the
        # label" isn't comparable across series the way "time since FRED last
        # touched it" is. Budget is still generic-by-frequency, not per-series.
        age_days = (today - last_updated.normalize()).days
        fresh = age_days <= lag_budget
        check(f"{series_id}: FRED last updated this series within {lag_budget}d for '{freq_short}' frequency"
              + (" (per-series override)" if override else ""),
              fresh,
              f"last_updated={last_updated_raw} is {age_days}d old (budget {lag_budget}d"
              # the override's rationale is only worth repeating when it's failing
              + (f", overridden because: {override[1]}" if override and not fresh else "")
              + f"); latest observation dated {series.index.max().date()}")

    return series, meta


# --------------------------------------------------------------------------
# Transform checks — independent recomputation vs. what data.py returns.
# --------------------------------------------------------------------------

def _independent_yoy(raw: pd.Series) -> pd.Series:
    """A from-scratch YoY-by-calendar-month implementation, deliberately not
    sharing code with data.yoy_change(), so this can catch a bug in that
    function rather than just re-running it."""
    by_month = {(ts.year, ts.month): val for ts, val in raw.items()}
    out = {}
    for ts, val in raw.items():
        prior = by_month.get((ts.year - 1, ts.month))
        if prior is not None and prior != 0:
            out[ts] = (val / prior - 1.0) * 100.0
    return pd.Series(out, dtype="float64").sort_index()


def transform_checks_yoy(today: pd.Timestamp) -> None:
    section("Transform checks — YoY (independent date-offset recomputation)")
    start = (today - pd.DateOffset(years=3)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    app_infl = data.fetch_inflation(start, end)
    for series_id in ("CPIAUCNS", "CPILFENS", "PCEPILFE"):
        raw = fred_obs(series_id, (today - pd.DateOffset(years=4)).strftime("%Y-%m-%d"), end)
        if raw is None or raw.empty:
            check(f"YoY {series_id}: raw source available for recomputation", False, "fetch failed")
            continue
        expected = _independent_yoy(raw)
        got = app_infl[series_id].dropna()
        common = expected.index.intersection(got.index)
        common = common[common >= today - pd.DateOffset(years=2)]
        if common.empty:
            check(f"YoY {series_id}: overlapping dates to compare", False, "no common dates")
            continue
        diffs = (expected.loc[common] - got.loc[common]).abs()
        worst = diffs.idxmax()
        yoy_ok = diffs.max() < 1e-6
        check(f"YoY {series_id}: matches independent date-offset recomputation "
              f"({len(common)} months compared)",
              yoy_ok,
              "" if yoy_ok else
              f"largest disagreement at {worst.date()}: data.py={got.loc[worst]:.4f}% "
              f"independent={expected.loc[worst]:.4f}%")

    # AHE — computed inline in app.py via data.yoy_change on raw labor levels.
    lab = data.fetch_labor(start, end)
    ahe_raw = fred_obs("CES0500000003", (today - pd.DateOffset(years=4)).strftime("%Y-%m-%d"), end)
    if ahe_raw is not None and not ahe_raw.empty:
        expected = _independent_yoy(ahe_raw)
        got = data.yoy_change(lab["CES0500000003"])
        common = expected.index.intersection(got.index)
        common = common[common >= today - pd.DateOffset(years=2)]
        if common.empty:
            check("YoY CES0500000003 (AHE): overlapping dates to compare", False, "no common dates")
        else:
            diffs = (expected.loc[common] - got.loc[common]).abs()
            worst = diffs.idxmax()
            ahe_ok = diffs.max() < 1e-6
            check(f"YoY CES0500000003 (AHE): matches independent recomputation "
                  f"({len(common)} months compared)",
                  ahe_ok,
                  "" if ahe_ok else
                  f"largest disagreement at {worst.date()}: app={got.loc[worst]:.4f}% "
                  f"independent={expected.loc[worst]:.4f}%")
    else:
        check("YoY CES0500000003 (AHE): raw source available for recomputation", False, "fetch failed")


def transform_checks_spreads(today: pd.Timestamp) -> None:
    section("Transform checks — spreads (independently recomputed from raw legs)")
    start = (today - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    for short_id, long_id, label in (("DGS2", "DGS10", "2s10s"), ("DGS5", "DGS30", "5s30s")):
        app_spread = data.spread(data.fetch_treasury_yields(start, end), short_id, long_id)
        raw_short = fred_obs(short_id, start, end)
        raw_long = fred_obs(long_id, start, end)
        if raw_short is None or raw_long is None:
            check(f"Spread {label}: raw legs available", False, "fetch failed")
            continue
        expected = (raw_long - raw_short).dropna()
        common = expected.index.intersection(app_spread.index)
        if common.empty:
            check(f"Spread {label}: overlapping dates to compare", False, "no common dates")
            continue
        diffs = (expected.loc[common] - app_spread.loc[common]).abs()
        worst = diffs.idxmax()
        spread_ok = diffs.max() < 1e-9
        check(f"Spread {label}: matches independent recomputation ({len(common)} days compared)",
              spread_ok,
              "" if spread_ok else
              f"largest disagreement at {worst.date()}: data.py={app_spread.loc[worst]:.6f} "
              f"independent={expected.loc[worst]:.6f}")

    # HY-IG quality spread (computed inline in app.py's Credit tab).
    app_credit = data.fetch_credit_spreads(start, end)
    raw_hy = fred_obs("BAMLH0A0HYM2", start, end)
    raw_ig = fred_obs("BAMLC0A0CM", start, end)
    if raw_hy is None or raw_ig is None:
        check("Spread HY-IG: raw legs available", False, "fetch failed")
    else:
        expected = (raw_hy - raw_ig).dropna() * 100.0
        got = (app_credit["BAMLH0A0HYM2"] - app_credit["BAMLC0A0CM"]).dropna() * 100.0
        common = expected.index.intersection(got.index)
        if common.empty:
            check("Spread HY-IG: overlapping dates to compare", False, "no common dates")
        else:
            diffs = (expected.loc[common] - got.loc[common]).abs()
            worst = diffs.idxmax()
            hy_ig_ok = diffs.max() < 1e-6
            check(f"Spread HY-IG: matches independent recomputation ({len(common)} days compared)",
                  hy_ig_ok,
                  "" if hy_ig_ok else
                  f"largest disagreement at {worst.date()}: app={got.loc[worst]:.4f}bps "
                  f"independent={expected.loc[worst]:.4f}bps")


def transform_checks_rolling(today: pd.Timestamp) -> None:
    section("Transform checks — rolling average (independently recomputed)")
    start = (today - pd.DateOffset(weeks=20)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    raw = fred_obs("ICSA", start, end)
    if raw is None or len(raw) < 4:
        check("Rolling avg ICSA 4-week: raw source available", False, "fetch failed or too short")
        return
    lab = data.fetch_labor(start, end)
    app_ma = lab["ICSA"].dropna().rolling(4).mean()  # what app.py actually computes

    mismatches = []
    for i in range(3, len(raw)):
        window = raw.iloc[i - 3 : i + 1]
        manual_mean = sum(window.values) / 4.0  # plain arithmetic, not pandas rolling
        as_of = raw.index[i]
        if as_of not in app_ma.index:
            continue
        app_val = app_ma.loc[as_of]
        if pd.isna(app_val) or abs(manual_mean - app_val) > 1e-6:
            mismatches.append((as_of, manual_mean, app_val))
    check(f"Rolling avg ICSA 4-week: matches manual mean of the trailing 4 raw prints "
          f"({len(raw) - 3} weeks compared)",
          not mismatches,
          "; ".join(f"{d.date()}: manual={m:.1f} app={a}" for d, m, a in mismatches[:5]))


def _independent_realized_vol(raw: pd.Series, days: int) -> pd.Series:
    """Annualized realized vol (bps) from daily yield levels, in plain Python:
    for each date with a full window, the sample (n-1) standard deviation of
    the previous ``days`` daily changes in bps, times sqrt(252). Deliberately
    not pandas ``rolling().std()``, so a window-alignment or ddof bug in
    data.realized_vol_10y() can't be mirrored here."""
    levels, dates = raw.tolist(), raw.index
    changes = [(levels[i] - levels[i - 1]) * 100.0 for i in range(1, len(levels))]
    out: dict[pd.Timestamp, float] = {}
    for end in range(days, len(changes) + 1):
        window = changes[end - days : end]   # changes[k] is the move into dates[k + 1]
        mean = sum(window) / days
        variance = sum((x - mean) ** 2 for x in window) / (days - 1)
        out[dates[end]] = math.sqrt(variance) * math.sqrt(252)
    return pd.Series(out, dtype="float64").sort_index()


def transform_checks_realized_vol(today: pd.Timestamp) -> None:
    section("Transform checks — realized 10Y vol (independent recomputation)")
    start = (today - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    raw = fred_obs("DGS10", (today - pd.DateOffset(years=1, days=90)).strftime("%Y-%m-%d"), end)
    if raw is None or raw.empty:
        check("Realized vol: raw DGS10 available for recomputation", False, "fetch failed")
        return

    # The window the app actually uses (data.py's default), plus a second window
    # so the *parameter* is exercised rather than one hardcoded value.
    default_days = inspect.signature(data.realized_vol_10y).parameters["days"].default
    for days in sorted({10, default_days}):
        got = data.realized_vol_10y(start, end, days)
        expected = _independent_realized_vol(raw, days)
        common = expected.index.intersection(got.index)
        common = common[common >= pd.Timestamp(start)]
        if common.empty:
            check(f"Realized vol ({days}d): overlapping dates to compare", False, "no common dates")
            continue
        diffs = (expected.loc[common] - got.loc[common]).abs()
        worst = diffs.idxmax()
        ok = diffs.max() < 1e-6
        check(f"Realized vol ({days}d): matches independent recomputation ({len(common)} days compared)",
              ok,
              "" if ok else
              f"largest disagreement at {worst.date()}: data.py={got.loc[worst]:.4f}bp "
              f"independent={expected.loc[worst]:.4f}bp")


# --------------------------------------------------------------------------
# FX tab — independent recomputation, feed shape, cross-source
# --------------------------------------------------------------------------

_BCRP_MONTH_NUM = {m: i for i, m in enumerate(
    ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Set", "Oct", "Nov", "Dic"], start=1)}


def _months_back(day: pd.Timestamp, months: int) -> pd.Timestamp:
    """Same day-of-month ``months`` earlier, clamped to the month's last day —
    stdlib calendar arithmetic, not pandas DateOffset."""
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    return pd.Timestamp(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _raw_bcrp_pen(start: str, end: str) -> list[tuple[pd.Timestamp, float]] | None:
    """Peruvian sol per USD straight from BCRP, parsed with a regex over the raw
    text (data.py parses the JSON) so a parsing bug can't be mirrored here."""
    url = f"{data.BCRP_PEN_URL}/{start}/{end}"
    for attempt in range(3):
        if attempt:
            time.sleep(1.5)
        try:
            text = requests.get(url, timeout=_TIMEOUT).text
        except requests.RequestException:
            continue
        rows = re.findall(r'"name":"(\d{2})\.([A-Za-z]{3})\.(\d{2})",\s*"values":\["([^"]*)"\]', text)
        if not rows:
            continue
        out: dict[pd.Timestamp, float] = {}
        for day, month, year, value in rows:
            if month not in _BCRP_MONTH_NUM:
                return None
            try:
                out[pd.Timestamp(2000 + int(year), _BCRP_MONTH_NUM[month], int(day))] = float(value)
            except ValueError:
                continue                      # "n.d." — no data that day
        return sorted(out.items())
    return None


def check_fx_live(today: pd.Timestamp, app_spot: pd.DataFrame, app_rates: pd.DataFrame) -> None:
    """The FX table's live prices (Yahoo hourly bars): fetched here with a
    different yfinance call (per-ticker ``Ticker.history``, not the app's batch
    ``download``), reference prices recomputed with plain loops, shape-checked,
    and cross-checked against the settled H.10 rates. A GitHub runner may be
    blocked by Yahoo (same environmental caveat as MOVE)."""
    section("FX tab — live prices (Yahoo hourly bars)")
    live, note = data.fetch_fx_live()
    if not check("FX live: fetch returned data", note is None and not live.empty,
                 "" if note is None and not live.empty else (note or "empty frame")):
        return
    try:
        import yfinance as yf
    except ImportError:
        check("FX live: yfinance importable for independent fetch", False, "not installed")
        return

    table = data.carry_table(app_spot, app_rates, live)
    for ccy, ticker in data.YAHOO_FX_TICKERS.items():
        pair = next(p for p in data.FX_PAIRS if p.ccy == ccy)
        try:
            hist = yf.Ticker(ticker).history(period="2mo", interval="1h", auto_adjust=False, timeout=20)
            bars = [(ts.tz_convert("UTC"), float(v)) for ts, v in hist["Close"].dropna().items() if v > 0]
        except Exception as exc:  # noqa: BLE001
            check(f"FX live {ccy}: independent hourly fetch", False, f"{type(exc).__name__}: {exc}")
            continue
        bars.sort()
        if not bars:
            check(f"FX live {ccy}: independent hourly fetch returned bars", False, "no bars")
            continue

        # shape: enough bars, fresh, no multi-day silences (weekends excepted), sane values
        stamps = [t for t, _ in bars]
        age = pd.Timestamp.now(tz="UTC") - stamps[-1]
        silences = [(a.date(), b.date()) for a, b in zip(stamps, stamps[1:]) if (b - a) > pd.Timedelta(days=4)]
        check(f"FX live {ccy}: >= 200 hourly bars in 2 months, latest within 5 days, no silences > 4 days",
              len(bars) >= 200 and age <= pd.Timedelta(days=5) and not silences,
              f"{len(bars)} bars, latest {stamps[-1]:%Y-%m-%d %H:%M} UTC ({age.days}d {age.seconds // 3600}h old)"
              + (f"; silences: {silences[:3]}" if silences else ""))

        # reference prices: last bar on or before 24h / 1 month earlier (plain loops + bisect)
        times = [t for t, _ in bars]
        last_t, last_p = bars[-1]
        refs = []
        for target in (last_t - pd.Timedelta(hours=24), _months_back(last_t.tz_convert(None), 1).tz_localize("UTC")
                       + pd.Timedelta(hours=last_t.hour, minutes=last_t.minute, seconds=last_t.second)):
            pos = bisect_right(times, target) - 1
            refs.append(bars[pos][1] if pos >= 0 else float("nan"))
        # the reference must be a genuinely different, earlier bar (not the live bar itself),
        # and the feed must not be a frozen tick: >= 2 distinct values in the last 6 bars
        ref_gap = last_t - times[bisect_right(times, last_t - pd.Timedelta(hours=24)) - 1] \
            if bisect_right(times, last_t - pd.Timedelta(hours=24)) > 0 else None
        distinct = len({round(v, 8) for _, v in bars[-6:]})
        check(f"FX live {ccy}: 24h reference is an earlier bar (12-96h before) and the feed is not a frozen tick",
              ref_gap is not None and pd.Timedelta(hours=12) <= ref_gap <= pd.Timedelta(hours=96) and distinct >= 2,
              f"reference gap {ref_gap}, {distinct} distinct values in the last 6 bars")
        got = live.loc[ccy] if ccy in live.index else None
        if got is None:
            check(f"FX live {ccy}: present in the app's live frame", False, "dropped by the app (stale/implausible)")
            continue
        problems = []
        # the two ends of each change are historical bars, so they must agree exactly;
        # the live price itself moves between two fetches, so allow a small gap
        for name, ours, theirs in (("Ref1D", got["Ref1D"], refs[0]), ("Ref1M", got["Ref1M"], refs[1])):
            if not (math.isnan(theirs) and pd.isna(ours)) and not abs(ours - theirs) < 1e-9:
                problems.append(f"{name} app={ours} independent={theirs}")
        if abs(got["Price"] / last_p - 1.0) > 0.005:
            problems.append(f"Price app={got['Price']} independent={last_p} (>0.5% apart)")
        check(f"FX live {ccy}: reference prices (24h, 1 month) match independent recomputation; "
              f"live price within 0.5% of a fresh fetch", not problems, "; ".join(problems))

        # the table's 1D / 1M follow the formula from the app's own price and the independent refs
        row = table.loc[pair.pair]
        bad = []
        for col, ref in (("1D %", refs[0]), ("1M %", refs[1])):
            if math.isnan(ref):
                continue
            exp = ((got["Price"] / ref - 1.0) if pair.usd_per_fc else (ref / got["Price"] - 1.0)) * 100.0
            if not abs(exp - row[col]) <= _FX_CHANGE_TOL:
                bad.append(f"{col} table={row[col]} independent={exp:.4f}")
        if abs(row["Spot"] - got["Price"]) > 1e-9:
            bad.append(f"Spot table={row['Spot']} live={got['Price']}")
        check(f"FX live {pair.pair}: table spot, 1D and 1M follow from the live price and references",
              not bad, "; ".join(bad))

        # cross-check against the settled H.10 series: catches a mis-scaled or inverted Yahoo ticker
        settled = app_spot[ccy].dropna() if ccy in app_spot else pd.Series(dtype="float64")
        if not settled.empty:
            gap = abs(got["Price"] / float(settled.iloc[-1]) - 1.0)
            check(f"FX live {ccy}: live price within {data._FX_LIVE_MAX_GAP:.0%} of the last settled H.10 rate "
                  f"({settled.index[-1]:%b %d})", gap <= data._FX_LIVE_MAX_GAP,
                  f"live={got['Price']:.4f} settled={float(settled.iloc[-1]):.4f} ({gap:.1%} apart)")
            # same-hour comparison: Yahoo's noon-NY bar vs that day's H.10 fix, which should agree closely
            noon = [(t, v) for t, v in bars if t.tz_convert("America/New_York").hour == 12]
            diffs = []
            for t, v in noon:
                day = t.tz_convert("America/New_York").tz_localize(None).normalize()
                if day in settled.index:
                    diffs.append(abs(v / float(settled.loc[day]) - 1.0))
            if diffs:
                check(f"FX live {ccy}: Yahoo noon-NY bars agree with H.10 fixes (<= 1.5% each, {len(diffs)} days)",
                      max(diffs) <= 0.015, f"worst {max(diffs):.2%}, mean {sum(diffs) / len(diffs):.2%}")


def _bcrp_series_name() -> str | None:
    """The series name BCRP itself reports for PD04638PD (its units are in the name)."""
    url = f"{data.BCRP_PEN_URL}/{(pd.Timestamp.today() - pd.Timedelta(days=10)):%Y-%m-%d}/{pd.Timestamp.today():%Y-%m-%d}"
    for attempt in range(3):
        if attempt:
            time.sleep(1.5)
        try:
            found = re.search(r'"name":"(Tipo de cambio[^"]*)"', requests.get(url, timeout=_TIMEOUT).text)
        except requests.RequestException:
            continue
        if found:
            return found.group(1)
    return None


def _raw_bis_policy(areas: list[str], start: str) -> dict[str, list[tuple[pd.Timestamp, float]]] | None:
    """BIS central-bank policy rates by area, read with the csv module."""
    url = data.BIS_POLICY_URL + "+".join(areas)
    try:
        resp = requests.get(
            url, params={"startPeriod": start, "detail": "dataonly"}, timeout=45,
            headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"},
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None
    out: dict[str, dict[pd.Timestamp, float]] = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        try:
            value = float(row["OBS_VALUE"])
            if not math.isfinite(value):
                continue                      # a missing-value marker (e.g. "NaN")
            out.setdefault(row["REF_AREA"], {})[pd.Timestamp(row["TIME_PERIOD"])] = value
        except (KeyError, ValueError):
            continue
    return {area: sorted(obs.items()) for area, obs in out.items()}


def _on_or_before(rows: list[tuple[pd.Timestamp, float]], target: pd.Timestamp) -> float | None:
    """The value of the last (date, value) row dated on or before ``target``."""
    pos = bisect_right([d for d, _ in rows], target) - 1
    return rows[pos][1] if pos >= 0 else None


def _independent_fx_row(rows: list[tuple[pd.Timestamp, float]], usd_per_fc: bool) -> dict[str, float]:
    """Latest level, 1D / 1M % change of the foreign currency vs the USD, and
    63-observation realized vol — plain loops, bisect and math.log."""
    asof, latest = rows[-1]
    out: dict[str, float] = {"Spot": latest}
    for key, target in (("1D %", asof - pd.Timedelta(days=1)), ("1M %", _months_back(asof, 1))):
        before = _on_or_before(rows, target)
        out[key] = float("nan") if before is None else (
            ((latest / before - 1.0) if usd_per_fc else (before / latest - 1.0)) * 100.0)
    window = [v for _, v in rows[-(data.FX_VOL_DAYS + 1):]]
    if len(window) == data.FX_VOL_DAYS + 1:
        rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        out["Vol %"] = math.sqrt(var) * math.sqrt(252) * 100.0
    else:
        out["Vol %"] = float("nan")
    return out


def transform_checks_fx(today: pd.Timestamp) -> dict[str, pd.Series]:
    """Recompute the FX tab's carry table from raw sources fetched here and
    compare it with data.carry_table() over the same data the app uses; shape-
    check the BIS and BCRP feeds; cross-check BIS policy rates against FRED.
    Returns the PEN history (for the generic own-history sanity bounds)."""
    section("FX tab — independent recomputation, feed shape, cross-source")
    start = (today - pd.DateOffset(months=14)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    app_spot, app_rates, notes = data.fetch_fx_window()
    check("FX: app fetch returned spot and policy rates without notes",
          not notes and not app_spot.empty and not app_rates.empty, "; ".join(notes))
    table = data.carry_table(app_spot, app_rates)
    check_fx_live(today, app_spot, app_rates)

    # --- raw sources, fetched independently -------------------------------------------
    raw: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    for pair in data.FX_PAIRS:
        if pair.spot_id:
            series = fred_obs(pair.spot_id, start, end)
            rows = [] if series is None else list(zip(series.index, series.tolist()))
        else:
            rows = _raw_bcrp_pen(start, end) or []
        if check(f"FX {pair.ccy}: raw spot fetched for recomputation", bool(rows),
                 "" if rows else "fetch failed or empty"):
            raw[pair.ccy] = rows

    areas = [data.FX_USD_AREA, *(p.area for p in data.FX_PAIRS)]
    bis = _raw_bis_policy(areas, start)
    if not check("FX: raw BIS policy rates fetched", bool(bis), "" if bis else "fetch failed or empty"):
        bis = {}

    # --- recomputation vs the app's table -----------------------------------------------
    us_rows = bis.get(data.FX_USD_AREA, [])
    us_latest = us_rows[-1][1] if us_rows else float("nan")
    for pair in data.FX_PAIRS:
        if pair.ccy not in raw:
            continue
        exp = _independent_fx_row(raw[pair.ccy], pair.usd_per_fc)
        got = table.loc[pair.pair]
        problems = []
        if abs(exp["Spot"] - got["Spot"]) > 1e-9:
            problems.append(f"Spot app={got['Spot']} independent={exp['Spot']}")
        for key in ("1D %", "1M %"):
            if not (math.isnan(exp[key]) and pd.isna(got[key])) and not abs(exp[key] - got[key]) <= _FX_CHANGE_TOL:
                problems.append(f"{key} app={got[key]} independent={exp[key]:.4f}")
        if not (math.isnan(exp["Vol %"]) and pd.isna(got["Vol %"])) and not abs(exp["Vol %"] - got["Vol %"]) < 1e-6:
            problems.append(f"Vol % app={got['Vol %']:.6f} independent={exp['Vol %']:.6f}")
        if raw[pair.ccy][-1][0] != got["Spot as of"]:
            problems.append(f"Spot as of app={got['Spot as of']} independent={raw[pair.ccy][-1][0]}")
        check(f"FX {pair.pair}: spot, 1D/1M change and 63d realized vol match independent recomputation",
              not problems, "; ".join(problems))

        rate_rows = bis.get(pair.area, [])
        if rate_rows and not math.isnan(us_latest):
            vs = round(rate_rows[-1][1] - us_latest, 4)
            exp_cv = vs / exp["Vol %"] if exp["Vol %"] > 0 else float("nan")
            bad = []
            if abs(rate_rows[-1][1] - got["Rate %"]) > 1e-9:
                bad.append(f"Rate app={got['Rate %']} independent={rate_rows[-1][1]}")
            if abs(vs - got["vs USD"]) > 1e-9:
                bad.append(f"vs USD app={got['vs USD']} independent={vs}")
            if not (math.isnan(exp_cv) and pd.isna(got["Carry/vol"])) and not abs(exp_cv - got["Carry/vol"]) < 1e-6:
                bad.append(f"Carry/vol app={got['Carry/vol']} independent={exp_cv}")
            check(f"FX {pair.pair}: policy rate, differential vs USD and carry/vol match independent recomputation",
                  not bad, "; ".join(bad))

    # --- quote orientation, read from the sources' own labels --------------------------
    # The recomputation above reuses FxPair.usd_per_fc, so a wrong flag would be
    # mirrored, not caught. The flag decides the sign of every 1D/1M change, so check
    # it against what each source says its series is.
    for pair in data.FX_PAIRS:
        if pair.spot_id:
            meta = fred_meta(pair.spot_id)
            title = (meta or {}).get("title", "")
            says_usd_per_fc = title.startswith("U.S. Dollars to ")
            says_fc_per_usd = title.endswith(" to U.S. Dollar") or " to U.S. Dollar " in title
            ok = meta is not None and (says_usd_per_fc != says_fc_per_usd) and says_usd_per_fc == pair.usd_per_fc
            check(f"FX {pair.ccy}: FRED {pair.spot_id} is quoted "
                  f"{'USD per ' if pair.usd_per_fc else ''}{pair.ccy}{'' if pair.usd_per_fc else ' per USD'}, as the tab assumes",
                  ok, f"FRED title: {title!r}, tab assumes usd_per_fc={pair.usd_per_fc}")
        else:
            name = _bcrp_series_name()
            ok = name is not None and "S/ por US$" in name and not pair.usd_per_fc
            check(f"FX {pair.ccy}: BCRP series is soles per US$, as the tab assumes", ok,
                  f"BCRP name: {name!r}, tab assumes usd_per_fc={pair.usd_per_fc}")

    # --- BIS feed shape -------------------------------------------------------------------
    missing = [a for a in areas if a not in bis]
    check("BIS: every requested country returned data", not missing,
          f"missing: {', '.join(missing)}" if missing else "")
    for area, rows in bis.items():
        age = (today - rows[-1][0]).days
        recent = [(d, v) for d, v in rows if d >= today - pd.Timedelta(days=90)]
        gaps = [(a[0].date(), b[0].date()) for a, b in zip(recent, recent[1:])
                if (b[0] - a[0]).days > _BIS_MAX_SILENCE_DAYS]
        lo, hi = _BIS_RATE_BAND
        ok = (age <= _BIS_MAX_AGE_DAYS and not gaps and len([1 for d, _ in rows if d >= today - pd.Timedelta(days=30)]) >= 10
              and lo <= rows[-1][1] <= hi)
        check(f"BIS {area}: fresh (<= {_BIS_MAX_AGE_DAYS}d), no silences > {_BIS_MAX_SILENCE_DAYS}d, "
              f"sane rate in [{lo:g}, {hi:g}]", ok,
              f"latest {rows[-1][0].date()} = {rows[-1][1]:g} ({age}d old)"
              + (f"; silences: {gaps[:3]}" if gaps else ""))

    # --- BIS vs FRED ----------------------------------------------------------------------
    def _fred_on_or_before(series_id: str, day: pd.Timestamp) -> float | None:
        series = fred_obs(series_id, (day - pd.Timedelta(days=14)).strftime("%Y-%m-%d"),
                          (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
        return None if series is None or series.empty else _on_or_before(
            list(zip(series.index, series.tolist())), day)

    if bis.get("XM"):
        day, value = bis["XM"][-1]
        ecb = _fred_on_or_before("ECBDFR", day)
        check(f"BIS XM vs FRED ECBDFR on {day.date()}: ECB deposit facility rate agrees",
              ecb is not None and abs(ecb - value) <= 0.01, f"BIS={value:g} FRED={ecb}")
    if us_rows:
        day, value = us_rows[-1]
        upper, lower = _fred_on_or_before("DFEDTARU", day), _fred_on_or_before("DFEDTARL", day)
        mid = None if upper is None or lower is None else (upper + lower) / 2.0
        check(f"BIS US vs FRED DFEDTARU/DFEDTARL on {day.date()}: BIS is the mid-point of the Fed target range",
              mid is not None and abs(mid - value) <= 0.01, f"BIS={value:g} FRED mid-point={mid}")

    for area, fred_id in _BIS_VS_FRED_RATE.items():
        rows = bis.get(area)
        fred = fred_obs(fred_id, (today - pd.DateOffset(months=5)).strftime("%Y-%m-%d"), end)
        if not rows or fred is None or fred.empty:
            check(f"BIS {area} vs FRED {fred_id}: both sources available", False, "one side empty")
            continue
        month = fred.index[-1]
        in_month = [v for d, v in rows if d.year == month.year and d.month == month.month]
        if len(in_month) < 5:
            check(f"BIS {area} vs FRED {fred_id}: BIS has data for {month:%b %Y}", False,
                  f"{len(in_month)} observations")
            continue
        avg = sum(in_month) / len(in_month)
        diff = avg - float(fred.iloc[-1])
        label = (f"BIS {area} vs FRED {fred_id} ({month:%b %Y} average): within "
                 f"{_BIS_VS_FRED_TOL:g}pp")
        detail = f"BIS avg={avg:.3f} FRED={float(fred.iloc[-1]):.3f} diff={diff:+.3f}pp"
        if abs(diff) <= _BIS_VS_FRED_TOL:
            check(label, True, detail)
        elif area in _BIS_VS_FRED_KNOWN_DIFFERENT:
            known(label, f"{detail} — persistent across 2026, cause unresolved; the tab uses BIS")
        else:
            check(label, False, detail)

    # --- BCRP shape ---------------------------------------------------------------------------
    pen = raw.get("PEN")
    if pen:
        recent = [(d, v) for d, v in pen if d >= today - pd.Timedelta(days=30)]
        age = (today - pen[-1][0]).days
        gaps = [(a[0].date(), b[0].date()) for a, b in zip(pen[-60:], pen[-59:])
                if (b[0] - a[0]).days > _PEN_MAX_SILENCE_DAYS]
        check(f"BCRP PEN: >= {_PEN_MIN_RECENT_OBS} observations in the last 30d, latest within "
              f"{_PEN_MAX_AGE_DAYS}d, no silences > {_PEN_MAX_SILENCE_DAYS}d, all values positive",
              len(recent) >= _PEN_MIN_RECENT_OBS and age <= _PEN_MAX_AGE_DAYS and not gaps
              and all(v > 0 for _, v in pen),
              f"{len(recent)} recent observations, latest {pen[-1][0].date()} ({age}d old)"
              + (f"; silences: {gaps[:3]}" if gaps else ""))
        return {"PEN": pd.Series([v for _, v in pen], index=pd.DatetimeIndex([d for d, _ in pen]))}
    return {}


def check_move_shape(today: pd.Timestamp) -> pd.Series | None:
    """Shape check on data.move_index() (Yahoo Finance ^MOVE via yfinance, an
    unofficial scrape). A partial or empty return must FAIL, not pass silently:
    a scrape that quietly degrades to a handful of rows, a stale tail, or
    garbage values still "returns data", so asserting only "non-empty" would
    let it through. Returns the series (for the generic sanity checks) or
    ``None`` if it didn't come back at all."""
    section("Shape checks — MOVE (Yahoo Finance ^MOVE)")
    series, note = data.move_index()
    returned = note is None and not series.empty
    if not check("MOVE: fetch returned data", returned,
                 "" if returned else (note or "empty series (no note given)")):
        return None

    recent = series[series.index >= today - pd.Timedelta(days=_MOVE_WINDOW_DAYS)]
    count_ok = len(recent) >= _MOVE_MIN_RECENT_OBS
    check(f"MOVE: plausible observation count in the last {_MOVE_WINDOW_DAYS}d",
          count_ok,
          f"{len(recent)} observations" if count_ok else
          f"{len(recent)} observations, need at least {_MOVE_MIN_RECENT_OBS} "
          "(~61 expected) — looks like a partial/sparse return")

    age = (today - series.index.max()).days
    check(f"MOVE: latest observation within {_MOVE_MAX_AGE_DAYS}d",
          age <= _MOVE_MAX_AGE_DAYS,
          f"latest observation {series.index.max().date()} is {age}d old")

    gaps = _find_gaps(recent.index, "D")
    check(f"MOVE: no multi-day silences in the last {_MOVE_WINDOW_DAYS}d",
          not gaps, "; ".join(g["detail"] for g in gaps))

    lo, hi = _MOVE_SANE_RANGE
    outside = recent[(recent < lo) | (recent > hi)]
    range_note = f"recent range [{recent.min():.1f}, {recent.max():.1f}]"
    check(f"MOVE: recent values within the sane range [{lo:g}, {hi:g}]",
          outside.empty,
          range_note if outside.empty else
          "; ".join(f"{d.date()}={v:.1f}" for d, v in outside.head(5).items())
          + (f" (+{len(outside) - 5} more)" if len(outside) > 5 else "")
          + f" — {range_note}")

    return series[series.index >= today - pd.DateOffset(years=_HISTORY_YEARS)]


def transform_checks_regime(today: pd.Timestamp) -> None:
    """Independent recomputation of the Rate Regime chart's derived values:
    the joined (10Y, MOVE) frame, the current regime run, and the regime path.
    Plain loops and ``bisect`` on lists — no pandas groupby/shift/searchsorted —
    so a bug in data.regime_run / data.regime_path can't be mirrored here. The
    raw 10Y comes from this script's own FRED fetch. MOVE has no second
    source, so it is the shared input (its own shape check covers it); if it
    didn't come back, this is skipped rather than double-reporting that."""
    from bisect import bisect_right

    section("Transform checks — regime run & path (independent recomputation)")
    move, note = data.move_index()
    if note or move.empty:
        return
    end = today.strftime("%Y-%m-%d")
    raw = fred_obs("DGS10", move.index.min().strftime("%Y-%m-%d"), end)
    if raw is None or raw.empty:
        check("Regime: raw DGS10 available for recomputation", False, "fetch failed")
        return

    y_by_day = raw.to_dict()
    rows = [(d, float(y_by_day[d]), float(m)) for d, m in move.items()
            if d in y_by_day and d <= today]
    dates = [r[0] for r in rows]
    thr_y, thr_m = 4.0, 100.0        # the logic is threshold-agnostic; any pair exercises it

    # 1. the joined frame itself (data.regime_history vs. this script's own join)
    got, _ = data.regime_history(end)
    same_days = list(got.index) == dates
    worst = max((abs(got["y10"].iloc[i] - rows[i][1]) for i in range(min(len(got), len(rows)))), default=0.0)
    check(f"Regime frame: matches an independent join of DGS10 and MOVE ({len(rows)} days)",
          same_days and worst < 1e-9,
          f"{len(got)} days in data.py vs {len(rows)} independent; max |Δ10Y|={worst:.6f}"
          if not (same_days and worst < 1e-9) else "")

    # 2. current regime run, walked back from the newest row with a plain loop
    def quadrant(y, m):
        return ("RESTRICTIVE" if y > thr_y else "ACCOMMODATIVE") + " / " + ("STRESSED" if m > thr_m else "CALM")
    i = len(rows) - 1
    while i > 0 and quadrant(*rows[i - 1][1:]) == quadrant(*rows[i][1:]):
        i -= 1
    run = data.regime_run(got, thr_y, thr_m)
    want = {"label": quadrant(*rows[-1][1:]), "entered": rows[i][0], "observations": len(rows) - i,
            "days": (rows[-1][0] - rows[i][0]).days, "truncated": i == 0}
    ok = run is not None and all(run[k] == v for k, v in want.items())
    check("Regime run: matches an independent backward walk",
          ok, "" if ok else f"data.py={run} independent={want}")

    # 3. the path at every cadence, for several windows. Targets are generated here
    #    with stdlib calendar arithmetic (not pandas DateOffset) so the month/year
    #    stepping is independently derived; "last observation on or before" is a
    #    bisect on a plain list.
    import calendar

    def months_back(day, n):
        month_index = day.year * 12 + (day.month - 1) - n
        year, month = divmod(month_index, 12)
        return pd.Timestamp(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))

    def independent_targets(start, cadence, weekday):
        out, step = [], {"M": 1, "Q": 3, "Y": 12}.get(cadence)
        if cadence == "D":
            d = today
            while d >= start:
                out.append(d); d -= pd.Timedelta(days=1)
        elif cadence == "W":
            d = today
            while d.weekday() != weekday:
                d -= pd.Timedelta(days=1)
            while d >= start:
                out.append(d); d -= pd.Timedelta(days=7)
        else:
            k = 0
            while (d := months_back(today, step * k)) >= start:
                out.append(d); k += 1
        return out

    bad, compared = [], 0
    for days_back in (91, 365, 730, 1825):
        start = today - pd.Timedelta(days=days_back)
        for cadence in ("D", "W", "M", "Q", "Y"):
            for weekday in (range(7) if cadence == "W" else (today.weekday(),)):
                expected = []
                for target in independent_targets(start, cadence, weekday):
                    pos = bisect_right(dates, target) - 1            # last observation on or before
                    if pos >= 0 and dates[pos] >= start and (not expected or expected[-1] != dates[pos]):
                        expected.append(dates[pos])
                expected.sort()
                in_window = [d for d in dates if start <= d <= today]
                if not expected or expected[-1] != in_window[-1]:
                    expected.append(in_window[-1])                   # always runs into the latest print
                path = data.regime_path(got, start, today, cadence, weekday)
                values_ok = all(
                    abs(path["y10"].loc[d] - rows[bisect_right(dates, d) - 1][1]) < 1e-9
                    and abs(path["move"].loc[d] - rows[bisect_right(dates, d) - 1][2]) < 1e-9
                    for d in expected if d in path.index
                )
                compared += 1
                if list(path.index) != expected or not values_ok:
                    bad.append(f"{days_back}d/{cadence}/wd{weekday}: {len(path)} vs {len(expected)} points")
    check(f"Regime path: matches an independent target walk ({compared} window x cadence x weekday cases)",
          not bad, "; ".join(bad[:4]))

    # 4. the property the cadence selection exists for: a readable point count at
    #    every standard period (the chart is unreadable past ~two dozen points)
    counts = {}
    for label, days_back in (("1M", 30), ("3M", 91), ("6M", 182), ("1Y", 365), ("2Y", 730), ("5Y", 1825)):
        path, cadence = data.auto_regime_path(got, today - pd.Timedelta(days=days_back), today, today.weekday())
        counts[label] = (cadence, len(path))
    unreadable = {k: v for k, v in counts.items() if not 10 <= v[1] <= data.REGIME_PATH_MAX_POINTS}
    check("Regime path: auto cadence keeps every standard period at a readable point count "
          f"(10-{data.REGIME_PATH_MAX_POINTS})",
          not unreadable,
          f"out of range: {unreadable}" if unreadable else
          ", ".join(f"{k} {c}/{n}" for k, (c, n) in counts.items()))


# --------------------------------------------------------------------------
# Cross-source checks
# --------------------------------------------------------------------------

def cross_source_h15(today: pd.Timestamp) -> None:
    section("Cross-source — FRED DGS vs. the Fed's own H.15 release")
    try:
        resp = requests.get(
            _H15_URL,
            params={
                "rel": "H15", "series": _H15_PACKAGE, "lastobs": 40, "filetype": "csv",
                "label": "include", "layout": "seriescolumn", "type": "package",
            },
            timeout=_TIMEOUT,
            headers={"User-Agent": "macro-dashboard-validate/1.0"},
        )
        resp.raise_for_status()
        lines = resp.text.splitlines()
        header = next(i for i, ln in enumerate(lines) if ln.startswith('"Time Period"'))
        cols = [c.strip('"') for c in lines[header].split(",")]
        h15 = pd.read_csv(io.StringIO("\n".join(lines[header:])))
        h15.columns = cols
        h15["Time Period"] = pd.to_datetime(h15["Time Period"])
        h15 = h15.set_index("Time Period")
    except (requests.RequestException, ValueError, StopIteration) as exc:
        check("H.15 release: reachable and parseable", False, f"{type(exc).__name__}: {exc}")
        return
    check("H.15 release: reachable and parseable", True, f"{len(h15)} recent rows")

    start = (today - pd.DateOffset(days=60)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    for dgs_id, h15_col in _H15_COLUMN.items():
        if h15_col not in h15.columns:
            check(f"H.15 vs FRED {dgs_id}: column present in H.15 CSV", False, f"missing {h15_col}")
            continue
        fred_series = fred_obs(dgs_id, start, end)
        h15_series = pd.to_numeric(h15[h15_col], errors="coerce").dropna()
        if fred_series is None or fred_series.empty or h15_series.empty:
            check(f"H.15 vs FRED {dgs_id}: both sources have data", False, "one side empty")
            continue
        common = fred_series.index.intersection(h15_series.index)
        if common.empty:
            check(f"H.15 vs FRED {dgs_id}: overlapping dates", False, "no common dates")
            continue
        diffs = (fred_series.loc[common] - h15_series.loc[common]).abs()
        worst = diffs.idxmax()
        h15_ok = diffs.max() <= 0.01
        check(f"H.15 vs FRED {dgs_id}: agree on {len(common)} common dates",
              h15_ok,
              "" if h15_ok else
              f"largest disagreement at {worst.date()}: FRED={fred_series.loc[worst]} "
              f"H.15={h15_series.loc[worst]}")


def cross_source_cleveland_nowcast() -> None:
    section("Cross-source — Cleveland Fed nowcast (feed vs. its own page)")
    # NOTE: the published page renders its chart from this same JSON feed, so
    # this is a reachability + recency check on both endpoints, not two
    # independent measurements of the same fact — there is no second,
    # separately-collected source for this number.
    try:
        page = requests.get(_CLEVELAND_PAGE_URL, timeout=_TIMEOUT,
                             headers={"User-Agent": "macro-dashboard-validate/1.0"})
        page_ok = page.ok
    except requests.RequestException as exc:
        page_ok = False
        page_note = f"{type(exc).__name__}: {exc}"
    else:
        page_note = f"HTTP {page.status_code}"
    check("Cleveland Fed nowcast page: reachable", page_ok, page_note if not page_ok else "")

    frame, note = data.fetch_cpi_nowcasts()
    check("Cleveland Fed nowcast feed: reachable and parses", note is None,
          note or "")
    if note is None and not frame.empty:
        latest_vintage = frame.index.max()
        today = pd.Timestamp.today().normalize()
        age_months = (today.year - latest_vintage.year) * 12 + (today.month - latest_vintage.month)
        check("Cleveland Fed nowcast feed: latest vintage is current or last month",
              age_months <= 1,
              f"latest vintage {latest_vintage.date()} is {age_months} month(s) old")


def cross_source_kalshi() -> None:
    section("Cross-source — Kalshi outcome probabilities sum near 100%")
    frame, note, _fetched = markets.fed_decision_markets()
    data_ok = note is None and not frame.empty
    if not check("Kalshi: fed_decision_markets() returns data", data_ok,
                  "" if data_ok else (note or "empty frame")):
        return
    lo, hi = _KALSHI_SUM_BAND
    for meeting, group in frame.groupby("Meeting"):
        total = group["Implied prob"].sum()
        breakdown = ", ".join(
            f"{row['Outcome']}={row['Implied prob']:.2f}" for _, row in group.iterrows()
        )
        check(f"Kalshi {meeting}: outcome probabilities sum within [{lo:.0%}, {hi:.0%}]",
              lo <= total <= hi,
              f"sum={total:.3f} across {len(group)} outcomes: {breakdown}")


# --------------------------------------------------------------------------
# Sanity bounds — derived from each series' own history.
# --------------------------------------------------------------------------

def sanity_bounds(series_id: str, series: pd.Series) -> None:
    series = series.dropna().sort_index()
    if len(series) < 30:
        check(f"Sanity {series_id}: enough history for bounds checks", False,
              f"only {len(series)} observations")
        return

    if series_id not in _MONOTONIC_LEVEL_SERIES:
        lo, hi = float(series.min()), float(series.max())
        latest = float(series.iloc[-1])
        ok = lo <= latest <= hi
        check(f"Sanity {series_id}: latest value ({latest:g}) within its own "
              f"{_HISTORY_YEARS}y range [{lo:g}, {hi:g}]",
              ok,
              "" if ok else f"latest={latest:g} is outside 10y range [{lo:g}, {hi:g}]")
    else:
        check(f"Sanity {series_id}: level min/max skipped (monotonic level; see move check)", True)

    diffs = series.diff().dropna()
    if len(diffs) < 10:
        check(f"Sanity {series_id}: enough history for a move-distribution check", False,
              f"only {len(diffs)} period-over-period changes")
        return
    std = float(diffs.std())
    latest_move = float(diffs.iloc[-1])
    threshold = _MOVE_SIGMA * std
    move_ok = abs(latest_move) <= threshold or threshold == 0
    check(f"Sanity {series_id}: latest move ({latest_move:+.4g}) within "
          f"{_MOVE_SIGMA:.0f}σ of its own history (σ={std:.4g})",
          move_ok,
          "" if move_ok else
          f"latest move {latest_move:+.4g} exceeds {_MOVE_SIGMA:.0f}σ={threshold:.4g} "
          f"(as of {series.index[-1].date()})")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def all_series() -> dict[str, str]:
    """Every FRED series id this dashboard uses, from data.py's own dicts —
    so this script tracks data.py automatically instead of hardcoding a
    separate series list."""
    out: dict[str, str] = {}
    for sid, label in data.TREASURY_SERIES.items():
        out[sid] = label
    for sid, label in data.INFLATION_SERIES.items():
        out[sid] = label
    for sid, label in data.LABOR_SERIES.items():
        out[sid] = label
    for sid, label in data.CREDIT_SERIES.items():
        out[sid] = label
    for sid, label in data.RATE_ENV_SERIES.items():
        out[sid] = label
    for pair in data.FX_PAIRS:
        if pair.spot_id:
            out[pair.spot_id] = f"{pair.pair} spot"
    return out


def main() -> int:
    global _QUIET
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quiet", action="store_true",
                         help="print only failures and the final summary")
    args = parser.parse_args()
    _QUIET = args.quiet

    if not data.FRED_API_KEY:
        print("FATAL: FRED_API_KEY not set (.env or environment) — cannot run.", file=sys.stderr)
        return 2

    today = pd.Timestamp.today().normalize()
    print(f"validate.py — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    section("Structural checks")
    histories: dict[str, pd.Series] = {}
    for series_id in all_series():
        result = audit_series(series_id)
        if result is not None:
            histories[series_id] = result[0]

    transform_checks_yoy(today)
    transform_checks_spreads(today)
    transform_checks_rolling(today)
    transform_checks_realized_vol(today)
    histories.update(transform_checks_fx(today))   # PEN joins the own-history sanity bounds

    move_history = check_move_shape(today)
    if move_history is not None:
        histories["MOVE"] = move_history   # joins the generic own-history sanity bounds
        transform_checks_regime(today)

    cross_source_h15(today)
    cross_source_cleveland_nowcast()
    cross_source_kalshi()

    section("Sanity bounds (derived from each series' own 10-year history)")
    for series_id, series in histories.items():
        sanity_bounds(series_id, series)

    # Always printed, --quiet included: the whole point of this section is
    # that a known, permanent condition stays visible instead of either
    # failing forever (training you to stop reading) or disappearing once
    # it's no longer a FAIL.
    print("\n== Known exceptions (excluded from pass/fail; always shown) ==")
    if _KNOWN_LOG:
        for line in _KNOWN_LOG:
            print(f"[KNOWN] {line}")
    else:
        print("(none)")

    total = _PASS + _FAIL
    print(f"\n{'=' * 60}\n{_PASS}/{total} checks passed, {_FAIL} failed, "
          f"{_KNOWN_COUNT} known exception(s).")

    write_status()
    return 1 if _FAIL else 0


def write_status() -> None:
    """Write validate_status.json for the dashboard's header status line.
    Best-effort: a failure to write shouldn't change this script's own exit
    code, since the checks themselves already ran and reported correctly."""
    status = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": _PASS,
        "failed": _FAIL,
        "known": _KNOWN_COUNT,
        "ok": _FAIL == 0,
        "failing_checks": list(_FAIL_LOG),
    }
    try:
        STATUS_FILE.write_text(json.dumps(status, indent=2))
    except OSError as exc:
        print(f"WARNING: could not write {STATUS_FILE}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
