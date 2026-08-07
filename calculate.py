"""Standalone NOI / Cost / Income Yield calculator.

Reads three manually-supplied CSVs (NOI query results, Cost query results, and
Topside Entries adjustments) and writes four JSON reports. No database
connection of any kind -- every input is a CSV file you provide.

Required input columns:

  data/input/noi.csv
      Property Code, Post Month, Actual MTD

  data/input/cost.csv
      Property Code, Post Month, Actual Beginning Balance, Actual MTD

  data/input/topside_entries.csv
      Either of two layouts is accepted:
        - Wide export straight out of the workbook: property code in the first
          column, a "Currency" column, and one column per month labeled like
          "Jan-23"/"Feb-23" with accounting-formatted amounts (e.g. "$1,847,640",
          "($16,630)"). Blank cells mean no entry that month.
        - Tidy format: property_code, currency, post_month, amount (one row per
          explicit "change point" -- see README).
      A property/month with no entry at or before it defaults to a $0 adjustment.

  data/input/building_startDebt.csv
      building, startDebt -- one row per building, the outstanding debt
      balance AS OF the earliest month in the reports (Jan-2023). That
      month's movements are NOT applied on top of it -- startDebt already
      is the Jan-2023 balance; the rollforward starts applying debt.csv
      movements from the following month onward.

  data/input/debt.csv
      Property Code, Post Month, Category, Actual MTD -- one row per GL
      account movement. Only two categories change the outstanding debt
      balance:
        - Payment/Draw: principal draws (positive) and paydowns (negative).
        - Capitalized Interest (the "WIP Cap Interest" GL account): interest
          added to the loan balance instead of being paid in cash.
      "Interest Expense" and "Capitalized Interest (Contra)" are P&L-only
      entries (the latter is the offsetting entry against Interest Expense
      when interest is capitalized) and are excluded -- including them would
      double-count the Capitalized Interest movement for construction loans
      and misstate the balance for every other loan, where interest is paid
      in cash and never touches principal.
      Each month's balance is rounded to a whole dollar before being carried
      forward as the next month's opening balance (matching the source
      ledger), so rounding compounds intentionally here rather than being
      deferred to output time as it is elsewhere in this file.
      A building with no debt.csv rows at all carries its startDebt balance
      forward flat across every month. A building with no startDebt at all
      (no row in building_startDebt.csv) gets a debt_balance of 0 in
      liveFinance.json, consistent with how every other missing property/month
      combo in this pipeline defaults to 0 rather than being omitted.

Calculations produced (data/output/*.json):

  monthly_noi.json
      SUM(Actual MTD) from noi.csv, grouped by Property Code and Post Month.

  monthly_cost.json
      SUM(Actual Beginning Balance + Actual MTD) from cost.csv, grouped by
      Property Code and Post Month, PLUS the Topside Entries adjustment for
      that property/month (most recent entry at or before that month, carried
      forward; 0 if none).

  quarterly_noi.json
      Annualized NOI per property per quarter:
        ytdNOI        = SUM of Monthly NOI from Jan 1 of that year through the
                        quarter's final month (inclusive)
        numMonths     = the quarter's final calendar month number
                        (Q1 -> 3, Q2 -> 6, Q3 -> 9, Q4 -> 12)
        annualizedNOI = ytdNOI * (12 / numMonths)
                        e.g. Q1 (3 months of YTD data) x 4, Q2 (6 months) x 2,
                        Q3 (9 months) x 4/3, Q4 (12 months) x 1

  quarterly_income_yield.json
      (annualizedNOI / cost) * 100, where cost = Monthly Cost as of the
      quarter's final month; 0 if cost is 0. Same annualizedNOI as
      quarterly_noi.json.

  liveFinance.json
      Monthly NOI and Monthly Cost merged per property, PLUS debt_balance:
      the building's startDebt (from building_startDebt.csv) rolled forward
      month by month with the Payment/Draw and Capitalized Interest movements
      from debt.csv (see the debt.csv note above for exactly which rows count
      and why).

All dollar figures in the output JSON are rounded to whole numbers (no
cents). Rounding is applied only when writing each file, never to the
DataFrames used internally, so it can't compound into a later calculation
(e.g. Monthly NOI feeding Quarterly NOI). yield_pct is a percentage rather
than a dollar figure, so it keeps 2 decimal places instead.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

MONTH_HEADER_FORMAT = "%b-%y"  # e.g. "Jan-23"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = BASE_DIR / "data" / "input"
DEFAULT_OUTPUT_DIR = BASE_DIR / "data" / "output"

NOI_REQUIRED_COLUMNS = ["Property Code", "Post Month", "Actual MTD"]
COST_REQUIRED_COLUMNS = ["Property Code", "Post Month", "Actual Beginning Balance", "Actual MTD"]
TOPSIDE_REQUIRED_COLUMNS = ["property_code", "post_month", "amount"]
DEBT_REQUIRED_COLUMNS = ["Property Code", "Post Month", "Category", "Actual MTD"]
BUILDING_START_DEBT_REQUIRED_COLUMNS = ["building", "startDebt"]

# Only these two debt.csv categories represent an actual change to the
# outstanding debt balance. "Interest Expense" and "Capitalized Interest
# (Contra)" are P&L-only bookkeeping entries -- see the module docstring.
DEBT_BALANCE_CATEGORIES = ["Payment/Draw"]


class InputValidationError(ValueError):
    pass


def _require_columns(df: pd.DataFrame, required: list, source: str) -> None:
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise InputValidationError(
            f"{source} is missing required column(s): {missing}. "
            f"Found columns: {list(df.columns)}"
        )


def _normalize_property_code(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip()


def _drop_blank_rows(df: pd.DataFrame, key_columns: list) -> pd.DataFrame:
    """Query exports sometimes have trailing blank rows (or a stray note row)
    after the real data -- drop any row missing a value in a key column."""
    return df.dropna(subset=key_columns).reset_index(drop=True)


def load_noi_csv(path: Path) -> pd.DataFrame:
    # Property Code must be read as text: if every value happens to be
    # numeric-looking (no alpha-suffixed codes like "2101op" in this export),
    # pandas would otherwise infer int/float64 -- and blank trailing rows push
    # it to float64, turning "1102" into "1102.0" and silently breaking every
    # join against it.
    df = pd.read_csv(path, dtype={"Property Code": str})
    _require_columns(df, NOI_REQUIRED_COLUMNS, path.name)
    df = _drop_blank_rows(df, NOI_REQUIRED_COLUMNS)
    df["Property Code"] = _normalize_property_code(df["Property Code"])
    df["Post Month"] = pd.to_datetime(df["Post Month"])
    df["Actual MTD"] = pd.to_numeric(df["Actual MTD"])
    return df


def load_cost_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"Property Code": str})
    _require_columns(df, COST_REQUIRED_COLUMNS, path.name)
    df = _drop_blank_rows(df, COST_REQUIRED_COLUMNS)
    df["Property Code"] = _normalize_property_code(df["Property Code"])
    df["Post Month"] = pd.to_datetime(df["Post Month"])
    df["Actual Beginning Balance"] = pd.to_numeric(df["Actual Beginning Balance"])
    df["Actual MTD"] = pd.to_numeric(df["Actual MTD"])
    return df


def load_building_start_debt_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"building": str})
    _require_columns(df, BUILDING_START_DEBT_REQUIRED_COLUMNS, path.name)
    df = _drop_blank_rows(df, BUILDING_START_DEBT_REQUIRED_COLUMNS)
    df["building"] = _normalize_property_code(df["building"])
    df["startDebt"] = pd.to_numeric(df["startDebt"])
    return df


def load_debt_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"Property Code": str})
    # The source export's Actual MTD header has stray leading/trailing spaces
    # (" Actual MTD ") -- normalize before validating required columns.
    df.columns = [str(col).strip() for col in df.columns]
    _require_columns(df, DEBT_REQUIRED_COLUMNS, path.name)
    df = _drop_blank_rows(df, DEBT_REQUIRED_COLUMNS)
    df["Property Code"] = _normalize_property_code(df["Property Code"])
    df["Post Month"] = pd.to_datetime(df["Post Month"])
    df["Category"] = df["Category"].astype(str).str.strip()
    # Actual MTD is accounting-formatted (e.g. " 13,212.02 ", "(4,209,523.24)"),
    # not a plain number -- reuse the same parser as the Topside Entries file.
    df["Actual MTD"] = df["Actual MTD"].apply(_parse_accounting_number)
    return df


def _parse_accounting_number(value) -> float:
    """Parse an accounting-formatted cell into a float, or None if blank.

    Handles '$1,847,640' (positive), '($16,630)' (negative, parens), and
    plain numbers. Returns None for blank/NaN cells."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    if text == "" or text.lower() == "nan":
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace("$", "").replace(",", "").strip()
    if text == "":
        return None
    number = float(text)
    return -number if negative else number


