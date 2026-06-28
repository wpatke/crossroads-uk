# Spatial Infrastructure & Boundaries — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

> **Superseded detail (shapefile → GeoJSON):** This document describes committing ONS boundary
> samples as shapefiles (`.shp/.shx/.dbf/.prj`) and reading them via `ST_Read`. The implemented
> design instead downloads and commits **GeoJSON** (ONS publishes ArcGIS FeatureServer GeoJSON, not
> shapefile ZIPs); the shapefile fixtures have been removed as unused. Treat shapefile-specific
> steps below as historical — the GeoJSON equivalent is what shipped in `spatial.py`.

Load the DuckDB **Spatial Extension** as foundational infrastructure, then ingest ONS **Local
Authority District (LAD)** and **County/Unitary Authority (CTYUA)** boundaries — as the
EPSG:27700 geometric base layer everything else joins against — through the Step 2 bronze →
silver → gold quality model, with bounding-box **R-Tree** spatial indices and **temporal
boundary-drift** support (`valid_from`/`valid_to`, selectable per build).

## Context & Objective

**What exists today (Steps 1 & 2, already merged).**
- `src/crossroads/transformers/base.py` — the `BaseTransformer` ABC: a `source_id` property,
  `is_active(**kwargs)`, `extract(cache_dir, **kwargs)`, `transform_and_load(con, cache_dir)`, and a
  **concrete** `quality_spec()` returning `SourceQuality | QualityExemption | None` (inherited default
  `None` = "undecided").
- `src/crossroads/registry.py` — `Registry` discovers **concrete** `BaseTransformer` subclasses in the
  `crossroads.transformers` package via `pkgutil` + `inspect` (abstract classes are skipped;
  `obj.__module__ == module_name` so only classes *defined* in that module count), sorted by `source_id`.
  **A single module may define multiple concrete transformer classes — all are discovered.**
- `src/crossroads/client.py` — `init_engine(...)` → `Client`; `Client.build(**kwargs)` opens a DuckDB
  connection (`self.con`), calls `quality.ensure_quality_tables`, then per active transformer
  `reset_source_audit` → `extract` → `transform_and_load`, then `resolve_quality_specs` (coverage gate)
  → `run_invariants` (fatal on violation). With zero transformers, `build()` is a clean no-op.
- `src/crossroads/quality.py` — the full Step 2 quality engine: `Dimension` / `SourceQuality` /
  `QualityExemption` dataclasses, `DEFAULT_REJECT_CEILING = 0.05`, `create_clean_view`, the shared audit
  tables + writers (`record_source_rows`, `log_exclusion`, `quarantine_row`, `record_exemption`,
  `reset_source_audit`), the three invariants (`check_conservation`, `check_flag_ledger_agreement`,
  `check_reject_rates`) + `check_schema_contract` + `run_invariants`, the `resolve_quality_specs`
  coverage gate, the exception hierarchy, and the module flag
  **`UNDECIDED_QUALITY_SPEC_IS_FATAL = False`** (warn-only interim).
- `tests/conftest.py` — a `con` fixture (fresh in-memory DuckDB, closed after each test).
- `tests/test_quality.py`, `tests/test_client.py`, `tests/test_registry.py`, `tests/test_package.py` — all green.
- `pyproject.toml` — package `crossroads-uk`, import `crossroads`, Hatchling, `requires-python>=3.11`,
  runtime dep **`duckdb>=1.0`** (installed locally as 1.5.4), dev dep `pytest`.

There is **no** `transformers/spatial.py` yet, **no** real transformer at all, and `build()` does not
load the spatial extension. A placeholder file
`docs/plans/003_spatial_infrastructure/00-PENDING-carryover-from-002.md` records obligations Step 2
deferred to Step 3 (see "Carryover from Step 2" below); this plan absorbs them and the placeholder is
deleted in Stage 02.

