# Meteorological Grid Integration (ERA5-Land) — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Add a fourth data source — Copernicus **ERA5-Land** gridded reanalysis weather — to Crossroads-UK. Weather loads as an ordinary dataset into its own queryable `weather` table; STATS19 then *optionally* stamps each collision with the matching hourly temperature and precipitation, exactly as it already stamps `lad_code`/`ctyua_code` from the boundary tables. A small dependency resolver makes the wizard and programmatic builds import `weather` before `stats19` when both are selected.

---

## Context & Objective

### What exists today

Crossroads-UK is a provider-plugin ETL pipeline (spec §4). `client.build()` opens one DuckDB connection, loads the DuckDB **Spatial** extension, discovers every concrete `BaseTransformer` in `crossroads.transformers`, filters to the active ones, and runs each transformer's `extract` then `transform_and_load`. Three sources exist:

- `src/crossroads/transformers/spatial.py` — ONS LAD + CTYUA boundary tables (`lad_boundaries`, `ctyua_boundaries`), EPSG:27700, `user_selectable = False` (always-on infrastructure).
- `src/crossroads/transformers/stats19.py` — DfT collision/vehicle/casualty. Builds the `collisions` silver (EPSG:27700 `geom`, naive-local `datetime_local`, `longitude`/`latitude` DOUBLE, `source_row_key`). **Crucially, its `_spatial_stamp` already reads the boundary tables and stamps `lad_code`/`ctyua_code` onto its own `collisions` table**, guarded by a `_table_exists` check that warns-and-skips if a boundary table is absent. `is_active` requires `years`. `user_selectable = True`.
- `src/crossroads/console.py` — the wizard. It **self-discovers** selectable datasets (`available_datasets()` → `Registry().selectable()`) and shows them numbered; picks flow through as `build(datasets=[...], years=[...], boundary_mode=...)`. A new `user_selectable` transformer appears in the menu automatically — no console edit.

The quality engine (`src/crossroads/quality.py`, spec §9) audits every source that returns a `SourceQuality` from `quality_spec()`: conservation, flag/ledger agreement, and a per-dimension reject-rate ceiling (default 5%). `client.build()` runs these at the end and halts on violation.

Registry discovery order is currently a plain sort by `source_id` (`src/crossroads/registry.py`, `_discover`). `client.build()` iterates `registry.get_active(**kwargs)` in that order.

**The problem this plan solves:** for STATS19 to stamp weather onto collisions, the `weather` table must already exist when `stats19` runs — i.e. `weather` must import **before** `stats19`. Today the source-id sort happens to place `era5_weather` before `stats19` (`e` < `s`), but relying on that alphabetical accident is fragile. We make the ordering **explicit and robust**: a source declares what it optionally depends on, and the registry resolves a correct import order.

The engine installs only `duckdb` (`pyproject.toml`); nothing for NetCDF or the Copernicus API.

> **Note on the `include_weather` flag.** The spec §8 example and the `BaseTransformer.is_active` docstring mention `include_weather=True`. That mechanism is **superseded**: weather is an ordinary *selectable dataset*, activated by the wizard menu / a `datasets=["era5_weather", ...]` argument plus `years`, like `stats19`. Do **not** add an `include_weather` build flag.

### What changes

1. **An optional dependency mechanism.** `BaseTransformer` gains a `depends_on` tuple of source-ids (default empty). The registry topologically sorts the *active* transformers so each runs after any of its dependencies that are also active, breaking ties by `source_id` for determinism, and **raises `DependencyCycleError` on a declared cycle** (fail loud — a silently under-enriched build is worse; actually *handling* a cycle by re-running the idempotent optional step is a deferred future feature). Optional = an edge to an inactive/unselected source is simply dropped; the dependent still runs and guards at ETL time.
2. **A new plain source** `src/crossroads/transformers/weather.py` — `Era5WeatherTransformer`. It downloads ERA5-Land NetCDF (via cdsapi, real path only), builds its own `weather` grid table (bronze/silver/gold) with EPSG:27700 cell centroids, `valid_time_utc` + derived `valid_time_local`, `temperature_c`, `precipitation_mm`, and an integer grid index; it is audited by the quality engine. It stamps nothing.
3. **STATS19 optionally consumes weather.** `stats19` declares `depends_on = ("era5_weather", "ons_lad", "ons_ctyua")` (making its existing implicit ordering explicit) and gains a `_weather_stamp` step — the exact twin of `_spatial_stamp`: if a `weather` table exists, join valid collisions to it (by grid cell + local hour) and fill `temperature_c`/`precipitation_mm` on `collisions`; if absent, warn and skip.
4. **An optional `[weather]` dependency extra** (`cdsapi`, `xarray`, `netCDF4`), lazy-imported inside `weather.py` only.
5. **A committed `scripts/build_weather_fixture.py`** that emits a tiny synthetic ERA5-Land-shaped `.nc`, aligned to the committed collision fixture, for offline tests.

