# Stage 02 — Weather grid source
> Part of *Meteorological Grid Integration (ERA5-Land)*. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Stage 01 is complete. Verify:

```bash
pip install -e '.[dev]'
pytest -q                       # green
python -c "from crossroads.transformers.base import BaseTransformer; print(BaseTransformer.depends_on)"   # ()
python -c "from crossroads.transformers.stats19 import Stats19Transformer as S; print(S().depends_on)"    # ('era5_weather', 'ons_lad', 'ons_ctyua')
```

- `BaseTransformer.depends_on` exists (default `()`); `crossroads.registry` has `resolve_order` + `DependencyCycleError` (raises on a declared cycle — cyclic deps not supported yet); `get_active` returns dependency-ordered active transformers.
- `Stats19Transformer` already declares `depends_on = ("era5_weather", "ons_lad", "ons_ctyua")`, so once a transformer with `source_id = "era5_weather"` exists it will sort before `stats19`.
- The quality engine (`src/crossroads/quality.py`) provides `SourceQuality`, `Dimension`, `record_source_rows`, `log_exclusion`, `create_clean_view` — imported the way `spatial.py` imports them.
- `client.build()` loads the DuckDB **Spatial** extension before any transformer runs.
- The committed collision fixture `tests/fixtures/stats19/dft-road-casualty-statistics-collision-2023.csv` exists (rows trimmed to fall inside the committed ONS LAD sample; e.g. lon `-1.2097`, lat `54.6603`, `30/09/2023`, `14:55`).

## Objective

Add `src/crossroads/transformers/weather.py` — the `Era5WeatherTransformer` — a **plain source** that builds its own queryable `weather` grid table from ERA5-Land NetCDF: extract (real, cdsapi, lazy) → bronze (faithful long table) → silver (EPSG:27700 centroid, `valid_time_utc` + derived `valid_time_local`, `temperature_c`, `precipitation_mm`, integer grid index, `geom_valid`) → `weather_clean` gold → audited by the quality engine. Add a committed `scripts/build_weather_fixture.py` + the synthetic `.nc` it produces. **Weather stamps nothing** — STATS19 consumes it in Stage 03.

## Implementation Steps

### Step 1 — The transformer module

Create `src/crossroads/transformers/weather.py`. Follow the structure and comment density of `spatial.py`. Key points, then the skeleton:

- **Lazy imports.** `cdsapi`, `xarray`, `pandas` are imported **inside** methods, never at module top, so the module imports cleanly for registry discovery without the `[weather]` extra.
- **Identity.** `source_id = "era5_weather"`, `display_name = "weather"`, `user_selectable` inherits `True`, `depends_on` inherits `()` (weather depends on nothing). `is_active` requires `years` — no `include_weather` flag.
- **ICU.** Load the DuckDB **ICU** extension in `transform_and_load` (`INSTALL icu; LOAD icu`) for the UTC→`Europe/London` conversion. Idempotent; loads offline (verified).
- **Grid.** ERA5-Land native resolution `0.1°`; grid index is `round(coord*10)` as `INTEGER`.

