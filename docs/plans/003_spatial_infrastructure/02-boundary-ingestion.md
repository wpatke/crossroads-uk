# Stage 02 — Boundary Ingestion (Snapshot) + Step 2 Carryover
> Part of Spatial Infrastructure & Boundaries. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

> **Superseded detail (shapefile → GeoJSON):** This document describes committing ONS boundary
> samples as shapefiles (`.shp/.shx/.dbf/.prj`) and reading them via `ST_Read`. The implemented
> design instead downloads and commits **GeoJSON** (ONS publishes ArcGIS FeatureServer GeoJSON, not
> shapefile ZIPs); the shapefile fixtures have been removed as unused. Treat shapefile-specific
> steps below as historical — the GeoJSON equivalent is what shipped in `spatial.py`.

## Prerequisites / Starting State
Stage 01 is done: `Client.build()` loads the spatial extension; `tests/test_spatial.py` exists and is
green. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: there is **no** `src/crossroads/transformers/spatial.py`; `quality.py` has
`UNDECIDED_QUALITY_SPEC_IS_FATAL = False`; `tests/test_quality.py` contains
`test_build_with_undecided_source_warns_in_interim`; the placeholder
`docs/plans/003_spatial_infrastructure/00-PENDING-carryover-from-002.md` still exists.

## Objective
Deliver the first real transformer: `spatial.py` ingests ONS **LAD** and **CTYUA** BGC boundaries
(snapshot / latest vintage only in this stage) through bronze → silver → gold, with geometry verified
EPSG:27700 and a `geom_valid` quality dimension, audited by the Step 2 invariants. Action the Step 2
carryover obligations. Commit a tiny **real** ONS sample as the integration-test fixture.

This stage uses **snapshot behaviour only** — load exactly one (latest) vintage. The silver schema
already includes `valid_from`/`valid_to` (latest vintage: `valid_from = <effective date>`,
`valid_to = NULL`); the `boundary_mode` kwarg and multi-vintage loading arrive in Stage 03.

## Implementation Steps

### A. Create the committed ONS sample fixtures (do this first — tests depend on it)

The integration test must run **offline** against committed real shapefiles. Produce a trimmed real
sample once, deterministically:

1. Download the real ONS BGC files (one-off, on a networked machine). On the ONS Open Geography Portal,
   locate **"Local Authority Districts (December 2024) Boundaries UK BGC"** and **"Counties and Unitary
   Authorities (December 2024) Boundaries UK BGC"**, download each as a shapefile ZIP, and unzip to a
   scratch dir. Confirm the geometry/attribute column names (expected: `LAD24CD`, `LAD24NM` for LAD;
   `CTYUA24CD`, `CTYUA24NM` for CTYUA — **verify and record the exact names**; the `.prj` must be
   British National Grid / EPSG:27700).

2. Trim each to a few polygons and write a small shapefile into the repo fixtures using DuckDB's GDAL
   writer (no extra tooling needed):
   ```bash
   source .venv/bin/activate
   mkdir -p tests/fixtures/ons/lad_2024 tests/fixtures/ons/ctyua_2024
   python - <<'PY'
   import duckdb
   c = duckdb.connect(); c.execute("INSTALL spatial"); c.execute("LOAD spatial")
   # LAD: keep 3 polygons. Adjust the source path + column names to the real download.
   c.execute("""
     COPY (
       SELECT LAD24CD, LAD24NM, geom
       FROM ST_Read('/scratch/LAD_DEC_2024_UK_BGC.shp')
       ORDER BY LAD24CD LIMIT 3
     ) TO 'tests/fixtures/ons/lad_2024/lad_sample.shp'
     WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile', SRS 'EPSG:27700')
   """)
   # CTYUA: keep 2 polygons.
   c.execute("""
     COPY (
       SELECT CTYUA24CD, CTYUA24NM, geom
       FROM ST_Read('/scratch/CTYUA_DEC_2024_UK_BGC.shp')
       ORDER BY CTYUA24CD LIMIT 2
     ) TO 'tests/fixtures/ons/ctyua_2024/ctyua_sample.shp'
     WITH (FORMAT GDAL, DRIVER 'ESRI Shapefile', SRS 'EPSG:27700')
   """)
   print("wrote fixtures")
   PY
   ```
   This emits `lad_sample.{shp,shx,dbf,prj}` and `ctyua_sample.{shp,shx,dbf,prj}`. **Commit all four
   sidecar files for each** (they are tiny). Record in a short `tests/fixtures/ons/README.md` the source
   dataset titles, vintage (Dec 2024), licence (Open Government Licence v3.0 — ONS), and that these are
   trimmed samples for testing only.

   > If the real column names differ from `LAD24CD`/`LAD24NM`, alias them to a **stable fixture schema**
   > in the `COPY SELECT` (e.g. `SELECT LAD24CD AS area_code, LAD24NM AS area_name, geom`). Decide one
   > convention and keep the transformer's column mapping consistent with the vintage registry (below).
   > Recommended: write fixtures with the **real ONS column names** so the transformer's real-world
   > column mapping is what the test exercises.

