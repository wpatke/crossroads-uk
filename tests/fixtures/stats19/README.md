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

Reproduction recipe (`SRC` = a scratch dir holding the three real downloaded files):

```python
import duckdb
c = duckdb.connect(); c.execute("INSTALL spatial"); c.execute("LOAD spatial")
SRC, OUT = "/scratch", "tests/fixtures/stats19"
LAD_FIXTURE = "tests/fixtures/ons/lad_2025/lad_sample.geojson"   # newest committed LAD vintage
c.execute(f"CREATE TABLE lad AS SELECT geom FROM ST_Read('{LAD_FIXTURE}')")
c.execute(f"CREATE TABLE col AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-collision-2023.csv', all_varchar=true)")
c.execute(f"CREATE TABLE veh AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-vehicle-2023.csv', all_varchar=true)")
c.execute(f"CREATE TABLE cas AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-casualty-2023.csv', all_varchar=true)")
c.execute("""CREATE TABLE cols AS
  SELECT col.* FROM col
  WHERE col.location_easting_osgr NOT IN ('-1','0','')
    AND col.location_northing_osgr NOT IN ('-1','0','')
    AND EXISTS (SELECT 1 FROM lad
                WHERE ST_Contains(lad.geom,
                      ST_Point(CAST(col.location_easting_osgr AS DOUBLE),
                               CAST(col.location_northing_osgr AS DOUBLE))))
  ORDER BY col.collision_index LIMIT 8""")
c.execute("CREATE TABLE vehs AS SELECT v.* FROM veh v SEMI JOIN cols USING (collision_index)")
c.execute("CREATE TABLE cass AS SELECT k.* FROM cas k SEMI JOIN cols USING (collision_index)")
for src, name in (("cols", "collision"), ("vehs", "vehicle"), ("cass", "casualty")):
    c.execute(f"""COPY (
        SELECT collision_index AS accident_index, collision_year AS accident_year,
               collision_ref_no AS accident_reference,
               * EXCLUDE (collision_index, collision_year, collision_ref_no)
        FROM {src}
    ) TO '{OUT}/dft-road-casualty-statistics-{name}-2023.csv' (HEADER, DELIMITER ',')""")
```