**Environment facts verified for this plan (DuckDB 1.5.4 + Spatial in `.venv`):**
- `INSTALL spatial; LOAD spatial` succeeds; `ST_Transform(geom, 'EPSG:27700', 'EPSG:4326')` reprojects
  correctly; `ST_Read('file.shp')` (GDAL) reads shapefiles; `ST_Point`, `ST_AsText`, `ST_X`/`ST_Y`,
  `ST_IsValid`, `ST_Extent` exist; `CREATE INDEX ... USING RTREE (geom)` builds an R-Tree.
- **No new Python dependency is required** — shapefile reading is in-database via `ST_Read`, and
  downloading uses the **standard library** (`urllib.request`, `zipfile`). Do **not** add `geopandas`,
  `requests`, `fiona`, or `pyogrio`.

**What changes (this step).**
1. `client.py` loads the spatial extension on every build connection (Stage 01).
2. A new `src/crossroads/transformers/spatial.py` defines an **abstract** `_BoundaryTransformer`
   (shared download / `ST_Read` / validate / load logic) plus **two concrete** subclasses,
   `LADBoundaryTransformer` (`source_id = "ons_lad"`) and `CTYUABoundaryTransformer`
   (`source_id = "ons_ctyua"`). Each is the **first real consumer of the Step 2 quality engine** and
   returns its own `SourceQuality` (Stages 02–04).
3. Boundary data flows bronze (`ons_lad_raw`, `ons_ctyua_raw`) → silver (`lad_boundaries`,
   `ctyua_boundaries`, keep-in-place 1:1, `geom_valid` dimension) → gold views (`lad_boundaries_clean`,
   `ctyua_boundaries_clean`). Geometry is **EPSG:27700** (ONS BGC native; not reprojected, validated by
   coordinate-range assertion).
4. **Temporal boundary drift** (spec §3C): silver carries `valid_from DATE` / `valid_to DATE`
   (`valid_to = NULL` means "current"); a `build(boundary_mode=...)` kwarg selects
   `"snapshot"` (default — latest vintage only) or `"temporal"` (all configured vintages, each stamped
   with its validity window). The `source_row_key` is the composite `"<code>|<vintage>"` so the same
   LAD code across vintages stays unique (Stage 03).
5. Bounding-box **R-Tree** indices are built on each boundary silver table inside `transform_and_load`
   (Stage 04).
6. **Carryover from Step 2 is actioned** (Stage 02): flip `UNDECIDED_QUALITY_SPEC_IS_FATAL` to `True`,
   rewrite the interim warn-test to assert the fatal path, and delete the carryover placeholder file.

**The goal.** After Step 3, `cr.init_engine(...).build(years=...)` produces a queryable DuckDB file
whose LAD and CTYUA boundary tables are populated from real ONS BGC shapefiles, in EPSG:27700, audited
by the Step 2 invariants, indexed by R-Trees, and (optionally) temporally sliced — the geometric base
layer Step 4's collision point-in-polygon joins will consume.

## Approach / Architecture

### Why two concrete classes in one module (locked decision)
The Step 2 `SourceQuality` audits exactly **one** bronze→silver pair, but Phase 1 produces **two**
boundary tables. Rather than change the Step 2 contract, `spatial.py` defines an abstract
`_BoundaryTransformer` holding all shared logic and **two concrete subclasses** (LAD, CTYUA). The
registry discovers both (multiple concrete classes per module is supported; the abstract base is skipped
by `inspect.isabstract`). Each subclass has its own `source_id`, table names, ONS attribute-column
mapping, vintage registry, gold-view name, and `SourceQuality` — so each is cleanly, independently
audited with **no edit to `quality.py`'s contract** (only the mandated flag flip). Alternatives rejected:
*extend `quality_spec()` to return a list* (edits the Step 2 contract more than the carryover mandates;
defer until Step 4 genuinely needs it); *one unified `boundaries` table with a type discriminator*
(mixes two geographic grains, and a composite key would still be needed).

### Spatial extension loaded centrally (locked decision)
`Client.build()` runs `INSTALL spatial; LOAD spatial` on the connection immediately after `connect`,
before the transformer loop. It is foundational (spec §5 Phase 1), idempotent, cheap, and **names no
source**, so provider-plugin purity (spec §4) holds. Loading once serves boundaries now and weather
(Step 6) later. `INSTALL` needs network only on first use, then is cached under `~/.duckdb/extensions`.