### B. `src/crossroads/transformers/spatial.py` — the transformer module

Create the module with an abstract base + two concrete classes. Keep it simple and well-commented.

1. **Imports & vintage registry.** Standard library only for download:
   ```python
   """ONS Local Authority District (LAD) and County/Unitary Authority (CTYUA)
   boundary ingestion — the EPSG:27700 geometric base layer (spec §3, Phase 1).

   One module, two concrete transformers (LAD, CTYUA) sharing an abstract base.
   Each is independently audited by the Step 2 quality engine via its own
   SourceQuality. Shapefiles are read in-database with ST_Read (GDAL); downloads
   use only the standard library. Boundaries are native EPSG:27700 (ONS BGC) and
   are NOT reprojected — only validated.
   """
   import os
   import urllib.request
   import zipfile
   from abc import abstractmethod
   from dataclasses import dataclass

   from crossroads.transformers.base import BaseTransformer
   from crossroads.quality import (
       SourceQuality, Dimension, create_clean_view,
       record_source_rows, log_exclusion,
   )

   # British National Grid envelope, used to verify geometry really is EPSG:27700
   # (DuckDB GEOMETRY stores no SRID, so we sanity-check coordinate ranges).
   BNG_MIN_E, BNG_MAX_E = 0, 700_000
   BNG_MIN_N, BNG_MAX_N = 0, 1_300_000


   @dataclass(frozen=True)
   class Vintage:
       """One published boundary vintage: where to get it, how its columns are
       named, and the validity window it represents (spec §3C temporal drift)."""
       label: str            # e.g. "2024"
       url: str              # ONS BGC shapefile ZIP download URL
       shp_name: str         # the .shp filename inside the unzipped cache dir
       code_col: str         # ONS area-code column, e.g. "LAD24CD"
       name_col: str         # ONS area-name column, e.g. "LAD24NM"
       valid_from: str       # 'YYYY-MM-DD' this layout takes effect
       valid_to: str | None  # 'YYYY-MM-DD' superseded, or None if current
   ```

