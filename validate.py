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
    Transform    — every YoY, spread and rolling-average value data.py/app.py
                   compute is independently recomputed here from raw
                   observations fetched directly (not via data.fetch_series)
                   and compared; any disagreement is an error.
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
import io
import json
import sys
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

def fred_meta(series_id: str) -> dict | None:
    try:
        resp = requests.get(
            FRED_SERIES_URL,
            params={"series_id": series_id, "api_key": data.FRED_API_KEY, "file_type": "json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        seriess = resp.json().get("seriess", [])
        return seriess[0] if seriess else None
    except (requests.RequestException, ValueError, IndexError):
        return None


def fred_obs(series_id: str, start: str, end: str) -> pd.Series | None:
    """Raw observations, independently fetched and parsed (own code path)."""
    try:
        resp = requests.get(
            FRED_OBS_URL,
            params={
                "series_id": series_id, "api_key": data.FRED_API_KEY,
                "file_type": "json", "observation_start": start, "observation_end": end,
            },
            timeout=_TIMEOUT,
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

    lag_budget = _LAG_BUDGET_DAYS.get(freq_short, 60)
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
        check(f"{series_id}: FRED last updated this series within {lag_budget}d for '{freq_short}' frequency",
              age_days <= lag_budget,
              f"last_updated={last_updated_raw} is {age_days}d old (budget {lag_budget}d); "
              f"latest observation dated {series.index.max().date()}")

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
