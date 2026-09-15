# CLAUDE.md — briefing for a fresh session

Streamlit macro dashboard: US rates/curve, curve-change table, credit spreads,
inflation, labor, an economic calendar, Fed-decision odds, and market news.
Run: `streamlit run app.py` (needs `FRED_API_KEY`; see below).

`python validate.py` (`--quiet` for cron/CI use) independently audits the data
pipeline against the sources themselves — no hardcoded expected values, every
threshold is either a generic per-frequency policy or derived from a series'
own history. Checks: series resolve + no gaps + freshness (via FRED's own
`last_updated`, not observation-date age — see the comment in `audit_series`
for why that distinction matters); every YoY/spread/rolling-average value
re-derived independently and compared to what `data.py`/`app.py` compute;
FRED's DGS yields cross-checked against the Fed's own H.15 release; Kalshi
outcome probabilities summing near 100%; latest values/moves outside the
series' own 10-year range or 5σ. Exits non-zero on any failure. A gap
matching a declared, verified entry in `_KNOWN_GAPS` (currently: the real Oct
2025 CPI-family shutdown gap — see the FRED quirk note below) reports KNOWN
instead of FAIL and doesn't affect the exit code, but is still listed every
run in its own "Known exceptions" section (always printed, `--quiet`
included) so it can't quietly turn into a FAIL you've learned to ignore. As
of this writing: 122/122 passed, 4 known exceptions.

Every run writes `validate_status.json` next to the script (gitignored —
`data.validation_status()` reads it, with no error if it's missing). The
dashboard header shows a small status line from it: green "Validation
passed…" when the last run was clean, red "Validation FAILED — <check
name>…" naming the first failing check when it wasn't, or a muted "no run
recorded yet" if validate.py has never run in this environment (e.g. a fresh
Streamlit Cloud deploy with no scheduled job wired up to run it yet — nothing
currently runs validate.py on a schedule; that's on you to set up, whether
cron, a GitHub Action, or Streamlit-side).

## Modules

| File | Responsibility |
| --- | --- |
| `app.py` | UI only. Top-of-page controls (period pills + custom-range popover), the 7 tabs, all chart/table assembly. No network calls of its own — everything comes from the other modules. |
| `data.py` | All FRED access + every other data fetch (Cleveland Fed nowcast, FX Blue NFP forecast) + pure transforms (`movement_table`, `changes_bps`, `spread`, `curve_snapshots`, `release_calendar`, `fomc_meeting_dates`, `release_overdue`). Fetchers are `@st.cache_data`. `_load_api_key(name)` = `st.secrets` then `.env`. |
| `feeds.py` | RSS/Atom fetch + parse for the News tab. Concurrent, 15-min cache, dedupe by normalized URL + fuzzy title. One dead feed never breaks the tab. |
| `markets.py` | Kalshi Fed-decision markets (public REST, no auth). 5-min cache. Returns `(frame, note, fetched_at)`; any failure → empty frame + note. |
| `theme.py` | Terminal styling: global CSS injected via `st.markdown`, the shared Plotly template (`bloomberg`), `TENOR_LINE` per-tenor line styles, `_PILL_TINTS` (pill-underline colours, must match the `_*_SPEC` colour lists in `app.py`), `render_chart()`. |

Tabs, in order: **Rates · Calendar · Rate Change · Credit · Inflation · Labor · News**.

## UI conventions

- Bloomberg-terminal look: near-black bg (`#0a0a0a`), light-grey text, monospace
  everywhere (JetBrains/IBM Plex Mono stack) including headers and metric values.
- No emoji. No rounded corners, no shadows. Section headers = small uppercase
  labels with a 1px underline. Tight vertical padding.
- **Colour is semantic only**: green `#3fb950` = up / red `#f85149` = down, on
  change columns and payroll bars. Levels stay neutral. The amber accent
  (`#ff9900`) is for data (chart series, links), never chrome — the Streamlit
  `primaryColor` is grey.
- **Pill selectors** replace Plotly legends: `st.pills` multi-select, borderless
  text, each option tinted to its series colour with a 2px underline when
  selected, ~30% opacity when off. Keyed groups: `tenors`, `credit_series`,
  `inflation_series`, `labor_ur`, `labor_claims`, `news_sources`. Colour lists
  live in `app.py` `_*_SPEC` and must stay in sync with `theme._PILL_TINTS`
  (matched by nth-of-type, so order matters).
