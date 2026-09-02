# Macro Dashboard

Streamlit dashboard for the US Treasury curve (3M / 2Y / 5Y / 10Y / 30Y), curve
spreads, credit spreads, and inflation — sourced from
[FRED](https://fred.stlouisfed.org/), with Fed-decision odds from Kalshi and
market news from RSS.

## Layout

| File | Responsibility |
| --- | --- |
| `data.py` | All FRED fetching + pure transforms. Fetch functions are cached with `@st.cache_data`. |
| `feeds.py` | RSS/Atom fetching + parsing for the News tab. Cached 15 min; one dead feed never breaks the tab. |
| `markets.py` | Kalshi Fed-decision markets (public REST, no auth). Cached 15 min; any failure returns an empty frame + note. |
| `theme.py` | Bloomberg-terminal styling: global CSS + shared Plotly template. |
| `app.py` | UI only — top-of-page controls, tabs, widgets, chart assembly. |

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then paste your FRED API key into .env
```

Get a free API key at https://fredaccount.stlouisfed.org/apikeys.

## Run

```bash
streamlit run app.py
```

## Features

**Controls (top of page)**

- Period pills (1M / 3M / 6M / 1Y / 2Y / 5Y, plus a Custom-range popover),
  top-right; apply to every chart.
- Tenor toggles above the yields chart, tinted to match the line colours.
- An "as of" line with the latest observation date per series and a cache-clearing
  refresh button.

**Rates tab**

- 2s10s and 5s30s spread metrics, each with the current level (bps) and 1-day
  change. `+0 bps` means a parallel shift — the two yields moved by the same
  amount, so the spread was flat.
- Yields over time for the selected tenors.
- Yield curve: today vs 1 month ago vs 1 year ago, plotted by tenor on one axis.
  Fetched over a fixed ~400-day window so it ignores the date-range selection.
- **Fed decision odds (Kalshi):** implied probability and volume for each outcome
  of the next two FOMC meetings, from `KXFEDDECISION` market prices. Shows a note
  instead of the table if Kalshi is unreachable.

**Credit tab**

- HY OAS (`BAMLH0A0HYM2`) and IG OAS (`BAMLC0A0CM`) with current level in bps and
  1D / 1W / 1M change in bps (fixed ~5-month window, independent of the selected
  range).
- HY OAS vs the 2s10s spread on a dual axis — credit and curve on one chart.

**Change tab**

- Movement table for the five tenors plus the 2s10s / 5s30s spreads: last level
  (%), then change in bps over 1D / 1W / 1M / 3M / YTD / 1Y. Each change compares
  the latest observation to the last observation on or before the lookback date,
  so weekends and holidays are handled by value, not row offset.

**Inflation tab**

- CPI (`CPIAUCSL`) and core PCE (`PCEPILFE`) as year-over-year %, plus the 10-year
  breakeven rate (`T10YIE`), on one chart, with latest-value metrics.
- CPI/PCE are index levels; ~13 months of extra history are fetched so the
  trailing-12-month change is populated from the first visible point.

**News tab**

- Cards sorted newest first: headline (links to the article), source, timestamp,
  a thumbnail when the feed provides one, and a short summary. No image → the
  card just renders text; a broken image URL falls back to a placeholder.
- Sources: Federal Reserve speeches / testimony / monetary-policy releases,
  the NY Fed (Liberty Street Economics), Treasury (via a Google News
  `site:home.treasury.gov` search — treasury.gov has no working native RSS),
  and Google News searches for "treasury yields", "bond market", "Federal Reserve".
- Feeds are fetched concurrently and cached for 15 minutes. Any feed that is
  unreachable or malformed contributes zero items and is listed in a small
  "no items from…" note; the tab still renders.