### Boundary ingestion data flow (per concrete transformer)
1. **`extract(cache_dir, **kwargs)`** — resolve which vintages to fetch from `boundary_mode`
   (`"snapshot"` → latest only; `"temporal"` → all in the class's vintage registry). For each needed
   vintage, if its shapefile is **not already cached**, download the ONS BGC zip via `urllib.request`
   and unzip the `.shp/.shx/.dbf/.prj` into `cache_dir` (offline-friendly: a pre-seeded cache skips
   download — this is how tests run without network). Record the resolved vintage list on the instance
   (`self._vintages_to_load`) — the engine always calls `extract` immediately before
   `transform_and_load` on the same instance, so this hand-off is reliable; document the dependency.
2. **`transform_and_load(con, cache_dir)`** —
   a. **Bronze:** `CREATE OR REPLACE TABLE <src>_raw AS` reading every resolved vintage shapefile with
      `ST_Read(path)` and `UNION ALL`, tagging each row with its `vintage`. Bronze is a faithful copy
      (original ONS attribute columns + raw `geom` + `vintage`).
   b. **Silver (keep-in-place 1:1):** `CREATE OR REPLACE TABLE <type>_boundaries AS` selecting from
      bronze with: `source_row_key = code || '|' || vintage`; typed `area_code` / `area_name` from the
      vintage's ONS columns; `geom` (already EPSG:27700); `geom_valid = (geom IS NOT NULL AND
      ST_IsValid(geom))`; `valid_from` / `valid_to` from the vintage registry. Every bronze row appears
      once (no filtering) — bad geometry is flagged, never dropped.
   c. **Ledger:** for each silver row with `geom_valid = FALSE`, write a `data_quality_log` row
      (`rule_id = 'ons.geom.invalid'`, `severity = 'reject_dimension'`) so flag/ledger agreement holds.
   d. **Conservation accounting:** `record_source_rows(con, source_id, <feature count read>)`.
   e. **Gold:** `create_clean_view(con, "<type>_boundaries_clean", "<type>_boundaries", ["geom_valid"])`.
   f. **R-Tree (Stage 04):** `CREATE INDEX ... USING RTREE (geom)` on the silver table.
3. **`quality_spec()`** — returns `SourceQuality(source_id, bronze_table, silver_table,
   dimensions=(Dimension("geom", "geom_valid", ("ons.geom.invalid",)),), key_column="source_row_key")`.

### EPSG:27700 handling
ONS **BGC (Generalised Clipped) UK** boundaries are published natively in EPSG:27700, so boundaries are
**not reprojected** — they are loaded as-is and *verified*. DuckDB `GEOMETRY` stores no SRID, so
"verify EPSG:27700" means asserting coordinates fall inside the British National Grid envelope
(roughly easting `0–700000`, northing `0–1300000`) via `ST_X`/`ST_Y` / `ST_Extent`, not a lat/lon range.
(Reprojection with `ST_Transform` is the stats19/weather path in later steps, not boundaries.)

### Temporal boundary drift (spec §3C)
Silver always carries `valid_from`/`valid_to`. `boundary_mode="snapshot"` (default) loads only the
latest vintage with `valid_from = <vintage effective date>`, `valid_to = NULL` ("current"). `"temporal"`
loads every vintage in the class's static vintage registry, each stamped with its `[valid_from, valid_to)`
window (`valid_to = NULL` for the latest). The composite `source_row_key = "<code>|<vintage>"` keeps the
same area code unique across vintages, so keep-in-place conservation and flag/ledger agreement hold.
Step 3 builds this temporally-aware base layer; the actual range join of collision points against
validity windows is **Step 4** (no collisions exist yet).

### Build integration (no loss of provider-plugin purity)
`client.py` gains exactly one generic line (load spatial). It still never names a source. `boundary_mode`
rides in the same `**kwargs` already forwarded to `is_active`/`extract`; transformers accept `**kwargs`
and ignore unknown keys, so non-spatial builds are unaffected.

### Carryover from Step 2 (actioned in Stage 02)
Per `00-PENDING-carryover-from-002.md`: (1) `spatial.py` returns a real `SourceQuality` — satisfied by
the LAD/CTYUA `quality_spec()`s; (2) flip `UNDECIDED_QUALITY_SPEC_IS_FATAL` to `True` in `quality.py`;
(3) rewrite `test_build_with_undecided_source_warns_in_interim` to assert `build()` raises
`UndecidedQualitySpecError` for an undecided active source (rename to
`test_build_with_undecided_source_is_fatal`); (4) full `pytest` green; (5) **delete the placeholder
file**. These land in the same stage that introduces the first audited real transformer, so the flip is
safe (no active source remains undecided).

## Cross-Cutting Constraints (every stage follows these)
- **No new dependencies.** `duckdb` + `pytest` only; downloads use stdlib `urllib.request`/`zipfile`;
  shapefiles read via in-DB `ST_Read`. Do not edit `pyproject.toml` dependencies.
- **One module:** all boundary code lives in `src/crossroads/transformers/spatial.py` (spec §7).
- **Provider-plugin purity (spec §4):** `client.py` and `registry.py` never name a concrete source. The
  one `client.py` change (load spatial) is a generic capability, not a source.
- **Keep-in-place (spec §9):** never delete or filter a boundary row; bad geometry is flagged
  (`geom_valid = FALSE`) and logged. Bronze == silver row counts (1:1).
- **Aggregate SQL, not Python row loops** (spec §9): bronze/silver are built with set-based
  `CREATE OR REPLACE TABLE AS SELECT`; the only per-row Python is writing ledger rows for the (rare)
  invalid geometries.
- **EPSG:27700 once, never at query time** (spec §3A): boundaries load natively in 27700; no query-time
  reprojection.
- **Determinism / reproducibility (spec §2):** same version + same fixtures + same params → identical
  tables. No wall-clock/randomness in logic; the only timestamp is the DB-side `ingested_at` default
  (provenance, never asserted). Vintage→date mappings are static constants in `spatial.py`.
- **SQL identifier interpolation:** table/column/vintage identifiers come from code-controlled
  constants/manifests (trusted) and may be interpolated; row VALUES are always bound with `?`. Comment
  the trust boundary, matching `quality.py`.
- **Idempotent re-build:** each transformer recreates its OWN bronze/silver with `CREATE OR REPLACE`
  (the engine resets shared audit rows). A second `build()` against the same on-disk DB must not double
  rows or break invariants.
- **Style:** plain-language comments, simple code (CLAUDE.md); match the comment density of `client.py`
  / `quality.py`.
- **Git discipline:** never stage or commit without explicit user permission (CLAUDE.md).

## Stage Map (sequential — do in order)

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Spatial extension (central load) | `Client.build()` runs `INSTALL spatial; LOAD spatial` on the connection before the transformer loop. | After any `build()`, spatial functions (`ST_Transform`, `ST_Read`, `ST_Point`, `ST_IsValid`) are available on `client.con`; all existing Step 1/2 tests still green. `pytest` green. | Step 2 | `01-spatial-extension.md` |
| 02 | Boundary ingestion (snapshot) | `spatial.py`: abstract `_BoundaryTransformer` + `LAD`/`CTYUA` concrete classes; bronze→silver→gold; EPSG:27700 verified; `geom_valid` dimension; real `SourceQuality`. Action the Step 2 carryover (flip flag, rewrite interim test, delete placeholder). Commit a tiny real ONS BGC sample. | A `build()` over the committed sample populates `lad_boundaries` + `ctyua_boundaries` (snapshot/latest vintage), geometry verified in the BNG envelope, gold views exist, and **all three Step 2 invariants pass via the real engine**. `UNDECIDED_QUALITY_SPEC_IS_FATAL is True`; carryover file deleted. `pytest` green. | Stage 01 | `02-boundary-ingestion.md` |
| 03 | Temporal slicing | `valid_from`/`valid_to` populated across vintages; `boundary_mode="snapshot"\|"temporal"` build kwarg; composite `source_row_key`; second committed vintage. | `build(boundary_mode="temporal")` loads ≥2 vintages per boundary type with correct validity windows; `"snapshot"` (default) loads only the latest; conservation/agreement/reject-rate hold in both modes; same area code across vintages stays unique. `pytest` green. | Stage 02 | `03-temporal-slicing.md` |
| 04 | R-Tree spatial indices | `CREATE INDEX ... USING RTREE (geom)` on each boundary silver table inside `transform_and_load`; existence verified. | After `build()`, an R-Tree index exists on `lad_boundaries.geom` and `ctyua_boundaries.geom` (visible in `duckdb_indexes()`); a same-file re-build is idempotent; all Step 2 invariants still pass. `pytest` green. | Stage 03 | `04-spatial-indices.md` |

## Global Testing & Ship
All tests are **real and runnable** (manual testing is not relied upon). A new `tests/test_spatial.py`
holds the boundary tests and reuses `tests/conftest.py`'s `con` fixture and `crossroads.init_engine()`.
Real ONS BGC sample fixtures live under `tests/fixtures/ons/` (committed `.shp/.shx/.dbf/.prj` sets,
trimmed to a handful of polygons). From the repo root with the venv active:

```bash
source .venv/bin/activate
python -m pytest -q
```
Expected at the end of **every** stage: all tests pass, zero failures/errors (including pre-existing
Step 1/2 tests).

**End-to-end ship proof for Step 3** attaches to **Stage 02** (and is extended by 03/04): a real
`build()` ingests the committed ONS sample end-to-end through bronze → silver → gold, geometry is
confirmed EPSG:27700 (BNG-envelope assertion), and the build passes the Step 2 invariants — proving the
boundary base layer is correct and audited. Stage 03 proves temporal multi-vintage loading; Stage 04
proves the R-Tree indices exist. The download path (`urllib`) is exercised by a **separate, opt-in /
network-marked** test (or is mocked) so the core suite stays offline and deterministic.

## Open Questions / Risks
- **Live ONS download URLs & attribute column names.** ONS publishes BGC UK boundaries on the Open
  Geography Portal; exact download URLs and column names (e.g. `LAD24CD`/`LAD24NM`,
  `CTYUA24CD`/`CTYUA24NM`) are versioned and can change. The plan pins these in a per-class **vintage
  registry constant**; the executor must confirm the live values against the portal at implementation.
  **Tests never depend on live URLs** — they run against the committed fixtures, so a stale URL fails
  only the opt-in download test, not the suite.
- **Obtaining the committed fixtures.** Stage 02 gives an exact, deterministic recipe to download a real
  BGC file once and trim it to a few polygons using DuckDB's GDAL writer
  (`COPY (SELECT ... LIMIT n) TO '...shp' WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile', SRS 'EPSG:27700')`),
  then commit the four sidecar files. Stage 03 adds a second vintage the same way.
- **`INSTALL spatial` needs network once.** First `build()` on a clean machine downloads the extension;
  thereafter it is cached. Existing Step 2 `build()` tests will now load spatial — confirm they stay
  green (Stage 01). If offline-first install becomes a concern, that is a packaging detail for Step 7,
  not Step 3.
- **DuckDB GEOMETRY has no SRID.** EPSG:27700 is verified by coordinate-range assertion, not stored SRID.
  Documented above and in Stage 02.
- **Temporal validity dates.** Real ONS vintage validity is tied to local-government reorganisation
  dates; the plan uses deterministic per-vintage effective dates from a static registry. Exact dates are
  a refine-at-implementation detail; the schema and windowing logic are fixed.
- **`extract`→`transform_and_load` instance-state hand-off.** Relies on the engine calling them
  back-to-back on the same instance (it does, see `client.py`). Documented as a contract in `spatial.py`.
