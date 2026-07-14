# Stage 02 — Risk-Metric Showcase Test & Real-Data Verification
> Part of DfT AADF Traffic Counts. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

- Stage 01 is complete: `aadf` builds offline from `tests/fixtures/aadf/`, stamps
  `lad_code`, and the full suite is green. Verify with `python -m pytest -m integration`.
- The STATS19 fixture (`tests/fixtures/stats19/...collision-2023.csv`) contains exactly
  one A689 collision (`first_road_class=3, first_road_number=689`) and one A179 collision
  (`first_road_class=3, first_road_number=179`), both in LAD `E06000001`. Re-verify the
  counts before writing assertions (the fixture may have changed):
  ```bash
  awk -F, 'NR>1 {print $19, $20}' tests/fixtures/stats19/dft-road-casualty-statistics-collision-2023.csv
  ```
- The real national CSV from Stage 01 Step 1 is still at `/tmp/aadf/` (re-download if not).

## Objective

Prove the README's "collisions per million vehicle-km" query end-to-end: an offline
integration test on committed real data, an opt-in live test of the real download, and a
runbook that produces the real M1 output table Stage 03 pastes into the README.

## The query (canonical shape)

Both the README example and the test use this one shape; only the road-class filter and
prefix differ. `first_road_class` codes: `1`=Motorway, `2`=A(M), `3`=A. Road names are
reconstructed as `'M' || first_road_number` (class 1) or `'A' || first_road_number`
(class 3). Class 2 — A(M) roads — is deliberately excluded from the examples: their AADF
`road_name` format (`A1(M)`) needs special-casing that adds noise, not insight.

```sql
-- Collisions per million vehicle-km, per road, per local authority.
-- Denominator: sum over that road's count points in that LAD of
--   (daily vehicles x link length km) = daily vehicle-km, annualised x365.
WITH traffic AS (
    SELECT road_name, lad_code,
           SUM(all_motor_vehicles * link_length_km) AS daily_vehicle_km
    FROM aadf_clean
    WHERE year = 2023 AND lad_code IS NOT NULL
    GROUP BY road_name, lad_code
),
crashes AS (
    SELECT 'A' || first_road_number AS road_name, lad_code,
           COUNT(*) AS collisions
    FROM collisions_spatial
    WHERE first_road_class = 3 AND first_road_number > 0 AND lad_code IS NOT NULL
    GROUP BY 1, 2
)
SELECT t.road_name, t.lad_code, c.collisions, t.daily_vehicle_km,
       c.collisions / (t.daily_vehicle_km * 365 / 1e6) AS collisions_per_million_vehicle_km
FROM traffic t
JOIN crashes c USING (road_name, lad_code)
ORDER BY collisions_per_million_vehicle_km DESC;
```

Before writing the test, confirm the collision silver column names and types with
`DESCRIBE collisions` on a fixture-built database (`first_road_class` /
`first_road_number` are expected as typed INTEGER columns per the column manifest,
`src/crossroads/reference/stats19_columns.csv` lines 20–21). Adapt the SQL if the real
names differ, and keep the README (Stage 03) and the test using the SAME shape.

## Implementation Steps

### Step 1 — Offline showcase integration test (PRIMARY)

Append to `tests/test_aadf.py`:

```python
@pytest.mark.integration
def test_risk_metric_query_end_to_end(tmp_path):
    """The README's showcase query — collisions per million vehicle-km joined on
    (road name x LAD) — runs against a real offline build of stats19 + aadf and
    returns the figures the committed real fixtures imply."""
```
1. Seed a cache with `_seed_full_cache` (imported from `tests.test_console`, as
   `tests/test_schema_doc.py` already does).
2. `client = init_engine(str(tmp_path/"risk.duckdb"), cache_dir=cache)` then
   `client.build(datasets=["stats19", "aadf"], years=[2023], boundary_mode="snapshot")`.
3. Run the canonical query above (A-road variant, year 2023).
4. Assert, deriving expected numbers from the fixtures rather than hardcoding blindly:
   - Exactly the roads present in BOTH fixtures appear (expected: `A689` and `A179`,
     each with `lad_code='E06000001'`) — no other rows.
   - `collisions` per road equals the count of matching fixture collision rows
     (expected 1 each; compute in the test from the fixture CSV so a fixture edit
     updates the expectation automatically, or hardcode with a comment naming the
     fixture rows — either is acceptable, prefer computing).
   - `daily_vehicle_km` equals `SUM(all_motor_vehicles * link_length_km)` computed
     directly from the AADF fixture rows for that road/year (read the fixture CSV in
     the test with `csv`/DuckDB and compare to ~1e-6 relative tolerance).
   - `collisions_per_million_vehicle_km` is positive and finite.