```python
"""Copernicus ERA5-Land gridded reanalysis weather (spec §3B, §5 Phase 4).

A plain data source: it loads its own queryable weather grid table and stamps
nothing. STATS19 optionally consumes the ``weather`` table to annotate collisions
(see docs/plans/006_weather_integration/03), the same way it consumes the boundary
tables — so this module never needs to know about collisions.

ERA5-Land is a LAND reanalysis on a regular 0.1° latitude/longitude grid, hourly,
UTC-native. We ingest 2 m temperature (t2m, Kelvin) and total precipitation (tp,
metres). Cell centroids are reprojected ONCE to EPSG:27700 (spec §3A); the raw UTC
instant is preserved and a UK-local time derived (spec §3B). Sea cells (outside the
land model) carry NULL metrics BY DOMAIN — kept in place, not rejected.

Heavy dependencies (cdsapi, xarray, netCDF4) live in the optional ``[weather]`` extra
and are imported lazily, so this module imports cleanly for discovery without them.
Offline tests seed the cache with a committed synthetic .nc, so extract() downloads
nothing.

Note on ``tp``: ERA5-Land total precipitation is an accumulation in metres. This
transformer takes each published hourly value, converts to millimetres, and stores
it as-is (the spec §5 baseline is "precipitation" with no de-accumulation). This is
a documented simplification.
"""

import logging
import os

from crossroads.transformers.base import BaseTransformer
from crossroads.quality import (
    SourceQuality, Dimension, create_clean_view, record_source_rows, log_exclusion,
)

# ERA5-Land native grid step (degrees). The join key is round(coord / GRID_DEG).
GRID_DEG = 0.1

# British National Grid envelope, to verify centroids really are EPSG:27700 in tests.
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000

# UK bounding box for the real cdsapi request [North, West, South, East].
UK_AREA = [61.0, -8.5, 49.5, 2.0]


class Era5WeatherTransformer(BaseTransformer):
    """Ingests ERA5-Land NetCDF into a weather grid (bronze/silver/gold), audited
    under source_id 'era5_weather'. A plain source — it stamps nothing."""

    source_id = "era5_weather"
    display_name = "weather"                 # friendly wizard-menu label

    BRONZE = "era5_weather_raw"
    SILVER = "weather"
    CLEAN_VIEW = "weather_clean"
    GEOM_RULE = "era5.geom.invalid"

    def is_active(self, **kwargs):
        # Nothing to ingest without a time range; a no-years build skips weather.
        return bool(kwargs.get("years"))

    # --- extract (real download; offline tests pre-seed the cache) ---------
    def _filename(self, year):
        return f"era5_land_{year}.nc"

    def extract(self, cache_dir, **kwargs):
        os.makedirs(cache_dir, exist_ok=True)
        self._years = [int(y) for y in (kwargs.get("years") or [])]
        for year in self._years:
            path = os.path.join(cache_dir, self._filename(year))
            if not os.path.exists(path):          # offline-friendly: skip if cached/seeded
                self._download(year, path)

    def _download(self, year, dest):
        """Download one year of ERA5-Land hourly t2m + tp over the UK as NetCDF.

        Real path only (needs a Copernicus ~/.cdsapirc). cdsapi is imported lazily so
        the module and the offline tests do not require the [weather] extra. The file
        is large; it is cached, so a re-run does not re-download."""
        import cdsapi                           # lazy: real download only
        client = cdsapi.Client()
        client.retrieve(
            "reanalysis-era5-land",
            {
                "variable": ["2m_temperature", "total_precipitation"],
                "year": str(year),
                "month": [f"{m:02d}" for m in range(1, 13)],
                "day": [f"{d:02d}" for d in range(1, 32)],
                "time": [f"{h:02d}:00" for h in range(24)],
                "area": UK_AREA,
                "data_format": "netcdf",
            },
            dest,
        )

    # --- transform_and_load ------------------------------------------------
    def transform_and_load(self, con, cache_dir):
        years = getattr(self, "_years", None) or []
        if not years:
            return                              # defensive: is_active gates on years

        con.execute("INSTALL icu"); con.execute("LOAD icu")   # UTC -> Europe/London below

        paths = [os.path.join(cache_dir, self._filename(y)) for y in years]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            raise FileNotFoundError(
                f"[era5_weather] no cached NetCDF for years {years}; extract() must run "
                f"first (or seed the cache in tests).")

        self._load_bronze(con, paths)
        self._derive_silver_and_ledger(con)
        n = con.execute(f"SELECT count(*) FROM {self.BRONZE}").fetchone()[0]
        record_source_rows(con, self.source_id, n)                    # conservation
        create_clean_view(con, self.CLEAN_VIEW, self.SILVER, ["geom_valid"])   # gold

    def _load_bronze(self, con, nc_paths):
        """Faithful bronze: one row per (grid cell, hour). Parse each .nc with xarray
        (lazy) into a long DataFrame (valid_time, latitude, longitude, t2m, tp),
        register it, and CREATE OR REPLACE the bronze table. Values are not edited here
        (that is silver's job). Tolerates the 'valid_time' or 'time' coordinate name
        across CDS vintages."""
        import xarray as xr                      # lazy
        import pandas as pd                      # lazy (xarray dependency)
        frames = []
        for p in nc_paths:
            ds = xr.open_dataset(p)
            tname = "valid_time" if ("valid_time" in ds.variables or "valid_time" in ds.coords) else "time"
            df = ds[["t2m", "tp"]].to_dataframe().reset_index()
            df = df.rename(columns={tname: "valid_time"})
            frames.append(df[["valid_time", "latitude", "longitude", "t2m", "tp"]])
            ds.close()
        bronze_df = pd.concat(frames, ignore_index=True)
        con.register("era5_bronze_df", bronze_df)
        try:
            con.execute(
                f"CREATE OR REPLACE TABLE {self.BRONZE} AS SELECT * FROM era5_bronze_df")
        finally:
            con.unregister("era5_bronze_df")

    def _derive_silver_and_ledger(self, con):
        """Keep-in-place silver, 1:1 with bronze. Factored out so tests can drive it
        against a synthetic bronze without a .nc file.

          • source_row_key  = grid_i|grid_j|YYYYMMDDHH (deterministic, unique per cell-hour)
          • geom            = EPSG:27700 centroid via ST_Transform(..., always_xy := true)
          • geom_valid      = coordinates present (a centroid always reprojects)
          • valid_time_utc  = raw naive UTC instant
          • valid_time_local= (utc AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London' (naive local)
          • temperature_c   = t2m - 273.15   (Kelvin -> Celsius; NULL stays NULL)
          • precipitation_mm= tp * 1000       (metres -> millimetres; NULL stays NULL)
          • grid_i/grid_j   = round(lat/lon * 10) integer grid index (STATS19's join key)

        Sea/NaN cells (ERA5-Land is a LAND model) keep NULL metrics BY DOMAIN — not a
        reject dimension. The only audited dimension is geom (like spatial.py)."""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.SILVER} AS "
            f"WITH typed AS ("
            f"  SELECT "
            f"    CAST(latitude  AS DOUBLE) AS latitude, "
            f"    CAST(longitude AS DOUBLE) AS longitude, "
            f"    CAST(valid_time AS TIMESTAMP) AS valid_time_utc, "
            f"    TRY_CAST(t2m AS DOUBLE) AS t2m_k, "
            f"    TRY_CAST(tp  AS DOUBLE) AS tp_m "
            f"  FROM {self.BRONZE}"
            f") "
            f"SELECT "
            f"  CAST(round(latitude * 10)  AS INTEGER) || '|' || "
            f"  CAST(round(longitude * 10) AS INTEGER) || '|' || "
            f"  strftime(valid_time_utc, '%Y%m%d%H') AS source_row_key, "
            f"  latitude, longitude, "
            f"  CAST(round(latitude * 10)  AS INTEGER) AS grid_i, "
            f"  CAST(round(longitude * 10) AS INTEGER) AS grid_j, "
            f"  ST_Transform(ST_Point(longitude, latitude), 'EPSG:4326', 'EPSG:27700', "
            f"               always_xy := true)::GEOMETRY AS geom, "
            f"  (latitude IS NOT NULL AND longitude IS NOT NULL) AS geom_valid, "
            f"  valid_time_utc, "
            f"  (valid_time_utc AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London' AS valid_time_local, "
            f"  CASE WHEN t2m_k IS NULL THEN NULL ELSE t2m_k - 273.15 END AS temperature_c, "
            f"  CASE WHEN tp_m  IS NULL THEN NULL ELSE tp_m  * 1000.0 END AS precipitation_mm "
            f"FROM typed"
        )
        # LEDGER: one reject_dimension row per geom_valid = FALSE, so flag/ledger
        # agreement holds. A missing coordinate is the only geom rejection.
        bad = con.execute(
            f"SELECT source_row_key FROM {self.SILVER} WHERE geom_valid = FALSE").fetchall()
        for (key,) in bad:
            log_exclusion(
                con, source_id=self.source_id, source_row_key=key,
                column_name="geom", rule_id=self.GEOM_RULE,
                rule_desc="grid cell latitude/longitude missing",
                severity="reject_dimension", raw_value=None)

    def quality_spec(self):
        # Audited like a boundary layer: geometry validity is the single dimension.
        # Missing land-model metrics over sea are NULL by domain, NOT a reject.
        return SourceQuality(
            source_id=self.source_id,
            bronze_table=self.BRONZE,
            silver_table=self.SILVER,
            dimensions=(Dimension("geom", "geom_valid", (self.GEOM_RULE,)),),
            key_column="source_row_key",
        )
```

