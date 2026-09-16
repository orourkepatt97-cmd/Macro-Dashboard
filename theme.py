"""Bloomberg-terminal styling for the dashboard.

UI only, no data. ``app.py`` calls :func:`apply` once right after
``st.set_page_config`` (injects the global CSS and registers the shared Plotly
template), then renders every figure through :func:`render_chart` and styles the
yields-over-time traces from :data:`TENOR_LINE`.
"""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

# Monospace stack — JetBrains Mono / IBM Plex Mono if the viewer has them, then
# the OS mono UI font, then generic fallbacks.
MONO = (
    '"JetBrains Mono","IBM Plex Mono",ui-monospace,SFMono-Regular,'
    'Menlo,Consolas,"Liberation Mono",monospace'
)

_BG = "#0a0a0a"      # near-black page
_PANEL = "#141414"   # sidebar / inputs / hover
_TEXT = "#d0d0d0"    # light grey body text
_MUTED = "#8a8a8a"   # labels, ticks, axis lines
_LINE = "#2a2a2a"    # 1px borders / rules
_GRID = "rgba(255,255,255,0.06)"  # thin low-opacity chart grid
_ACCENT = "#ff9900"  # amber

# Passed to every st.plotly_chart call: no modebar, no logo.
PLOTLY_CONFIG = {"displayModeBar": False, "displaylogo": False, "showTips": False}

# Per-tenor line encoding for the yields-over-time chart. Every line is solid;
# tenor is read from colour (near-white -> deep amber, wide uneven spacing) and
# line weight. 3M is a thin, low-opacity near-white line so it recedes; 2Y and
# 10Y (the benchmarks) read loudest at full opacity and a medium-heavy weight.
TENOR_LINE: dict[str, dict] = {
    "DGS3MO": {"color": "#f2efe6", "width": 1.0, "opacity": 0.50},
    "DGS2":   {"color": "#e3c07d", "width": 2.0, "opacity": 1.00},
    "DGS5":   {"color": "#cf9038", "width": 1.6, "opacity": 0.85},
    "DGS10":  {"color": "#b9741f", "width": 2.6, "opacity": 1.00},
    "DGS30":  {"color": "#8f4f14", "width": 3.4, "opacity": 1.00},
}

# st.pills toggle groups that double as chart legends. Each key maps to the
# ordered list of line colours for its options; the pill for option N is tinted
# colour N and gets a 2px underline in that colour when selected. Colours must
# match the trace colours set in app.py (_CREDIT_SPEC / _INFLATION_SPEC /
# _LABOR_UR_SPEC / _CLAIMS_SPEC) and TENOR_LINE, in the same option order.
_PILL_TINTS: dict[str, list[str]] = {
    "tenors": [TENOR_LINE[sid]["color"]
               for sid in ("DGS3MO", "DGS2", "DGS5", "DGS10", "DGS30")],
    "credit_series": ["#cf9038", "#d0d0d0", "#e05252", "#7f8fbf"],   # HY OAS, IG OAS, HY-IG, 2s10s
    "inflation_series": ["#cf9038", "#e05252", "#d0d0d0", "#7f8fbf"],  # CPI, Core CPI, Core PCE, 10Y breakeven
    "labor_ur": ["#cf9038", "#7f8fbf"],                              # Unemployment, Participation
    "labor_claims": ["#cf9038", "#e05252", "#7f8fbf"],               # Initial, Initial 4wk avg, Continuing
}

# Every pill group that uses the borderless underline style. The tinted ones
# (above) act as chart legends; "news_sources" is a plain filter and gets a
# single neutral underline instead of per-option colours.
_PILL_KEYS = (*_PILL_TINTS, "news_sources")


# st.pills renders plain <button>s with no aria-label, so tint by position.
def _pill_sel(suffix: str = "") -> str:
    """`.st-key-<key><suffix>` for every pill group, comma-joined."""
    return ",\n".join(f".st-key-{key}{suffix}" for key in _PILL_KEYS)


