# Stats19 Collision Ingestion & Normalization — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Ingest the DfT **STATS19** Collision, Vehicle, and Casualty CSV datasets through the Step 2 bronze →
silver → gold quality model; cast collision Eastings/Northings into native **EPSG:27700** geometry
points (flagging the `-1`/`0` "missing" sentinels, never deleting); relationally link vehicles and
casualties to their collision; and point-in-polygon join valid collision points to the Step 3 ONS
LAD/CTYUA boundaries, exposing the `collisions_spatial` clean view.

## Context & Objective

**What exists today (Steps 1–3, already merged).**
- `src/crossroads/transformers/base.py` — the `BaseTransformer` ABC: a `source_id` property,
  `is_active(**kwargs)`, `extract(cache_dir, **kwargs)`, `transform_and_load(con, cache_dir)`, and a
  **concrete** `quality_spec()` returning `SourceQuality | QualityExemption | None` (inherited default
  `None` = "undecided", which is now **fatal** for an active source).
- `src/crossroads/registry.py` — `Registry` auto-discovers **concrete** `BaseTransformer` subclasses in
  `crossroads.transformers` via `pkgutil` + `inspect` (abstract classes skipped; `obj.__module__ ==
  module_name`), instantiates them, and returns them **sorted by `source_id`**. A single module may
  define several concrete transformers.
- `src/crossroads/client.py` — `init_engine(...)` → `Client`; `Client.build(**kwargs)` opens a DuckDB
  connection (`self.con`), runs `INSTALL spatial; LOAD spatial`, calls `quality.ensure_quality_tables`,
  then **per active transformer**: `quality.reset_source_audit(con, transformer.source_id)` →
  `extract(cache_dir, **kwargs)` → `transform_and_load(con, cache_dir)`. After the loop it runs
  `quality.resolve_quality_specs(con, active)` (coverage gate) then `quality.run_invariants(...)`
  (fatal on violation). An optional `reject_ceiling` kwarg overrides the global default.
- `src/crossroads/quality.py` — the Step 2 quality engine: `Dimension` / `SourceQuality` /
  `QualityExemption` dataclasses; `DEFAULT_REJECT_CEILING = 0.05`; `create_clean_view`; the shared audit
  tables + writers (`record_source_rows`, `log_exclusion`, `quarantine_row`, `record_exemption`,
  `reset_source_audit`); the pre-flight `check_schema_contract`; the three invariants
  (`check_conservation`, `check_flag_ledger_agreement`, `check_reject_rates`); `run_invariants`; the
  `resolve_quality_specs` coverage gate; the exception hierarchy; and
  **`UNDECIDED_QUALITY_SPEC_IS_FATAL = True`**.
- `src/crossroads/transformers/spatial.py` — the working reference transformer. One module, an abstract
  `_BoundaryTransformer` + two concrete classes `LADBoundaryTransformer` (`source_id="ons_lad"`,
  silver `lad_boundaries`, gold `lad_boundaries_clean`) and `CTYUABoundaryTransformer`
  (`source_id="ons_ctyua"`, silver `ctyua_boundaries`, gold `ctyua_boundaries_clean`). Boundary silver
  carries `source_row_key` (`"<area_code>|<vintage>"`), `area_code`, `area_name`, `vintage`, `geom`
  (bare `GEOMETRY`, EPSG:27700), `geom_valid`, `valid_from DATE`, `valid_to DATE` (`NULL` = current),
  and an **R-Tree index** `<silver>_geom_rtree` on `geom`. A `build(boundary_mode="snapshot"|"temporal")`
  kwarg selects latest-vintage-only vs all-vintages-with-windows. Study this module closely — Stats19
  mirrors its structure (abstract-base pattern, `extract`→`transform_and_load` instance hand-off,
  `_derive_*` helpers that tests drive directly, trusted-identifier-interpolation comments).
- `tests/conftest.py` — a `con` fixture (fresh in-memory DuckDB, closed after each test).
- `tests/test_spatial.py`, `test_quality.py`, `test_client.py`, `test_registry.py`, `test_package.py`,
  `test_update_script.py` — **all green**. Integration tests are marked `@pytest.mark.integration` and
  deselected by default (`addopts = "-m 'not integration'"`), run deliberately with `pytest -m integration`.