### Step 2 — The synthetic fixture generator script

Create `scripts/build_weather_fixture.py` (committed; **not** shipped in the wheel; not run at build/test time — mirrors `scripts/build_stats19_codebook.py`). It writes `tests/fixtures/weather/era5_land_sample.nc`. Design (keep it simple, comment in plain language):

1. **Read the committed collision fixture** `tests/fixtures/stats19/dft-road-casualty-statistics-collision-2023.csv`. For the first few rows with valid `longitude`/`latitude` and parseable `date`/`time`, compute:
   - grid node `lat0 = round(lat, 1)`, `lon0 = round(lon, 1)` (the 0.1° cell centre);
   - the collision's **local** datetime floored to the hour, then converted to the **UTC** instant with stdlib `zoneinfo.ZoneInfo("Europe/London")` (so the weather row's derived *local* hour matches the collision's local hour). Collect `(lat0, lon0, utc_hour)` triples.
2. **Build a small regular grid + hour set** covering those triples (distinct `lat0`, `lon0`, `utc_hour`, optionally ± one 0.1° cell of margin). This guarantees ≥1 collision stamps in Stage 03 by construction, deterministically.
3. **Assign deterministic values** from indices only (no randomness, no wall-clock): `t2m` in Kelvin (e.g. `285.0 + 0.1*i - 0.05*j`) and `tp` in metres (e.g. `0.0005 * (h_index + 1)`).
4. **Include exactly one all-NaN cell** (a "sea" cell: valid coordinates, `t2m`/`tp` = `numpy.nan`) so the offline test proves NaN → NULL keep-in-place without tripping invariants.
5. **Write NetCDF with real ERA5-Land structure:** dims `(valid_time, latitude, longitude)`; float `latitude`/`longitude`; a CF-decoded `valid_time` coordinate; variables `t2m` (attr `units="K"`) and `tp` (attr `units="m"`). Use `xarray.Dataset(...).to_netcdf(path)`.

   > **Fixture fidelity.** The structure above — dim/coord names, variable names `t2m`/`tp`, units `K`/`m` — must match a **real** ERA5-Land download. Verify it once at development time against a genuine sample (Copernicus credentials once, offline thereafter); record the imitated `ncdump -h` header in `tests/fixtures/weather/README.md`. The Stage-02 test then asserts the parsed schema, so the fixture is checked against real ERA5-Land shape, not hand-guessed.

