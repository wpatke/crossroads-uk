# Stage 03 — STATS19 weather stamping & wiring
> Part of *Meteorological Grid Integration (ERA5-Land)*. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Stages 01 and 02 are complete. Verify:

```bash
pip install -e '.[weather]'
pytest -q                                          # default suite green
pytest -m integration -q tests/test_weather.py     # weather-only build green
python -c "from crossroads.registry import Registry; print([t.source_id for t in Registry().get_active(datasets=['stats19','era5_weather'], years=[2023])])"
# era5_weather (and boundaries) sort BEFORE stats19
```

- `Era5WeatherTransformer` (`src/crossroads/transformers/weather.py`) builds a `weather` silver with `grid_i`, `grid_j`, `valid_time_local`, `temperature_c`, `precipitation_mm` (and `geom`, `geom_valid`). It stamps nothing.
- `Stats19Transformer` already declares `depends_on = ("era5_weather", "ons_lad", "ons_ctyua")` (Stage 01), so `weather` imports before `stats19` when both are active.
- `stats19._spatial_stamp` is the model to copy: it `_table_exists`-guards each boundary table, `UPDATE`s `collisions` from a `GROUP BY` subquery, and warns-and-skips a missing table. `_derive_collision_silver` already adds `CAST(NULL AS VARCHAR) AS lad_code, CAST(NULL AS VARCHAR) AS ctyua_code` to the collisions silver, filled later by the stamp.
- `transform_and_load` calls `_spatial_stamp(con)` after building silver and before building `collisions_spatial` + the R-Tree.

## Objective

Make STATS19 optionally consume the `weather` table: add two always-present NULL columns to the `collisions` silver (`temperature_c`, `precipitation_mm`) and a `_weather_stamp` step — the existence-guarded twin of `_spatial_stamp` — that fills them by joining valid collisions to `weather` on grid cell + local hour. Prove end-to-end offline that a stats19+weather build stamps collisions, a stats19-only build is unchanged (weather columns present but NULL), and all §9 invariants hold.

## Implementation Steps

### Step 1 — Add the always-present weather columns to the collisions silver

File: `src/crossroads/transformers/stats19.py`, method `_derive_collision_silver`.

In the outer `SELECT`, next to the existing boundary placeholders, add two typed-NULL columns so the collisions schema is stable whether or not weather is built (exactly as `lad_code`/`ctyua_code` are always present):

```python
            # Filled by the Stage 04 spatial stamp; present now so the schema is stable.
            f"  CAST(NULL AS VARCHAR) AS lad_code, "
            f"  CAST(NULL AS VARCHAR) AS ctyua_code, "
            # Filled by _weather_stamp when a weather table exists; NULL otherwise
            # (mirrors lad_code — collisions always carry these columns). DOUBLE:
            # Celsius and millimetres.
            f"  CAST(NULL AS DOUBLE) AS temperature_c, "
            f"  CAST(NULL AS DOUBLE) AS precipitation_mm, "
```

(Insert immediately after the `ctyua_code` line, keeping the trailing comma chain correct.) These are bespoke NULL columns, not bronze columns, so they never collide with the broad-clean loop (same as `lad_code`).

Expected result: `DESCRIBE collisions` shows `temperature_c` and `precipitation_mm` as `DOUBLE`, NULL in every row until stamped.

### Step 2 — The `_weather_stamp` method (twin of `_spatial_stamp`)

File: `src/crossroads/transformers/stats19.py`. Add a rule id constant near the other rule ids (it is used only in a warning, not a ledger rule — weather enrichment has no reject dimension) and the method near `_spatial_stamp`:

