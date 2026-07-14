# Stage 01 — AADF Source Transformer
> Part of DfT AADF Traffic Counts. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

- Repo root is the `crossroads-uk` package; `python -m pytest` and
  `python -m pytest -m integration` are green before you start (verify this first).
- These files exist and are your style/pattern references:
  - `src/crossroads/transformers/bank_holidays.py` — the most recent small transformer
    (atomic download, cache-skip `extract`, bronze/silver factoring, docstring style).
  - `src/crossroads/transformers/spatial.py` — `SourceQuality` + per-row ledger writes +
    R-Tree creation + BNG envelope constants pattern.
  - `src/crossroads/transformers/stats19.py` — `_spatial_stamp` (the point-in-polygon
    `UPDATE` this stage copies, around line 702) and `_table_exists` (around line 683).
  - `tests/fixtures/stats19/dft-road-casualty-statistics-collision-2023.csv` — 8 real
    Hartlepool (LAD `E06000001`) collision rows; two are A-road collisions:
    `first_road_class=3, first_road_number=689` (A689) and `=3, 179` (A179).
- Network access is required ONCE in this stage (Step 1, fixture creation).

## Objective

Add `src/crossroads/transformers/aadf.py` — a fully audited `aadf` source loading the DfT
national AADF-by-count-point dataset into `aadf_raw` (bronze) → `aadf` (silver, LAD/CTYUA
stamped, R-Tree indexed) → `aadf_clean` (gold view) — plus its committed real-data test
fixture, unit/integration tests, and every test-suite ripple fix, leaving the whole suite green.

## Implementation Steps

### Step 1 — Create the real-data fixture (needs network, once)

1. Download the national dataset to a throwaway directory (NOT the repo):
   ```bash
   curl -L -o /tmp/aadf/dft_traffic_counts_aadf.zip --create-dirs \
     "https://storage.googleapis.com/dft-statistics/road-traffic/downloads/data-gov-uk/dft_traffic_counts_aadf.zip"
   cd /tmp/aadf && unzip dft_traffic_counts_aadf.zip && ls -la
   ```
   Note the extracted CSV's name (call it `NATIONAL.csv` below). Record its header:
   ```bash
   head -1 /tmp/aadf/NATIONAL.csv
   ```
   **Expected columns** (verified against a real per-LA AADF file; confirm against the
   national header and adapt the silver SELECT in Step 2 if they differ):
   `count_point_id, year, region_id, region_name, local_authority_id,
   local_authority_name, road_name, road_type, start_junction_road_name,
   end_junction_road_name, easting, northing, latitude, longitude, link_length_km,
   link_length_miles, estimation_method, estimation_method_detailed, pedal_cycles,
   two_wheeled_motor_vehicles, cars_and_taxis, buses_and_coaches, lgvs,
   hgvs_2_rigid_axle, hgvs_3_rigid_axle, hgvs_4_or_more_rigid_axle,
   hgvs_3_or_4_articulated_axle, hgvs_5_articulated_axle, hgvs_6_articulated_axle,
   all_hgvs, all_motor_vehicles`