6. **`--check` mode:** regenerate to a temp file and compare the **decoded** dataset (coords, variable names, units, data arrays) against the committed `.nc`; exit non-zero on any difference. Do **not** byte-compare (NetCDF embeds library metadata). Running without `--check` rewrites the committed fixture.

Also create `tests/fixtures/weather/README.md`: the file is **synthetic**, its provenance (aligned to the committed collision fixture; structure verified against real ERA5-Land), how to regenerate (`python scripts/build_weather_fixture.py`) and verify (`--check`), and the imitated `ncdump -h` header.

Generate it now:

```bash
pip install -e '.[weather]'
python scripts/build_weather_fixture.py            # writes tests/fixtures/weather/era5_land_sample.nc
python scripts/build_weather_fixture.py --check    # exits 0
```

### Step 3 — Packaging note

No `pyproject.toml` change is needed for the fixture (it lives under `tests/`, not shipped in the wheel) or the transformer (ships under the existing `packages = ["src/crossroads"]`). The `[weather]` extra was added in Stage 01.

## Testing & Verification

Weather tests require the `[weather]` extra; guard weather-code tests with `pytest.importorskip("xarray")` so the default `pip install -e '.[dev]'` suite stays green (skips). Commands:

```bash
pip install -e '.[weather]'
pytest -q tests/test_weather.py
pytest -m integration -q tests/test_weather.py
pytest -q                          # whole default suite still green
```

Create `tests/test_weather.py` with a module-level `pytest.importorskip("xarray")` and `SAMPLE_NC = tests/fixtures/weather/era5_land_sample.nc`.

### A. Silver derivation on a synthetic bronze (no .nc) — core unit test

