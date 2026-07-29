# Low Tide Properties — NOI / Cost Calculator

A standalone, database-free calculator: feed it three CSVs, get four JSON
reports back. No SQL connection, no external services — everything it needs
is a file you provide in `data/input/`.

Replicates the Monthly NOI, Monthly Cost, Quarterly NOI, and Quarterly Income
Yield logic from the source workbook, so the numbers match what finance
already expects.

## Setup

```bash
pip install -r requirements.txt
```

## Inputs (`data/input/`)

| File | Required columns | Source |
|---|---|---|
| `noi.csv` | `Property Code`, `Post Month`, `Actual MTD` | Export of the NOI query results |
| `cost.csv` | `Property Code`, `Post Month`, `Actual Beginning Balance`, `Actual MTD` | Export of the Cost query results |
| `topside_entries.csv` | `property_code`, `currency`, `post_month`, `amount` | Manually-maintained topside/other adjustments |

Extra columns beyond the required ones (e.g. `Property Name`, `Currency`,
`GL Account Code`) are fine — they're simply ignored.

`topside_entries.csv` only needs **change points**, not a row for every
month — the calculator carries the last known amount forward to every month
present in `cost.csv`, same as the workbook's "same as last month" formulas:

```
property_code,currency,post_month,amount
1201,CAD,2023-01-01,-16630
1601,CAD,2023-01-01,-35950
1601,CAD,2026-07-01,0
```

A property/month with no entry at or before it defaults to a $0 adjustment.
The included `topside_entries.csv` is seeded with the real entries pulled
from the source workbook — add rows to it as new adjustments come in.

## Running it

```bash
python calculate.py
python calculate.py --input-dir data/input --output-dir data/output
python calculate.py --noi-file q3_noi_export.csv   # override individual file names
```

Writes four JSON files to `data/output/` (an array of records each):

- **`monthly_noi.json`** — `{property_code, post_month, actual_mtd}`. `SUM(Actual MTD)` from `noi.csv`, grouped by property and month. No topside adjustment applies here.
- **`monthly_cost.json`** — `{property_code, post_month, base_cost, topside_amount, total_cost}`. `SUM(Actual Beginning Balance + Actual MTD)` from `cost.csv`, grouped by property and month, plus the topside adjustment for that property/month.
- **`quarterly_noi.json`** — `{property_code, quarter, ytd_noi, num_months, annualized_noi}`. Annualized NOI per property per quarter:
  - `ytd_noi` = sum of Monthly NOI from Jan 1 of that year through the quarter's final month (inclusive)
  - `num_months` = the quarter's final calendar month number (Q1 → 3, Q2 → 6, Q3 → 9, Q4 → 12)
  - `annualized_noi` = `ytd_noi × (12 / num_months)` — e.g. Q1 (3 months of YTD data) × 4, Q2 (6 months) × 2, Q3 (9 months) × 4/3, Q4 (12 months) × 1
- **`quarterly_income_yield.json`** — `{property_code, quarter, annualized_noi, cost, yield_pct}`. `(annualized_noi / cost) × 100`, where `cost` is Monthly Cost as of the quarter's final month (0% if cost is 0). Uses the exact same `annualized_noi` as `quarterly_noi.json`.

If a required column is missing from an input CSV, the script exits with a
clear error naming the file and the missing column rather than crashing
partway through. If an input file is missing entirely, same thing.

### Two deliberate deviations from the source workbook

1. **Topside property-code matching bug, fixed.** The original workbook
   formula forces topside property codes through a numeric conversion before
   matching, which silently zeroes out the adjustment for alpha-suffixed
   codes like `2101op`/`2101dev`/`2101ltp` — even when real dollar entries
   exist for them. This script matches property codes as plain strings, so
   those entries are applied correctly. Numbers for those two properties will
   differ slightly from the live workbook as a result.
2. **Dynamic quarter range.** Quarters are derived from whatever months
   actually exist in `noi.csv`, not a hardcoded range — so new data just
   works without any script changes.

## Running in GitHub Actions

This calculator has no external network dependency (no database, no APIs),
so it runs fine on a normal GitHub-hosted runner — no self-hosted runner
needed. `.github/workflows/calculate.yml` runs on `workflow_dispatch`: commit
your CSVs into `data/input/`, trigger the workflow from the Actions tab, and
it uploads the four JSON files as a downloadable `calculated-reports`
artifact.

## Project layout

```
├── calculate.py                    # the whole calculator — load CSVs, compute, write JSON
├── data/
│   ├── input/                       # noi.csv, cost.csv, topside_entries.csv go here
│   └── output/                      # monthly_noi.json etc. land here (git-ignored)
├── tests/test_calculate.py          # unit tests for every calculation, incl. the annualization formula
└── .github/workflows/calculate.yml  # workflow_dispatch: run calculate.py, upload JSON artifact
```

## Tests

```bash
python -m pytest tests/
```

Covers Monthly NOI aggregation, Monthly Cost with topside forward-fill (and
the alpha-suffixed property code fix), the Quarterly NOI annualization
formula (Q1×4, Q2×2, etc.), Quarterly Income Yield, input validation errors,
JSON date formatting, and a full end-to-end run against temporary CSVs.
