#!/usr/bin/env python3
"""Derive src/crossroads/reference/stats19_columns.csv from the fixture headers + rules.

This is the auditable, reproducible provenance of the column manifest. It is a
developer tool — NOT used by the package at runtime and never runs during `build()`
or the test suite (the package reads the committed CSV via read_csv).

Unlike the codebook, the manifest is NOT machine-extractable from DfT's guide: the
guide's `code/format` column mixes real code lists with format strings and the
`-1`-on-numerics sentinel, so it cannot separate `coded` from `numeric`/`text`. The
classification is therefore authored by the explicit, documented rules below, applied
to the exact column headers of the committed real DfT sample CSVs. Every column lands
in exactly one `kind`; the rule sets ARE the audit trail. Nothing here is taken from
the GPL `stats19` package.

`kind` is one of identity / geo / datetime / coded / numeric / text. `dtype` is the
target type for `numeric` (INTEGER/DOUBLE) and `coded` (INTEGER); blank otherwise.

Examples:
    python scripts/build_stats19_column_manifest.py          # regenerate the CSV
    python scripts/build_stats19_column_manifest.py --check   # fail if committed CSV differs

Standard library only; no new dependencies.
"""

import argparse
import csv
import io
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(REPO_ROOT, "src", "crossroads", "reference", "stats19_columns.csv")
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "stats19")
TABLES = ("collision", "vehicle", "casualty")

# --- Classification rules (the audit trail) --------------------------------------
# A column is classified by the FIRST set it belongs to; anything unmatched defaults
# to `coded` (an integer DfT code list). Each set records WHY these columns differ
# from the coded default.

# Join keys / references — not analysed, carried through as-is.
IDENTITY = {
    "accident_index", "accident_year", "accident_reference",
    "vehicle_reference", "casualty_reference",
}
# Coordinates. OSGR eastings/northings and WGS84 lon/lat. Handled by the bespoke
# Stage 02 geometry logic, so dtype is left blank here.
GEO = {
    "location_easting_osgr", "location_northing_osgr", "longitude", "latitude",
}
# Date/time strings, parsed by the bespoke Stage 02 datetime logic.
DATETIME = {"date", "time"}
# Continuous integer quantities with a -1 sentinel and no code list.
NUMERIC_INT = {
    "number_of_vehicles", "number_of_casualties",
    "first_road_number", "second_road_number",
    "age_of_driver", "age_of_casualty", "age_of_vehicle", "engine_capacity_cc",
    "driver_imd_decile", "casualty_imd_decile",
}
# Probabilistic severity-adjustment weights (real-valued), the only DOUBLE columns.
NUMERIC_DOUBLE = {
    "collision_adjusted_severity_serious", "collision_adjusted_severity_slight",
    "casualty_adjusted_severity_serious", "casualty_adjusted_severity_slight",
}
# Free text, opaque identifiers, and STRING-coded fields the integer codebook cannot
# decode: the ONS-code local_authority_* fields (e.g. E06000036) and LSOA codes.
# Carried raw; ONS-code -> name decoding is deferred. NB: local_authority_district
# uses integer DfT codes, so it is NOT here — it stays `coded`.
TEXT = {
    "generic_make_model",
    "lsoa_of_accident_location", "lsoa_of_driver", "lsoa_of_casualty",
    "local_authority_ons_district", "local_authority_highway",
    "local_authority_highway_current",
}


def classify(col):
    """Return (kind, dtype) for one column header, applying the rules in order."""
    if col in IDENTITY:
        return "identity", ""
    if col in GEO:
        return "geo", ""
    if col in DATETIME:
        return "datetime", ""
    if col in NUMERIC_DOUBLE:
        return "numeric", "DOUBLE"
    if col in NUMERIC_INT:
        return "numeric", "INTEGER"
    if col in TEXT:
        return "text", ""
    return "coded", "INTEGER"   # default: an integer DfT code list


def _fixture_header(table):
    """The exact lower-cased column headers of one committed sample CSV."""
    path = os.path.join(FIXTURE_DIR, f"dft-road-casualty-statistics-{table}-2023.csv")
    with open(path, encoding="utf-8") as f:
        return [h.strip().lower() for h in f.readline().strip().split(",")]


def build_text():
    """Return the manifest CSV as a string (deterministic: fixture order, then rules)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["table", "column", "kind", "dtype"])
    for table in TABLES:
        for col in _fixture_header(table):
            kind, dtype = classify(col)
            w.writerow([table, col, kind, dtype])
    return buf.getvalue()


def main(argv=None):
    p = argparse.ArgumentParser(description="Rebuild stats19_columns.csv from fixture headers + rules.")
    p.add_argument("--check", action="store_true",
                   help="fail if the committed CSV differs from a fresh derivation")
    args = p.parse_args(argv)

    fresh = build_text()
    n_rows = fresh.count("\n") - 1
    print(f"column manifest: {n_rows} rows")

    if args.check:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            committed = f.read()
        if fresh != committed:
            print(f"MISMATCH: committed {OUTPUT_PATH} differs from a fresh derivation.")
            return 1
        print("OK: committed CSV matches a fresh derivation from the rules.")
        return 0

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(fresh)
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