### The goal

`crossroads` lists **weather** as a selectable dataset. Selecting it with `stats19` yields a DuckDB file where `collisions` carries the temperature/precipitation at each collision's cell and hour, and the `weather` grid table is queryable; the resolver guarantees `weather` imports first. Selecting weather alone yields just the weather grid. Selecting stats19 alone behaves exactly as today. All §9 invariants hold. Proven by offline automated tests — no Copernicus credentials, no network.

---

## Approach / Architecture (shared by all stages)

**Ordering — an explicit optional dependency.** A transformer declares `depends_on = (<source_id>, ...)`: "if any of these sources is also active this build, run me after it." The registry, after filtering to the active set, topologically sorts by these edges (edges to inactive sources dropped), choosing the smallest `source_id` among ready nodes at each step for determinism (spec §2). With no `depends_on` anywhere, this reproduces today's source-id order exactly.

**Cycles — fail loud now, real handling deferred.** A declared cycle cannot be topologically ordered, so the resolver raises `DependencyCycleError` with a message that says cyclic optional dependencies are *not supported yet*. We fail rather than degrade because a best-effort single pass would silently under-enrich one direction of the cycle, and a warning is too easy to miss. *Handling* a cycle is a legitimate future feature — and a clean one, because optional enrichment steps are **idempotent** (a pure function of current table state — e.g. STATS19's weather stamp is an `UPDATE`) and **existence-guarded**, so the natural implementation is to re-run the optional step to a fixpoint. It is **deferred**: today there are zero cycles and one optional edge, so building the multi-pass/tracking machinery now would be speculative. The exception branch is inert until a source actually declares a cycle.

**Enrichment — consumer-pull, reusing the `lad_code` pattern.** Weather is a plain source that only loads its own tables. STATS19 — which *owns* `collisions` — does the stamping in its own `transform_and_load`, reading the already-loaded `weather` table. This is the same shape as the existing `_spatial_stamp` (which reads `lad_boundaries`/`ctyua_boundaries` and writes `lad_code` onto `collisions`), so there is **no cross-source table mutation** and **no new pattern**: weather never writes to `collisions`; STATS19 writes only to its own table. The `_table_exists` guard makes the dependency *optional* — collisions simply carry NULL weather when no `weather` table was built (just as they carry NULL `lad_code` in a boundary-less build).

**Spatial match — reproject for storage, join by grid index.** Weather grid centroids are reprojected once to EPSG:27700 (`ST_Transform(ST_Point(lon,lat), 'EPSG:4326','EPSG:27700', always_xy := true)`) and stored as `geom` (spec §3A single-CRS). The *join*, however, uses the deterministic integer index of the native 0.1° ERA5-Land grid: `grid_i = CAST(round(latitude*10) AS INTEGER)`, `grid_j = CAST(round(longitude*10) AS INTEGER)`. A collision's cell is found by reprojecting its `geom` back to lon/lat (`ST_Transform(..., 'EPSG:27700','EPSG:4326', always_xy := true)`) and rounding the same way — an O(collisions) hash join, not an O(collisions × cells) spatial scan, and fully deterministic.

**Temporal match — UK local hour (spec §3B).** Weather is UTC-native: bronze carries the raw instant; silver stores `valid_time_utc` (naive TIMESTAMP) and derives `valid_time_local` via the DuckDB **ICU** extension: `(valid_time_utc AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London'` (DST-aware; verified 13:00 UTC → 14:00 BST in summer, 13:00 GMT in winter). Collisions are local-native (`datetime_local`). STATS19's stamp joins on `date_trunc('hour', datetime_local) = date_trunc('hour', valid_time_local)`. Because `valid_time_local` is materialized in the weather silver, the stamp itself needs no ICU — only the weather build does.

**Land-model NULLs are not rejections.** ERA5-Land is a land-only model; a UK bounding box contains sea cells whose metrics are missing (NaN). These are kept 1:1 in the weather silver with `temperature_c`/`precipitation_mm` set to `NULL` **by domain** and are **not** a reject dimension. Weather's only audited dimension is `geom` (centroid validity), mirroring `spatial.py`. This keeps conservation honest (`count(bronze) == count(silver)`) without the sea NaNs tripping the 5% ceiling.

**Alternatives rejected:**
- *Weather runs last and stamps collisions itself (enricher-push, via a bridge table + capability marker)* — a new pattern that has weather write into another source's territory. Rejected for the simpler consumer-pull model that reuses the established `_spatial_stamp` shape.
- *Re-run-to-fixpoint cycle resolution, built now* — deferred, not rejected outright. Re-running an *idempotent* optional step (which ours is) is deterministic and safe, so this is the right approach if a real cyclic-enrichment case ever appears; but building the multi-pass/tracking machinery for a system with zero cycles and one optional edge is speculative. Until then, a cycle fails loud (`DependencyCycleError`) rather than degrading silently.
- *Rely on the `era5_weather` < `stats19` alphabetical accident for ordering* — fragile and intention-hiding. Rejected for an explicit `depends_on` declaration.
- *Resolve ordering in the wizard* — the programmatic `client.build()` (spec §8) bypasses the wizard and would get the wrong order. Rejected: resolution lives in the registry so every entry point benefits.
- *Spatial nearest-neighbour join (ST_Distance / cell polygons)* — O(collisions × cells) and needlessly complex; the regular 0.1° grid makes an integer-index join exact and cheap.
- *Hard `cdsapi`/`xarray` core dependencies* — burdens every stats19-only user with numpy/pandas/HDF5. Rejected for an opt-in `[weather]` extra (spec §2).

**Data flow (stats19 + weather build):**
`build(datasets=["stats19","era5_weather"], years=[2023])` → `registry.get_active` filters (is_active + dataset gate) then **resolves order** via `depends_on`: `era5_weather`, `ons_ctyua`, `ons_lad`, `stats19` (weather and boundaries before stats19) → weather builds `era5_weather_raw`→`weather`→`weather_clean` and is recorded/audited → boundaries load → stats19 builds `collisions`, stamps `lad_code`/`ctyua_code` (existing), then `_weather_stamp` fills `temperature_c`/`precipitation_mm` from the `weather` table → build-end §9 invariants run over all sources → done.

---

## Cross-Cutting Constraints (every stage follows these)

- **Provider-plugin purity (spec §4).** Core engine files name no specific source. The registry gains a generic `depends_on`-driven topological sort; it never mentions weather. A consumer naming its optional dependency in its *own* file (as `stats19` already references the boundary tables) is fine.
- **Single CRS (spec §3A).** All geometry EPSG:27700, reprojected once at ingestion (`always_xy := true`), never at query time.
- **Temporal rules (spec §3B).** Naive `TIMESTAMP` columns only (never `TIMESTAMP WITH TIME ZONE`). A UTC-native source carries `*_utc` **and** a derived `*_local`; `*_local` is the cross-source join surface. Mandatory zone suffix on every temporal column.
- **Zero unaccounted loss / keep-in-place (spec §2, §9).** Weather's silver is 1:1 with its bronze; bad/missing values are NULLed and (where a real defect) flagged + logged, never deleted. `count(bronze) == count(silver)`.
- **Reproducibility & determinism (spec §2).** Same version + parameters → structurally identical DB. The resolver's tie-break and cycle rejection keep import order deterministic. All conversions deterministic. The synthetic fixture is regenerated by a committed `--check` script.
- **Idempotent rebuild.** Each transformer recreates the tables it owns with `CREATE OR REPLACE`; STATS19's `_weather_stamp` is an `UPDATE` on a freshly `CREATE OR REPLACE`d `collisions` (same shape as `_spatial_stamp`). The engine resets shared audit rows per source before it runs.
- **No new *core* runtime dependency.** NetCDF/Copernicus libraries live in an optional `[weather]` extra, imported lazily inside `weather.py` (no top-level heavy import, so the module imports for discovery without the extra).
- **Offline, deterministic tests.** The default `pytest` suite touches no network and does not require the `[weather]` extra (weather tests `importorskip`). Real NetCDF parsing runs against a committed synthetic `.nc`.
- **Licensing.** Crossroads is MIT. Do **not** copy or adapt any GPL-licensed reference package.
- **Git discipline (CLAUDE.md).** Never stage or commit.
- **Keep it simple; comment in plain language (CLAUDE.md).** Match the docstring/comment density of `spatial.py` and `stats19.py`.

---

## Stage Map (do in order)

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Optional dependency resolution | Add `depends_on` to the transformer contract and a topological resolver (deterministic tie-break; cycle → `DependencyCycleError`, message "not supported yet") to the registry's active-ordering; make STATS19's existing ordering explicit by declaring its boundary dependencies. | `BaseTransformer` exposes `depends_on` (default `()`); `registry.get_active` returns the active set in dependency order and raises `DependencyCycleError` on a declared cycle; `stats19.depends_on` names the boundary sources. No behaviour change (order identical to today); full suite green. | — | `01-dependency-resolution.md` |
| 02 | Weather grid source | `Era5WeatherTransformer` builds its own queryable `weather` grid table end-to-end (cdsapi extract lazily; NetCDF→bronze via xarray lazily; silver with 27700 centroid, `valid_time_utc/local`, °C/mm, grid index, `geom_valid`; `weather_clean` gold; `SourceQuality` audit). `[weather]` extra; `build_weather_fixture.py` + synthetic `.nc`. Weather stamps nothing. | A weather-only build (`datasets=["era5_weather"], years=[2023]`) offline produces a populated `weather` table with EPSG:27700 centroids, all §9 invariants passing; `weather` appears in the wizard menu. | 01 | `02-weather-grid-source.md` |
| 03 | STATS19 weather stamping & wiring | `stats19` declares `depends_on` on `era5_weather`; add `_weather_stamp` (existence-guarded twin of `_spatial_stamp`) filling `temperature_c`/`precipitation_mm` on `collisions`. Verify weather auto-appears in the wizard and imports first. Full offline integration tests (stats19+weather and weather-alone). | A `datasets=["stats19","era5_weather"], years=[2023]` build offline stamps ≥1 collision with correct hourly weather, all §9 invariants hold; a stats19-only build is unchanged; a weather-only build builds the grid and skips stamping. | 02 | `03-stats19-weather-stamping.md` |

---

## Global Testing & Ship

Real, offline integration tests prove the feature ships (manual testing may not occur). Commands:

```bash
pip install -e '.[dev]'            # default suite (weather tests importorskip -> skipped)
pytest -q                          # full default suite, offline, green

pip install -e '.[weather]'        # add cdsapi + xarray + netCDF4 to actually run weather code/tests
pytest -q                          # weather unit tests now run too
pytest -m integration -q           # offline real-build integration tests (incl. weather e2e)
```

- **Stage 01** attaches: resolver ordering test (edges honoured, deterministic tie-break), cycle-detection test (raises `DependencyCycleError`), and unchanged behaviour of the existing suite.
- **Stage 02** attaches: weather grid unit tests (silver EPSG:27700, `valid_time_local` DST, °C/mm, keep-in-place NaN cell) + a weather-only offline integration build; the fixture `--check` script.
- **Stage 03** attaches: the end-to-end stats19+weather offline integration test (≥1 collision stamped, invariants hold), a stats19-only regression (unchanged), a weather-only build (grid only, no stamp), and a menu-discovery/ordering assertion.

Ship-readiness for the whole feature (end of Stage 03):

```bash
python -c "from crossroads.registry import Registry; r=Registry(); print([t.source_id for t in r.get_active(datasets=['stats19','era5_weather'], years=[2023])])"
# -> era5_weather (and boundaries) sort BEFORE stats19
python -c "from crossroads.transformers.stats19 import Stats19Transformer as S; print(S().depends_on)"
# -> ('era5_weather', 'ons_lad', 'ons_ctyua')
```

---

## Open Questions / Risks (cross-cutting)

- **Real ERA5-Land download volume.** A full year of hourly UK ERA5-Land is large; the real `extract` requests per-year NetCDF over a UK bounding box and caches it. Inherent to the source (spec §5 Phase 4); offline tests never download.
- **cdsapi credentials.** Real downloads need a Copernicus `~/.cdsapirc`. Tests never touch cdsapi (cache seeded + `importorskip`). The synthetic fixture's *structure* is verified against one real ERA5-Land sample at development time (Stage 02).
- **DST fall-back ambiguity.** The autumn hour that occurs twice in local time is matched by local-hour value only; a collision in that hour may match either instance. Documented limitation (spec §3B).
- **ERA5-Land coordinate/variable naming across CDS vintages.** The parser tolerates both `valid_time` and `time`; variables `t2m` (2 m temperature, K) and `tp` (total precipitation, m). Documented in Stage 02.
- **Weather columns on `collisions` in weather-less builds.** `collisions` carries `temperature_c`/`precipitation_mm` (NULL) even when weather was not built, mirroring how it always carries `lad_code`. Accepted for schema stability and consistency with the existing stamp.