- Charts: `hovermode="closest"` on any chart with custom `hovertext` (the
  template's `x unified` swallows it). Fixed y-axis ranges (`_padded_range`) so
  toggling a pill doesn't rescale. Dual-axis via `make_subplots`.
- Timestamps shown to the user are US Eastern with an explicit " ET" suffix
  (`app._et`, zoneinfo).

## Data sources & quirks

- **FRED** (`FRED_API_KEY`). Publication lag is normal, not staleness:
  DGS/H.15 daily series run ~1 business day behind; CPI/PCE/payrolls are monthly
  and weeks behind by design. `release_overdue()` only flags a series once its
  *next scheduled* release has passed. Inflation YoY uses `CPIAUCNS` /
  `CPILFENS` (NSA) — BLS's own convention for 12-month changes, matching the
  NSA-based Cleveland Fed nowcast. YoY is computed by calendar-month offset
  (`data.yoy_change`), not row offset, so a missed release — e.g. CPI's
  October 2025 government-shutdown gap — can't misalign the 12-month lookback
  (this bit us once: SA + row-offset showed 3.71% for Aug 2026 vs BLS's
  3.4%). Any other YoY calc (e.g. labor's AHE) should go through the same
  helper for the same reason — audited both, plus core CPI and core PCE,
  against the actual Aug 2026 releases (3.4% headline / 2.4% core CPI, 3.1%
  AHE all matched). Core PCE (`PCEPILFE`) deliberately stays SA, not NSA like
  CPI: BEA doesn't publish a monthly NSA core-PCE index at all, and BEA's own
  headline YoY is itself computed off the SA index (confirmed: our 3.34% vs
  BEA's reported 3.3% for Jul 2026).
- **ICE BofA OAS** (`BAMLH0A0HYM2` HY, `BAMLC0A0CM` IG, via FRED). Redistribution
  is restricted by ICE's terms — fine for a personal dashboard, do not
  republish the raw series or expose a bulk data export.
- **Cleveland Fed inflation nowcast** — *not on FRED*. Scraped from
  `clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_year.json`
  (~7 MB FusionCharts JSON, 6-hr cache). Fragile format; parses the final
  pre-release CPI / Core CPI YoY nowcast per vintage month.
- **Finnhub economic calendar** — paid add-on only; the free/any key returns
  401/403. That call was removed. The Calendar tab is now a hardcoded recurring
  schedule (`release_calendar`) + `fomc_meeting_dates` (Fed's published 2026 /
  tentative 2027 dates).
- **FX Blue NFP forecast** — scraped from
  `api.fxblue.com/calendar/item/Nonfarm_Payrolls_US`, "Past events" table
  (`PastEventRow` divs), 12-hr cache. These are **first-print vintage**: the
  forecast/actual pairs are as-published, never revised. The payroll-bar tooltip
  pairs this forecast with the bar's own (FRED, revised) actual — forecast only,
  no surprise math. Missing forecast or a scrape failure → actual alone.
- **Kalshi** — no auth for market data. The *series* ticker `KXFEDDECISION` is
  hardcoded (with a category-scan discovery fallback) — that's stable across
  every meeting, not tied to one. Which *meeting* shows is dynamic: markets
  are always queried `status=open`, and a resolved meeting's markets drop out
  of that filter entirely (verified against the real Jul 29, 2026 decision —
  empty list, not stale prices or an error), so the next 2 meetings roll
  forward automatically with no code change needed per FOMC date. A "nothing
  to show" state (no series found, API down, no open/upcoming markets) always
  surfaces as a `⚠️` caption in the Rates tab, never a silent blank.
- **News** — Treasury.gov has no working RSS (uses a Google News `site:` search);
  "NY Fed" is the Liberty Street Economics feed.

## Deploy

- Repo: `github.com/orourkepatt97-cmd/Macro-Dashboard`, branch `main`.
- Push to `main` → Streamlit Community Cloud auto-redeploys.
- Secrets are **not** in the repo. `FRED_API_KEY` goes in the Streamlit Cloud
  app settings (Secrets); locally it's `.env` (gitignored, `.env.example` is the
  template). `.streamlit/secrets.toml` is gitignored.
- `requirements.txt` is the only dependency manifest. `tzdata` is pinned so
  `zoneinfo` works on the Cloud container.

## Outstanding

- `release_calendar`'s hardcoded CPI/PPI/payrolls/retail/PCE dates
  (`_RELEASE_DATES` in `data.py`) only cover CY2026 — OMB hasn't published the
  CY2027 schedule yet as of Sep 2026. Extend the table once it does (usually
  posted each fall at whitehouse.gov); until then, a release past Dec 2026
  just won't appear rather than guess at a date.
- Cleveland Fed nowcast fetch is a 7 MB scrape with no lighter endpoint; a
  format change on their side breaks it silently (falls back to actual-only).
- Labor tab shows a 4th metric (AHE YoY) beyond the spec's 3 — leave or move.
- FOMC dates need a yearly refresh (`_FOMC_DECISION_DAYS` in `data.py`).
- The dev/sandbox FRED data looked synthetic (implausible payroll prints); real
  Cloud data will differ — don't tune visuals to the sample.