```python
    def _weather_stamp(self, con):
        """Optionally stamp temperature_c/precipitation_mm onto valid collisions from an
        already-built weather grid. The exact shape of _spatial_stamp: if the weather
        table is absent (weather not selected/built this run), warn and leave the columns
        NULL — collisions still build. This is the 'optional dependency' guard at ETL time
        (the registry has already ordered weather before stats19 when both are active).

        Match (spec §3A/§3B): reproject each collision point back to lon/lat, round to the
        0.1° ERA5-Land grid index (grid_i, grid_j), and match the weather cell with the same
        index AND the same UK-local hour (weather.valid_time_local is pre-materialised, so
        no ICU is needed here). weather is unique per (cell, hour), so min() aggregates over
        at most one row — the same defensive GROUP BY _spatial_stamp uses for area_code."""
        if not self._table_exists(con, "weather"):
            warnings.warn(
                "stats19: weather table not found; temperature_c/precipitation_mm left NULL "
                "(build the weather dataset alongside stats19 to enable weather stamping).",
                stacklevel=2)
            return
        con.execute(
            f"UPDATE {self.COLLISION_SILVER} AS c "
            f"SET temperature_c = m.temperature_c, precipitation_mm = m.precipitation_mm "
            f"FROM ("
            f"  SELECT k, min(temperature_c) AS temperature_c, "
            f"         min(precipitation_mm) AS precipitation_mm "
            f"  FROM ("
            f"    SELECT c2.source_row_key AS k, w.temperature_c, w.precipitation_mm "
            f"    FROM ("
            f"      SELECT source_row_key, datetime_local, "
            f"             ST_Transform(geom, 'EPSG:27700', 'EPSG:4326', always_xy := true) AS ll "
            f"      FROM {self.COLLISION_SILVER} "
            f"      WHERE geom IS NOT NULL AND datetime_local IS NOT NULL"
            f"    ) c2 "
            f"    JOIN weather w "
            f"      ON w.grid_i = CAST(round(ST_Y(c2.ll) * 10) AS INTEGER) "
            f"     AND w.grid_j = CAST(round(ST_X(c2.ll) * 10) AS INTEGER) "
            f"     AND date_trunc('hour', w.valid_time_local) = date_trunc('hour', c2.datetime_local)"
            f"  ) j "
            f"  GROUP BY k"
            f") m WHERE c.source_row_key = m.k"
        )
```

Ensure `import warnings` is present at the top of `stats19.py` (it is — `_spatial_stamp` already uses it).

### Step 3 — Call the stamp in `transform_and_load`

File: `src/crossroads/transformers/stats19.py`. Immediately **after** the `self._spatial_stamp(con)` call and before `create_clean_view(con, "collisions_spatial", ...)`, add:

```python
        # --- WEATHER STAMP (optional): fill temperature_c/precipitation_mm from the
        # weather grid if it was built this run (the registry orders weather first).
        self._weather_stamp(con)
```

Expected result: after a stats19+weather build, valid collisions whose cell+hour is covered by the weather grid carry real `temperature_c`/`precipitation_mm`; after a stats19-only build the columns stay NULL and a warning is emitted.

### Step 4 — Documentation touch-up

File: `src/crossroads/transformers/base.py`. The `is_active` docstring cites `include_weather=True` as the example flag. Soften it so it does not imply a real build flag, e.g. change the example to: *"A source gated behind a build parameter overrides this (e.g. `return bool(kwargs.get("years"))`)."* Do **not** introduce an `include_weather` flag.

### Step 5 — Console: verify only (no code change)

The wizard self-discovers datasets, so `weather` already appears; the registry orders imports. Confirm — do **not** edit `console.py`. (Menu order was settled in Stage 02: `selectable()` lists in source_id order, so `1. weather`, `2. stats19` — pick both with `1-2`.)

```bash
pip install -e '.[weather]'
printf 'x.db\n1-2\n2023\nsnapshot\nn\n' | crossroads   # menu shows weather + stats19, then aborts (exit 0)
```

## Testing & Verification

Commands:

```bash
pip install -e '.[weather]'
pytest -q tests/test_stats19.py tests/test_weather.py
pytest -m integration -q tests/test_stats19.py tests/test_console.py
pytest -q                          # whole default suite still green (weather tests skip w/o extra)
```

### A. Unit — `_weather_stamp` matches cell + hour (twin of `test_spatial_stamp_snapshot`)

Add to `tests/test_stats19.py` (does not need the `[weather]` extra — no NetCDF, just SQL; but it does use ST_Transform/ICU, so load spatial+icu):

