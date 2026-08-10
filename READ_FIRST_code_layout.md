# App_V5 — the single, complete source of truth (2026-08-04)

This folder contains **every file that belongs in the GitHub repo**, in one place. It
replaces the old split where app code lived in `App_V4/` *and* was copied to the repo
root, with older `App/`, `App_V2/`, `App_V3/` versions alongside. **Edit here and nowhere
else.** App_V5 is verified: all Python compiles, and it includes the 2026-08-04 fixes
(web.py M-4 weighting, scraper alerting + non-fatal footing check).

## How this maps to GitHub / the live app
The Streamlit app runs from the **repo root**. So make the repo contain exactly these
files. Two equivalent ways:
- **(Recommended) Flatten:** put the *contents* of App_V5 at the repo root (as today),
  and delete the old `App/`, `App_V2/`, `App_V3/`, `App_V4/` folders from the repo. One
  copy, no subfolders.
- **(Alternative) Point Streamlit at the folder:** keep files in an `App_V5/` folder in
  the repo and set the Streamlit app's *Main file path* to `App_V5/app.py`.

Either way there is now ONE copy of each file.

## What's inside
**App (Streamlit):** app.py, calc.py, config.py, datasource.py, datastore.py,
identity.py, scorecard_pdf.py, requirements.txt, and two renderers —
`web.py` (admin / district view) and `dashboard.py` (store-login view). Both are live.

**Scraper (GitHub Actions):** scraper.py, autopoll_parser.py, baseline.json,
`.github/workflows/hourly.yml`.

**Config / infra:** `.streamlit/config.toml`, `.gitignore`, `.env.example`,
`supabase_schema_v4.sql`.

**Assets / docs:** Store_Level_Dashboard_Guide_V4.pdf, README.md, this file.

**Tests / tools:** test_parser.py, fleet_check.py (diagnostic, run locally).

## NOT included (on purpose)
Secrets. `SUPABASE_URL/KEY`, `AUTOPOLL_USER/PASS`, `ADMIN_PASSWORD`, and the `STORES`
variable live only as GitHub Actions + Streamlit secrets — never in the repo.

## requirements.txt must stay the UNION
It covers both the app and the scraper: requests, beautifulsoup4, streamlit, plotly,
fpdf2, pymupdf. A slimmer copy once broke the scraper.
