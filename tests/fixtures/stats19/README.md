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
(`collision_index`/`collision_year`/`collision_ref_no`), not `accident_*`. Per-year
files are only published individually for 2020 onward; none of those use the older
`accident_*` naming. To keep the committed fixture on the canonical `accident_*`
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