```python
def _stub_weather(con):
    # One weather cell (54.7,-1.2) at 14:00 LOCAL (13:00 UTC in BST).
    con.execute("INSTALL icu"); con.execute("LOAD icu")
    con.execute(
        "CREATE TABLE weather AS SELECT "
        "  CAST(round(54.7*10) AS INTEGER) AS grid_i, "
        "  CAST(round(-1.2*10) AS INTEGER) AS grid_j, "
        "  ((TIMESTAMP '2023-06-15 13:00:00') AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London' AS valid_time_local, "
        "  15.0 AS temperature_c, 1.0 AS precipitation_mm")


def _stub_collisions_geo(con, rows):
    # rows: (key, lon, lat, iso_local_dt). Build 27700 geom via reprojection so the
    # stamp's inverse reprojection lands back on the same grid cell.
    vals = ", ".join(
        f"('{k}', ST_Transform(ST_Point({lon},{lat}),'EPSG:4326','EPSG:27700',always_xy:=true)::GEOMETRY, "
        f" TRUE, TIMESTAMP '{dt}', NULL, NULL, CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE))"
        for k, lon, lat, dt in rows)
    con.execute(
        f"CREATE TABLE collisions AS SELECT * FROM (VALUES {vals}) AS "
        f"t(source_row_key, geom, geom_valid, datetime_local, lad_code, ctyua_code, "
        f"  temperature_c, precipitation_mm)")


def test_weather_stamp_matches_cell_and_hour(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    _stub_weather(con)
    _stub_collisions_geo(con, [
        ("k_in",  -1.2, 54.7, "2023-06-15 14:30:00"),   # same cell + hour -> stamped
        ("k_hour", -1.2, 54.7, "2023-06-15 16:30:00"),  # same cell, wrong hour -> NULL
        ("k_cell", 0.0, 52.0, "2023-06-15 14:30:00"),   # wrong cell -> NULL
    ])
    Stats19Transformer()._weather_stamp(con)
    res = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT source_row_key, temperature_c, precipitation_mm FROM collisions").fetchall()}
    assert res["k_in"] == (15.0, 1.0)
    assert res["k_hour"] == (None, None)
    assert res["k_cell"] == (None, None)


def test_weather_stamp_tolerates_missing_weather_table(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    _stub_collisions_geo(con, [("k1", -1.2, 54.7, "2023-06-15 14:30:00")])
    with pytest.warns(UserWarning, match="weather table"):
        Stats19Transformer()._weather_stamp(con)          # no weather table -> warn, skip
    assert con.execute("SELECT temperature_c FROM collisions").fetchone()[0] is None
```

### B. Schema — collisions always carries the weather columns

Add to `tests/test_stats19.py` (reuse the stats19-only client helper `_stats19_client`):

```python
def test_collisions_has_weather_columns_even_without_weather(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)          # no weather in this build
    dt = {r[0].lower(): r[1] for r in client.con.execute("DESCRIBE collisions").fetchall()}
    assert dt["temperature_c"] == "DOUBLE" and dt["precipitation_mm"] == "DOUBLE"
    # All NULL (no weather table existed).
    assert client.con.execute(
        "SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0] == 0
    client.close()
```

### C. End-to-end offline — stats19 + weather (integration, THE ship proof)

Add to `tests/test_stats19.py` (guard with `pytest.importorskip("xarray")` at function level since it drives a real weather build):

```python
@pytest.mark.integration
def test_stats19_plus_weather_stamps_collisions_offline(tmp_path):
    pytest.importorskip("xarray")
    from crossroads.transformers.weather import Era5WeatherTransformer
    cache = str(tmp_path / "cache")
    _seed_cache(cache); _seed_ons_cache(cache)             # existing stats19 + ONS seeders
    weather_nc = os.path.join(os.path.dirname(__file__), "fixtures", "weather", "era5_land_sample.nc")
    shutil.copy(weather_nc, os.path.join(cache, "era5_land_2023.nc"))

    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [
        CTYUABoundaryTransformer(), LADBoundaryTransformer(),
        Stats19Transformer(), Era5WeatherTransformer()]    # get_active resolves weather-first
    client.build(datasets=["stats19", "era5_weather"], years=YEARS)   # runs §9 invariants
    try:
        # Weather grid built.
        assert client.con.execute("SELECT count(*) FROM weather").fetchone()[0] > 0
        # At least one collision stamped (fixtures are aligned by construction — Stage 02).
        stamped = client.con.execute(
            "SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0]
        assert stamped >= 1, ("No collisions stamped — regenerate the weather fixture with "
                              "scripts/build_weather_fixture.py so its cells/hours cover the "
                              "committed collision fixture.")
        # Row count unchanged (stamp is an UPDATE, not a join that fans out).
        assert client.con.execute("SELECT count(*) FROM collisions").fetchone()[0] > 0
    finally:
        client.close()
```

### D. Rebuild idempotency (weather-inclusive, on-disk)

```python
@pytest.mark.integration
def test_weather_build_is_idempotent(tmp_path):
    pytest.importorskip("xarray")
    from crossroads.transformers.weather import Era5WeatherTransformer
    db = str(tmp_path / "w.db"); cache = str(tmp_path / "cache")
    _seed_cache(cache); _seed_ons_cache(cache)
    shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "weather", "era5_land_sample.nc"),
                os.path.join(cache, "era5_land_2023.nc"))

    def run():
        cl = crossroads.init_engine(database_path=db, cache_dir=cache)
        cl.registry._transformers = [CTYUABoundaryTransformer(), LADBoundaryTransformer(),
                                     Stats19Transformer(), Era5WeatherTransformer()]
        cl.build(datasets=["stats19", "era5_weather"], years=YEARS); return cl
    a = run(); n1 = a.con.execute("SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0]; a.close()
    b = run(); n2 = b.con.execute("SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0]
    assert n1 == n2 and n2 >= 1
    b.close()
```