def _parse_month_header(value) -> pd.Timestamp:
    """Parse a month column header like 'Jan-23' into a Timestamp for the
    first of that month, or None if it doesn't look like one (including a
    blank cell, which pandas parses to NaT rather than raising)."""
    text = str(value).strip()
    if text == "":
        return None
    try:
        parsed = pd.to_datetime(text, format=MONTH_HEADER_FORMAT)
    except (ValueError, TypeError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.replace(day=1)


def _load_topside_wide_csv(path: Path) -> pd.DataFrame:
    """Parse the wide export straight out of the workbook: property code in
    column 0, a 'Currency' column, and one accounting-formatted column per
    month (e.g. 'Jan-23'). Blank cells mean no entry for that month."""
    raw = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
    header_row = raw.iloc[0]

    month_cols = {}
    for col_idx, value in header_row.items():
        month = _parse_month_header(value)
        if month is not None:
            month_cols[col_idx] = month

    if not month_cols:
        raise InputValidationError(
            f"{path.name} does not look like a Topside Entries export -- expected a month columns formatted like 'Jan-23'."
        )

    records = []
    for _, row in raw.iloc[1:].iterrows():
        property_code = str(row[0]).strip()
        if not property_code:
            continue
        for col_idx, month in month_cols.items():
            amount = _parse_accounting_number(row[col_idx])
            if amount is None:
                continue
            if property_code in ["1803", "1702", "1601"]:
                logger.info(
                    "TOPSIDE PARSE property=%s month=%s amount=%s",
                    property_code,
                    month.strftime("%Y-%m-%d"),
                    amount,
                )
            records.append(
                {
                    "property_code": property_code,
                    "post_month": month,
                    "amount": amount,
                }
            )
    return pd.DataFrame(records, columns=TOPSIDE_REQUIRED_COLUMNS)


def _load_topside_tidy_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"property_code": str})
    _require_columns(df, TOPSIDE_REQUIRED_COLUMNS, path.name)
    df = _drop_blank_rows(df, TOPSIDE_REQUIRED_COLUMNS)
    df["property_code"] = _normalize_property_code(df["property_code"])
    df["post_month"] = pd.to_datetime(df["post_month"])
    df["amount"] = pd.to_numeric(df["amount"])
    return df


