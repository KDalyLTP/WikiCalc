"""Building stacking plan builder.

Rebuilds building-stacking.json from a Yardi stackingDataQuery.csv export plus
a buildingOverrides.json of hand-maintained values the export cannot supply.

Two subcommands:

  parse              stackingDataQuery.csv + buildingOverrides.json
                     -> building-stacking.json          (the normal pipeline run)

  extract-overrides  an existing building-stacking.json
                     -> buildingOverrides.json          (bootstrap / refresh the
                                                         hand-maintained values)

`extract-overrides` is a maintenance command: it is how buildingOverrides.json
was originally produced from a known-good stacking JSON, and how you refresh it
after editing overrides upstream. The normal pipeline only runs `parse`.


WHAT COMES FROM WHERE
---------------------
From the CSV (verified 1:1 against a known-good stacking JSON, 328/328 units):
    unitId, unit, tenantID, tenant, tenantSF, leaseExpiry, bomaArea,
    parkingArea, storageArea, signageArea, basementArea, yardArea,
    monthlyRent, monthlyRentPerArea, currentAmendmentId, currentAmendmentType,
    currentAmendmentLeaseFrom, currentAmendmentLeaseTo
  derived: buildingId (numeric prefix of Property Code), floor (Floor Code),
           floorSF (sum of the floor's tenantSF)

From buildingOverrides.json (not derivable from the export):
    per building : propertyId, acquiredYear, acquisitionCost
    per unit     : type, bomaMeasurementAsPerLease, typeLinkxcl
    per unit     : tenancy{...}  -- only for units whose tenancy is blank in the
                   CSV but live in the stack (see below)

Deliberately NOT emitted:
    bomaFloorSF  -- computed downstream in the SWA app. The source JSON's own
                    values are unreliable: 8 of 158 floors disagree with the sum
                    of their own units' bomaArea (1602/1603/1704, the buildings
                    whose stack merges two Yardi property codes).
    rentPSF      -- derivable as monthlyRentPerArea * 12 for 322/328 units, but
                    the remaining 6 are driven by rentPSFxcl, which is a manual
                    Excel override that is not captured. Compute downstream if
                    needed rather than emitting a field that is silently wrong
                    for 6 units.
    rentPSFxcl, tenantSFxcl -- manual Excel overrides, not captured anywhere.


CONVENTIONS REPRODUCED FROM THE SOURCE JSON
-------------------------------------------
* Blank tenant -> "Vacant"; blank numerics -> 0; blank areas -> "" (not 0).
* Dates render as "%Y-%m-%d %-H:%M" (e.g. "2024-08-01 0:00").
* Floor codes: "Ground"/"Basement"/blank -> 0, "300" -> 3 (Yardi codes that
  mezzanine as 300), otherwise the numeric value.
* currentAmendmentType is OMITTED entirely on units with no current amendment.
  The source JSON is not uniformly shaped -- 210 spaces carry the key and 118
  do not -- and that presence exactly tracks currentAmendmentId being set.
* Integral numbers are emitted as ints (tenantSF, floorSF, floor); monthlyRent
  and monthlyRentPerArea are always floats; area fields are floats or "".
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = BASE_DIR / "data" / "input"
DEFAULT_OUTPUT_DIR = BASE_DIR / "data" / "output"

DEFAULT_CSV_FILE = DEFAULT_INPUT_DIR / "stackingDataQuery.csv"
DEFAULT_OVERRIDES_FILE = DEFAULT_INPUT_DIR / "buildingOverrides.json"
DEFAULT_STACKING_FILE = DEFAULT_OUTPUT_DIR / "building-stacking.json"

CSV_REQUIRED_COLUMNS = [
    "Property Id",
    "Property Code",
    "Floor Code",
    "Unit Id",
    "Code",
    "Current Tenant Id",
    "Current Tenant Name",
    "Current Amendment Id",
    "Current Amendment Type",
    "Current Amendment Lease From",
    "Current Amendment Lease To",
    "Current Occupancy Expiry",
    "Area - Default",
    "Area - BOMA",
    "Area - Parking Stall",
    "Area - Storage Area",
    "Area - Signage",
    "Area - Basement Area",
    "Area - Yard Area",
    "Monthly Rent",
    "Monthly Rent per Area - Default",
]

# propertyId cannot be derived for the three buildings whose stack merges two
# Yardi property codes (1602off/1602ret, 1603off/1603ret, 1704off/1704ret).
# Each code carries its own Property Id and the stacking JSON keeps only one of
# the pair, but not consistently -- 1602 and 1603 keep the "off" id while 1704
# keeps the "ret" id -- so neither min(), max(), nor an "office wins" rule
# reproduces all three. It is a stable per-building constant, so it is pinned.
BUILDING_OVERRIDE_FIELDS = ["propertyId", "acquiredYear", "acquisitionCost"]

# `type` is hand-entered free text (50 distinct values like "Service, Restaurant
# Class 1" or "TBD"); the CSV's Unit Type Description only reproduces 181 of 328.
UNIT_OVERRIDE_FIELDS = ["type", "bomaMeasurementAsPerLease", "typeLinkxcl"]

# Preserved only for units the CSV cannot supply a tenancy for.
TENANCY_OVERRIDE_FIELDS = [
    "tenant",
    "tenantID",
    "currentAmendmentId",
    "currentAmendmentType",
    "currentAmendmentLeaseFrom",
    "currentAmendmentLeaseTo",
    "leaseExpiry",
]

DATE_FORMAT_IN = "%Y-%m-%d %H:%M"
VACANT = "Vacant"


class InputValidationError(ValueError):
    pass


# ---------------------------------------------------------------- conversions


def _blank(value):
    """None for empty/missing cells, otherwise the stripped string."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text == "" else text