2. **Abstract base `_BoundaryTransformer`** holding all shared logic. It is abstract (subclasses
   supply the identity bits), so `Registry` does **not** discover it directly.
   ```python
   class _BoundaryTransformer(BaseTransformer):
       """Shared LAD/CTYUA ingestion. Concrete subclasses set the identity:
       source_id, table names, gold-view name, and the vintage registry."""

       # --- identity, supplied by subclasses ---
       @property
       @abstractmethod
       def source_id(self): ...
       @property
       @abstractmethod
       def bronze_table(self): ...
       @property
       @abstractmethod
       def silver_table(self): ...
       @property
       @abstractmethod
       def clean_view(self): ...
       @property
       @abstractmethod
       def vintages(self):
           """Tuple[Vintage, ...] newest LAST (the latest vintage is vintages[-1])."""

       GEOM_RULE = "ons.geom.invalid"

       # --- vintage selection (snapshot in this stage; temporal added in Stage 03) ---
       def _vintages_for(self, **kwargs):
           # Stage 02: snapshot only -> the latest vintage. Stage 03 widens this.
           return (self.vintages[-1],)

       def extract(self, cache_dir, **kwargs):
           os.makedirs(cache_dir, exist_ok=True)
           wanted = self._vintages_for(**kwargs)
           for v in wanted:
               shp_path = os.path.join(cache_dir, v.shp_name)
               # Offline-friendly: if already cached (or test-seeded), skip download.
               if not os.path.exists(shp_path):
                   self._download_and_unzip(v, cache_dir)
           # Hand off the resolved vintages to transform_and_load. The engine always
           # calls extract() immediately before transform_and_load() on this same
           # instance (see client.py build loop), so instance state is reliable here.
           self._vintages_to_load = wanted

       def _download_and_unzip(self, vintage, cache_dir):
           # Download the ONS BGC zip with the standard library and unzip the
           # shapefile sidecars (.shp/.shx/.dbf/.prj) into cache_dir.
           zip_path = os.path.join(cache_dir, vintage.label + "_" + self.source_id + ".zip")
           urllib.request.urlretrieve(vintage.url, zip_path)
           with zipfile.ZipFile(zip_path) as zf:
               zf.extractall(cache_dir)

       def transform_and_load(self, con, cache_dir):
           vintages = getattr(self, "_vintages_to_load", None) or self._vintages_for()

           # --- BRONZE: faithful copy of every feature, one block per vintage,
           # tagged with its vintage label. Identifiers come from code-controlled
           # Vintage constants (trusted interpolation); paths are bound as values
           # inside ST_Read via string building of a UNION ALL of per-vintage reads.
           selects = []
           params = []
           for v in vintages:
               path = os.path.join(cache_dir, v.shp_name)
               # ST_Read needs a literal path; we validate it exists and embed it.
               # (Path is code/cache-derived, not user input.)
               selects.append(
                   f"SELECT '{v.label}' AS vintage, "
                   f"{v.code_col} AS area_code, {v.name_col} AS area_name, geom "
                   f"FROM ST_Read('{path}')"
               )
           bronze_sql = " UNION ALL ".join(selects)
           con.execute(f"CREATE OR REPLACE TABLE {self.bronze_table} AS {bronze_sql}")

           # --- SILVER: keep-in-place 1:1 from bronze. Composite key keeps the same
           # area_code unique across vintages. geom is already EPSG:27700 (ONS BGC) so
           # it is NOT reprojected; geom_valid flags null/invalid geometry.
           # valid_from/valid_to come from the vintage registry via a CASE map.
           vf_case, vt_case = self._validity_case_sql(vintages)
           con.execute(
               f"CREATE OR REPLACE TABLE {self.silver_table} AS "
               f"SELECT "
               f"  area_code || '|' || vintage AS source_row_key, "
               f"  area_code, area_name, vintage, "
               f"  geom, "
               f"  (geom IS NOT NULL AND ST_IsValid(geom)) AS geom_valid, "
               f"  {vf_case} AS valid_from, "
               f"  {vt_case} AS valid_to "
               f"FROM {self.bronze_table}"
           )

           # --- LEDGER: one reject_dimension row per invalid-geometry silver row,
           # so flag/ledger agreement holds. This is the only per-row Python and is
           # rare (ONS BGC geometry is clean); the scan itself is aggregate SQL.
           bad = con.execute(
               f"SELECT source_row_key, area_code FROM {self.silver_table} "
               f"WHERE geom_valid = FALSE"
           ).fetchall()
           for key, code in bad:
               log_exclusion(
                   con, source_id=self.source_id, source_row_key=key,
                   column_name="geom", rule_id=self.GEOM_RULE,
                   rule_desc="boundary geometry is NULL or fails ST_IsValid",
                   severity="reject_dimension", raw_value=str(code),
               )

           # --- CONSERVATION accounting: rows read from source == bronze rows.
           n = con.execute(f"SELECT count(*) FROM {self.bronze_table}").fetchone()[0]
           record_source_rows(con, self.source_id, n)

           # --- GOLD: valid-geometry view.
           create_clean_view(con, self.clean_view, self.silver_table, ["geom_valid"])

       def _validity_case_sql(self, vintages):
           # Build CASE expressions mapping each vintage label to its validity dates.
           # Labels/dates are code-controlled constants (trusted interpolation).
           vf = "CASE vintage " + " ".join(
               f"WHEN '{v.label}' THEN DATE '{v.valid_from}'" for v in vintages
           ) + " END"
           vt = "CASE vintage " + " ".join(
               f"WHEN '{v.label}' THEN " +
               (f"DATE '{v.valid_to}'" if v.valid_to else "NULL")
               for v in vintages
           ) + " END"
           return vf, vt

       def quality_spec(self):
           # First real consumer of the Step 2 quality engine (carryover item 1).
           return SourceQuality(
               source_id=self.source_id,
               bronze_table=self.bronze_table,
               silver_table=self.silver_table,
               dimensions=(Dimension("geom", "geom_valid", (self.GEOM_RULE,)),),
               key_column="source_row_key",
           )
   ```