def load_topside_csv(path: Path) -> pd.DataFrame:
    """Accepts either the wide workbook export or the tidy change-point format
    -- detected from the file's own first-row header."""
    with open(path, newline="") as f:
        first_line = f.readline()
    header_cells = [cell.strip().strip('"') for cell in first_line.split(",")]
    if set(TOPSIDE_REQUIRED_COLUMNS).issubset(header_cells):
        df = _load_topside_tidy_csv(path)
    else:
        df = _load_topside_wide_csv(path)

    df["property_code"] = _normalize_property_code(df["property_code"])
    return df


def compute_monthly_noi(noi_df: pd.DataFrame) -> pd.DataFrame:
    result = (
        noi_df.groupby(["Property Code", "Post Month"], as_index=False)["Actual MTD"]
        .sum()
        .rename(
            columns={
                "Property Code": "property_code",
                "Post Month": "post_month",
                "Actual MTD": "actual_mtd",
            }
        )
    )
                # Build complete property x month matrix
    all_properties = result["property_code"].unique()
    all_months = pd.date_range(
        start=result["post_month"].min(),
        end=result["post_month"].max(),
        freq="MS"
         )

    full_index = pd.MultiIndex.from_product(
        [all_properties, all_months],
        names=["property_code", "post_month"]
        )

    result = (
        result
        .set_index(["property_code", "post_month"])
        .reindex(full_index, fill_value=0)
        .reset_index()
        )

    return result.sort_values(["property_code", "post_month"]).reset_index(drop=True)