def _number(value, default=0):
    """Numeric cell -> int when integral, float otherwise, `default` when blank."""
    text = _blank(value)
    if text is None:
        return default
    number = float(text.replace(",", ""))
    return int(number) if number.is_integer() else number


def _float(value, default=0.0) -> float:
    text = _blank(value)
    if text is None:
        return default
    return float(text.replace(",", ""))


def _int(value):
    text = _blank(value)
    return None if text is None else int(float(text))


def _area(value):
    """Area cells render as "" rather than 0 when empty, matching the source."""
    text = _blank(value)
    if text is None:
        return ""
    number = float(text.replace(",", ""))
    return "" if number == 0 else number


def _datetime(value):
    """Yardi timestamp -> the source JSON's "%Y-%m-%d %-H:%M" rendering."""
    text = _blank(value)
    if text is None:
        return None
    for fmt in (DATE_FORMAT_IN, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return f"{parsed.strftime('%Y-%m-%d')} {parsed.hour}:{parsed.strftime('%M')}"
    raise InputValidationError(f"Unrecognized date value: {value!r}")


def _floor_code(value):
    """Floor Code -> floor number. Blank/Ground/Basement are ground level; Yardi
    mezzanines are coded 300."""
    text = _blank(value)
    if text is None:
        return 0
    if text.lower() in ("ground", "basement"):
        return 0
    try:
        number = float(text)
    except ValueError:
        return 0
    if number == 300:
        return 3
    return int(number) if number.is_integer() else number


def _building_id(property_code):
    """Building id is the numeric prefix of the property code -- the stack merges
    suffixed codes like 1602off/1602ret into one building."""
    text = _blank(property_code)
    if text is None:
        return None
    digits = ""
    for char in text:
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


# --------------------------------------------------------------------- inputs


def load_stacking_csv(path: Path) -> list:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in CSV_REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise InputValidationError(
                f"{path.name} is missing required column(s): {missing}. "
                f"Found columns: {reader.fieldnames}"
            )
        rows = [r for r in reader if _blank(r.get("Unit Id")) is not None]
    if not rows:
        raise InputValidationError(f"{path.name} contains no unit rows.")
    return rows


def load_overrides(path: Path) -> dict:
    if not path.exists():
        raise InputValidationError(
            f"{path} not found. Generate it with: python stacking.py extract-overrides"
        )
    with open(path) as f:
        return json.load(f)


# ------------------------------------------------------------ overrides build


def _iter_spaces(stacking: dict):
    for building in stacking.get("buildings", []):
        for floor in building.get("floors", []):
            for space in floor.get("spaces", []):
                yield building, space


def units_missing_tenancy_in_csv(stacking: dict, rows: list) -> set:
    """unitIds where the stacking JSON has a tenant but the CSV row does not.

    Without an override these units silently regenerate as Vacant, dropping a
    live lease from the stack."""
    csv_tenant_by_unit = {
        str(_int(r["Unit Id"])): _blank(r.get("Current Tenant Id")) for r in rows
    }
    missing = set()
    for _, space in _iter_spaces(stacking):
        if space.get("tenantID") is None:
            continue
        if not csv_tenant_by_unit.get(str(space["unitId"])):
            missing.add(str(space["unitId"]))
    return missing


def build_overrides(stacking: dict, tenancy_unit_ids: set = None) -> dict:
    """Extract the hand-maintained fields out of a known-good stacking JSON.

    Every building and unit is emitted, including those whose override values
    are null -- the file is a complete manifest of the override surface, so a
    blank entry is an explicit "nothing to preserve here yet" slot rather than
    a silently missing key."""
    tenancy_unit_ids = tenancy_unit_ids or set()
    buildings = {}

    for building in stacking.get("buildings", []):
        units = {}
        for floor in building.get("floors", []):
            for space in floor.get("spaces", []):
                unit_key = str(space["unitId"])
                unit = {field: space.get(field) for field in UNIT_OVERRIDE_FIELDS}
                if unit_key in tenancy_unit_ids:
                    unit["tenancy"] = {f: space.get(f) for f in TENANCY_OVERRIDE_FIELDS}
                units[unit_key] = unit

        entry = {field: building.get(field) for field in BUILDING_OVERRIDE_FIELDS}
        entry["units"] = units
        buildings[str(building["buildingId"])] = entry

    return {"buildings": buildings}


# -------------------------------------------------------------- stacking build


def _build_space(row: dict, unit_override: dict) -> dict:
    tenancy = unit_override.get("tenancy")

    if tenancy:
        tenant = tenancy["tenant"]
        tenant_id = tenancy["tenantID"]
        amendment_id = tenancy["currentAmendmentId"]
        amendment_type = tenancy["currentAmendmentType"]
        lease_from = tenancy["currentAmendmentLeaseFrom"]
        lease_to = tenancy["currentAmendmentLeaseTo"]
        lease_expiry = tenancy["leaseExpiry"]
    else:
        tenant = _blank(row["Current Tenant Name"]) or VACANT
        tenant_id = _int(row["Current Tenant Id"])
        amendment_id = _int(row["Current Amendment Id"])
        amendment_type = _blank(row["Current Amendment Type"])
        lease_from = _datetime(row["Current Amendment Lease From"])
        lease_to = _datetime(row["Current Amendment Lease To"])
        lease_expiry = _datetime(row["Current Occupancy Expiry"])

    space = {
        "unitId": _int(row["Unit Id"]),
        "unit": _blank(row["Code"]),
        "tenantID": tenant_id,
        "tenant": tenant,
        "tenantSF": _number(row["Area - Default"]),
        "type": unit_override.get("type"),
        "leaseExpiry": lease_expiry,
        "typeLinkxcl": unit_override.get("typeLinkxcl"),
        "bomaMeasurementAsPerLease": unit_override.get("bomaMeasurementAsPerLease"),
        "bomaArea": _area(row["Area - BOMA"]),
        "parkingArea": _area(row["Area - Parking Stall"]),
        "storageArea": _area(row["Area - Storage Area"]),
        "signageArea": _area(row["Area - Signage"]),
        "basementArea": _area(row["Area - Basement Area"]),
        "yardArea": _area(row["Area - Yard Area"]),
        "monthlyRent": _float(row["Monthly Rent"]),
        "monthlyRentPerArea": _float(row["Monthly Rent per Area - Default"]),
        "currentAmendmentId": amendment_id,
        "currentAmendmentLeaseFrom": lease_from,
        "currentAmendmentLeaseTo": lease_to,
    }
    # Key is omitted entirely on units with no current amendment -- the source
    # JSON is shaped that way and downstream code tests for its presence.
    if amendment_id is not None:
        space["currentAmendmentType"] = amendment_type
    return space


def build_stacking(rows: list, overrides: dict) -> dict:
    override_buildings = overrides.get("buildings", {})
    grouped = {}

    for row in rows:
        building_id = _building_id(row["Property Code"])
        if building_id is None:
            raise InputValidationError(
                f"Could not derive a building id from Property Code {row['Property Code']!r}"
            )
        grouped.setdefault(building_id, {}).setdefault(_floor_code(row["Floor Code"]), []).append(row)

    buildings = []
    for building_id in sorted(grouped):
        building_override = override_buildings.get(str(building_id), {})
        unit_overrides = building_override.get("units", {})

        floors = []
        for floor_code in sorted(grouped[building_id], reverse=True):
            spaces = [
                _build_space(row, unit_overrides.get(str(_int(row["Unit Id"])), {}))
                for row in grouped[building_id][floor_code]
            ]
            # Left as a plain sum: int + int stays an int, and a single
            # fractional tenantSF makes the total a float -- which is exactly
            # how the source renders it (e.g. 9770.0, not 9770).
            floors.append(
                {
                    "floor": floor_code,
                    "floorSF": sum(s["tenantSF"] for s in spaces),
                    "spaces": spaces,
                }
            )

        buildings.append(
            {
                "buildingId": building_id,
                "propertyId": building_override.get("propertyId"),
                "floors": floors,
                "acquiredYear": building_override.get("acquiredYear"),
                "acquisitionCost": building_override.get("acquisitionCost"),
            }
        )

    return {"buildings": buildings}


def missing_override_report(stacking: dict, overrides: dict) -> list:
    """Buildings/units present in the CSV but absent from the overrides file --
    these render with null reference data until an override is added."""
    override_buildings = overrides.get("buildings", {})
    problems = []
    for building in stacking["buildings"]:
        key = str(building["buildingId"])
        if key not in override_buildings:
            problems.append(f"building {key} has no overrides entry")
            continue
        units = override_buildings[key].get("units", {})
        for floor in building["floors"]:
            for space in floor["spaces"]:
                if str(space["unitId"]) not in units:
                    problems.append(f"building {key} unit {space['unitId']} has no overrides entry")
    return problems


# ---------------------------------------------------------------------- output


def _write_json(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ------------------------------------------------------------------- commands


def cmd_parse(args) -> int:
    csv_path, overrides_path, out_path = Path(args.csv_file), Path(args.overrides_file), Path(args.stacking_file)

    rows = load_stacking_csv(csv_path)
    overrides = load_overrides(overrides_path)
    stacking = build_stacking(rows, overrides)

    for problem in missing_override_report(stacking, overrides):
        print(f"WARNING: {problem}", file=sys.stderr)

    _write_json(stacking, out_path)

    units = sum(len(f["spaces"]) for b in stacking["buildings"] for f in b["floors"])
    floors = sum(len(b["floors"]) for b in stacking["buildings"])
    print(f"Wrote {out_path}: {len(stacking['buildings'])} buildings, {floors} floors, {units} units.")
    return 0


def cmd_extract_overrides(args) -> int:
    stacking_path, out_path, csv_path = Path(args.stacking_file), Path(args.overrides_file), Path(args.csv_file)

    with open(stacking_path) as f:
        stacking = json.load(f)

    if csv_path.exists():
        tenancy_unit_ids = units_missing_tenancy_in_csv(stacking, load_stacking_csv(csv_path))
    else:
        tenancy_unit_ids = set()
        print(f"WARNING: {csv_path} not found -- no tenancy overrides detected.", file=sys.stderr)

    overrides = build_overrides(stacking, tenancy_unit_ids)
    _write_json(overrides, out_path)

    units = sum(len(b["units"]) for b in overrides["buildings"].values())
    typed = sum(1 for b in overrides["buildings"].values() for u in b["units"].values() if u.get("type"))
    print(
        f"Wrote {out_path}: {len(overrides['buildings'])} buildings, {units} units "
        f"({typed} with a type, {len(tenancy_unit_ids)} with a tenancy override: "
        f"{sorted(tenancy_unit_ids, key=int)})."
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("parse", help="Build building-stacking.json from the CSV + overrides.")
    p.add_argument("--csv-file", default=str(DEFAULT_CSV_FILE))
    p.add_argument("--overrides-file", default=str(DEFAULT_OVERRIDES_FILE))
    p.add_argument("--stacking-file", default=str(DEFAULT_STACKING_FILE))
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("extract-overrides", help="Extract buildingOverrides.json from a stacking JSON.")
    p.add_argument("--stacking-file", default=str(DEFAULT_STACKING_FILE))
    p.add_argument("--overrides-file", default=str(DEFAULT_OVERRIDES_FILE))
    p.add_argument("--csv-file", default=str(DEFAULT_CSV_FILE))
    p.set_defaults(func=cmd_extract_overrides)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except (InputValidationError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
