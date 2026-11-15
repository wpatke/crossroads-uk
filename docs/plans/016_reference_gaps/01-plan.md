# 016 — STATS19 reference-data gaps: plan

Read [00-findings.md](00-findings.md) first. In one line: refresh the codebook to DfT's
2025 Data Guide, give the codebook a `table` column so `escooter_flag` can have different
labels for vehicles and casualties, and refresh the 2023 test fixtures from DfT's current
2023 files so the manifest picks up casualty `escooter_flag`.

## Decisions for the maintainer

Each has a recommendation. Nothing is built until these are agreed.

1. **Move the codebook to the 2025 guide.** *Recommended.* It is DfT's current edition and
   the only source that defines `junction_detail` 19 and code 33. The old pinned URL is
   dead anyway. New pin: the gov.uk `assets.publishing.service.gov.uk` address in the
   findings. The sheet name stays `2024_code_list`.
2. **Add a `table` column to the codebook.** *Recommended.* The guide lists codes per
   table, and `escooter_flag` now has different labels in two tables. Keying on
   `(table, variable, code)` follows the guide exactly. It is an additive change (a new
   column), but anyone who joins `codebook` on `variable` alone will get two rows for
   `escooter_flag` codes, so the CHANGELOG must say so.
   Rejected alternative: keep one label per variable. One table's rows would then show the
   other table's wording (for example a casualty labelled "Vehicle was an e-scooter").
3. **Correct the guide's `casualty_type` typo in the script.** *Recommended.* Add one
   clearly commented correction: the code-list row `casualty`, `casualty_type`, `23`,
   "Personal Powered Transporter" is read as code `33`, citing the guide's own conversion
   sheet. The label stays exactly as the code-list row has it. The script stops with an
   error if that row is no longer in the guide, so a corrected future edition is noticed
   and the correction can be removed.
   Rejected alternative: leave casualty `33` unlabelled and document it. The guide does
   define it (on its other sheet), so leaving it out would throw away DfT's own answer.
4. **Refresh the three 2023 fixture files from DfT's current 2023 files.** *Recommended.*
   Keep exactly the same 8 collisions, 17 vehicles and 12 casualties, but take every value
   and the header from the current DfT files. This adds casualty `escooter_flag`, and
   brings in one real `vehicle_type` 33 and one real `casualty_type` 33 (the fixture
   already contains an e-scooter collision, `20231700H1932`). It also updates the
   severity-adjustment weights (6 collisions, 10 casualties), which no test checks by value.
   Rejected alternative: append only the casualty `escooter_flag` column. Smaller, but the
   fixture would mix two DfT releases and would not test code 33.
5. **Version.** New column in `casualties` and in `codebook`: `SCHEMA_VERSION` 4 → 5 and a
   MINOR release (1.1.0) under the project's rules. Plan 015 may also change the version; whichever
   lands second takes the next number.

## Changes

### A. `scripts/build_stats19_codebook.py`

- `GUIDE_URL` → the 2025 gov.uk address. Update the docstring (2025 edition, published
  September 2026, still sheet `2024_code_list`).
- Read the guide's `table` column too: `lower(trim("table")) AS tbl`. Write it to the CSV
  as the first column, named `table` (same name as in `stats19_columns.csv`).
- `SELECT DISTINCT` and `ORDER BY` include the table: order by `table, variable, code`.
- Add a small `GUIDE_CORRECTIONS` list near `MISSING_LABELS`, one entry, with a comment
  in plain words:

  ```python
  # Known typos in the guide's code-list sheet, fixed using the same guide's
  # "2011_to_2024_conversion" sheet. Each entry is
  # (table, variable, code in the guide, label in the guide, correct code).
  GUIDE_CORRECTIONS = [
      # Listed as 23 (a second 23, after "Electric motorcycle rider or passenger").
      # The conversion sheet gives it as 33, matching vehicle_type 33.
      ("casualty", "casualty_type", "23", "Personal Powered Transporter", "33"),
  ]
  ```

  Applied with a plain `UPDATE` on the staging table before the `COPY`. If an entry
  matches no row, stop with a clear error message.
- After building, check the result is unique on `(table, variable, code)`. If not, stop
  with an error that names the duplicates. This would have caught the typo on its own.
- `MISSING_LABELS` is unchanged: the 2025 guide adds no new "not recorded" labels (checked).

Expected result: 1,135 rows (was 1,130), still 71 variables. The 5 new rows:
`junction_detail` 19, `vehicle_type` 33, `casualty_type` 33, and casualty `escooter_flag`
0 and 1. Every existing row is kept unchanged apart from the new `table` value.

### B. `src/crossroads/transformers/stats19.py`

All small, and all just "also match on table":