_PILL_COLOUR_RULES = "\n".join(
    f'.st-key-{key} button:nth-of-type({i}),'
    f' .st-key-{key} button:nth-of-type({i}) * {{ color: {colour} !important; }}\n'
    f'.st-key-{key} button:nth-of-type({i})[data-selected="true"]'
    f' {{ border-bottom: 2px solid {colour} !important; }}'
    for key, colours in _PILL_TINTS.items()
    for i, colour in enumerate(colours, start=1)
)


_CSS = f"""
<style>
:root {{
  --bbg-mono: {MONO};
  --bbg-bg: {_BG}; --bbg-panel: {_PANEL}; --bbg-text: {_TEXT};
  --bbg-muted: {_MUTED}; --bbg-line: {_LINE}; --bbg-accent: {_ACCENT};
}}

/* ---- monospace everything, headers and metric values included ----
   `span` is matched only when it is NOT a Material icon glyph: forcing the
   mono family onto those spans breaks the icon-font ligatures and the raw
   token (e.g. "arrow_right") shows through. */
html, body, .stApp,
h1, h2, h3, h4, h5, h6, p, label, li, td, th, a, small, strong, em, code, kbd, pre,
span:not([data-testid="stIconMaterial"]):not([class*="material-symbols"]):not([class*="material-icons"]),
button, input, textarea, select,
[data-testid="stMetricValue"], [data-testid="stMetricLabel"],
[data-testid="stMetricDelta"] {{
  font-family: var(--bbg-mono) !important;
}}

/* Re-assert Streamlit's Material icon font (expander chevrons, etc.). */
[data-testid="stIconMaterial"],
[class*="material-symbols"], [class*="material-icons"],
.material-icons, .material-symbols-rounded, .material-symbols-outlined {{
  font-family: "Material Symbols Rounded", "Material Symbols Outlined",
               "Material Icons", "Material Icons Round" !important;
}}

/* ---- flatten: no rounded corners, no shadows, anywhere ---- */
*, *::before, *::after {{ border-radius: 0 !important; box-shadow: none !important; }}

/* ---- tighten vertical rhythm ---- */
/* clear the fixed 3.75rem Streamlit header so the page title isn't clipped */
.block-container {{ padding-top: 4rem !important; padding-bottom: 1rem !important; }}
[data-testid="stVerticalBlock"] {{ gap: 0.5rem !important; }}
[data-testid="stHorizontalBlock"] {{ gap: 0.6rem !important; }}
[data-testid="stElementContainer"] {{ margin-bottom: 0 !important; }}
hr, [data-testid="stDivider"] {{ margin: 0.5rem 0 !important; border-color: var(--bbg-line) !important; }}

/* ---- app title + subtitle ---- */
h1 {{
  font-size: 18px !important; font-weight: 700 !important;
  text-transform: uppercase; letter-spacing: normal;
  color: var(--bbg-text); margin: 0 0 0.15rem !important;
}}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {{
  font-size: 12px !important; text-transform: none;
  letter-spacing: normal; color: var(--bbg-muted) !important;
}}

/* ---- section headers -> small uppercase label, 1px underline ---- */
h2, h3, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3 {{
  font-size: 11px !important; font-weight: 700 !important;
  text-transform: uppercase; letter-spacing: 0.13em;
  color: var(--bbg-muted) !important;
  border-bottom: 1px solid var(--bbg-line);
  padding: 0 0 3px 0 !important; margin: 0.5rem 0 0.35rem !important;
}}

/* ---- st.metric ---- */
[data-testid="stMetric"] {{
  padding: 2px 0 4px !important; background: transparent !important; border: none !important;
}}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p {{
  font-size: 11px !important; font-weight: 600 !important;
  text-transform: uppercase; letter-spacing: 0.09em;
  color: var(--bbg-muted) !important;
}}
[data-testid="stMetricValue"] {{
  font-size: 20px !important; font-weight: 600 !important;
  line-height: 1.15 !important; color: var(--bbg-text);
}}
/* plain green/red delta text, no rounded pill */
[data-testid="stMetricDelta"] {{
  font-size: 12px !important; font-weight: 600 !important;
  background: none !important; background-color: transparent !important;
  padding: 0 !important; margin: 0 !important; border: 0 !important;
}}
[data-testid="stMetricDelta"] svg {{ display: none !important; }}

/* ---- tabs: squared, tight, terminal-like ---- */
[data-baseweb="tab-list"] {{ gap: 0 !important; border-bottom: 1px solid var(--bbg-line); }}
[data-baseweb="tab"] {{
  font-size: 11px !important; text-transform: uppercase; letter-spacing: 0.12em;
  padding: 6px 14px !important; background: transparent !important;
  color: var(--bbg-muted) !important;
}}
[data-baseweb="tab"][aria-selected="true"] {{
  color: var(--bbg-text) !important;
  border-bottom: 2px solid var(--bbg-muted) !important;
}}
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] {{ display: none !important; }}

/* ---- period selector: small label, pinned to the right ---- */
.st-key-period {{ display: flex; flex-direction: column; align-items: flex-end; }}
.st-key-period [data-testid="stWidgetLabel"] p {{
  font-size: 10px !important; font-weight: 700 !important;
  text-transform: uppercase; letter-spacing: 0.16em;
  color: var(--bbg-muted) !important; margin-bottom: 2px !important;
}}
.st-key-period [data-testid="stButtonGroup"],
.st-key-period [data-testid="stButtonGroup"] > div {{
  justify-content: flex-end; flex-wrap: wrap;
}}
.st-key-period [data-testid="stButtonGroup"] > div {{ gap: 4px; }}
.st-key-period button {{ padding: 3px 9px !important; font-size: 12px !important; }}

/* ---- borderless underline pill toggles (tenor / credit / inflation legends
       and the News source filter): text only, ~30% opacity when off ---- */
{_pill_sel(' [data-testid="stButtonGroup"] > div')} {{ gap: 4px; flex-wrap: wrap; }}
{_pill_sel(' button')} {{
  background: transparent !important; border: none !important;
  padding: 2px 7px !important; min-height: 0 !important;
  font-weight: 700 !important; letter-spacing: 0.06em;
  opacity: 0.3 !important; transition: opacity 80ms linear;
}}
{_pill_sel(' button:hover')} {{ opacity: 0.65 !important; }}
{_pill_sel(' button[data-selected="true"]')},
{_pill_sel(' button[aria-pressed="true"]')} {{ opacity: 1 !important; }}
{_PILL_COLOUR_RULES}
/* News filter: one neutral tint + underline for every source */
.st-key-news_sources button, .st-key-news_sources button * {{ color: var(--bbg-text) !important; }}
.st-key-news_sources button[data-selected="true"] {{ border-bottom: 2px solid var(--bbg-text) !important; }}

/* ---- top-right toolbar menu: keep it legible on the near-black header ---- */
[data-testid="stHeader"] {{ background: var(--bbg-bg) !important; }}
[data-testid="stToolbar"] {{ opacity: 1 !important; visibility: visible !important; }}
[data-testid="stMainMenu"] button,
[data-testid="stToolbar"] button {{
  color: var(--bbg-text) !important; opacity: 1 !important;
  background: transparent !important;
}}
[data-testid="stMainMenu"] button svg,
[data-testid="stToolbar"] button svg {{
  fill: var(--bbg-text) !important; color: var(--bbg-text) !important;
}}
[data-testid="stMainMenu"] button:hover {{ background: var(--bbg-panel) !important; }}

/* ---- sidebar ---- */
[data-testid="stSidebar"] {{
  background: var(--bbg-panel) !important; border-right: 1px solid var(--bbg-line);
}}
[data-testid="stSidebar"] .block-container {{ padding-top: 1rem !important; }}

/* ---- bordered containers (news cards) ---- */
[data-testid="stVerticalBlockBorderWrapper"] {{ border: 1px solid var(--bbg-line) !important; }}

/* ---- inputs / multiselect / buttons ---- */
[data-baseweb="select"] > div, [data-baseweb="input"] > div,
.stTextInput input, .stDateInput input, .stNumberInput input {{
  background: var(--bbg-panel) !important; border: 1px solid var(--bbg-line) !important;
}}
/* multiselect chips (Streamlit renders these as span[data-tag]) */
span[data-tag], [data-baseweb="tag"] {{
  background: var(--bbg-line) !important; color: var(--bbg-text) !important;
}}
span[data-tag] > span {{ color: var(--bbg-text) !important; }}
span[data-tag] button, span[data-tag] svg {{ color: var(--bbg-muted) !important; fill: var(--bbg-muted) !important; }}
.stButton button {{
  background: var(--bbg-panel) !important; border: 1px solid var(--bbg-line) !important;
  color: var(--bbg-text) !important;
}}

/* ---- expanders + dataframe ---- */
[data-testid="stExpander"] details {{ border: 1px solid var(--bbg-line) !important; }}
[data-testid="stExpander"] summary {{
  font-size: 11px !important; text-transform: uppercase; letter-spacing: 0.1em;
}}
[data-testid="stDataFrame"] {{ border: 1px solid var(--bbg-line) !important; }}

/* ---- st.table (movement / credit / Kalshi tables) ---- */
[data-testid="stTable"] table {{
  font-size: 12px; border-color: var(--bbg-line) !important;
  /* Streamlit forces min-width:100%; with table-layout:auto the browser then
     dumps the leftover width into one column and it reads as a stray empty
     column between the row label and the first value. Shrink to content. */
  width: auto !important; min-width: 0 !important;
}}
[data-testid="stTable"] th, [data-testid="stTable"] td {{
  border-color: var(--bbg-line) !important; padding: 3px 12px !important;
}}
[data-testid="stTable"] thead th {{
  color: var(--bbg-muted) !important; background: var(--bbg-panel) !important;
  text-transform: uppercase; font-size: 10px !important; letter-spacing: 0.08em;
  text-align: right !important;
}}
[data-testid="stTable"] thead th.blank {{ text-align: left !important; }}
[data-testid="stTable"] tbody th {{
  color: var(--bbg-muted) !important; font-weight: 700 !important; text-align: left !important;
}}

/* ---- Rate Change table (Rates tab): the one st.table that should span full
   width to match the charts above/below it, instead of shrinking to content
   like every other st.table. table-layout:fixed splits that width evenly
   across columns; without it, table-layout:auto dumps all the extra width
   into a single stray-looking column instead of distributing it (the reason
   every other st.table above is pinned to width:auto in the first place). */
.st-key-rate_change_table [data-testid="stTable"] table {{
  width: 100% !important; min-width: 100% !important; table-layout: fixed;
}}

/* ---- links ---- */
a, a:visited {{ color: var(--bbg-accent) !important; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
</style>
"""


