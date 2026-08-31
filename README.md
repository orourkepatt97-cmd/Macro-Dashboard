# Macro Dashboard

Streamlit dashboard for US Treasury yields (2Y / 10Y) and the 2s10s spread, sourced from [FRED](https://fred.stlouisfed.org/).

## Layout

| File | Responsibility |
| --- | --- |
| `data.py` | All FRED fetching. Fetch functions are cached with `@st.cache_data`. |
| `app.py` | UI only — widgets, layout, chart assembly. |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then paste your FRED API key into .env
```

Get a free API key at https://fredaccount.stlouisfed.org/apikeys.

## Run

```bash
streamlit run app.py
```

## Features

- **2s10s spread metric** above the chart, showing the latest value and the day-over-day change in basis points.
- **DGS2 and DGS10** on one Plotly chart.
- **Date range selector** defaulting to the trailing 2 years.