- `_load_codebook`: load the new column as `tbl` (as `_load_column_manifest` already does
  for the manifest). Update the docstring.
- `_clean_fragment`: take `table_kind` as an extra argument and add
  `AND tbl = '<table_kind>'` to the `is_missing` lookup. `_broad_fragments` already has
  `table_kind` and passes it on.
- `_core_severity_fragments`: no change needed. `collision_severity` and
  `casualty_severity` each exist in one table only. Add `tbl` anyway if the maintainer
  prefers every codebook lookup to look the same.
- `_create_labelled_views`: add `AND {a}.tbl = '<table_kind>'` to each `LEFT JOIN`, and
  `AND tbl = ?` to the "covered" count. Update the docstring: unique on
  `(tbl, variable, code)`.

No change is needed for casualty `escooter_flag` itself: once the manifest lists it,
`_broad_fragments` cleans it, and an older casualty file without the column gets a typed
NULL placeholder like any other missing column.

### C. Fixtures: `tests/fixtures/stats19/`

- Re-cut the three `...-2023.csv` files from DfT's current 2023 files, using the same
  `collision_index` values as today (not the `LIMIT 8` selection, so the rows cannot
  change), with the same `accident_*` rename.
- `README.md`: say the files were re-cut from the October 2026 re-issue, list what changed
  (casualty `escooter_flag` added; e-scooter `vehicle_type`/`casualty_type` now 33 instead
  of -1; revised adjustment weights), and update the recipe to select by the fixed list of
  8 collision indexes.

### D. `scripts/build_stats19_column_manifest.py` and `stats19_columns.csv`

- No rule change: `escooter_flag` is not in any rule set, so it falls to the `coded`
  default, as it already does for vehicles. Re-run the script to regenerate the CSV.
- Expected: 100 rows (was 99), coded 60 (was 59), new row `casualty,escooter_flag,coded,INTEGER`.

### E. Tests: `tests/test_stats19.py`

- Uniqueness check on the codebook → `(tbl, variable, code)`.
- New: `codebook` has a `tbl` column, and `escooter_flag` has rows for both `vehicle` and
  `casualty` with different labels.
- New: `junction_detail` 19, `vehicle_type` 33 and `casualty_type` 33 are in the codebook
  for the right table.
- Manifest test: 99 → 100 rows, coded 59 → 60.
- The existing "every present code in a covered column decodes" test needs no change. With
  the re-cut fixture it now really exercises `vehicle_type`/`casualty_type` 33, so it fails
  if the codebook loses them.
- Add a check that `casualties` has an `escooter_flag` column and the e-scooter casualty in
  the fixture has value 1 (and label "Casualty was using an e-scooter" in
  `casualties_labelled`).
- Update the "covered" lookup in that test to filter on `tbl`.

### F. Documentation

- `src/crossroads/reference/README.md`: 2025 edition, new URL and SHA-256, derivation date,
  the `table` column, the correction and why, new row counts, casualty `escooter_flag`.
- `docs/schema.md`: `codebook` gains `tbl`; `casualties` gains `escooter_flag`.
- `src/crossroads/__init__.py`: `SCHEMA_VERSION = 5`.
- `CHANGELOG.md`, under a new 1.1.0 entry:
  - Added: `casualties.escooter_flag`; `codebook.tbl`.
  - Changed: codebook now built from the 2025 Data Guide; labels for `junction_detail` 19
    "Other junction", `vehicle_type` 33 "Personal powered transporter", `casualty_type` 33
    "Personal Powered Transporter"; the undecoded-code warning for these no longer appears.
  - Note: a query that joins `codebook` on `variable` alone must now also match `tbl`.
  - Database `schema_version` 4 → 5.

## How we know it worked

```bash
python scripts/build_stats19_codebook.py --check
python scripts/build_stats19_column_manifest.py --check
python -m pytest
```

Plus one full build of 2023 from the current DfT files, checking that the
"undecoded codes" warning no longer mentions `junction_detail`, `vehicle_type` or
`casualty_type`, and that `casualties.escooter_flag` has 1,117 ones.

## Overlap with plan 015

Plan 015 makes a "combined" fixture by copying rows from the 2023 fixture and edits
`tests/fixtures/stats19/README.md` and `CHANGELOG.md`. Whichever plan lands second should
re-cut or re-check the combined fixture against the refreshed 2023 fixture (it will need
the casualty `escooter_flag` column) and merge the README and CHANGELOG edits.

## Follow-ups (not in this plan)

- Give `enhanced_severity_collision` labels from the guide's `enhanced_collision_severity`
  list (the names differ), and correct the README and schema doc that say no list exists.
- Detect when DfT re-issues a file under the same name (for example, record a file hash in
  `source_ingest_log`).