2. Extract the fixture rows — Hartlepool count points on the A689 and A179 for 2023
   (matching the committed STATS19 fixture's LAD and roads), via the `duckdb` Python
   package already installed for the project:
   ```bash
   python - <<'EOF'
   import duckdb
   con = duckdb.connect()
   con.execute("""
     COPY (
       SELECT * FROM read_csv('/tmp/aadf/NATIONAL.csv', header=true, all_varchar=true)
       WHERE local_authority_name = 'Hartlepool'
         AND road_name IN ('A689', 'A179')
         AND year IN ('2022', '2023')
       ORDER BY road_name, year, count_point_id
     ) TO '/tmp/aadf/aadf_sample.csv' (HEADER, DELIMITER ',')
   """)
   print(con.execute("SELECT count(*) FROM read_csv('/tmp/aadf/aadf_sample.csv', header=true)").fetchone())
   EOF
   ```
   Expect roughly 4–20 rows (a few count points per road per year). If zero rows, check
   the actual `local_authority_name` spelling with
   `SELECT DISTINCT local_authority_name FROM read_csv(...) WHERE local_authority_name ILIKE '%hartlep%'`.
3. Verify the chosen points stamp to the committed sample boundary (generalised polygons
   can clip real points). Using the LAD fixture GeoJSON (find the newest under
   `tests/fixtures/ons/lad_*/lad_sample.geojson`):
   ```bash
   python - <<'EOF'
   import duckdb
   con = duckdb.connect()
   con.execute("INSTALL spatial; LOAD spatial")
   rows = con.execute("""
     SELECT a.count_point_id, a.road_name, bool_or(ST_Contains(b.geom, ST_Point(CAST(a.easting AS INT), CAST(a.northing AS INT))))
     FROM read_csv('/tmp/aadf/aadf_sample.csv', header=true, all_varchar=true) a,
          ST_Read('tests/fixtures/ons/lad_2025/lad_sample.geojson') b
     GROUP BY 1, 2
   """).fetchall()
   for r in rows: print(r)
   EOF
   ```
   Every row should end `True`. Drop (from the fixture CSV) any count point that is
   `False` — note the drop in the fixture README. At least one A689 and one A179 point
   for 2023 must remain; if not, widen Step 2's filter to more Hartlepool A-roads and
   pick points that do stamp.
4. Commit the fixture:
   - `tests/fixtures/aadf/dft_traffic_counts_aadf_sample.csv` — the verified sample,
     exact national header, unmodified rows.
   - `tests/fixtures/aadf/README.md` — mirror the style of
     `tests/fixtures/stats19/README.md`: source URL, download date, filter used
     (Hartlepool, A689/A179, 2022–2023), any dropped points, and the OGL v3.0
     attribution ("Contains public sector information licensed under the Open
     Government Licence v3.0. Source: Department for Transport, Road Traffic
     Statistics.").
5. Keep `/tmp/aadf/NATIONAL.csv` around — Stage 02's runbook reuses it.

### Step 2 — Write `src/crossroads/transformers/aadf.py`

One module, one class. Follow `bank_holidays.py` for structure/tone and `spatial.py` for
the quality/ledger/R-Tree mechanics. Contents:

**Module docstring** — what AADF is (DfT annual average daily flow per count point on
major roads, one row per link per year, years 2000 onward), that eastings/northings are
already EPSG:27700, that the full history is always landed (no year slicing), that
minor-road figures may be `estimation_method = 'Estimated'` (kept + exposed, never dropped),
and that LAD/CTYUA stamping honours the build's `boundary_mode` — snapshot = latest
vintage, temporal = the vintage in force at a mid-year (1 July) date derived from each
count's `year` (approximate only in a year a boundary changed).

**Constants:**
```python
AADF_ZIP_URL = ("https://storage.googleapis.com/dft-statistics/road-traffic/"
                "downloads/data-gov-uk/dft_traffic_counts_aadf.zip")
ZIP_CACHE_FILE = "dft_traffic_counts_aadf.zip"
CSV_CACHE_FILE = "dft_traffic_counts_aadf.csv"   # canonical extracted name in the cache
# British National Grid envelope for validating EPSG:27700 coordinates
# (same convention as the other spatial sources).
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000
COORD_RULE = "aadf.coord.invalid"
COUNT_RULE = "aadf.count.invalid"
```

**Class `AadfTransformer(BaseTransformer)`:**

- Identity:
  ```python
  source_id = "aadf"
  display_name = "traffic counts (AADF)"
  # Ordering only: run after the boundary sources so the LAD/CTYUA stamp finds
  # their tables. Alphabetically 'aadf' would otherwise run first.
  depends_on = ("ons_lad", "ons_ctyua")
  BRONZE = "aadf_raw"
  SILVER = "aadf"
  CLEAN_VIEW = "aadf_clean"
  ```
- `is_active(self, **kwargs)` → `return bool(kwargs.get("years"))` — same gate as the
  stats19/weather sources: a no-years (boundary-only) build must not trigger a 40 MB
  download. Note in a comment that AADF still lands its FULL history when active.
- `extract(self, cache_dir, **kwargs)`:
  1. `os.makedirs(cache_dir, exist_ok=True)`.
  1a. Capture the boundary mode for the later stamp, exactly as stats19 does
     (`src/crossroads/transformers/stats19.py:159`):
     `self._boundary_mode = kwargs.get("boundary_mode", "snapshot")`.
  2. If the extracted CSV (`CSV_CACHE_FILE`) exists → return (offline/test path).
  3. Else if the zip exists → unzip it (step 5) and return.
  4. Else download the zip with the atomic `.part` pattern from
     `bank_holidays.BankHolidaysTransformer._download` (urllib, timeout, write temp,
     `os.replace`; on any failure remove the temp and re-raise). No parse check is
     possible on a zip mid-download; instead validate by opening it in step 5.
  5. Unzip via stdlib `zipfile`: list members ending `.csv` (case-insensitive); require
     exactly one (raise `ValueError` naming the members otherwise); extract it to a
     temp path in `cache_dir` and `os.replace` to `CSV_CACHE_FILE`. A corrupt zip makes
     `zipfile.ZipFile` raise — that is the fail-loud path.
- `transform_and_load(self, con, cache_dir)`:
  1. Fail with a clear `FileNotFoundError` if the cached CSV is missing (mirror
     `bank_holidays.transform_and_load`).
  2. **Bronze** — faithful, header-driven, no year filter:
     ```python
     con.execute(
         f"CREATE OR REPLACE TABLE {self.BRONZE} AS "
         f"SELECT * FROM read_csv('{path}', header=true, all_varchar=true)")
     ```
  3. **Silver** — call `self._derive_silver(con)` (factored out so tests can drive it
     against a synthetic bronze, like `spatial._derive_silver_and_ledger`). It creates:
     ```sql
     CREATE OR REPLACE TABLE aadf AS
     SELECT
       count_point_id || '|' || year AS source_row_key,
       TRY_CAST(count_point_id AS INTEGER) AS count_point_id,
       TRY_CAST(year AS INTEGER)           AS year,
       region_name, local_authority_name,
       road_name, road_type,
       start_junction_road_name, end_junction_road_name,
       -- keep-in-place: raw coordinate twins + typed values + geometry + flag
       easting  AS easting_raw,
       northing AS northing_raw,
       TRY_CAST(easting  AS INTEGER) AS easting,
       TRY_CAST(northing AS INTEGER) AS northing,
       CASE WHEN <typed easting/northing both NOT NULL and inside BNG envelope>
            THEN ST_Point(TRY_CAST(easting AS INTEGER), TRY_CAST(northing AS INTEGER))::GEOMETRY
            ELSE NULL END AS geom,
       <same predicate> AS geom_valid,
       TRY_CAST(link_length_km AS DOUBLE) AS link_length_km,
       estimation_method, estimation_method_detailed,
       -- traffic volumes: raw twin + typed value + flag for the headline count
       all_motor_vehicles AS all_motor_vehicles_raw,
       TRY_CAST(all_motor_vehicles AS BIGINT) AS all_motor_vehicles,
       (TRY_CAST(all_motor_vehicles AS BIGINT) IS NOT NULL
        AND TRY_CAST(all_motor_vehicles AS BIGINT) >= 0) AS count_valid,
       -- remaining per-class volumes, typed (not separately flagged)
       TRY_CAST(pedal_cycles AS BIGINT) AS pedal_cycles,
       TRY_CAST(two_wheeled_motor_vehicles AS BIGINT) AS two_wheeled_motor_vehicles,
       TRY_CAST(cars_and_taxis AS BIGINT) AS cars_and_taxis,
       TRY_CAST(buses_and_coaches AS BIGINT) AS buses_and_coaches,
       TRY_CAST(lgvs AS BIGINT) AS lgvs,
       TRY_CAST(all_hgvs AS BIGINT) AS all_hgvs,
       -- stamped after creation by the point-in-polygon join
       CAST(NULL AS VARCHAR) AS lad_code,
       CAST(NULL AS VARCHAR) AS ctyua_code
     FROM aadf_raw
     ```
     Notes: write the envelope predicate once in Python and interpolate it into both the
     `geom` CASE and `geom_valid` (identifiers/constants only — never row values).
     `latitude`/`longitude` and duplicate columns (`link_length_miles`, region/LA ids,
     per-axle HGV splits) stay in bronze only — document that in the module docstring.
     If the national header differs from the expected list (overview risk), adapt here
     and record the deviation.
  4. **Ledger** — one `log_exclusion` row per failed dimension (exact shape of
     `spatial.py`'s bad-geometry loop): `WHERE geom_valid = FALSE` → `COORD_RULE`
     (column `geom`, raw value `easting_raw||','||northing_raw`), and
     `WHERE count_valid = FALSE` → `COUNT_RULE` (column `all_motor_vehicles`).
     On real data both sets should be near-empty — the loop is cheap.
  5. **Area stamping** — `self._stamp_area_codes(con)`: copy
     `stats19._spatial_stamp`'s `UPDATE … FROM (SELECT … min(b.area_code) …
     ST_Contains(b.geom, c2.geom) {pred} … GROUP BY …)` shape for
     `("lad_code", "lad_boundaries")` and `("ctyua_code", "ctyua_boundaries")`, with the
     same `_table_exists` guard + `warnings.warn` fallback (copy `_table_exists` locally;
     it is 4 lines).

     **Honour the build's `boundary_mode`** (this is the key behaviour, mirroring
     `stats19._spatial_stamp`/`_boundary_predicate` at
     `src/crossroads/transformers/stats19.py:704-717`). Read the mode with
     `mode = getattr(self, "_boundary_mode", "snapshot")` (defaults to snapshot so a test
     that calls the stamp without running `extract` still works), then build the
     point-in-polygon extra predicate:
     - **snapshot** (default): `pred = "AND b.valid_to IS NULL"` — latest vintage only,
       exactly as today.
     - **temporal**: resolve each count to the vintage whose `[valid_from, valid_to)`
       window contains a **mid-year date derived from the row's `year`**. Mirror the
       stats19 temporal predicate verbatim, swapping its `CAST(c2.datetime_local AS DATE)`
       date expression for `make_date(c2.year, 7, 1)`:
       ```
       pred = ("AND b.valid_from <= make_date(c2.year, 7, 1) "
               "AND (b.valid_to IS NULL OR make_date(c2.year, 7, 1) < b.valid_to)")
       ```
       `year` is the typed INTEGER column from silver; a NULL year makes `make_date`
       return NULL, so that row matches no vintage and is left unstamped (acceptable —
       near-zero on real DfT data). 1 July is chosen because UK boundary changes take
       effect 1 April, so mid-year picks the vintage in force for most of the count year.
       Write a one-line comment stating this convention.
     Interpolate `pred` into the join's `ON` clause (identifiers/constants only — never a
     row value). Do NOT try to share code with stats19 — the date expression differs, and
     a copied 3-line predicate is simpler than an abstraction (per CLAUDE.md).
  6. **Conservation**: `record_source_rows(con, self.source_id, <count of BRONZE>)`.
  7. **Gold**: `create_clean_view(con, self.CLEAN_VIEW, self.SILVER, ["geom_valid", "count_valid"])`.
  8. **Index**: drop/create `aadf_geom_rtree` `USING RTREE (geom)` exactly as
     `spatial.py` does (NULL geoms are skipped by the index without error).
- `quality_spec(self)`:
  ```python
  return SourceQuality(
      source_id=self.source_id,
      bronze_table=self.BRONZE,
      silver_table=self.SILVER,
      dimensions=(
          Dimension("geom", "geom_valid", (COORD_RULE,)),
          Dimension("count", "count_valid", (COUNT_RULE,)),
      ),
      key_column="source_row_key",
  )
  ```

### Step 3 — Test-suite ripple fixes (the menu renumber)

The selectable menu is source_id-sorted; it becomes
`1=aadf, 2=bank holidays, 3=era5_weather, 4=stats19`.

1. `tests/test_console.py` around line 306: scripted pick `"3"` → `"4"`; update the
   comment listing the menu order.
2. `tests/test_schema_doc.py` around line 73: scripted pick `"2-3"` → `"3-4"` (same
   datasets as before: weather + stats19; `aadf` joins this build in Stage 04 when the
   drift guard covers it).
3. `tests/test_console.py::_seed_full_cache` (around line 277): copy the new fixture in:
   ```python
   AADF_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "aadf",
                               "dft_traffic_counts_aadf_sample.csv")
   ...
   shutil.copy(AADF_FIXTURE, os.path.join(cache_dir, "dft_traffic_counts_aadf.csv"))
   ```
   (destination name = the transformer's `CSV_CACHE_FILE`, so `extract()` skips the
   download).
4. Any test that calls `client.build(years=[...])` **without** a `datasets=` list now
   activates `aadf` and will try to download. Run the full suite and fix each such site
   by seeding the fixture into that test's cache dir (same `shutil.copy` as above).
   Known candidates from a pre-plan grep: `tests/test_client.py:23`,
   `tests/test_stats19.py` (its `_seed_cache`-style helpers), and any
   `tests/test_quality.py` integration builds that pass `years`. Do NOT change those
   tests' `build()` arguments — seeding the cache is the established pattern.

### Step 4 — New tests: `tests/test_aadf.py`

Follow the structure/tone of the existing per-source test files. Fast, offline,
deterministic unless marked. Cover:

1. **Silver typing & flags (unit, synthetic bronze):** create a tiny `aadf_raw` by hand
   (3 rows: one good; one with blank easting; one with `all_motor_vehicles = 'x'`), call
   `AadfTransformer()._derive_silver(con)` plus the ledger step, then assert: row counts
   1:1; good row has `geom_valid=TRUE, count_valid=TRUE` and correct typed values; bad
   rows have the right flag `FALSE`, `geom`/typed value `NULL`, raw twins preserved, and
   a matching `data_quality_log` row with the right `rule_id`. (Use the shared DuckDB
   fixture from `tests/conftest.py`; load spatial + `ensure_quality_tables` first —
   mirror how `tests/test_spatial.py` sets up.)
2. **Extract cache-skip (unit, no network):** seed a cache dir with a file named
   `CSV_CACHE_FILE`; `extract()` must return without touching the network (it must not
   raise, and the file must be unchanged).
3. **Zip extraction (unit, no network):** build a tiny zip in `tmp_path` containing one
   CSV member with an arbitrary name; put it in the cache as `ZIP_CACHE_FILE`; run
   `extract()`; assert `CSV_CACHE_FILE` appears with the member's content. Also assert a
   zip with two CSV members raises `ValueError`.
4. **Full offline build (integration):** `@pytest.mark.integration` — seed a cache with
   the STATS19/ONS/bank-holidays/AADF fixtures (reuse `_seed_full_cache` from
   `tests.test_console`), `init_engine(tmp db).build(datasets=["aadf"], years=[2023])`,
   then assert: `aadf` row count equals the fixture's row count; every row has
   `geom_valid=TRUE`; `lad_code='E06000001'` on all rows (the Step 1.3 verification
   guarantees this); the `aadf_clean` view exists and matches the silver count; the
   `aadf_geom_rtree` index exists (query `duckdb_indexes()`); and the build passed the
   invariants (it did not raise).
5. **Boundary-mode stamping (unit, synthetic tables) — the core proof of the new
   behaviour.** No real multi-vintage ONS fixture is needed; hand-build the two tables
   the stamp joins against. Setup on the shared DuckDB fixture (`INSTALL spatial; LOAD
   spatial`):
   - Create a `lad_boundaries` table with the columns the stamp reads — at minimum
     `area_code VARCHAR, geom GEOMETRY, geom_valid BOOLEAN, valid_from DATE, valid_to
     DATE` — holding **two vintages that cover the same point but carry different codes
     and non-overlapping windows**, e.g.
     `('OLD01', <polygon around point P>, TRUE, DATE '2010-04-01', DATE '2020-04-01')`
     and `('NEW01', <same polygon>, TRUE, DATE '2020-04-01', NULL)`. (Match the real
     column names/types from a fixture build's `DESCRIBE lad_boundaries` if they differ.)
   - Create an `aadf` silver table by hand (or via `_derive_silver` on a synthetic
     bronze) with two rows at `geom = ST_Point(P)`, one `year = 2015` and one
     `year = 2023`, `geom_valid = TRUE`, `lad_code = NULL`.
   - **Temporal:** set `t = AadfTransformer(); t._boundary_mode = "temporal"`, call
     `t._stamp_area_codes(con)`, then assert the 2015 row got `lad_code = 'OLD01'`
     (1 July 2015 falls in `[2010-04-01, 2020-04-01)`) and the 2023 row got
     `lad_code = 'NEW01'` (1 July 2023 ≥ 2020-04-01, open window).
   - **Snapshot:** reset both `lad_code`s to NULL, set `t._boundary_mode = "snapshot"`,
     re-stamp, and assert **both** rows got `lad_code = 'NEW01'` (latest vintage,
     `valid_to IS NULL`), regardless of year.
   This single test pins both modes and the mid-year (1 July) boundary. The pattern of
   building synthetic boundary rows mirrors `tests/test_spatial.py`'s temporal tests
   (`_two_vintage_lad`, `test_temporal_mode_loads_all_vintages_with_windows`).

## Testing & Verification

```bash
python -m pytest                    # fast suite — must be green
python -m pytest -m integration     # offline integration suite — must be green
```
Ship-readiness checklist for this stage:
- [ ] `tests/fixtures/aadf/` contains the verified sample CSV + provenance README.
- [ ] `crossroads` wizard (run it, then Ctrl-C at the years prompt) shows
      `1. traffic counts (AADF)` in the dataset menu.
- [ ] Full suite green with zero network access (disconnect Wi-Fi to prove it if unsure).

## End State / Handoff

- `src/crossroads/transformers/aadf.py` exists; `Registry().selectable()` lists `aadf`
  first; `depends_on` orders it after both boundary sources.
- A fixture-seeded `build(datasets=["aadf"], years=[2023])` produces populated
  `aadf_raw` / `aadf` / `aadf_clean` + R-Tree, passes all spec §9 invariants, and stamps
  `lad_code='E06000001'` on every fixture row.
- `_stamp_area_codes` honours `self._boundary_mode`: snapshot (default) uses the latest
  vintage; temporal resolves each row via `make_date(year, 7, 1)`. The synthetic
  two-vintage unit test (Step 4.5) proves both modes. Stage 03 (wizard) and Stage 04
  (docs) may assume this behaviour exists.
- The full test suite (fast + integration) is green offline.
- `/tmp/aadf/NATIONAL.csv` (or the zip) is still available locally for Stage 02.
- Stage 02 may assume: fixture rows exist for A689 and A179 (year 2023) whose LAD stamp
  matches the STATS19 fixture collisions' district.

## Failure Modes & Rollback

- **National header differs from expected** → silver SELECT fails loudly on the missing
  column; adapt the SELECT + fixture, note the deviation in the fixture README.
- **Fixture points outside sample polygons** → Step 1.3 catches it before any code is
  written; swap points.
- **Download URL moves** → check https://roadtraffic.dft.gov.uk/downloads for the new
  "AADF by count point" link and update `AADF_ZIP_URL`.
- **Reject-rate tripwire** → coordinate/count failures on real DfT data should be ≈0%;
  if the 5% default ceiling ever trips, that signals an upstream format change, which is
  exactly what the tripwire is for — investigate, don't raise the ceiling.
- **Rollback:** delete `src/crossroads/transformers/aadf.py`, `tests/test_aadf.py`,
  `tests/fixtures/aadf/`, and revert the Step 3 edits. The registry forgets the source
  automatically; no core file was touched.