3. **Concrete subclasses.** Fill `url`/`shp_name`/columns from the verified ONS values. In tests the
   cache is seeded with the fixture shapefile (named to match `shp_name`), so the URL is only used by
   the opt-in download test.
   ```python
   class LADBoundaryTransformer(_BoundaryTransformer):
       source_id = "ons_lad"
       bronze_table = "ons_lad_raw"
       silver_table = "lad_boundaries"
       clean_view = "lad_boundaries_clean"
       vintages = (
           Vintage(label="2024",
                   url="<ONS LAD Dec 2024 BGC shapefile ZIP URL>",
                   shp_name="lad_sample.shp",     # matches the committed fixture
                   code_col="LAD24CD", name_col="LAD24NM",
                   valid_from="2024-12-01", valid_to=None),
       )

   class CTYUABoundaryTransformer(_BoundaryTransformer):
       source_id = "ons_ctyua"
       bronze_table = "ons_ctyua_raw"
       silver_table = "ctyua_boundaries"
       clean_view = "ctyua_boundaries_clean"
       vintages = (
           Vintage(label="2024",
                   url="<ONS CTYUA Dec 2024 BGC shapefile ZIP URL>",
                   shp_name="ctyua_sample.shp",
                   code_col="CTYUA24CD", name_col="CTYUA24NM",
                   valid_from="2024-12-01", valid_to=None),
       )
   ```
   > `source_id`/`bronze_table`/etc. are declared as plain class attributes here, which satisfy the
   > abstract `@property`/`@abstractmethod` declarations on the base (a class attribute overrides an
   > abstract property). If `inspect.isabstract` still reports a subclass abstract, convert the
   > subclass attributes to `@property` methods returning the constant, and note the deviation.

   > **Production `shp_name` vs fixture:** the committed fixture uses `lad_sample.shp`. For real runs the
   > unzipped ONS `.shp` has a different name (e.g. `LAD_DEC_2024_UK_BGC.shp`). Keep `shp_name` matching
   > the **fixture** for the offline test path; the opt-in download test sets a transformer whose
   > `shp_name` matches the real unzipped file. Simplest: make `shp_name` the value the test seeds, and
   > in the download test point a subclass/instance at the real filename. Document the choice you make.

### C. Action the Step 2 carryover (in this same change)

4. **Flip the escalation flag.** In `src/crossroads/quality.py` change:
   ```python
   UNDECIDED_QUALITY_SPEC_IS_FATAL = False
   ```
   to
   ```python
   UNDECIDED_QUALITY_SPEC_IS_FATAL = True    # enforced from Step 3 onward
   ```
   (Update the adjacent comment to say the escalation has landed.)

