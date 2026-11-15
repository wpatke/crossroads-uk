# STATS19 test fixtures

Trimmed samples for testing only — **not** the full dataset.

- **Publisher:** Department for Transport (DfT)
- **Dataset:** Road Safety Data — Road Casualty Statistics, 2023 tranche
  (Collision, Vehicle, Casualty CSVs)
- **Licence:** Open Government Licence v3.0
- **Source:** https://data.dft.gov.uk/road-accidents-safety-data/dft-road-casualty-statistics-{collision,vehicle,casualty}-2023.csv

## Row counts

- `dft-road-casualty-statistics-collision-2023.csv` — 8 collisions
- `dft-road-casualty-statistics-vehicle-2023.csv` — 17 vehicles
- `dft-road-casualty-statistics-casualty-2023.csv` — 12 casualties

## Geographic alignment (Stage 04 spatial join)

The 8 collisions were re-selected (superseding the plain `LIMIT` originally used, see
"How they were produced" below) so their OSGR points fall **inside** the committed
ONS LAD sample (`tests/fixtures/ons/lad_2025/lad_sample.geojson`). This lets the
Stage 04 end-to-end test observe a real point-in-polygon stamp (`lad_code` populated)
instead of only proving the join logic via synthetic fixtures.

## Naming note

The live 2023 files use the `collision_*` identity naming
(`collision_index`/`collision_year`/`collision_ref_no`), not `accident_*`. DfT publishes
single-year files only for its most recent years (2021–2025 when checked in November
2026), and none of those use the older `accident_*` naming. To keep the committed fixture on the canonical `accident_*`
convention (so the offline test suite exercises that path), the three identity
columns were renamed at fixture-generation time:
`collision_index`→`accident_index`, `collision_year`→`accident_year`,
`collision_ref_no`→`accident_reference`. Every other column is verbatim from the
real DfT files. The `collision_*` naming (as published live) is instead exercised by
a small synthetic test (`test_collision_reference_alias_branch`), independent of
these fixtures.

## How they were produced

1. Downloaded the three real per-year 2023 files from the URLs above.
2. Selected 8 collisions with valid (non-sentinel) coordinates whose OSGR point
   falls inside a polygon in the committed LAD sample (`ST_Contains`), ordered by
   `collision_index`, via DuckDB + the `spatial` extension.
3. Kept only vehicle/casualty rows whose `collision_index` matches one of those 8
   collisions (`SEMI JOIN`), preserving referential integrity.
4. Renamed the three identity columns as described above and wrote each table back
   out as CSV with the original headers otherwise untouched.

## Re-cut from DfT's autumn 2026 re-issue (2026-11-14)

DfT re-issued its 2023 files in autumn 2026 under the same file names. The fixtures
were re-cut from that re-issue (downloaded 31 October 2026), keeping **exactly the
same 8 collisions, 17 vehicles and 12 casualties**. What changed:

- The casualty file gained an `escooter_flag` column (its last column).
- The e-scooter in collision `20231700H1932` is now `vehicle_type` 33 and
  `casualty_type` 33 ("Personal powered transporter" in DfT's 2025 Data Guide).
  The earlier copy coded both as `-1`.
- The severity-adjustment weights (`*_adjusted_severity_serious`/`_slight`) were
  revised by DfT: 6 collisions and 10 casualties.

Nothing else changed. The selection in step 2 still picks the same 8 collisions
from the re-issued files, but the recipe below uses the fixed list instead, and
sorts the rows, so a re-cut always gives the same rows in the same order.

Reproduction recipe (`SRC` = a scratch dir holding the three real downloaded files):

```python
import duckdb
SRC, OUT = "/scratch", "tests/fixtures/stats19"
# The 8 fixture collisions (originally chosen as in step 2 above).
KEEP = ["20231700H1732", "20231700H1822", "20231700H1862", "20231700H1872",
        "20231700H1882", "20231700H1932", "2023170H10013", "2023170H10023"]
c = duckdb.connect()
keys = ", ".join(f"'{k}'" for k in KEEP)
order = {"collision": "collision_index",
         "vehicle": "collision_index, CAST(vehicle_reference AS INTEGER)",
         "casualty": "collision_index, CAST(vehicle_reference AS INTEGER), "
                     "CAST(casualty_reference AS INTEGER)"}
for name, by in order.items():
    c.execute(f"""COPY (
        SELECT collision_index AS accident_index, collision_year AS accident_year,
               collision_ref_no AS accident_reference,
               * EXCLUDE (collision_index, collision_year, collision_ref_no)
        FROM read_csv('{SRC}/dft-road-casualty-statistics-{name}-2023.csv', all_varchar=true)
        WHERE collision_index IN ({keys})
        ORDER BY {by}
    ) TO '{OUT}/dft-road-casualty-statistics-{name}-2023.csv' (HEADER, DELIMITER ',')""")
```

## Combined-file sample (`combined/`)

`combined/dft-road-casualty-statistics-{collision,vehicle,casualty}-1979-latest-published-year.csv`
stand in for DfT's large combined 1979-onwards files in the offline tests of the
older-year fallback (download once, then split into per-year files).

These are **hand-made, not DfT data verbatim.** They copy the 2023 rows above, re-labelled
as 2005 (3 collisions, 8 vehicles, 6 casualties), 2019 (2, 3, 2) and 2020 (3, 6, 4):

- The header is DfT's real current header: the `collision_*` identity names, as in the
  real combined files (the 2023 fixture's `accident_*` rename is undone).
- `collision_index` gets the new year in place of `2023`, `collision_year` is the new year,
  and `date` moves to the same day and month in the new year, with `day_of_week`
  recomputed for it (DfT: 1 = Sunday). Coordinates are unchanged, so the points still fall
  inside the committed LAD sample.
- 2005 rows have `-1` for fields DfT did not collect that year (`driver_imd_decile`,
  `lsoa_of_driver`, `journey_purpose_of_driver`, `casualty_imd_decile`).
- Rows are written with the years **mixed together** (2019, 2005, 2020, 2005, …), as in
  DfT's real file, so the tests prove the split does not assume rows are grouped by year.

Recipe (run from the repository root):

```python
import csv, datetime, os

FIX = "tests/fixtures/stats19"
OUT = os.path.join(FIX, "combined")
os.makedirs(OUT, exist_ok=True)

# Which 2023 collisions become which year. The ORDER of this list is the order rows are
# written, so years are mixed together the way they are in DfT's real combined file.
PLAN = [("20231700H1872", 2019), ("20231700H1732", 2005), ("20231700H1932", 2020),
        ("20231700H1822", 2005), ("20231700H1882", 2019), ("2023170H10013", 2020),
        ("20231700H1862", 2005), ("2023170H10023", 2020)]
# DfT's current files use these names; the 2023 fixture renamed them to accident_*.
RENAME = {"accident_index": "collision_index", "accident_year": "collision_year",
          "accident_reference": "collision_ref_no"}
# Fields DfT did not collect in 2005 (blank or -1 for that year in DfT's real data).
NOT_COLLECTED_2005 = {"driver_imd_decile", "lsoa_of_driver", "journey_purpose_of_driver",
                      "casualty_imd_decile"}

for ftype in ("collision", "vehicle", "casualty"):
    with open(os.path.join(FIX, f"dft-road-casualty-statistics-{ftype}-2023.csv"), newline="") as f:
        rows = list(csv.reader(f))
    header = [RENAME.get(c, c) for c in rows[0]]
    col = {name: i for i, name in enumerate(header)}
    by_index = {}
    for r in rows[1:]:
        by_index.setdefault(r[col["collision_index"]], []).append(r)
    out = [",".join(header)]
    for old_index, year in PLAN:
        for r in by_index[old_index]:
            r = list(r)
            r[col["collision_index"]] = str(year) + old_index[4:]
            r[col["collision_year"]] = str(year)
            if ftype == "collision":
                d = datetime.datetime.strptime(r[col["date"]], "%d/%m/%Y").date().replace(year=year)
                r[col["date"]] = d.strftime("%d/%m/%Y")
                r[col["day_of_week"]] = str(d.isoweekday() % 7 + 1)   # DfT: 1 = Sunday
            if year == 2005:
                for name in NOT_COLLECTED_2005 & set(col):
                    r[col[name]] = "-1"
            out.append(",".join(r))
    name = f"dft-road-casualty-statistics-{ftype}-1979-latest-published-year.csv"
    with open(os.path.join(OUT, name), "w", newline="") as f:
        f.write("\n".join(out) + "\n")
    print(name, len(out) - 1, "rows")
```
