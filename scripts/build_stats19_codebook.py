#!/usr/bin/env python3
"""Derive src/crossroads/reference/stats19_codebook.csv from DfT's published guide.

This is the auditable, reproducible provenance of the codebook. It is a developer
tool — it is NOT used by the package at runtime and never runs during `build()` or
the test suite (the package reads the committed CSV via read_csv).

Source of truth: the DfT "Road Safety Open Dataset Data Guide" (2024 edition,
published 25 September 2025), Open Government Licence v3.0 — the same publisher and
licence as the collision/vehicle/casualty CSVs. Nothing here derives from the GPL
`stats19` R package; the codebook is built solely from DfT's own guide.

The guide is a single-sheet .xlsx (sheet `2024_code_list`) with columns
`table, field name, code/format, label, note`. We keep only rows whose code is an
integer (dropping ONS string codes like E06000036, format strings like (DD/MM/YYYY),
and range descriptions like "1 to 9999"), and flag `is_missing` for the numeric `-1`
sentinel plus the guide's "not recorded" labels.

Examples:
    python scripts/build_stats19_codebook.py            # download guide, regenerate CSV
    python scripts/build_stats19_codebook.py --guide /path/to/guide.xlsx   # use a local copy
    python scripts/build_stats19_codebook.py --check    # verify committed CSV matches (CI-friendly)

Requires `duckdb` (already a package dependency) and its `excel` extension, which the
script installs on demand. Downloading uses only the standard library.
"""

import argparse
import os
import sys
import tempfile
import urllib.request

import duckdb

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(REPO_ROOT, "src", "crossroads", "reference", "stats19_codebook.csv")

# Pinned source. Re-issued annually (~late September); bump the URL + SHEET on a refresh.
GUIDE_URL = ("https://data.dft.gov.uk/road-accidents-safety-data/"
             "dft-road-casualty-statistics-road-safety-open-dataset-data-guide-2024.xlsx")
GUIDE_SHEET = "2024_code_list"

# A code is treated as "not recorded" (is_missing = TRUE) when it is the numeric
# sentinel -1, OR its label is one of these "not recorded" markers that occur in the
# 2024 guide. Self-reported unknowns ("unknown (self reported)", codes 9/99) are NOT
# in this set — they are a real, recorded category and are kept. See the reference
# README for the full rationale and judgement calls.
MISSING_LABELS = (
    "data missing or out of range",
    "unknown",
    "undefined",
    "not known",
    "code deprecated",
)


def _download_guide(dest):
    """Fetch the pinned DfT guide to `dest` (stdlib only; sets a UA so the CDN serves it)."""
    req = urllib.request.Request(GUIDE_URL, headers={"User-Agent": "crossroads-uk-build"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def build_csv(guide_path, out_path):
    """Read the guide xlsx and write the codebook CSV. Returns (rows, variables)."""
    con = duckdb.connect()
    con.execute("INSTALL excel; LOAD excel;")   # xlsx reader (authoring only)
    con.execute(
        "CREATE TABLE g AS "
        "SELECT lower(trim(\"field name\")) AS variable, "
        "       trim(\"code/format\")       AS code, "
        "       trim(\"label\")             AS label "
        f"FROM read_xlsx('{guide_path}', sheet='{GUIDE_SHEET}', all_varchar=true, header=true) "
        "WHERE \"field name\" IS NOT NULL AND \"code/format\" IS NOT NULL AND \"label\" IS NOT NULL")
    # is_missing = numeric -1 sentinel OR a "not recorded" label (exact, case-insensitive).
    miss = " OR ".join(f"lower(trim(label)) = '{m}'" for m in MISSING_LABELS)
    con.execute(
        f"COPY ("
        f"  SELECT DISTINCT variable, code, label, (code = '-1' OR {miss}) AS is_missing "
        f"  FROM g "
        f"  WHERE TRY_CAST(code AS INTEGER) IS NOT NULL "   # integer codes only
        f"  ORDER BY variable, TRY_CAST(code AS INTEGER)"
        f") TO '{out_path}' (HEADER, DELIMITER ',')")
    n = con.execute(f"SELECT count(*), count(DISTINCT variable) "
                    f"FROM read_csv('{out_path}', header=true)").fetchone()
    return n[0], n[1]


def main(argv=None):
    p = argparse.ArgumentParser(description="Rebuild stats19_codebook.csv from DfT's guide.")
    p.add_argument("--guide", metavar="PATH",
                   help="use a local copy of the guide .xlsx instead of downloading it")
    p.add_argument("--check", action="store_true",
                   help="regenerate to a temp file and fail if it differs from the committed CSV")
    args = p.parse_args(argv)

    # Resolve the guide: a local path if given, else download the pinned edition.
    tmp_guide = None
    if args.guide:
        guide_path = args.guide
    else:
        tmp_guide = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False).name
        print(f"Downloading guide: {GUIDE_URL}")
        _download_guide(tmp_guide)
        guide_path = tmp_guide

    try:
        target = (tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
                  if args.check else OUTPUT_PATH)
        rows, variables = build_csv(guide_path, target)
        print(f"codebook: {rows} rows, {variables} variables")

        if args.check:
            with open(target, encoding="utf-8") as f:
                fresh = f.read()
            os.unlink(target)
            with open(OUTPUT_PATH, encoding="utf-8") as f:
                committed = f.read()
            if fresh != committed:
                print(f"MISMATCH: committed {OUTPUT_PATH} differs from a fresh derivation.")
                return 1
            print(f"OK: committed CSV matches a fresh derivation from the guide.")
        else:
            print(f"Wrote {OUTPUT_PATH}")
        return 0
    finally:
        if tmp_guide and os.path.exists(tmp_guide):
            os.unlink(tmp_guide)


if __name__ == "__main__":
    sys.exit(main())