5. **Rewrite the interim tripwire test.** In `tests/test_quality.py`, replace
   `test_build_with_undecided_source_warns_in_interim` with a fatal-path assertion and rename it:
   ```python
   def test_build_with_undecided_source_is_fatal():
       # From Step 3 on, UNDECIDED_QUALITY_SPEC_IS_FATAL is True: an active source
       # that never overrode quality_spec() (inherits None) halts the build.
       client = _client_with(_UndecidedTransformer())
       with pytest.raises(UndecidedQualitySpecError):
           client.build()
       client.close()
   ```
   Keep `_UndecidedTransformer` as-is. Ensure `UndecidedQualitySpecError` is imported in the test
   (it already is, in the Stage 03 import block of `test_quality.py`).

6. **Delete the placeholder.** Remove
   `docs/plans/003_spatial_infrastructure/00-PENDING-carryover-from-002.md` (its obligations are now in
   this plan and actioned). This is a plan-artifact deletion, not a code change.

## Testing & Verification

Add to `tests/test_spatial.py`. A helper seeds the cache with the committed fixtures so `extract()`
skips the network, then runs a real `build()`.

```python
import os, shutil
import pytest
import crossroads
from crossroads.transformers.spatial import (
    LADBoundaryTransformer, CTYUABoundaryTransformer,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ons")


def _seed_cache(cache_dir):
    # Copy committed fixture shapefiles (all sidecars) into the build cache so
    # extract() finds them and performs no download.
    os.makedirs(cache_dir, exist_ok=True)
    for sub, stem in (("lad_2024", "lad_sample"), ("ctyua_2024", "ctyua_sample")):
        src = os.path.join(FIXTURES, sub)
        for ext in ("shp", "shx", "dbf", "prj"):
            shutil.copy(os.path.join(src, stem + "." + ext), cache_dir)


def _boundary_client(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_cache(cache)
    client = crossroads.init_engine(cache_dir=cache)  # in-memory DB, seeded cache
    client.registry._transformers = [
        CTYUABoundaryTransformer(), LADBoundaryTransformer(),
    ]
    return client


def test_build_ingests_boundaries_end_to_end(tmp_path):
    client = _boundary_client(tmp_path)
    client.build()  # real extract (offline) + transform_and_load + invariants

    # Silver tables populated (keep-in-place 1:1 with bronze).
    lad = client.con.execute("SELECT count(*) FROM lad_boundaries").fetchone()[0]
    ctyua = client.con.execute("SELECT count(*) FROM ctyua_boundaries").fetchone()[0]
    assert lad == 3 and ctyua == 2          # matches the committed sample sizes
    assert client.con.execute("SELECT count(*) FROM ons_lad_raw").fetchone()[0] == lad

    # Gold views exist and (clean sample) equal silver.
    assert client.con.execute(
        "SELECT count(*) FROM lad_boundaries_clean"
    ).fetchone()[0] == lad
    client.close()


def test_boundary_geometry_is_epsg_27700(tmp_path):
    client = _boundary_client(tmp_path)
    client.build()
    # Every geometry must sit inside the British National Grid envelope.
    row = client.con.execute(
        "SELECT min(ST_XMin(geom)), max(ST_XMax(geom)), "
        "       min(ST_YMin(geom)), max(ST_YMax(geom)) FROM lad_boundaries"
    ).fetchone()
    assert 0 <= row[0] and row[1] <= 700000      # easting band
    assert 0 <= row[2] and row[3] <= 1300000     # northing band
    client.close()


def test_boundary_build_passes_step2_invariants(tmp_path):
    # The real quality engine runs at build end; a clean sample must pass all
    # three invariants (conservation, flag/ledger agreement, reject-rate).
    client = _boundary_client(tmp_path)
    client.build()  # would raise a QualityInvariantError if any invariant failed
    # Sanity: source row count recorded for each source.
    n = client.con.execute(
        "SELECT count(*) FROM source_ingest_log WHERE source_id = 'ons_lad'"
    ).fetchone()[0]
    assert n == 1
    client.close()


def test_quality_spec_shape():
    spec = LADBoundaryTransformer().quality_spec()
    assert spec.source_id == "ons_lad"
    assert spec.silver_table == "lad_boundaries"
    assert spec.dimensions[0].flag_column == "geom_valid"
```