```python
import os, shutil
import pytest
pytest.importorskip("xarray")
import crossroads
from crossroads.transformers.weather import Era5WeatherTransformer
from crossroads.quality import ensure_quality_tables

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "weather")
SAMPLE_NC = os.path.join(FIXTURES, "era5_land_sample.nc")


def _weather_bronze(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    con.execute("INSTALL icu"); con.execute("LOAD icu")
    ensure_quality_tables(con)              # so the geom ledger write path is exercised
    con.execute(
        "CREATE TABLE era5_weather_raw AS SELECT * FROM (VALUES "
        "  (TIMESTAMP '2023-06-15 13:00:00', 54.7, -1.2, 288.15, 0.0010), "   # land (BST)
        "  (TIMESTAMP '2023-06-15 13:00:00', 54.8, -1.2, 289.15, 0.0000), "   # land
        "  (TIMESTAMP '2023-06-15 13:00:00', 55.9, -3.0, CAST('NaN' AS DOUBLE), CAST('NaN' AS DOUBLE)) "  # sea
        ") AS t(valid_time, latitude, longitude, t2m, tp)")


def test_weather_silver_epsg27700_and_conversions(con):
    _weather_bronze(con)
    Era5WeatherTransformer()._derive_silver_and_ledger(con)

    assert con.execute("SELECT count(*) FROM weather").fetchone()[0] == 3   # keep-in-place

    e = con.execute("SELECT min(ST_X(geom)), max(ST_X(geom)), min(ST_Y(geom)), max(ST_Y(geom)) "
                    "FROM weather WHERE geom IS NOT NULL").fetchone()
    assert 0 <= e[0] and e[1] <= 700_000 and 0 <= e[2] and e[3] <= 1_300_000   # EPSG:27700

    row = con.execute(
        "SELECT temperature_c, precipitation_mm FROM weather "
        "WHERE grid_i = CAST(round(54.7*10) AS INTEGER) AND grid_j = CAST(round(-1.2*10) AS INTEGER)").fetchone()
    assert abs(row[0] - 15.0) < 1e-6 and abs(row[1] - 1.0) < 1e-6           # K->C, m->mm

    local = con.execute("SELECT CAST(valid_time_local AS VARCHAR) FROM weather LIMIT 1").fetchone()[0]
    assert local.startswith("2023-06-15 14:00:00")                          # 13:00 UTC -> 14:00 BST

    sea = con.execute("SELECT temperature_c, precipitation_mm, geom_valid FROM weather "
                      "WHERE grid_i = CAST(round(55.9*10) AS INTEGER)").fetchone()
    assert sea[0] is None and sea[1] is None and sea[2] is True             # NULL by domain, geom valid

    # No geom rejection for these all-coordinate rows.
    assert con.execute("SELECT count(*) FROM data_quality_log "
                       "WHERE source_id='era5_weather'").fetchone()[0] == 0
```

### B. Fixture has real ERA5-Land structure

```python
def test_sample_nc_has_era5land_structure():
    import xarray as xr
    ds = xr.open_dataset(SAMPLE_NC)
    try:
        assert "t2m" in ds.variables and "tp" in ds.variables
        assert ds["t2m"].attrs.get("units") == "K" and ds["tp"].attrs.get("units") == "m"
        assert "latitude" in ds.coords and "longitude" in ds.coords
        assert ("valid_time" in ds.coords) or ("time" in ds.coords)
    finally:
        ds.close()
```

### C. Weather-only offline build — real parse path + invariants (integration)

```python
@pytest.mark.integration
def test_weather_only_build_offline(tmp_path):
    cache = str(tmp_path / "cache"); os.makedirs(cache, exist_ok=True)
    shutil.copy(SAMPLE_NC, os.path.join(cache, "era5_land_2023.nc"))   # seed the year filename

    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [Era5WeatherTransformer()]        # weather only this test
    client.build(datasets=["era5_weather"], years=[2023])             # runs §9 invariants
    try:
        nb = client.con.execute("SELECT count(*) FROM era5_weather_raw").fetchone()[0]
        ns = client.con.execute("SELECT count(*) FROM weather").fetchone()[0]
        assert nb > 0 and nb == ns                                    # conservation
        assert client.con.execute("SELECT count(*) FROM weather_clean").fetchone()[0] == \
               client.con.execute("SELECT count(*) FROM weather WHERE geom_valid").fetchone()[0]
        b = client.con.execute("SELECT min(ST_X(geom)), max(ST_X(geom)) FROM weather "
                               "WHERE geom IS NOT NULL").fetchone()
        assert 0 <= b[0] and b[1] <= 700_000
    finally:
        client.close()
```

### D. Menu discovery + ordering — keep OUTSIDE the importorskip module

`weather.py` has no top-level heavy imports, so discovery works without the `[weather]` extra. Put this in `tests/test_registry.py` (no `importorskip`) so it runs in the default suite and proves the module imports cleanly:

```python
def test_weather_is_selectable_and_orders_before_stats19():
    from crossroads.registry import Registry
    reg = Registry()
    sel = {t.source_id: t.display_name for t in reg.selectable()}
    assert sel.get("era5_weather") == "weather"                       # shown as "weather"
    order = [t.source_id for t in reg.get_active(datasets=["stats19", "era5_weather"], years=[2023])]
    assert order.index("era5_weather") < order.index("stats19")       # weather imports first
```