def _register_template() -> None:
    """Register + activate the shared dark Plotly template."""
    axis = dict(
        showgrid=True,
        gridcolor=_GRID,
        gridwidth=1,
        zeroline=False,
        showline=True,
        linecolor=_LINE,
        ticks="outside",
        tickcolor=_LINE,
        ticklen=3,
        tickfont=dict(family=MONO, size=10, color=_MUTED),
        title=dict(font=dict(family=MONO, size=10, color=_MUTED)),
    )

    pio.templates["bloomberg"] = go.layout.Template(
        layout=dict(
            paper_bgcolor=_BG,
            plot_bgcolor=_BG,
            font=dict(family=MONO, size=11, color=_TEXT),
            margin=dict(l=52, r=14, t=10, b=28),
            hovermode="x unified",
            hoverlabel=dict(
                font=dict(family=MONO, size=11, color=_TEXT),
                bgcolor=_PANEL,
                bordercolor=_LINE,
            ),
            legend=dict(
                font=dict(family=MONO, size=10, color=_MUTED),
                bgcolor="rgba(0,0,0,0)",
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
            ),
            colorway=[_TEXT, _MUTED, _ACCENT, "#6f9f6f", "#b06f6f", "#7f8fbf"],
            xaxis={**axis},
            # right-aligned fixed-decimal numbers on the value axis
            yaxis={**axis, "tickformat": ".2f", "hoverformat": ".2f", "automargin": True},
        )
    )
    pio.templates.default = "plotly_dark+bloomberg"


def apply() -> None:
    """Inject the global CSS and activate the Plotly template. Call once."""
    st.markdown(_CSS, unsafe_allow_html=True)
    _register_template()


def render_chart(fig: go.Figure) -> None:
    """``st.plotly_chart`` with the modebar hidden and Streamlit's own theme
    disabled so the ``bloomberg`` template shows through."""
    st.plotly_chart(fig, theme=None, config=PLOTLY_CONFIG, width="stretch")
