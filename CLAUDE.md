# CLAUDE.md — briefing for a fresh session

Streamlit macro dashboard: US rates/curve, curve-change table, rate regime and
volatility, credit spreads, inflation, labor, FX spot and carry, an economic
calendar, Fed-decision odds, and market news.
Run: `streamlit run app.py` (needs `FRED_API_KEY`; see below).

`python validate.py` (`--quiet` for cron/CI use) independently audits the data
pipeline against the sources themselves — no hardcoded expected values, every
threshold is either a generic per-frequency policy or derived from a series'
own history. Checks: series resolve + no gaps + freshness (via FRED's own
`last_updated`, not observation-date age — see the comment in `audit_series`
for why that distinction matters; `_LAG_BUDGET_OVERRIDES` holds the one
documented exception, `THREEFYTP10`'s weekly-batch cadence); every
YoY/spread/rolling-average/realized-vol value re-derived independently and
compared to what `data.py`/`app.py` compute (including the FX tab's carry table
— spot, 1D/1M change, 63-day realized vol, policy rate, differential, carry/vol
— recomputed from raw FRED/BCRP/BIS fetches of its own, with shape checks on the
BIS and BCRP feeds, BIS-vs-FRED policy-rate cross-checks, each series' quote
orientation read from its source's own label; and the Rate Regime chart's
joined frame, current regime run, and path — every cadence × several windows
× every anchor weekday, with plain loops/`bisect` and stdlib calendar maths, plus
a direct check that the auto cadence keeps every standard period at a readable
point count; mutation-tested so a bug in any is known to be caught); FRED's DGS
yields cross-checked
against the Fed's own H.15 release; Kalshi outcome probabilities summing near
100%; a shape check on the MOVE scrape (recent-observation count, freshness,
no multi-day silences, values in 40–200 — partial/empty FAILS); latest
values/moves outside the series' own 10-year range or 5σ. Exits non-zero on
any failure. A gap
matching a declared, verified entry in `_KNOWN_GAPS` (currently: the real Oct
2025 CPI-family shutdown gap — see the FRED quirk note below) reports KNOWN
instead of FAIL and doesn't affect the exit code, but is still listed every
run in its own "Known exceptions" section (always printed, `--quiet`
included) so it can't quietly turn into a FAIL you've learned to ignore. As
of this writing: 312/312 passed, 5 known exceptions (the four Oct 2025 gaps, plus
Mexico's BIS-vs-FRED policy-rate disagreement — see the FX data-source notes).

Every run writes `validate_status.json` next to the script (gitignored —
`data.validation_status()` reads it, with no error if it's missing). The
dashboard header shows a small status line from it: green "Validation
passed…" when the last run was clean, red "Validation FAILED — <check
name>…" naming the first failing check when it wasn't, or a muted "no run
recorded yet" if validate.py has never run in this environment.

`.github/workflows/validate.yml` runs it weekly (Mondays 21:30 UTC, after the
Fed's 4:15pm ET H.15 release) plus on `workflow_dispatch` for a manual run
from the Actions tab, using the `FRED_API_KEY` repo secret. After the run
(pass or fail) it commits `validate_status.json` back to the repo as
`github-actions[bot]` (`chore: update validation status [skip ci]`, skipped
when the file is unchanged) and pushes to the branch that triggered it,
*then* fails the job if `validate.py` found problems — in that order, so a
failing run still gets its status committed before the workflow goes red.
`validate_status.json` is tracked in git (no longer gitignored) specifically
so this push reaches Streamlit Cloud's own checkout on its next deploy, which
is what lets the header reflect it. Needs `permissions: contents: write` on
the workflow, which it has. One bootstrap note: the file only exists in the
repo after this workflow has actually run at least once post-merge (scheduled
or manual) — until then the header still reads "no run recorded yet", same
as any environment where validate.py hasn't run.

## Modules

| File | Responsibility |
| --- | --- |
| `app.py` | UI only. Top-of-page controls (period pills + custom-range popover), the 8 tabs, all chart/table assembly. No network calls of its own — everything comes from the other modules. |
| `data.py` | All FRED access + every other data fetch (Cleveland Fed nowcast, FX Blue NFP forecast, MOVE via yfinance) + pure transforms (`movement_table`, `changes_bps`, `spread`, `curve_snapshots`, `release_calendar`, `fomc_meeting_dates`, `release_overdue`). Rate Environment fetchers: `realized_vol_10y`, `move_index`, `term_premium`. FX fetchers: `fetch_fx_spot`, `fetch_policy_rates`, `fetch_fx_window` (BCRP and BIS parsers inside) and the pure `carry_table`, `fx_change_pct`, `fx_realized_vol`; the currency list is `FX_PAIRS`. Fetchers are `@st.cache_data`. `_load_api_key(name)` = `st.secrets` then `.env`. |
| `feeds.py` | RSS/Atom fetch + parse for the News tab. Concurrent, 15-min cache, dedupe by normalized URL + fuzzy title. One dead feed never breaks the tab. |
| `markets.py` | Kalshi Fed-decision markets (public REST, no auth). 5-min cache. Returns `(frame, note, fetched_at)`; any failure → empty frame + note. |
| `theme.py` | Terminal styling: global CSS injected via `st.markdown`, the shared Plotly template (`bloomberg`), `TENOR_LINE` per-tenor line styles, `_PILL_TINTS` (pill-underline colours, must match the `_*_SPEC` colour lists in `app.py`), `render_chart()`. |

Tabs, in order: **Rates · Rate Environment · Inflation · Labor · Credit · FX ·
News · Calendar**. Rate Change is no longer its own tab — it's a section inside Rates,
between "Yields over time" and the yield-curve snapshot chart. The `with` blocks
in `app.py` run in this same order, and nothing depends on tab position: tabs are
bound by variable name, every pill group has a stable string key, and the
pill-tint match (`_*_SPEC` ↔ `theme._PILL_TINTS`) is by option order *within* a
group, so reordering tabs never touches it.

**Tab isolation.** Streamlit runs every tab's code on each rerun, so a bare
`st.stop()` in one tab would halt the script and blank every tab after it. Don't
use `st.stop()` inside a tab block: call `_stop_tab()` (raises `_StopTab`) and
render the tab as `with _tab(x_tab):`, which catches only that sentinel and
carries on with the next tab. The two `st.stop()`s in the page header (invalid
custom date range) are real stops on purpose — no tab has a valid range then.
Verified with simulated outages on the old vs. new code: an outage in one tab's
fetch now blanks only the tabs that actually depend on it (e.g. a Treasury-yield
outage blanks Rates, Rate Environment and Credit, but Inflation, Labor, FX, News and
Calendar still render; before, it blanked every tab after the failing one). `_StopTab` is
swallowed silently; any other exception inside a tab is caught by `_tab()` and
shown in that tab via `st.exception()`, so later tabs still render (Streamlit's
own `st.stop`/rerun exceptions are `BaseException`, so they pass through).
Side effect: in a total FRED outage every tab now
attempts its own fetch and shows its own error, rather than the first tab's
failure short-circuiting the rest.

**Rate Environment** is regime characterization — not where rates are, but what
kind of environment they're in. Three sections, rendered by
`_render_rate_environment` in `app.py` (a function, so a failure just `return`s):

1. **Rate regime** — scatter of 10Y yield (x) vs MOVE (y). Every day in the
   selected period is a faint grey point (the background distribution). The
   trajectory is a **path whose cadence scales with the period**
   (`data.auto_regime_path`) so it stays at roughly a dozen to two dozen points
   — a daily 2Y path was 500 points and an unreadable tangle. Start from a band
   by span: under 3 months daily, up to a year weekly, longer monthly; then, while
   the path exceeds `REGIME_PATH_MAX_POINTS` (28), step to the next-coarser
   cadence (D → W → M → Q → Y). The step-up is deliberate: the bands alone leave
   1Y at ~53 weekly points and 5Y at ~60 monthly ones, so in practice 1M is daily
   (~20 points), 3M and 6M weekly (14 / 27), 1Y and 2Y monthly (13 / 24), 5Y
   quarterly (~20). Every cadence keeps one anchor convention: each target date
   (same weekday for weekly; same day-of-month for monthly/quarterly, calendar
   arithmetic so a 31st clamps to a short month) resolves to the *last
   observation on or before it* — the Rate Change table's lookback convention, so
   a holiday falls back to the prior print. Targets that resolve to the same
   observation collapse to one point, and the latest observation is always the
   path's end. Segments brighten toward today. Every 3rd or 4th point (3 when
   there are 16 or fewer, counting back from the newest) is a label candidate,
   and a candidate is drawn only if it wouldn't overlap one already placed
   (`theme.pick_labels`, which estimates label boxes from the axis ranges over an
   assumed ~780px plot — measured in the browser, 0 overlaps on every period,
   but a much narrower window has less room). The latest observation is **one**
   large amber point: a single-row trace, the path-point markers exclude the
   newest row, and the figure's colorway is pinned to grey so no trace can pick
   up the template's default amber (its third colour). Every marker — grey, path,
   latest — has date / 10Y / MOVE hover text. Dotted dividers at 10Y = 4.00% and
   MOVE = 100 (`_RESTRICTIVE_10Y`, `_STRESSED_MOVE`) and four corner quadrant
   labels; the axis ranges always include the divider values, so they stay valid
   even when the period sits wholly on one side of a threshold. 4.00% is a
   judgment threshold (est. nominal neutral = 3.0% longer-run dot + term
   premium), not a published number. The caption under the chart gives the
   current quadrant and how long it has lasted (`data.regime_run`): the
   **unbroken run** of consecutive observations in that quadrant ending at the
   latest one — one observation in another quadrant breaks it, so a regime left
   for a day and re-entered counts from the re-entry. Days are calendar days from
   entry to the latest observation (the data's as-of date, not today's). The run
   is computed over MOVE's full history (`data.regime_history`), not the selected
   period, so it's right even in a 1M view. A reading exactly on a divider falls
   to ACCOMMODATIVE / CALM (strict `>`). If MOVE fails or doesn't overlap the
   period it falls back to a ⚠️ caption plus the 10Y line against its 4.00%
   threshold (no vol axis).
2. **Rate volatility** — MOVE (implied) and 20-observation realized 10Y vol
   (annualized bp) on **one shared y-axis** (both are basis-point vols, so the gap
   between the lines is a real implied-vs-realized gap; the range spans both series
   so toggling a pill doesn't rescale). Pills key `vol_series` (`_VOL_SPEC` ↔
   `theme._PILL_TINTS`).
3. **Term premium** — `THREEFYTP10` with a zero reference line; the sign is the read.

**FX** (after Credit) is spot plus carry for nine currencies — EUR, JPY, GBP, MXN,
PEN, BRL, AUD, CHF, CAD (`data.FX_PAIRS`, in that order), rendered inline in
`app.py`'s `with _tab(fx_tab):` block. Quotes stay in market convention exactly as
the source publishes them (EUR/USD, USD/JPY, USD/PEN…) and are never re-inverted;
`FxPair.usd_per_fc` records which way each runs, and every *change* is expressed as
the **foreign currency's** move against the USD (`fx_change_pct`), so green always
means the currency strengthened. Parts:

1. **Table** (fixed ~14-month window, independent of the period pills): Pair, Spot
   (market quote — **live**, see below), 1D % and 1M % (labelled "ccy vs USD"), Policy rate (%) (the country's stated rate, BIS), Carry (pp), 3M vol (%),
   Carry/vol. Two groups — G10 (EUR, JPY, GBP, AUD, CHF, CAD) and EM (MXN, BRL, PEN),
   `FxPair.group` — each under a muted label row and sorted by carry/vol, highest
   first. Cells are pre-formatted text (Arrow-safe; numbers rounded half-up so
   -2.625 reads -2.63), green/red comes from a parallel frame of the raw changes.
   Carry = local policy rate minus the US rate (positive: holding that currency
   funded in USD earns it; negative: a funding currency). Vol is the sample std of
   the last 63 daily log returns × √252 (orientation-independent); carry/vol uses
   *realized* vol, not implied. 1D/1M use the Rate Change table's convention (last
   observation on or before the lookback date). The footnote computes its dates
   from the data (most common spot date and rate date, then only the rows that
   differ) — nothing hard-coded.
2. **Carry vs volatility** — scatter directly under the table: x = 3M vol, y =
   carry, one labelled point per currency, G10 blue / EM amber (explained in a
   caption, no legend), zero line, dashed rays at carry/vol 0.25 and 0.5 labelled at
   the right edge (the y-axis top is raised so the 0.5 ray always ends there). Points
   come from `data.carry_vol_points(table)`, so they are the table's numbers.
   `theme.place_labels` picks one of eight label positions per point so labels never
   overlap each other or a marker (estimated geometry over an assumed ~900px plot).
**Live spot.** The table's Spot, 1D % and 1M % come from Yahoo Finance *hourly* bars
(`data.fetch_fx_live`, 5-minute cache, one batch `yf.download`): Spot = the latest bar;
1D / 1M = that price against the last bar on or before 24 hours / one calendar month
earlier, all computed in UTC so both ends share a basis (`live_from_hourly`,
`carry_table(..., live)`). PEN **is** live (`PEN=X`): an earlier version excluded it after seeing
~3% jumps, but that was Yahoo's *daily* bars — its hourly PEN bars agree with BCRP's
official rate to 0.11% on average (max 0.47%, 41 days). BCRP stays the settled history
and the fallback. Vol, carry/vol and the
stay on the settled H.10/BCRP series. **Never use Yahoo's *daily* FX
closes** — measured against H.10 they are wrong or a day late on some bars (BRL closed
8% off the real level on one bar; JPY moves landed a day late) — hourly bars at noon NY
agree with H.10 to 0.05-0.13% on average (max 0.47%). A currency whose latest bar is
>5 days old or >25% from its last settled rate is dropped to the settled rate, and any
Yahoo failure falls back to the settled H.10 table with a ⚠️ note. The footnote text
switches between "Yahoo Finance live prices … as of <time> ET" and the H.10 wording.

Sources: settled spot = the Fed's H.10 via FRED (`DEXUSEU`, `DEXJPUS`, `DEXUSUK`, `DEXMXUS`,
`DEXBZUS`, `DEXUSAL`, `DEXSZUS`, `DEXCAUS`; noon-NY rates, published weekly so
~3 business days behind), PEN = Peru's central bank (BCRP) interbank rate; policy
rates = BIS `WS_CBPOL` for all ten countries (US = mid-point of the Fed's target
range, euro area = ECB deposit facility rate). **Failure handling:** each spot
source fails independently (its own column drops and a ⚠️ caption names it); a BIS
failure shows spot without carry. See the data-source notes for the quirks.

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
  `inflation_series`, `labor_ur`, `labor_claims`, `vol_series`, `news_sources`. Colour lists
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
- **Term premium** (`THREEFYTP10`, via FRED) — the **Kim-Wright** 10Y term
  premium (Federal Reserve Board model), the only 10Y term premium FRED carries.
  **`ACMTP10` is not a FRED series** (FRED answers "series does not exist"): it's
  a column in the NY Fed's own ACM data file, so the NY Fed's ACM estimate would
  need a direct download from newyorkfed.org, not a FRED call. The two are
  different model estimates with the same sign convention — the tab says
  "Kim-Wright" and does not call it ACM. Daily observations, but FRED
  publishes it as a **weekly batch (every Tuesday, data through the prior
  Friday)**, so it runs about a week behind the yields, and `validate.py` gives
  it a documented per-series freshness override (`_LAG_BUDGET_OVERRIDES`) —
  under the generic daily budget it would fail every Monday.
- **MOVE index** (`^MOVE` via `yfinance`, Yahoo Finance) — ICE BofA's Treasury
  implied-vol index. An **unofficial scrape of a licensed ICE index**, same
  personal-use posture as the ICE BofA OAS series above: do not republish it or
  expose a bulk export (so the Rate Environment tab deliberately has no
  "MOVE data" expander, unlike Credit's). `move_index()` returns
  `(series, note)`; any failure (yfinance missing, network, Yahoo blocking or
  returning nothing — cloud IPs do get blocked) → empty series + note, and the
  tab degrades rather than erroring. A failure is cached for the 1-hour TTL (the
  header ↻ button retries sooner). Yahoo's latest row can be a live intraday
  bar, so today's MOVE point may still move during market hours. `validate.py`
  shape-checks it (count / freshness / gaps / sane 40–200 range) and a partial
  or empty return FAILS — note GitHub Actions runner IPs may be blocked by
  Yahoo, in which case that check fails for environmental reasons.
- **FX spot** (FRED H.10 + BCRP). H.10 is a **weekly** release (FRED vintage dates are
  consecutive Mondays), so `validate.py` gives those eight series a 9-day freshness
  override like `THREEFYTP10`. The sol is not in H.10 (`DEXPEUS` doesn't exist), so
  PEN comes from BCRP's API
  (`estadisticas.bcrp.gob.pe/estadisticas/series/api/PD04638PD/json/<start>/<end>`,
  interbank sell rate, S/ per US$). It is free and unauthenticated but sometimes
  answers with an anti-bot HTML page instead of JSON (the fetch retries three times);
  period names use Spanish month abbreviations (`Set` = September), an unrecognised
  month raises, and `n.d.` marks a day with no data (today's value is usually `n.d.`).
- **Policy rates** (BIS `WS_CBPOL`, `stats.bis.org/api/v2`, no auth, SDMX-CSV with
  `detail=dataonly`, ~1 MB for five years). Daily series for every country, about a
  week behind (AU lags the most, ~2 weeks). Values can be `NaN` for a day (dropped).
  Cross-checked in `validate.py`: euro area = FRED `ECBDFR`, US = the mid-point of
  `DFEDTARU`/`DFEDTARL`, and JP/CA/AU/BR/GB month averages within 0.25pp of FRED's
  monthly call-money series (`IRSTCI01xxM156N`). **Mexico disagrees by a persistent
  ~1.3pp** (BIS 6.50 vs FRED 5.2 as month averages, every month of 2026) and is
  reported as a KNOWN exception rather than a failure; the tab uses BIS. Unresolved
  which is right — worth checking against Banxico directly (its API needs a token)
  before relying on the MXN carry number. FRED's CHF and PEN rate series are unusable
  (CHF stopped in 2024, PEN doesn't exist), which is why BIS is the rate source.
- **FRED rate limit.** FRED allows ~120 requests/minute and answers 429 beyond it.
  `data.fetch_series` backs off and retries twice on 429; `validate.py` (which makes
  a few hundred calls) retries with longer backoff.
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
- Cleveland Fed nowcast fetch is a 7 MB scrape with no lighter endpoint — still
  true, no lighter endpoint exists. What's fixed: a *total* format break
  already returned a note; `_cleveland_nowcasts` (`data.py`) now also detects
  the quieter partial breaks — headline or core coming back completely empty
  while the other still has data (one seriesname changed, not both), and the
  newest parsed vintage being >1 month old (recent vintages stopped parsing
  while older ones still do) — and reports both while still keeping whatever
  data did parse, rather than silently reporting success either way.
- Labor tab shows a 4th metric (AHE YoY) beyond the spec's 3 — leave or move.
- FX: the Mexico policy-rate discrepancy above (BIS 6.50 vs FRED call-money 5.2) is
  unresolved; and carry/vol uses realized vol, not implied (no free source for FX
  implied vol).
- FOMC dates need a yearly refresh (`_FOMC_DECISION_DAYS` in `data.py`).
- The dev/sandbox FRED data looked synthetic (implausible payroll prints); real
  Cloud data will differ — don't tune visuals to the sample.