### E. Regression

- `tests/test_registry.py` `selectable()` subset tests still pass (`era5_weather` added, nothing removed).
- Existing `main`/wizard console tests pick menu index `1` = `stats19` (weather sorts after stats19 in the *menu*, which is `selectable()` in source_id order: `era5_weather` < `stats19`? — see note). **Check:** `selectable()` returns registry order. If `era5_weather` now appears first in the menu, existing tests that assume `1 == stats19` break. Confirm the menu order and, if weather is first, update those console tests to the new indices (or assert by label). See Stage 03 for the console-facing verification; if this ordering shift surfaces here, fix the affected console tests in this stage.
- Default `pytest -q` (without `[weather]`) skips the `importorskip` weather tests and stays green.

> **Menu-order caveat (resolve it here).** `Registry.selectable()` returns transformers in `_discover` order, which is the `source_id` sort — so with weather added the selectable menu becomes `era5_weather` (`e`) **before** `stats19` (`s`): i.e. `1. weather`, `2. stats19`. This **changes existing wizard menu indices**. Options: (a) accept it and update the console tests that hardcode `"1"`→stats19 to the new order; (b) sort `selectable()` by `display_name` instead of `source_id` so the label order is stable. Recommended: **(a)** — keep `selectable()` as-is (source_id order, deterministic) and update the affected console tests, because the menu order was never a contract. Whichever you choose, make the default suite green in this stage and note the decision in the End State.

### Stage ship-readiness checklist

- [ ] `python -c "import crossroads.transformers.weather"` succeeds **without** the `[weather]` extra (no top-level heavy import).
- [ ] `python scripts/build_weather_fixture.py --check` exits 0.
- [ ] With `[weather]`: `pytest -q tests/test_weather.py` and `pytest -m integration -q tests/test_weather.py` green.
- [ ] `pytest -q` (default suite) green with and without the `[weather]` extra.
- [ ] `get_active(datasets=["stats19","era5_weather"], years=[2023])` places `era5_weather` before `stats19`.

## End State / Handoff (the contract)

- `src/crossroads/transformers/weather.py` defines `Era5WeatherTransformer` (`source_id="era5_weather"`, `display_name="weather"`, `is_active` requires years, `depends_on` empty). It builds `era5_weather_raw` (bronze) → `weather` (silver, EPSG:27700 `geom`, `grid_i`/`grid_j`, `valid_time_utc`, `valid_time_local`, `temperature_c`, `precipitation_mm`, `geom_valid`) → `weather_clean` (gold), records source rows, and is audited (geom dimension). **It stamps nothing.**
- A weather-only offline build passes all §9 invariants; `weather` appears in the wizard menu as `weather` and, when both are active, `get_active` orders `era5_weather` before `stats19`.
- `scripts/build_weather_fixture.py` (+ `--check`) and `tests/fixtures/weather/era5_land_sample.nc` (+ README) exist, aligned to the committed collision fixture.
- Any menu-index shift from adding weather has been resolved and the default suite is green. Note which option (a/b) you took.
- Stage 03 may assume the `weather` silver carries `grid_i`, `grid_j`, `valid_time_local`, `temperature_c`, `precipitation_mm`, and that `era5_weather` imports before `stats19`.

## Failure Modes & Rollback

- **`always_xy` omitted in `ST_Transform`.** Lon/lat get swapped and centroids leave the BNG envelope. Guardrail: the EPSG:27700 assertions in tests A/C.
- **ICU not loaded.** `AT TIME ZONE` errors; `transform_and_load` loads it; the DST assertion in test A pins the conversion.
- **`.nc` coordinate named `time` not `valid_time`.** `_load_bronze` tolerates both; test B asserts one is present.
- **Sea NaN tripping the reject ceiling.** Prevented by design (metrics NULL over sea are not a dimension; only `geom` is audited). Test A asserts the sea cell is `geom_valid = TRUE`, NULL metrics, no ledger row.
- **`[weather]` extra missing.** Module imports (lazy) but `transform_and_load` raises `ImportError` on first heavy use; weather tests `importorskip`, so the default suite is unaffected.
- **Rollback:** delete `weather.py`, `scripts/build_weather_fixture.py`, `tests/test_weather.py`, `tests/fixtures/weather/`, and the Stage-02 registry test; revert any console-test index changes. The registry re-discovers only the three prior sources; Stage 01 seams remain (harmless). Suite returns to the Stage 01 green state.