def monthly_noi_to_json(df):
    output = {}

    for month, month_df in df.groupby("post_month"):
        month_key = month.strftime("%Y-%m")

        output[month_key] = {
            str(row.property_code): {
                "actual_mtd": row.actual_mtd
            }
            for row in month_df.itertuples()
        }

    return output

def _topside_adjustment_for_months(topside_df: pd.DataFrame, target_months) -> pd.DataFrame:
    """For every (property_code, month) in target_months, find the topside amount
    that applies -- the most recent explicit entry at or before that month,
    defaulting to 0 if the property has no entry at that month."""
    target_months = pd.DatetimeIndex(sorted(pd.to_datetime(pd.Series(target_months)).unique()))

    frames = []
    for property_code, group in topside_df.groupby("property_code"):
        entries = group.sort_values("post_month").drop_duplicates("post_month", keep="last")
        combined_index = pd.DatetimeIndex(sorted(set(entries["post_month"]) | set(target_months)))
        carried = (
            entries.set_index("post_month")["amount"]
            .reindex(target_months)
            .fillna(0.0)
        )
        frames.append(
            pd.DataFrame(
                {
                    "property_code": property_code,
                    "post_month": target_months,
                    "topside_amount": carried.values,
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=["property_code", "post_month", "topside_amount"])
    return pd.concat(frames, ignore_index=True)


def compute_monthly_cost(cost_df: pd.DataFrame, topside_df: pd.DataFrame) -> pd.DataFrame:
    df = cost_df.copy()
    df["_base"] = df["Actual Beginning Balance"].fillna(0) + df["Actual MTD"].fillna(0)

    base_cost = (
        df.groupby(["Property Code", "Post Month"], as_index=False)["_base"]
        .sum()
        .rename(columns={"Property Code": "property_code", "Post Month": "post_month", "_base": "base_cost"})
    )

    # Full grid: every property x every month that appears anywhere in the Cost
    # data -- cells with no matching Cost rows are treated as 0, not omitted.
    months = pd.date_range(
    start=df["Post Month"].min(),
    end=df["Post Month"].max(),
    freq="MS"
    )
    properties = sorted(df["Property Code"].unique())
    grid = pd.MultiIndex.from_product([properties, months], names=["property_code", "post_month"]).to_frame(
        index=False
    )
    grid = grid.merge(base_cost, on=["property_code", "post_month"], how="left")
    grid["base_cost"] = grid["base_cost"].fillna(0.0)

    topside_adjustment = _topside_adjustment_for_months(topside_df, months)
    for code in ["1601", "1702", "1803"]:
        logger.info(
          "\nCalculated topside adjustments for %s:\n%s",
          code,
          topside_adjustment[
                topside_adjustment["property_code"] == code
          ].tail(24)
        )
  
    grid = grid.merge(topside_adjustment, on=["property_code", "post_month"], how="left")
    grid["topside_amount"] = grid["topside_amount"].fillna(0.0)
    for code in ["1601", "1702", "1803"]:
        logger.info(
          "\nMerged results for %s:\n%s",
          code,
          grid[
              grid["property_code"] == code
          ][[
              "property_code",
              "post_month",
              "base_cost",
              "topside_amount"
          ]].tail(24)
        )

    

    grid["total_cost"] = grid["base_cost"] + grid["topside_amount"]
    return grid.sort_values(["property_code", "post_month"]).reset_index(drop=True)


def compute_monthly_debt_balance(
    debt_df: pd.DataFrame, building_start_debt_df: pd.DataFrame, target_months
) -> pd.DataFrame:
    """Roll each building's startDebt forward month by month.

    startDebt is already the balance AS OF the first target month (Jan 2023)
    -- no movement is applied for that month. Each following month's balance
    is the PRIOR month's *rounded* balance plus that month's Payment/Draw and
    Capitalized Interest movements from debt.csv, rounded to a whole dollar
    before being carried forward as the next month's opening balance (this
    matches the source ledger, which books and carries forward whole-dollar
    balances rather than fractional cents). A building with no matching
    debt.csv rows carries its startDebt forward flat (zero movement every
    month)."""
    target_months = pd.DatetimeIndex(sorted(pd.to_datetime(pd.Series(target_months)).unique()))

    movement = (
        debt_df[debt_df["Category"].isin(DEBT_BALANCE_CATEGORIES)]
        .groupby(["Property Code", "Post Month"], as_index=False)["Actual MTD"]
        .sum()
        .rename(columns={"Property Code": "property_code", "Post Month": "post_month", "Actual MTD": "movement"})
    )
    movement_by_property = {
        property_code: group.set_index("post_month")["movement"]
        for property_code, group in movement.groupby("property_code")
    }

    frames = []
    for row in building_start_debt_df.itertuples():
        building = row.building
        monthly_movement = movement_by_property.get(building)
        if monthly_movement is None:
            monthly_movement = pd.Series(0.0, index=target_months)
        else:
            monthly_movement = monthly_movement.reindex(target_months).fillna(0.0)

        balances = []
        balance = round(row.startDebt)
        for i, month in enumerate(target_months):
            if i > 0:
                balance = round(balance + monthly_movement.loc[month])
            balances.append(balance)

        frames.append(
            pd.DataFrame(
                {
                    "property_code": building,
                    "post_month": target_months,
                    "debt_balance": balances,
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=["property_code", "post_month", "debt_balance"])
    return pd.concat(frames, ignore_index=True).sort_values(["property_code", "post_month"]).reset_index(drop=True)


def _quarter_label(post_month: pd.Timestamp) -> str:
    quarter_num = (post_month.month - 1) // 3 + 1
    return f"{post_month.year}-Q{quarter_num}"


def _sorted_quarters(quarters) -> list:
    return sorted(set(quarters), key=lambda q: (int(q[:4]), int(q[-1])))


def _ytd_annualized_noi(monthly_noi: pd.DataFrame) -> pd.DataFrame:
    """Per property per quarter: YTD NOI (Jan 1 of that year through the
    quarter's final month) annualized by (12 / numMonths)."""
    noi = monthly_noi.copy()
    noi["quarter"] = noi["post_month"].apply(_quarter_label)

    properties = sorted(noi["property_code"].unique())
    quarters = _sorted_quarters(noi["quarter"].unique())

    rows = []
    for quarter in quarters:
        year = int(quarter[:4])
        quarter_num = int(quarter[-1])
        end_month_num = quarter_num * 3
        quarter_end_date = pd.Timestamp(year=year, month=end_month_num, day=1)
        num_months = end_month_num

        ytd_mask = (noi["post_month"] >= pd.Timestamp(year=year, month=1, day=1)) & (
            noi["post_month"] <= quarter_end_date
        )
        ytd_noi_by_property = noi[ytd_mask].groupby("property_code")["actual_mtd"].sum()

        for property_code in properties:
            ytd_noi = ytd_noi_by_property.get(property_code, 0.0)
            annualized_noi = ytd_noi * (12 / num_months)
            rows.append(
                {
                    "property_code": property_code,
                    "quarter": quarter,
                    "quarter_end_month": quarter_end_date,
                    "ytd_noi": ytd_noi,
                    "num_months": num_months,
                    "annualized_noi": annualized_noi,
                }
            )

    result = pd.DataFrame(rows)
    result["_year"] = result["quarter"].str[:4].astype(int)
    result["_q"] = result["quarter"].str[-1].astype(int)
    result = result.sort_values(["property_code", "_year", "_q"]).drop(columns=["_year", "_q"])
    return result.reset_index(drop=True)


def compute_quarterly_noi(monthly_noi: pd.DataFrame) -> pd.DataFrame:
    annualized = _ytd_annualized_noi(monthly_noi)
    return annualized[["property_code", "quarter", "ytd_noi", "num_months", "annualized_noi"]]


def compute_quarterly_income_yield(monthly_noi: pd.DataFrame, monthly_cost: pd.DataFrame) -> pd.DataFrame:
    annualized = _ytd_annualized_noi(monthly_noi)
    cost_lookup = monthly_cost.set_index(["property_code", "post_month"])["total_cost"]

    def _cost_for(row):
        return cost_lookup.get((row["property_code"], row["quarter_end_month"]), 0.0)

    annualized["cost"] = annualized.apply(_cost_for, axis=1)
    annualized["yield_pct"] = annualized.apply(
        lambda row: 0.0 if row["cost"] == 0 else (row["annualized_noi"] / row["cost"]) * 100, axis=1
    )
    return annualized[["property_code", "quarter", "annualized_noi", "cost", "yield_pct"]]


def financial_to_json(df):
    output = {}

    for month, month_df in df.groupby("post_month"):
        month_key = month.strftime("%Y-%m")

        output[month_key] = {}

        for row in month_df.itertuples():
            output[month_key][str(row.property_code)] = {
                "actual_mtd": row.actual_mtd,
                "base_cost": row.base_cost,
                "topside_amount": row.topside_amount,
                "total_cost": row.total_cost,
                "debt_balance": row.debt_balance
            }

    return output


def _round_for_output(df: pd.DataFrame, whole_dollar_columns=(), decimal_columns=None) -> pd.DataFrame:
    """Round a copy of df for display -- whole_dollar_columns become integers
    (no cents), decimal_columns are rounded to the given number of decimal
    places. Does not mutate df, so upstream calculations that reuse the
    unrounded DataFrame (e.g. Monthly NOI feeding Quarterly NOI) aren't
    affected by this rounding."""
    df = df.copy()
    for col in whole_dollar_columns:
        df[col] = df[col].round(0).astype("int64")
    for col, decimals in (decimal_columns or {}).items():
        df[col] = df[col].round(decimals)
    return df


def _write_json(data, path: Path) -> None:
    if isinstance(data, pd.DataFrame):
        records = data.to_dict(orient="records")

        for record in records:
            for key, value in record.items():
                if isinstance(value, pd.Timestamp):
                    record[key] = value.strftime("%Y-%m-%d")

        with open(path, "w") as f:
            json.dump(records, f, indent=2, default=str)

    else:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Calculate Monthly NOI, Monthly Cost, Quarterly NOI, and Quarterly Income Yield from manually-supplied CSVs.")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="Directory containing noi.csv, cost.csv, topside_entries.csv, debt.csv, building_startDebt.csv.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory to write output JSON to.")
    parser.add_argument("--noi-file", default="noi.csv")
    parser.add_argument("--cost-file", default="cost.csv")
    parser.add_argument("--topside-file", default="topside_entries.csv")
    parser.add_argument("--debt-file", default="debt.csv")
    parser.add_argument("--building-start-debt-file", default="building_startDebt.csv")
    args = parser.parse_args(argv)

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        logger.info("Loading input CSVs from %s", input_dir)
        noi_df = load_noi_csv(input_dir / args.noi_file)
        cost_df = load_cost_csv(input_dir / args.cost_file)
        topside_df = load_topside_csv(input_dir / args.topside_file)
        debt_df = load_debt_csv(input_dir / args.debt_file)
        building_start_debt_df = load_building_start_debt_csv(input_dir / args.building_start_debt_file)
    except (InputValidationError, FileNotFoundError) as exc:
        logger.error(str(exc))
        return 1

    logger.info(
    "Loaded %s topside rows across %s properties",
    len(topside_df),
    topside_df["property_code"].nunique(),
    )

    for code in ["1601", "1702", "1803"]:
        logger.info(
        "\nRaw topside data for %s:\n%s",
        code,
        topside_df[topside_df["property_code"] == code],
    )
    logger.info("Computing Monthly NOI...")
    monthly_noi = compute_monthly_noi(noi_df)

    monthly_noi = _round_for_output(
        monthly_noi,
        whole_dollar_columns=["actual_mtd"]
    )

    monthly_noi_json = monthly_noi_to_json(monthly_noi)

    _write_json(
        monthly_noi_json,
        output_dir / "monthly_noi.json"
    )
    logger.info("Monthly NOI: %d properties, %d months", monthly_noi["property_code"].nunique(), monthly_noi["post_month"].nunique())

    logger.info("Computing Monthly Cost (with topside adjustments)...")
    monthly_cost = compute_monthly_cost(cost_df, topside_df)
    _write_json(
        _round_for_output(monthly_cost, whole_dollar_columns=["base_cost", "topside_amount", "total_cost"]),
        output_dir / "monthly_cost.json",
    )
    logger.info("Monthly Cost: %d properties, %d months", monthly_cost["property_code"].nunique(), monthly_cost["post_month"].nunique())

    logger.info("Computing Quarterly NOI (annualized)...")
    quarterly_noi = compute_quarterly_noi(monthly_noi)
    _write_json(
        _round_for_output(quarterly_noi, whole_dollar_columns=["ytd_noi", "annualized_noi"]),
        output_dir / "quarterly_noi.json",
    )
    logger.info("Quarterly NOI: %d properties, %d quarters", quarterly_noi["property_code"].nunique(), quarterly_noi["quarter"].nunique())

    logger.info("Computing Quarterly Income Yield...")
    quarterly_income_yield = compute_quarterly_income_yield(monthly_noi, monthly_cost)
    _write_json(
        _round_for_output(
            quarterly_income_yield,
            whole_dollar_columns=["annualized_noi", "cost"],
            decimal_columns={"yield_pct": 2},
        ),
        output_dir / "quarterly_income_yield.json",
    )
    logger.info(
        "Quarterly Income Yield: %d properties, %d quarters",
        quarterly_income_yield["property_code"].nunique(),
        quarterly_income_yield["quarter"].nunique(),
    )

    logger.info("Computing Monthly Debt Balance...")
    debt_target_months = pd.DatetimeIndex(
        sorted(set(monthly_noi["post_month"]) | set(monthly_cost["post_month"]) | set(debt_df["Post Month"]))
    )
    monthly_debt_balance = compute_monthly_debt_balance(debt_df, building_start_debt_df, debt_target_months)
    monthly_debt_balance = _round_for_output(monthly_debt_balance, whole_dollar_columns=["debt_balance"])
    logger.info(
        "Monthly Debt Balance: %d buildings, %d months",
        monthly_debt_balance["property_code"].nunique(),
        monthly_debt_balance["post_month"].nunique(),
    )

    financial = (
    monthly_noi
    .merge(
        monthly_cost,
        on=["property_code", "post_month"],
        how="outer"
    )
    .merge(
        monthly_debt_balance,
        on=["property_code", "post_month"],
        how="outer"
    )
    .fillna(0)
)

    financial_json = financial_to_json(financial)

    _write_json(
        financial_json,
        output_dir / "liveFinance.json"
    )


    logger.info("All calculations complete. Output written to %s", output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
