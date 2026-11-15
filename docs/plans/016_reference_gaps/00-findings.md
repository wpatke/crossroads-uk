# 016 — STATS19 reference-data gaps: findings

Status: **implemented** (approved by the maintainer as recommended). The fix is
described in [01-plan.md](01-plan.md).

Three gaps were reported in the STATS19 reference data (`src/crossroads/reference/`). All
three are real. All three have the same root cause: **DfT re-issued its files and published a
new edition of its Data Guide (the 2025 edition) in autumn 2026, and our reference files are
still built from the older 2024 edition and an older copy of the 2023 data.**

## What was checked

- Two copies of DfT's 2023 per-year files on this machine: one downloaded on 15 July 2026 and
  one downloaded on 31 October 2026. Same row counts (104,258 collisions, 189,815 vehicles,
  132,977 casualties) but some values and one column differ.
- The **2024 edition** of the Road Safety Open Dataset Data Guide (the one the codebook is
  pinned to), via its gov.uk mirror.
- The **2025 edition** of the same guide, linked from DfT's road-safety open-data page:
  `https://assets.publishing.service.gov.uk/media/6ab2a71d997a4b2950cced58/dft-road-casualty-statistics-road-safety-open-dataset-data-guide-2025.xlsx`
  (SHA-256 `51b08606ef3dad1ae5d4c8726241b022fc8e4bbdc910ef0e5cc2fb92b054fc12`, 85,112 bytes).
  Like the 2024 edition, its code-list sheet is called `2024_code_list` (the name refers to
  the 2024 STATS19 *specification*, not the guide's edition). It also has a second sheet,
  `2011_to_2024_conversion`, that maps each old code to its new code.

Only DfT material (Open Government Licence v3.0) was used.

## Gap 1 — casualty `escooter_flag` never reaches `casualties`

**Confirmed.**

- The 31 October 2026 copy of the 2023 casualty file has `escooter_flag` as its last column.
  The 15 July copy does not. So DfT **added the column when it re-issued the files**, after our
  fixture was made.
- The 2025 guide lists it for the casualty table: `0` = "Casualty was not using an e-scooter",
  `1` = "Casualty was using an e-scooter" (note: "field introduced in 2025"). The 2024 guide
  lists `escooter_flag` for vehicles only.
- In 2023, 1,117 casualties have `escooter_flag = 1`.
- `scripts/build_stats19_column_manifest.py` builds `stats19_columns.csv` from the header row
  of the committed fixture `tests/fixtures/stats19/dft-road-casualty-statistics-casualty-2023.csv`.
  That fixture was cut from the July copy, so it has no `escooter_flag`, so the manifest has no
  `casualty,escooter_flag` row. The column lands in bronze (`stats19_casualty_raw`) but the
  silver clean only carries manifest columns, so it is dropped from `casualties`.

## Gap 2 — `junction_detail` code `19`

**Confirmed, and the 2025 guide defines it.**

- 2023 has 6,183 collisions with `junction_detail = 19` (both the July and October copies).
- The 2024 guide has no `19` for `junction_detail`, so the codebook has none.
- The 2025 guide adds: `junction_detail`, `19`, **"Other junction"**. Its conversion sheet
  confirms it: old `junction_detail_historic` `9` "Other junction" → new `junction_detail` `19`
  "Other junction".
- The data agrees: every one of the 6,183 rows with `junction_detail = 19` has
  `junction_detail_historic = 9`.

## Gap 3 — `vehicle_type` and `casualty_type` code `33`

**Confirmed. The 2025 guide defines both, but has a typo for the casualty one.**

- In the July copy, the 1,299 e-scooter vehicles had `vehicle_type = -1` (missing). In the
  October copy, the same vehicles have `vehicle_type = 33`. Likewise the 1,117 e-scooter
  casualties have `casualty_type = 33`. In 2023, **every** `33` is an e-scooter
  (`escooter_flag = 1`) and every e-scooter is a `33`.
- 2025 guide, code-list sheet:
  - `vehicle_type`, `33`, **"Personal powered transporter"** ("category introduced in 2024
    specification").
  - `casualty_type` has **no `33`**. Instead it lists code `23` twice: once as "Electric
    motorcycle rider or passenger" (the real 23, also in the 2024 guide) and once as
    "Personal Powered Transporter" ("introduced in 2024 specification").
- 2025 guide, conversion sheet (DfT's own words):
  - `vehicle_type` old `90` "Other vehicle + free text field" → new `33` "Personal Powered
    Transporter".
  - `casualty_type` old `90` "Other vehicle + free text field" → new **`33`** "Personal Powered
    Transporter rider".

So the second casualty `23` row is a typo for `33`. We do not have to guess this: the same
guide says so on its other sheet, and the data agrees. The plan fixes it with one small,
clearly labelled correction in the codebook script (see [01-plan.md](01-plan.md)).

## A problem the fix has to handle: one name, two tables

The codebook is keyed on `(variable, code)` with no table. Until now no integer-coded variable
appeared in more than one table, so that was fine. In the 2025 guide **`escooter_flag` is in
both `vehicle` and `casualty`, with different labels** (e.g. "Vehicle was an e-scooter" vs
"Casualty was using an e-scooter"). It is the only such variable.

If we simply re-ran the codebook script on the 2025 guide, the codebook would get two rows
for `escooter_flag`/`0` and two for `escooter_flag`/`1`, plus two for `casualty_type`/`23`.
The `*_labelled` views would then return duplicate rows, and the existing uniqueness test
would fail. So the plan adds the table to the codebook.

## Also found (not part of this fix)

- **The pinned guide URL is dead.** `https://data.dft.gov.uk/.../data-guide-2024.xlsx` now
  returns 404 (so does a `...-2025.xlsx` guess at the same address). So
  `python scripts/build_stats19_codebook.py --check` fails today unless `--guide` is passed.
  With the gov.uk mirror of the 2024 guide, `--check` passes: the committed codebook still
  matches the 2024 edition exactly. The plan moves the pin to the gov.uk address of the 2025
  edition, which fixes this.
- **Collision enhanced severity does have a code list.** The reference README and
  `docs/schema.md` say `enhanced_severity_collision` has "no codebook coverage" because its
  list is absent from the guide. In fact both guide editions list it, under the name
  `enhanced_collision_severity` (codes 1, 3, 5, 6, 7 and -1), while the data column is called
  `enhanced_severity_collision`. Separate follow-up.
- **DfT re-issues files without changing their names.** Besides the new column and the
  `-1` → `33` move, the October copy has revised severity-adjustment weights
  (`*_adjusted_severity_*`). A build from an old cache and a build from a fresh download can
  differ. Worth thinking about separately (for example, recording a file hash at ingest).
