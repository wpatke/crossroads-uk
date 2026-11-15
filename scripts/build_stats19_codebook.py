#!/usr/bin/env python3
"""Derive src/crossroads/reference/stats19_codebook.csv from DfT's published guide.

This is the auditable, reproducible provenance of the codebook. It is a developer
tool — it is NOT used by the package at runtime and never runs during `build()` or
the test suite (the package reads the committed CSV via read_csv).

Source of truth: the DfT "Road Safety Open Dataset Data Guide" (2025 edition, file
dated 22 September 2026), Open Government Licence v3.0 — the same publisher and
licence as the collision/vehicle/casualty CSVs. Nothing here derives from the GPL
`stats19` R package; the codebook is built solely from DfT's own guide.

The guide is an .xlsx whose code list is on sheet `2024_code_list` (named after the
2024 STATS19 specification, not the guide's edition), with columns
`table, field name, code/format, label, note`. We keep only rows whose code is an
integer (dropping ONS string codes like E06000036, format strings like (DD/MM/YYYY),
and range descriptions like "1 to 9999"), and flag `is_missing` for the numeric `-1`
sentinel plus the guide's "not recorded" labels.

Each codebook row keeps the guide's `table` (collision / vehicle / casualty), because
the same field name can appear in two tables with different labels (escooter_flag is
in both vehicle and casualty). The codebook is unique on (table, variable, code); the
script stops with an error if it is not.

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
# (The older data.dft.gov.uk address for the guide now returns 404, so we pin the
# gov.uk copy linked from DfT's "Road safety open data" page.)
GUIDE_URL = ("https://assets.publishing.service.gov.uk/media/6ab2a71d997a4b2950cced58/"
             "dft-road-casualty-statistics-road-safety-open-dataset-data-guide-2025.xlsx")
GUIDE_SHEET = "2024_code_list"

# A code is treated as "not recorded" (is_missing = TRUE) when it is the numeric
# sentinel -1, OR its label is one of these "not recorded" markers that occur in the
# guide. Self-reported unknowns ("unknown (self reported)", codes 9/99) are NOT
# in this set — they are a real, recorded category and are kept. See the reference
# README for the full rationale and judgement calls.
MISSING_LABELS = (
    "data missing or out of range",
    "unknown",
    "undefined",
    "not known",
    "code deprecated",
)

# Known typos in the guide's code-list sheet. Each is fixed using the SAME guide's
# other sheet, "2011_to_2024_conversion", which maps old codes to new ones — so the
# fix is DfT's own answer, not a guess. Each entry is
# (table, variable, code as printed in the guide, label in the guide, correct code).
# If an entry stops matching (e.g. DfT fixes the typo in a later edition), the script
# stops with an error so the entry can be removed.
GUIDE_CORRECTIONS = [
    # The code list prints this as a SECOND casualty_type 23 (the first 23 is
    # "Electric motorcycle rider or passenger"). The conversion sheet gives it as 33,
    # matching vehicle_type 33 "Personal powered transporter".
    ("casualty", "casualty_type", "23", "Personal Powered Transporter", "33"),
]


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
        "SELECT lower(trim(\"table\"))      AS tbl, "
        "       lower(trim(\"field name\")) AS variable, "
        "       trim(\"code/format\")       AS code, "
        "       trim(\"label\")             AS label "
        f"FROM read_xlsx('{guide_path}', sheet='{GUIDE_SHEET}', all_varchar=true, header=true) "
        "WHERE \"field name\" IS NOT NULL AND \"code/format\" IS NOT NULL AND \"label\" IS NOT NULL")

    # Apply the known-typo corrections (see GUIDE_CORRECTIONS). Values are fixed
    # constants from this script, passed as parameters.
    for tbl, variable, wrong_code, label, right_code in GUIDE_CORRECTIONS:
        where = "tbl = ? AND variable = ? AND code = ? AND label = ?"
        params = [tbl, variable, wrong_code, label]
        if con.execute(f"SELECT count(*) FROM g WHERE {where}", params).fetchone()[0] == 0:
            raise SystemExit(
                f"GUIDE_CORRECTIONS entry no longer matches the guide: {tbl}.{variable} "
                f"code {wrong_code} '{label}'. The guide may have been fixed; check the "
                f"entry and remove it if so.")
        con.execute(f"UPDATE g SET code = ? WHERE {where}", [right_code] + params)

    # is_missing = numeric -1 sentinel OR a "not recorded" label (exact, case-insensitive).
    miss = " OR ".join(f"lower(trim(label)) = '{m}'" for m in MISSING_LABELS)
    con.execute(
        f"CREATE TABLE cb AS "
        f"SELECT DISTINCT tbl AS \"table\", variable, code, label, "
        f"       (code = '-1' OR {miss}) AS is_missing "
        f"FROM g "
        f"WHERE TRY_CAST(code AS INTEGER) IS NOT NULL")   # integer codes only

    # Each (table, variable, code) must have exactly one label, or the labelled views
    # would return duplicate rows. A duplicate means a new typo in the guide: stop and
    # name it, so it can be checked and added to GUIDE_CORRECTIONS.
    dups = con.execute(
        "SELECT \"table\", variable, code, string_agg(label, ' | ') FROM cb "
        "GROUP BY ALL HAVING count(*) > 1 ORDER BY ALL").fetchall()
    if dups:
        lines = "\n".join(f"  {t}.{v} code {c}: {labels}" for t, v, c, labels in dups)
        raise SystemExit(f"The guide gives more than one label for these codes:\n{lines}")

    con.execute(
        f"COPY (SELECT * FROM cb ORDER BY \"table\", variable, TRY_CAST(code AS INTEGER)) "
        f"TO '{out_path}' (HEADER, DELIMITER ',')")
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