- `pyproject.toml` — package `crossroads-uk`, import `crossroads`, Hatchling, `requires-python>=3.11`,
  runtime dep **`duckdb>=1.5`** (installed 1.5.4), dev dep `pytest`.

There is **no** `src/crossroads/transformers/stats19.py` yet, and no collision/vehicle/casualty data.

**Environment facts verified for this plan (DuckDB 1.5.4 + Spatial in `.venv`).**
- `read_csv_auto('f.csv', all_varchar=true)` yields a faithful all-string table (bronze). `read_csv([...],
  union_by_name=true, all_varchar=true)` merges files with differing columns, filling absent columns with
  `NULL` — the mechanism that lets historical (`accident_*`) and modern (`collision_*`) tranches coexist.
- `ST_Point(easting, northing)` builds a BNG point; `ST_Contains(boundary.geom, point)` is the
  point-in-polygon predicate. A `LEFT JOIN ... ON (p.geom IS NOT NULL AND ST_Contains(b.geom, p.geom))`
  leaves `lad_code` `NULL` for sentinel (`geom IS NULL`) points — verified.
- `TRY_STRPTIME(s, '%d/%m/%Y %H:%M')` parses the DfT `date`+`time` format to a naive `TIMESTAMP`,
  returning `NULL` (not erroring) on malformed input.
- **No new Python dependency is required.** CSV reading is in-database; downloads use the standard
  library (`urllib.request`). Do **not** add `pandas`, `requests`, `rpy2`, `geopandas`, etc.

**STATS19 domain facts (from the reference `../stats19/` package — inspiration only, GPL-3, never copy).**
- **Download URL pattern** (per-year files): `https://data.dft.gov.uk/road-accidents-safety-data/`
  `dft-road-casualty-statistics-{collision|vehicle|casualty}-{YEAR}.csv`
  (also `-1979-latest-published-year.csv` and `-last-5-years.csv` combined files exist). Verify the exact
  live filenames at implementation; **tests never touch the network** (they run on committed fixtures).
- **Identity / link columns.** Collision primary key: `accident_index` (globally unique; pre-2024
  naming) — renamed `collision_index` from 2024 on (with `accident_year`→`collision_year`,
  `accident_reference`→`collision_reference`). Vehicle links via `accident_index` (+ its own
  `vehicle_reference`). Casualty links via `accident_index` (+ `vehicle_reference` +
  `casualty_reference`). **Canonical silver names are the `accident_*` forms** (matches spec §9 worked
  example); the transformer normalizes `collision_*`→`accident_*` (see "Identity normalization" below).
- **Coordinates.** OSGR eastings/northings: `location_easting_osgr`, `location_northing_osgr` (native
  EPSG:27700). WGS84: `longitude`, `latitude`. Missing/out-of-range is encoded `-1` (and per spec §9,
  `0`); the reference package also treats `""`/`NA`/non-numeric as missing.
- **Date/time.** `date` (`DD/MM/YYYY`), `time` (`HH:MM`, may be blank). STATS19 is **local-native**
  (spec §3B/§3C), so its only temporal column is `datetime_local` (no `*_utc`).
- Collision ≈ 46 columns, Vehicle ≈ 32, Casualty ≈ 23. Bronze keeps them all verbatim; silver types only
  the analytically load-bearing ones and carries the rest through unchanged.

**What changes (this step).**
1. A new `src/crossroads/transformers/stats19.py` defines **one** concrete `Stats19Transformer`
   (`source_id="stats19"`) that ingests all three file types and declares **three** `SourceQuality`
   audit units (`stats19_collision`, `stats19_vehicle`, `stats19_casualty`).