Optional but recommended — prove the `geom_valid` dimension actually flags + logs a bad row, without
needing an invalid shapefile, by driving the silver-derivation against a hand-built bronze:

```python
def test_invalid_geometry_is_flagged_and_logged(con):
    # Build a synthetic bronze with one NULL-geom feature, then run only the
    # silver/ledger derivation. Proves geom_valid=FALSE + a matching ledger row.
    # (If the silver-derivation logic is private, expose a small helper to call it,
    #  or assert via a full build using a fixture that contains a degenerate geom.)
    ...  # see "Failure Modes" for the recommended factoring
```
> If factoring a callable silver-derivation helper is more code than it's worth, instead commit a second
> tiny fixture containing one degenerate/empty geometry and assert the full build flags + logs it. Either
> way, **one test must exercise the FALSE branch** so flag/ledger agreement is proven, not just assumed.

Run after all steps (including the carryover flip + test rewrite):
```bash
source .venv/bin/activate
python -m pytest -q
```
Expected: **all green**, including the rewritten `test_build_with_undecided_source_is_fatal`.

**Stage ship-readiness checklist:**
- [ ] `tests/fixtures/ons/{lad_2024,ctyua_2024}/*.{shp,shx,dbf,prj}` committed + a fixtures README.
- [ ] `spatial.py` defines `_BoundaryTransformer` (abstract) + `LADBoundaryTransformer` +
      `CTYUABoundaryTransformer`; both discovered by the registry; each returns a `SourceQuality`.
- [ ] A real `build()` over the sample populates LAD+CTYUA silver, gold views, EPSG:27700 verified,
      Step 2 invariants pass.
- [ ] One test exercises the `geom_valid = FALSE` flag/ledger path.
- [ ] `UNDECIDED_QUALITY_SPEC_IS_FATAL is True`; interim test rewritten to assert the fatal path.
- [ ] `00-PENDING-carryover-from-002.md` deleted.
- [ ] No new dependency; `client.py`/`registry.py` untouched (beyond Stage 01).

## End State / Handoff
`spatial.py` exists with two discovered, audited boundary transformers. A `build()` produces
`lad_boundaries`/`ctyua_boundaries` (silver, 1:1, snapshot/latest vintage), their `_clean` gold views,
EPSG:27700 geometry, and passes all Step 2 invariants. The silver schema already carries
`valid_from`/`valid_to` (latest vintage open-ended). The coverage gate is now **fatal** on undecided
sources. Stage 03 may assume this schema and add multi-vintage temporal loading via a `boundary_mode`
kwarg. The carryover placeholder is gone.

## Failure Modes & Rollback
- **`ST_Read` column-name mismatch:** the binder errors on `LAD24CD`/`LAD24NM` etc. Fix: confirm the
  real ONS column names and update the `Vintage.code_col`/`name_col` (and the fixture `COPY SELECT` if
  you aliased). This is the single most likely break — verify against the actual download.
- **Subclass still reported abstract by the registry** (not discovered): convert the class-attribute
  identity members to `@property` methods returning the constant; re-run; note the deviation.
- **Recommended factoring for the FALSE-branch test:** keep the bronze→silver+ledger derivation in a
  small method (e.g. `_derive_silver_and_ledger(con)`) callable against a pre-built bronze, so the
  invalid-geometry test can drive it directly without an invalid shapefile.
- **Re-build doubling:** ensured by `CREATE OR REPLACE TABLE` for bronze/silver + the engine's
  `reset_source_audit`. If a same-file re-build breaks conservation, check both halves are present.
- **Rollback:** delete `spatial.py` and its tests/fixtures, revert the flag flip and the test rename,
  restore the placeholder file. The suite returns to the Stage 01 state.