### E. Wizard e2e including weather (offline integration, console)

Add to `tests/test_console.py` (function-level `pytest.importorskip("xarray")`; seed weather `.nc` on top of the existing `_seed_full_cache`):

```python
@pytest.mark.integration
def test_wizard_builds_weather_offline(tmp_path):
    pytest.importorskip("xarray")
    cache = str(tmp_path / "cache"); _seed_full_cache(cache)
    shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "weather", "era5_land_sample.nc"),
                os.path.join(cache, "era5_land_2023.nc"))
    db_path = str(tmp_path / "wiz.duckdb")
    # Menu order is source_id: 1=weather (era5_weather), 2=stats19. Pick both with "1-2".
    reader, writer, _ = scripted([db_path, "1-2", "2023", "snapshot", "y"])
    client = console.run_wizard(reader, writer, cache_dir=cache)
    try:
        assert client is not None and os.path.exists(db_path)
        assert client.con.execute("SELECT count(*) FROM weather").fetchone()[0] > 0
        assert client.con.execute(
            "SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0] >= 1
    finally:
        client.close()
```

### F. Regression

- **All existing stats19 tests stay green.** Adding two NULL columns is additive: `test_all_silver_tables_full_width` checks header columns are present (extra columns are allowed); column-named SELECTs are unaffected. `test_end_to_end_build_stamps_collisions` builds stats19-only (no weather in cache), so `_weather_stamp` warns-and-skips and the columns stay NULL — the test asserts nothing about them.
- **Menu-order change from Stage 02** (weather now index 1) is already reflected in the console tests updated there; this stage's wizard test uses `1-2`.
- Default `pytest -q` (no `[weather]`) skips every `importorskip`-guarded weather test and stays green.

### Stage ship-readiness checklist

- [ ] `pytest -m integration -q tests/test_stats19.py tests/test_console.py` green with the `[weather]` extra.
- [ ] `pytest -q` (default suite) green with **and** without the `[weather]` extra.
- [ ] After a stats19+weather build, `SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL` ≥ 1; after a stats19-only build it is 0 and the columns exist.
- [ ] A weather-only build (Stage 02 test) still builds the `weather` grid and skips stamping (no `collisions` table).
- [ ] `printf 'x.db\n1-2\n2023\nsnapshot\nn\n' | crossroads` shows weather + stats19 then aborts cleanly (exit 0).
- [ ] Full §9 invariants pass on every build above.

## End State / Handoff (the contract — feature complete)

- `collisions` carries `temperature_c`/`precipitation_mm` (DOUBLE, always present; NULL when no weather was built). STATS19's `_weather_stamp` fills them from the `weather` grid by cell + local hour, guarded by a `_table_exists` check — the same optional-consumption shape as `_spatial_stamp`. STATS19 never writes to a foreign table; weather never writes to `collisions`.
- A `datasets=["stats19","era5_weather"], years=[2023]` build (wizard or programmatic) offline stamps ≥1 collision with the correct hourly weather and passes all §9 invariants; a stats19-only build is byte-for-byte as before (weather columns NULL); a weather-only build builds just the grid.
- `weather` is a first-class selectable dataset, ordered before `stats19` by the registry's `depends_on` resolver — no console/registry edits beyond the generic seam from Stage 01.
- Spec §5 Phase 4 deliverables met: ERA5-Land NetCDF ingested via cdsapi (real path), centroids reprojected to EPSG:27700, collisions stamped with hourly precipitation/temperature at the §3B local-time grain; `include_weather` is not used.

## Failure Modes & Rollback

- **No collision stamped in test C** → the weather fixture's cells/hours no longer cover the committed collision fixture (e.g. the collision fixture was re-trimmed). Regenerate: `python scripts/build_weather_fixture.py`, then `--check`. The assertion message says exactly this.
- **Duplicate weather rows per cell-hour** → the `GROUP BY k` + `min()` aggregate collapses them deterministically (weather is unique per cell-hour, so this is defensive only).
- **DST fall-back hour** → a collision in the autumn repeated local hour may match either weather instance (local-hour match only). Documented limitation (spec §3B).
- **collisions schema coupling** → `collisions` always carries the two weather columns even in weather-less builds, mirroring `lad_code`. Accepted for schema stability; revert Step 1 if a future decision wants them conditional.
- **Rollback:** remove `_weather_stamp` + its call + the two silver columns (Steps 1–3), the `base.py` docstring edit, and the Stage-03 tests. STATS19 reverts to boundary-only stamping; weather remains a standalone source (Stage 02). To remove weather entirely, follow the Stage 02 and Stage 01 rollbacks.