2. A small, well-motivated **generalization of the quality engine** so a transformer may declare more
   than one audit unit: `quality_spec()` may return a `SourceQuality`, a `QualityExemption`, a
   **tuple/list of those**, or `None`; `resolve_quality_specs` flattens; and `client.build()` resets the
   shared audit rows for **every `source_id` the transformer declares** (not just `transformer.source_id`).
   This is exactly the extension the Step 3 overview deferred ("*extend `quality_spec()` to return a
   list … defer until Step 4 genuinely needs it*").
3. Data flow: bronze (`stats19_collision_raw`, `stats19_vehicle_raw`, `stats19_casualty_raw`, faithful
   all-string copies) → silver (`collisions`, `vehicles`, `casualties`, keep-in-place 1:1) → gold views
   (`collisions_spatial`, `vehicles_clean`, `casualties_clean`).
4. Collision silver: identity normalized to `accident_index`; typed `easting`/`northing` (`NULL` on
   sentinel); `geom` = `ST_Point(easting, northing)::GEOMETRY` (EPSG:27700, `NULL` on sentinel) with
   `geom_valid`; `datetime_local TIMESTAMP` with `datetime_valid`; `lad_code`/`ctyua_code` stamped by the
   spatial join. Sentinels retained + logged (`rule_id='stats19.coord.sentinel'`), never deleted.
5. Vehicle/casualty silver: typed, keep-in-place, each carrying a `link_valid` dimension (its
   `accident_index` resolves to a collision row) with a matching ledger rule.
6. Spatial join: valid collision points point-in-polygon against the boundary silver tables. **Snapshot
   is the default** (join against the latest boundary vintage); a `boundary_mode="temporal"` path
   range-joins each point to the boundary vintage whose `[valid_from, valid_to)` window contains the
   incident date.

**The goal.** After Step 4, the spec §8 flow
`cr.init_engine(...).build(years=[2022,2023,2024], spatial_grain="local_authority")` produces a
queryable DuckDB file whose `collisions`/`vehicles`/`casualties` silver tables are populated from real
DfT CSVs, coordinates cast to EPSG:27700 geometry with sentinels flagged, vehicles/casualties linked to
collisions, valid collisions stamped with their LAD/CTYUA codes, and the whole build audited green by the
Step 2 invariants.

## Approach / Architecture

### One transformer, three audit units (locked decision)
STATS19 is three related tables that must be built **in dependency order** (collision silver first, then
vehicle/casualty silver which compute `link_valid` by joining to it, then the spatial stamp of
collisions). The Step 2 audit machinery keys `source_ingest_log` / `data_quality_log` /
`reset_source_audit` by **`source_id`**, and `check_conservation` compares one bronze/silver pair per
`source_id`. So each of the three tables needs its **own** audit `source_id`
(`stats19_collision`/`stats19_vehicle`/`stats19_casualty`) for conservation to hold.

Rather than three separate transformer classes, Step 4 uses **one** `Stats19Transformer`
(`source_id="stats19"`) that owns the whole pipeline and declares three `SourceQuality` from
`quality_spec()`.

*Alternatives rejected.* **Three concrete transformers** — the registry runs transformers sorted by
`source_id`, and no ordering of `stats19_casualty`/`stats19_collision`/`stats19_vehicle` puts collision
first, so the linkage dependency cannot be expressed; and `extract` would have to coordinate one download
across three instances. **Reusing a single `source_id` for all three specs** — breaks
`check_conservation` (its per-`source_id` `source_ingest_log` sum would triple-count) and
`reset_source_audit`. The multi-spec generalization is the smaller, cleaner change and was explicitly
anticipated in Step 3.

### Quality-engine generalization (Stage 01, foundational)
Minimal, backward-compatible:
- `resolve_quality_specs(con, transformers)` — for each transformer, `quality_spec()` may now return a
  `SourceQuality`, a `QualityExemption`, a **tuple/list** of those, or `None`. Flatten list/tuple
  returns; keep the existing single-value and `None`-is-fatal behaviour for everything else. Existing
  single-source transformers (boundaries) are unaffected (a lone `SourceQuality` still works).
- New `quality.declared_source_ids(transformer)` — returns the list of `source_id`s a transformer will
  write audit rows under: the `source_id`s of its `SourceQuality`(s), or `[transformer.source_id]` when
  it returns `QualityExemption`/`None`. Used by the build loop for resets.
- `client.build()` — replace `reset_source_audit(con, transformer.source_id)` with a loop over
  `quality.declared_source_ids(transformer)`. One generic change; still names no source.

`quality_spec()` is a pure constructor of frozen dataclasses, so calling it once at the loop top (for
resets) and again in `resolve_quality_specs` (at build end) is cheap and deterministic.

### Stats19 data flow (inside `Stats19Transformer.transform_and_load`, in order)
1. **Bronze** (×3): `CREATE OR REPLACE TABLE stats19_<type>_raw AS SELECT * FROM read_csv([...cached
   per-year files...], union_by_name=true, all_varchar=true)`. Faithful, append-only-in-spirit copy.
   `record_source_rows(con, "stats19_<type>", <bronze count>)` for conservation.
2. **Collision silver** (keep-in-place 1:1): normalize identity → `accident_index`; type
   `easting`/`northing` (`NULL` when the raw value is a sentinel/non-numeric); `geom =
   ST_Point(easting, northing)::GEOMETRY` (`NULL` when either coordinate is `NULL`); `geom_valid =
   (geom IS NOT NULL AND ST_IsValid(geom))`; `datetime_local` via `TRY_STRPTIME`; `datetime_valid`;
   `lad_code`/`ctyua_code` (created `NULL`, filled in step 5); carry all raw columns through.
   `source_row_key = accident_index`.
3. **Vehicle / casualty silver** (keep-in-place 1:1): normalize identity; type the load-bearing fields;
   `link_valid = accident_index IN (SELECT accident_index FROM collisions)`; carry raw columns.
   `source_row_key = accident_index || '|' || vehicle_reference` (vehicle) and
   `… || '|' || casualty_reference` (casualty).
4. **Ledger**: one `reject_dimension` row per FALSE flag (invalid geom, invalid datetime, orphan link),
   written by scanning each silver table once (aggregate SQL + a small Python loop over the FALSE rows,
   exactly as `spatial.py` does).
5. **Spatial stamp**: `UPDATE collisions SET lad_code = …, ctyua_code = …` from a point-in-polygon
   `LEFT JOIN` against the boundary silver tables (snapshot: latest vintage; temporal: window predicate
   on the incident date). Guarded so a build without boundary tables leaves the codes `NULL` + warns.
6. **Gold views**: `collisions_spatial` (`WHERE geom_valid`), `vehicles_clean`/`casualties_clean`
   (`WHERE link_valid`). Optional R-Tree on `collisions.geom`.
7. **`quality_spec()`** returns the three `SourceQuality` (dimensions grow stage by stage).

### Identity normalization (`accident_*` canonical)
Because `union_by_name` bronze may contain **either** `accident_index` **or** `collision_index` (never
guaranteed both), a bare `COALESCE(collision_index, accident_index)` errors when one column is absent. A
helper `_coalesce_present(con, table, candidates, alias)` inspects `information_schema.columns` and builds
a `COALESCE(...) AS alias` over **only the columns that exist** (or `NULL AS alias` if none). Apply it for
`accident_index` (`collision_index`, `accident_index`), `accident_year` (`collision_year`,
`accident_year`), `accident_reference` (`collision_reference`, `accident_reference`). Identifiers are
code-controlled constants (trusted interpolation); row values are never interpolated.

### Coordinates & EPSG:27700 (spec §3A, §9 worked example)
OSGR eastings/northings **are** EPSG:27700 — cast, do **not** reproject. Sentinel rule: a coordinate is
missing when its raw value is `-1`, `0`, blank, `NA`, or non-numeric → typed value `NULL` → `geom NULL`
→ `geom_valid = FALSE` → ledger row (`rule_id='stats19.coord.sentinel'`), **row retained**. `geom` is cast
to a bare `GEOMETRY` (same reason as `spatial.py`: RTREE and consistency need a CRS-unqualified column).
EPSG:27700 is verified by BNG-envelope coordinate range in tests, never by a stored SRID.

### Temporal grain (spec §3B — decided with the user)
Silver carries exactly one temporal column, **`datetime_local`** (naive `TIMESTAMP`, UK civil time), built
from `date`+`time`. **No separate `date` column** (derive with `CAST(datetime_local AS DATE)` when needed)
and **no materialized hourly/interval keys** — DuckDB derives those at join time
(`date_trunc('hour', datetime_local)`, `time_bucket(...)`), so the weather step (Step 6) computes them
where they are consumed. `datetime_valid = FALSE` only when the **date** is unparseable (a missing `time`
falls back to midnight and is not a rejection). Raw `date`/`time` strings remain in bronze (and carry
through silver), preserving fidelity.

### Boundary join mode (spec §3C — decided with the user)
Default `boundary_mode="snapshot"`: join every valid collision point against the **latest** boundary
vintage. `boundary_mode="temporal"`: range-join each point to the vintage whose `[valid_from, valid_to)`
window contains the incident date (`CAST(datetime_local AS DATE)`), using the `valid_from`/`valid_to`
columns already on boundary silver. The kwarg rides the same `**kwargs` the engine forwards to
`extract`; the transformer stashes it on the instance (like `spatial.py`'s `_vintages_to_load`) for
`transform_and_load` to read. Snapshot needs only the latest boundary vintage loaded; temporal needs the
boundaries themselves built with `boundary_mode="temporal"` (the user passes one mode per build; both
transformers honour it).

### Differential testing (spec §2 — decided with the user)
**Do not run R.** The reference `../stats19/` `testthat` suite is read for **inspiration** on what to
assert (e.g. `-1`/blank/non-numeric → `NA`; a known sample's row count and a known `collision_index`),
and those checks are **independently reimplemented** in our pytest against our committed fixtures. No R
runtime, no `rpy2`, no GPL-3 code copied — MIT-clean.

### Registry ordering (why boundaries build before Stats19)
`Registry` sorts by `source_id`: `ons_ctyua` < `ons_lad` < `stats19`. So in a full default-registry
build the boundary silver tables already exist when `Stats19Transformer.transform_and_load` runs its
spatial stamp. The stamp still guards for their absence (defensive, and for isolated tests).

### `is_active` gating (avoids an awkward empty-build)
`Stats19Transformer.is_active(**kwargs)` returns `bool(kwargs.get("years"))`. With no `years` there is
nothing to ingest, so the transformer is simply skipped — no empty tables, no schema-contract gymnastics,
and existing no-`years` builds (e.g. boundary-only tests) are unaffected. A real build always passes
`years` (spec §8). Document this clearly.

### Codebook, missing-value cleaning & labelled views (Stages 05–07)
STATS19 encodes most non-coordinate fields as integers whose meaning lives in DfT's published data guide
(e.g. `casualty_severity` 1=Fatal, 2=Serious, 3=Slight). Two accuracy hazards follow, and Stages 05–07
handle them as **two clearly separate jobs**.

**Job A — missing-value cleaning (writes to the stored tables; Stage 06).** DfT does not encode "missing"
as a single value: it uses a *set* of sentinels ("Data missing or out of range", "Unknown", "Undefined",
"Not known", a deprecated-code marker, and the numeric `-1`). Left raw, these silently corrupt aggregates
(an `AVG`/count over a column salted with `-1`s is wrong). Job A converts the **full** missing set — not
just `-1` — to `NULL` when producing a typed/computed column. The value stays **numeric/coded**: `NULL`
means "empty cell", never a label. This reuses the exact data-quality discipline of Stages 02/03 —
keep-in-place (never drop a row), keep the raw copy, and for a promoted field add a `*_valid` flag +
`reject_dimension` ledger row + a `Dimension` on the `SourceQuality`.

**Job B — code→label translation (NEVER stored; Stage 07).** English labels are **not** baked into the
stored tables; silver keeps integer codes. Translation is an **opt-in surface**: a labelled *view*
(`casualties_labelled`) that `JOIN`s the codebook, sitting *alongside* the canonical coded table. The
default surface is codes (`casualties`); "turning translation off" just means querying the coded table
instead of the view. There is no global on/off flag and no hidden default translation layer. A labelled
view is built only where there is a real consumer (type-on-demand).

**The codebook (shared reference data; Stage 05).** Both jobs read one small reference table,
`codebook(variable, code, label, is_missing)`. It is loaded — once, at the **top** of
`transform_and_load` — from a committed CSV that ships inside the package, exactly as `spatial.py` ships
and loads `ons_boundaries.json` via `os.path.dirname(__file__)`. It is a **reference table, not an audited
source**: no new `source_id`, no bronze/silver pair, no conservation obligation (a static lookup, the kind
the quality overview names as a legitimate non-source). The engine still audits only the three STATS19
`SourceQuality` units; the codebook is just another table the transformer builds, like an index or a view.
*Alternatives rejected:* a dedicated `CodebookTransformer` (more machinery for a table only STATS19 uses
today) and a fourth bronze/silver audit unit (treats static reference data as observational — heavy and
wrong-shaped).

**Source of truth (resolved, not hand-waved).** The codebook is derived **independently from DfT's
published "Guide to variables / code lists" for the Road Safety Open Dataset** — the same publisher and
Open Government Licence v3.0 as the CSVs themselves. `is_missing` is set during that one-time derivation by
matching each label against a documented missing-marker vocabulary (plus the `-1` sentinel), and the
committed CSV carries `is_missing` as an explicit column so the decision is auditable in the repo. A
licence-incompatible **GPL** reference package ships a similar code-list table; it must **not** be copied —
derive from the DfT guide. Provenance + the derivation recipe live in a committed README beside the CSV,
mirroring the `tests/fixtures/**/README.md` discipline.

**Decode by intersect (graceful degradation).** Cleaning only touches a column that appears in **both** the
incoming data **and** the codebook. A future-year file that renames or drops a coded column degrades
gracefully (the field is left raw / a stable NULL placeholder + a warning) instead of erroring — the same
robustness principle as `_coalesce_present`'s presence check for the `accident_*`/`collision_*` rename.

**What gets promoted now (lean, type-on-demand).** Exactly **one** field is promoted to full validation in
this pass: **`casualty_severity`** — the headline killed-or-seriously-injured outcome, with a small clean
domain and an obvious consumer. It gains a typed `casualty_severity` (`INTEGER`, missing→`NULL`), a
`casualty_severity_valid` flag, a ledger rule, and a `severity` `Dimension` on the casualty spec; the
reusable `_clean_coded_column` helper it is built on generalises to any future promotion. Every other coded
field stays raw exactly as today — promote it the same way when a consumer appears. This mirrors Stage 03's
deliberate "linkage now, per-field typing on demand" stance.

**Scope / blast radius.** This is confined to a **new reference table** + the **casualty** silver
derivation + **one labelled view**. Collisions, vehicles, the linkage model, and the **Stage 04 spatial
join are untouched** (geometry has nothing to do with coded-field decoding). Plan-stage order (05–07 come
after 04) is independent of within-build execution order — the codebook is built early in
`transform_and_load` even though its stage number is late, just as Stage 04's spatial stamp runs late.

## Cross-Cutting Constraints (every stage follows these)
- **No new dependencies.** `duckdb` + `pytest` only. CSV read is in-DB; download via stdlib
  `urllib.request`. Do not edit `pyproject.toml` dependencies.
- **One module:** all Stats19 code lives in `src/crossroads/transformers/stats19.py` (spec §7).
- **Provider-plugin purity (spec §4):** `client.py`/`registry.py` never name a concrete source. The
  Stage-01 `client.py` change (reset per declared `source_id`) is generic — it names no source.
- **Keep-in-place (spec §9):** never delete or filter a source row. Bad coordinates/datetime/links are
  flagged (`*_valid = FALSE`) + logged; the row stays. `count(bronze) == count(silver)` per table.
- **Aggregate SQL, not Python row loops** (spec §9): bronze/silver via set-based
  `CREATE OR REPLACE TABLE AS SELECT`; the only per-row Python is writing ledger rows for the (bounded)
  FALSE-flag sets.
- **EPSG:27700 once, never at query time** (spec §3A): coordinates cast at ingestion; no query-time
  reprojection.
- **Determinism / reproducibility (spec §2):** same version + same fixtures + same params → identical
  tables. No wall-clock/randomness in logic; the only timestamp is the DB-side `ingested_at` default
  (provenance, never asserted).
- **SQL identifier interpolation:** table/column identifiers come from code-controlled
  constants/manifests (trusted) and may be interpolated; row VALUES are always bound with `?`. Comment
  the trust boundary, matching `quality.py`/`spatial.py`.
- **Idempotent re-build:** the transformer recreates its OWN bronze/silver with `CREATE OR REPLACE`
  (the engine resets shared audit rows for each declared `source_id`). A second `build()` against the
  same on-disk DB must not double rows or break invariants.
- **Offline, deterministic tests:** the core suite never hits the network. Real downloads are exercised
  only by an opt-in `@pytest.mark.integration` test. Fixtures are tiny committed real DfT samples.
- **Style:** plain-language comments, simple code (CLAUDE.md); match the comment density of
  `spatial.py`/`quality.py`.
- **Git discipline:** never stage or commit without explicit user permission (CLAUDE.md).

## Stage Map (sequential — do in order)

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Download & bronze + multi-spec engine | Generalize the quality engine for multiple audit units per transformer; `stats19.py` with `Stats19Transformer` that downloads (offline-seedable) the three CSV types into bronze and builds minimal keep-in-place silver (identity + `source_row_key`, **no dimensions yet**); commit tiny real DfT fixtures. | `build(years=[...])` over the committed sample creates `stats19_{collision,vehicle,casualty}_raw` bronze and `collisions`/`vehicles`/`casualties` silver (1:1), each an audited `SourceQuality`; conservation holds; all Step 1–3 tests still green. `pytest` green. | Step 3 | `01-download-bronze.md` |
| 02 | Collision silver & coordinates | Enrich collision silver: identity normalization, typed `easting`/`northing`, `geom` (EPSG:27700), `geom_valid` + sentinel ledger, `datetime_local` + `datetime_valid`; add the `geom`/`datetime` dimensions to the collision spec; `collisions` gold-ready. | A build casts valid coords to BNG geometry, flags+logs `-1`/`0`/blank/non-numeric coords (geom `NULL`, retained), builds `datetime_local`; flag/ledger agreement + reject-rate hold; FALSE-branch proven by a synthetic-bronze test. `pytest` green. | Stage 01 | `02-collision-silver.md` |
| 03 | Vehicle & casualty silver & linkage | Type vehicle/casualty silver; compute `link_valid` (accident_index resolves to a collision) with ledger rules; add the `link` dimension to each spec; `vehicles_clean`/`casualties_clean` gold views. | A build links vehicles/casualties to collisions, flags+logs orphans (retained), and passes all invariants across all three sources; orphan FALSE-branch proven. `pytest` green. | Stage 02 | `03-vehicle-casualty-linkage.md` |
| 04 | Spatial join & gold view | Point-in-polygon stamp `lad_code`/`ctyua_code` onto valid collisions (snapshot default; temporal window option); build `collisions_spatial`; confirm reject ceilings + all invariants; optional R-Tree on `collisions.geom`. | An end-to-end build (boundaries + Stats19) stamps valid collisions with the correct LAD/CTYUA codes, leaves sentinel points unstamped, exposes `collisions_spatial`, and passes every Step 2 invariant. `pytest` green. | Stage 03 | `04-spatial-join.md` |
| 05 | Codebook reference table | Commit `src/crossroads/reference/stats19_codebook.csv` (`variable,code,label,is_missing`) derived independently from DfT's published data guide, + a provenance README; load it into a `codebook` table at the top of `transform_and_load` (a reference table, **not** an audited source). | A build creates a `codebook` table; `casualty_severity` decodes correctly (2→"Serious") and every missing marker (incl. non-`-1`) is `is_missing = TRUE`; the CSV is unique on `(variable, code)`; all prior tests stay green. `pytest` green. | Stage 04 | `05-codebook-reference.md` |
| 06 | Missing-value cleaning (Job A) | Add the reusable `_clean_coded_column` decode-by-intersect helper; promote **`casualty_severity`** to a typed column (full missing set→`NULL`, raw kept) with `casualty_severity_valid` + ledger rule + a `severity` `Dimension`. Nothing else promoted (type-on-demand). | A build produces `casualty_severity` (`INTEGER`, missing→`NULL`), flags+logs missing values (row retained), passes flag/ledger agreement + reject-rate across all sources; the FALSE branch and the column-absent degrade-gracefully path are proven by synthetic tests. `pytest` green. | Stage 05 | `06-missing-value-cleaning.md` |
| 07 | Labelled views (Job B) | Add the opt-in `casualties_labelled` view that `JOIN`s the codebook to expose `casualty_severity_label` alongside the codes; stored tables keep **codes only** (no label column ever written); no global translation flag. | A build exposes `casualties_labelled` (code 2 shows "Serious"); the stored `casualties` table has **no** `*_label` column; the view has the same row count as `casualties`. `pytest` green. | Stage 06 | `07-labelled-views.md` |

## Global Testing & Ship
All tests are **real and runnable** (manual testing is not relied upon). A new `tests/test_stats19.py`
holds the Stats19 tests and reuses `tests/conftest.py`'s `con` fixture and `crossroads.init_engine()`.
Tiny **real** DfT CSV samples live under `tests/fixtures/stats19/` (trimmed, referential-integrity
preserved), committed so the suite runs fully offline. From the repo root with the venv active:

```bash
source .venv/bin/activate
python -m pytest -q          # whole suite, offline; expected: all green
python -m pytest -m integration -q   # opt-in: exercises the real DfT download
```
Expected at the end of **every** stage: all tests pass, zero failures/errors (including pre-existing
Step 1–3 tests).

**End-to-end ship proof for Step 4** attaches to **Stage 04** (built up by 01–03): a real `build()`
ingests the committed Collision/Vehicle/Casualty samples end-to-end through bronze → silver → gold,
casts coordinates to EPSG:27700 (BNG-envelope assertion), flags+logs a sentinel coordinate without
dropping it, links vehicles/casualties to collisions, stamps valid collisions with LAD/CTYUA codes, and
passes all three Step 2 invariants — the cumulative proof the collision layer is correct and audited.
Correctness of each FALSE-branch (sentinel coord, invalid datetime, orphan link) and of the spatial
stamp is proven by focused synthetic-input tests (driving the `_derive_*`/`_spatial_stamp` helpers
directly, mirroring `spatial.py`'s `test_invalid_geometry_is_flagged_and_logged`) so they are
deterministic and independent of the geographic fixture coupling.

## Open Questions / Risks (resolve within the relevant stage)
- **Live DfT filenames / column headers.** Per-year filenames and the `accident_*`↔`collision_*` naming
  are versioned and can drift. Pin them in a code constant (a per-type filename template + the alias
  map); the executor confirms live values at implementation. Tests depend only on the committed
  fixtures, so a stale URL fails just the opt-in download test.
- **Fixture ↔ boundary geographic coupling (Stage 04).** For the real-sample e2e to show stamping, the
  committed collision coordinates must fall inside the committed ONS LAD/CTYUA polygons. Stage 04 gives a
  recipe to align them (or verify with `ST_Contains` and adjust); the authoritative stamp-correctness
  test uses a controlled synthetic boundary+point so it never depends on that alignment.
- **Reject-rate ceiling for historical coordinates.** Very old STATS19 tranches have a higher share of
  missing coordinates; the geom reject rate could exceed the 5% default. The committed sample is kept
  clean (rate ≈ 0). The `geom` `Dimension.reject_ceiling` is left at the default but is documented as
  overridable per-build (`build(reject_ceiling=...)`) or per-dimension when ingesting deep history.
- **`extract`→`transform_and_load` instance hand-off.** Relies on the engine calling them back-to-back
  on the same instance (it does — see `client.py`). Document the contract in `stats19.py`, as `spatial.py` does.
- **Codebook source of truth & drift (Stages 05–07).** DfT's data guide is versioned and its layout can
  change; the committed CSV is a point-in-time transcription, and `is_missing` involves a documented
  judgement call for a few ambiguous labels (e.g. "Other" is deliberately **not** treated as missing).
  Pin the guide URL/vintage and the missing-marker vocabulary in the committed README; tests depend only on
  the committed CSV, so a guide change never breaks the offline suite — it is a deliberate, reviewed
  refresh. The codebook must be derived from the DfT guide, never copied from a GPL-licensed package's
  shipped code-list table (licence-incompatible).
- **Labels never stored (Stage 07).** The stored tables keep integer codes; English labels appear only in
  opt-in views. A test asserts the stored `casualties` table carries no `*_label` column so this cannot
  regress into a hidden default translation layer.