5. Also assert the join is honestly scoped: a query for motorways
   (`first_road_class = 1`) over this fixture DB returns zero rows (the fixture has no
   motorway collisions — this pins the fact that the query never invents matches).
6. `client.close()` in a `finally`.

### Step 2 — Opt-in live download test

Append to `tests/test_aadf.py`, following the `live` marker convention in
`pyproject.toml` (`CROSSROADS_RUN_LIVE=1` + `-m live`; see how
`tests/test_weather.py` gates its live test and copy that skip mechanism):

```python
@pytest.mark.live
def test_live_national_download_shape(tmp_path):
    """Downloads the real national AADF zip (~40 MB) and sanity-checks it. Run
    deliberately before a release: CROSSROADS_RUN_LIVE=1 pytest -m live -k aadf."""
```
1. `AadfTransformer().extract(str(tmp_path), years=[2023])` — real download + unzip.
2. Assert the canonical CSV exists in the cache dir.
3. Via DuckDB `read_csv(..., all_varchar=true)`: row count `>= 500_000` (600,551 at
   plan time; assert a floor, not equality); every column named in the transformer's
   silver SELECT is present in the header; `count(DISTINCT year)` `>= 20`.
4. Do NOT run a full national `transform_and_load` here — shape-checking the artifact is
   the goal; the full build is the Step 3 runbook (and would slow the marker suite).

### Step 3 — Real-build runbook (produces the README's output table)

This is executed by hand once (it needs the network and a few minutes); record it as a
comment block at the top of `tests/test_aadf.py`'s live test or in the fixture README —
wherever you put it, Stage 03 needs its OUTPUT.

1. Build a real database (uses the national CSV already in `/tmp/aadf` if you point
   `cache_dir` there; otherwise it downloads):
   ```python
   import crossroads as cr
   client = cr.init_engine("/tmp/aadf/showcase.db", cache_dir="/tmp/aadf")
   client.build(datasets=["stats19", "aadf"], years=[2023], boundary_mode="snapshot")
   ```
   Note: STATS19 2023 downloads from DfT on first run — expect a few minutes total.
2. Run the canonical query in its **motorway** form (`first_road_class = 1`,
   `road_name = 'M' || first_road_number`, `WHERE t.road_name = 'M1'` — or unfiltered
   top-10 across all motorways, whichever reads better) against `/tmp/aadf/showcase.db`.
3. Save the top ~8 result rows verbatim (road, LAD code, collisions, daily vehicle-km,
   risk metric) to `docs/plans/012_aadf_traffic/showcase-output.txt` for Stage 03.
   Sanity-check before accepting: collisions per road-LAD should be small integers,
   `daily_vehicle_km` for an M1 LAD stretch should be order 1e5–1e7, and the metric
   should be well under 1.0. If anything looks absurd, debug BEFORE it goes near the
   README — a wrong number in the launch README is worse than no number.

## Testing & Verification

```bash
python -m pytest                                        # fast suite green
python -m pytest -m integration                         # includes the new showcase test
CROSSROADS_RUN_LIVE=1 python -m pytest -m live -k aadf  # real download (deliberate)
```
Ship-readiness checklist:
- [ ] Showcase test passes offline (no network — the seeded cache covers everything).
- [ ] Live test passed at least once on this machine, today.
- [ ] `showcase-output.txt` exists with believable real M1 numbers.

## End State / Handoff

- `tests/test_aadf.py` contains the showcase integration test (offline, green) and the
  live download test (green when run deliberately).
- The canonical query text in the test matches, modulo the road-class filter, what
  Stage 03 will put in the README.
- `docs/plans/012_aadf_traffic/showcase-output.txt` holds real, sanity-checked M1 output
  for the README.

## Failure Modes & Rollback

- **Showcase test finds zero joined rows** → the fixtures drifted apart. Check (a) the
  AADF fixture's `road_name`/year values, (b) the collision fixture's road numbers,
  (c) both sides' `lad_code` stamps. Fix the fixture, not the query.
- **DESCRIBE reveals different collision column names** → adapt the SQL in test + README
  together; they must stay identical in shape.
- **Live download slow/unavailable at test time** → it is opt-in; release verification
  simply requires one green run, retry later.
- **Rollback:** delete the two tests and `showcase-output.txt`; Stage 01's state is
  untouched.
