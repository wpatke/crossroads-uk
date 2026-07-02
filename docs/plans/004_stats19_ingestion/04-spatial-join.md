# Stage 04 — Spatial Join & `collisions_spatial` Gold View
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map + Approach first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stages 01–03 are done: all three STATS19 silver tables exist, collisions carry `geom`/`geom_valid`/
`datetime_local` and `lad_code`/`ctyua_code` **placeholders (all NULL)**, vehicles/casualties carry
`link_valid`. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: no spatial join runs yet (`lad_code`/`ctyua_code` are always NULL); there is no
`collisions_spatial` gold view; no R-Tree on `collisions.geom`.

## Objective
Point-in-polygon **join valid collision points to the ONS boundaries** built by Step 3, stamping each
collision's `lad_code` / `ctyua_code`. **Snapshot is the default** (join against the latest boundary
vintage); `boundary_mode="temporal"` range-joins each point to the vintage whose `[valid_from, valid_to)`
window contains the incident date. Expose the `collisions_spatial` clean view (spec §9 worked example) and
an R-Tree on `collisions.geom`. This is the cumulative Step 4 ship stage.

## Implementation Steps

### A. Add the spatial stamp (`src/crossroads/transformers/stats19.py`)

```python
import warnings   # add to the imports at the top of the module

    # ... inside Stats19Transformer ...

    def _table_exists(self, con, name):
        return con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [name]).fetchone()[0] > 0

    def _boundary_predicate(self, mode):
        """Extra ON-clause for the point-in-polygon join.

        snapshot (default): only the current boundary vintage (valid_to IS NULL).
        temporal: the vintage whose [valid_from, valid_to) window contains the
                  incident date (CAST(datetime_local AS DATE)); a NULL datetime
                  matches nothing (cannot be placed in time) and is left unstamped.
        """
        if mode == "temporal":
            return ("AND b.valid_from <= CAST(c2.datetime_local AS DATE) "
                    "AND (b.valid_to IS NULL "
                    "     OR CAST(c2.datetime_local AS DATE) < b.valid_to)")
        return "AND b.valid_to IS NULL"

    def _spatial_stamp(self, con):
        """Stamp lad_code/ctyua_code onto valid collision points via point-in-polygon
        against the Step 3 boundary silver tables. Defensive: if a boundary table is
        absent (e.g. a stats19-only build/test), leave that code NULL and warn — the
        pipeline still succeeds. ST_Contains needs the boundary R-Tree (built in Step 3)
        to stay fast (spec §5). area_code is aggregated with min() for a deterministic
        result even if polygons were to overlap (they should not within one vintage)."""
        mode = getattr(self, "_boundary_mode", "snapshot")
        pred = self._boundary_predicate(mode)
        for code_col, btable in (("lad_code", "lad_boundaries"),
                                 ("ctyua_code", "ctyua_boundaries")):
            if not self._table_exists(con, btable):
                warnings.warn(
                    f"stats19: boundary table {btable} not found; {code_col} left NULL "
                    f"(build boundaries alongside stats19 to enable the spatial join).",
                    stacklevel=2)
                continue
            con.execute(
                f"UPDATE {self.COLLISION_SILVER} AS c SET {code_col} = m.area_code "
                f"FROM ("
                f"  SELECT c2.source_row_key AS k, min(b.area_code) AS area_code "
                f"  FROM {self.COLLISION_SILVER} c2 JOIN {btable} b "
                f"    ON c2.geom IS NOT NULL AND b.geom_valid = TRUE "
                f"       AND ST_Contains(b.geom, c2.geom) {pred} "
                f"  GROUP BY c2.source_row_key"
                f") m WHERE c.source_row_key = m.k"
            )
```

> `lad_code`/`ctyua_code` deliberately get **no quality dimension**: a valid point can legitimately fall
> outside every loaded boundary polygon (rural area, or outside a trimmed sample), so a NULL code is not a
> data defect and must not be flagged as a rejection.

### B. Wire the stamp, the gold view, and the R-Tree into `transform_and_load`

After the three silver derivations (and the vehicle/casualty gold views from Stage 03), add:
```python
        # --- SPATIAL STAMP: valid collision points -> LAD/CTYUA codes. ---
        self._spatial_stamp(con)

        # --- GOLD: the valid-geometry collision projection (spec §9 worked example). ---
        create_clean_view(con, "collisions_spatial", self.COLLISION_SILVER, ["geom_valid"])

        # --- INDEX: R-Tree on collision geometry for downstream spatial queries.
        # Built AFTER the stamp UPDATE so the index is not maintained during the update.
        # collisions was CREATE OR REPLACE'd (dropping any prior index); the DROP is
        # belt-and-suspenders. NULL geom rows are skipped by the RTREE without error.
        con.execute("DROP INDEX IF EXISTS collisions_geom_rtree")
        con.execute(
            f"CREATE INDEX collisions_geom_rtree ON {self.COLLISION_SILVER} USING RTREE (geom)")
```
Confirm the final `transform_and_load` order is: bronze ×3 → `_derive_collision_silver` →
`_derive_vehicle_silver` → `_derive_casualty_silver` → vehicle/casualty gold views → `_spatial_stamp` →
`collisions_spatial` → R-Tree.

### C. Align the collision fixture with the boundary fixtures (so the e2e actually stamps)

The Stage-01 collision fixture was trimmed by `LIMIT`, so its coordinates likely fall **outside** the tiny
committed ONS polygons and nothing would stamp. Re-trim it (once, on a networked machine) to collisions
that lie **inside** the committed LAD sample, preserving child referential integrity. This supersedes
Stage 01 step C's plain `LIMIT`.
```bash
source .venv/bin/activate
python - <<'PY'
import duckdb
c = duckdb.connect(); c.execute("INSTALL spatial"); c.execute("LOAD spatial")
SRC, OUT = "/scratch", "tests/fixtures/stats19"
# The e2e build seeds the cache from the NEWEST LAD vintage's fixture. Match it:
# find the newest vintage's fixture year (see tests/fixtures/ons/lad_<year>/).
LAD_FIXTURE = "tests/fixtures/ons/lad_2025/lad_sample.geojson"   # adjust to newest committed vintage
c.execute(f"CREATE TABLE lad AS SELECT geom FROM ST_Read('{LAD_FIXTURE}')")
c.execute(f"CREATE TABLE col AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-collision-2023.csv', all_varchar=true)")
c.execute(f"CREATE TABLE veh AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-vehicle-2023.csv', all_varchar=true)")
c.execute(f"CREATE TABLE cas AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-casualty-2023.csv', all_varchar=true)")
# Collisions whose OSGR point falls inside a committed LAD polygon.
c.execute("""CREATE TABLE cols AS
  SELECT col.* FROM col
  WHERE col.location_easting_osgr NOT IN ('-1','0','')
    AND col.location_northing_osgr NOT IN ('-1','0','')
    AND EXISTS (SELECT 1 FROM lad
                WHERE ST_Contains(lad.geom,
                      ST_Point(CAST(col.location_easting_osgr AS DOUBLE),
                               CAST(col.location_northing_osgr AS DOUBLE))))
  ORDER BY col.accident_index LIMIT 8""")
n = c.execute("SELECT count(*) FROM cols").fetchone()[0]
assert n >= 1, "No sample collisions fall inside the LAD fixture; widen the LAD sample or use CTYUA."
c.execute("CREATE TABLE vehs AS SELECT v.* FROM veh v SEMI JOIN cols USING (accident_index)")
c.execute("CREATE TABLE cass AS SELECT k.* FROM cas k SEMI JOIN cols USING (accident_index)")
for tbl, name in (("cols","collision"),("vehs","vehicle"),("cass","casualty")):
    c.execute(f"COPY {tbl} TO '{OUT}/dft-road-casualty-statistics-{name}-2023.csv' (HEADER, DELIMITER ',')")
print("aligned collisions:", n)
PY
```
Update `tests/fixtures/stats19/README.md` to note the collisions were selected to intersect the committed
LAD sample. Re-run the whole suite after re-trimming (row counts in earlier tests are `> 0` checks, so they
stay valid).

> **If you cannot reach the network to re-trim:** keep the Stage-01 fixture and rely on the **synthetic**
> stamp tests (step D) as the authoritative correctness proof; then relax the e2e `stamped >= 1` assertion
> to the consistency-only assertion (stamped codes ⊆ boundary codes). Note the deviation. The synthetic
> tests fully prove the join logic regardless of fixture geography.

### D. Tests — `tests/test_stats19.py`

**Unit (authoritative stamp correctness, synthetic — independent of fixture geography):**
```python
def _stub_boundaries(con):
    # Two boundary silver stubs with one polygon each, current vintage (valid_to NULL).
    for tbl, code in (("lad_boundaries", "E-LAD"), ("ctyua_boundaries", "E-CTY")):
        con.execute(
            f"CREATE TABLE {tbl} AS SELECT * FROM (VALUES "
            f"  ('{code}','Area', "
            f"   ST_GeomFromText('POLYGON((0 0,0 100,100 100,100 0,0 0))'), TRUE, "
            f"   DATE '2020-01-01', CAST(NULL AS DATE))"
            f") AS t(area_code, area_name, geom, geom_valid, valid_from, valid_to)")


def _stub_collisions(con, rows):
    # rows: list of (key, easting, northing, iso_datetime)
    values = ", ".join(
        f"('{k}', ST_Point({e},{n})::GEOMETRY, TRUE, TIMESTAMP '{dt}', "
        f" CAST(NULL AS VARCHAR), CAST(NULL AS VARCHAR))"
        for k, e, n, dt in rows)
    con.execute(
        f"CREATE TABLE collisions AS SELECT * FROM (VALUES {values}) "
        f"AS t(source_row_key, geom, geom_valid, datetime_local, lad_code, ctyua_code)")


def test_spatial_stamp_snapshot(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    _stub_boundaries(con)
    _stub_collisions(con, [("k_in", 50, 50, "2023-01-01 08:00"),
                           ("k_out", 500, 500, "2023-01-01 08:00")])   # inside / outside
    t = Stats19Transformer(); t._boundary_mode = "snapshot"; t._spatial_stamp(con)
    res = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT source_row_key, lad_code, ctyua_code FROM collisions").fetchall()}
    assert res["k_in"] == ("E-LAD", "E-CTY")      # point inside -> stamped
    assert res["k_out"] == (None, None)           # point outside -> unstamped


def test_spatial_stamp_temporal_picks_window(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    # Same polygon under two vintages with different codes and adjacent windows.
    con.execute(
        "CREATE TABLE lad_boundaries AS SELECT * FROM (VALUES "
        "  ('OLD','Old', ST_GeomFromText('POLYGON((0 0,0 100,100 100,100 0,0 0))'), TRUE, DATE '2010-01-01', DATE '2020-01-01'), "
        "  ('NEW','New', ST_GeomFromText('POLYGON((0 0,0 100,100 100,100 0,0 0))'), TRUE, DATE '2020-01-01', CAST(NULL AS DATE))"
        ") AS t(area_code, area_name, geom, geom_valid, valid_from, valid_to)")
    _stub_collisions(con, [("k_2015", 50, 50, "2015-06-01 08:00"),
                           ("k_2023", 50, 50, "2023-06-01 08:00")])
    t = Stats19Transformer(); t._boundary_mode = "temporal"; t._spatial_stamp(con)
    res = {r[0]: r[1] for r in con.execute(
        "SELECT source_row_key, lad_code FROM collisions").fetchall()}
    assert res["k_2015"] == "OLD" and res["k_2023"] == "NEW"


def test_spatial_stamp_tolerates_missing_boundary_table(con):
    # No boundary tables -> codes stay NULL and a warning is emitted (build still works).
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    _stub_collisions(con, [("k1", 50, 50, "2023-01-01 08:00")])
    t = Stats19Transformer(); t._boundary_mode = "snapshot"
    with pytest.warns(UserWarning, match="boundary table"):
        t._spatial_stamp(con)
    assert con.execute("SELECT lad_code FROM collisions").fetchone()[0] is None
```

**Integration (cumulative end-to-end ship proof — boundaries + stats19 together, offline):**
```python
import shutil
from crossroads.transformers.spatial import LADBoundaryTransformer, CTYUABoundaryTransformer

ONS_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ons")


def _seed_ons_cache(cache_dir):
    # Copy each committed ONS fixture to the name the newest vintage expects (mirrors
    # tests/test_spatial.py::_seed_cache).
    for prefix, cls in (("lad", LADBoundaryTransformer), ("ctyua", CTYUABoundaryTransformer)):
        newest = cls().vintages[-1]
        year = newest.valid_from[:4]
        src = os.path.join(ONS_FIXTURES, f"{prefix}_{year}", f"{prefix}_sample.geojson")
        shutil.copy(src, os.path.join(cache_dir, newest.source_file))


def _full_client(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_cache(cache)          # stats19 CSVs (from Stage 01 helper)
    _seed_ons_cache(cache)      # ONS boundary geojson
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [
        CTYUABoundaryTransformer(), LADBoundaryTransformer(), Stats19Transformer()]
    return client


def test_end_to_end_build_stamps_collisions(tmp_path):
    client = _full_client(tmp_path)
    client.build(years=YEARS)          # snapshot default; runs all Step 2 invariants

    # collisions_spatial view == valid-geometry collisions.
    n_view = client.con.execute("SELECT count(*) FROM collisions_spatial").fetchone()[0]
    n_valid = client.con.execute(
        "SELECT count(*) FROM collisions WHERE geom_valid").fetchone()[0]
    assert n_view == n_valid and n_valid > 0

    # Every stamped code is a real LAD code (consistency).
    bad = client.con.execute(
        "SELECT count(*) FROM collisions WHERE lad_code IS NOT NULL "
        "AND lad_code NOT IN (SELECT area_code FROM lad_boundaries)").fetchone()[0]
    assert bad == 0

    # At least one collision stamped (requires the aligned fixture from step C).
    stamped = client.con.execute(
        "SELECT count(*) FROM collisions WHERE lad_code IS NOT NULL").fetchone()[0]
    assert stamped >= 1, ("No collisions stamped — re-trim the collision fixture to fall "
                          "inside the committed LAD sample (Stage 04 step C).")

    # R-Tree exists on collisions.geom.
    idx = {r[0] for r in client.con.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'collisions'").fetchall()}
    assert "collisions_geom_rtree" in idx
    client.close()


def test_rebuild_same_file_is_idempotent(tmp_path):
    # A second build against the SAME on-disk DB must not double rows or break invariants.
    db = str(tmp_path / "s.db")
    cache = str(tmp_path / "cache")
    _seed_cache(cache); _seed_ons_cache(cache)

    def run():
        cl = crossroads.init_engine(database_path=db, cache_dir=cache)
        cl.registry._transformers = [
            CTYUABoundaryTransformer(), LADBoundaryTransformer(), Stats19Transformer()]
        cl.build(years=YEARS)
        return cl

    first = run(); n1 = first.con.execute("SELECT count(*) FROM collisions").fetchone()[0]; first.close()
    second = run(); n2 = second.con.execute("SELECT count(*) FROM collisions").fetchone()[0]
    assert n1 == n2 and n2 > 0
    assert second.con.execute(
        "SELECT count(*) FROM duckdb_indexes() WHERE table_name='collisions'").fetchone()[0] == 1
    second.close()
```

**Optional — differential sanity vs the reference package's assertions (inspiration only, no R run).**
Read `../stats19/tests/testthat/test-read.R` for the properties it checks (e.g. `-1`/blank → `NA`;
non-numeric eastings → `NA`; a known sample row count / `collision_index`) and confirm the equivalent
holds in our silver via a plain SQL assertion on the committed sample. Do **not** import or execute any R.

Run the whole suite:
```bash
source .venv/bin/activate
python -m pytest -q                    # expected: all green
python -m pytest -m integration -q     # opt-in: real DfT + ONS downloads
```

## Testing & Verification
**Integration (PRIMARY):** `test_end_to_end_build_stamps_collisions` is the cumulative Step 4 ship proof —
a real offline build of boundaries + STATS19 that ingests all three CSV types, casts coordinates to
EPSG:27700, links vehicles/casualties, stamps valid collisions with real LAD/CTYUA codes, exposes
`collisions_spatial`, builds the R-Tree, and passes **all** Step 2 invariants (the build raises otherwise).
`test_rebuild_same_file_is_idempotent` proves re-build safety.

**Unit:** the three `_spatial_stamp` tests prove snapshot containment, temporal window selection, and the
missing-boundary guard deterministically, independent of fixture geography.

**Ship-readiness checklist (Step 4 complete):**
- [ ] `_spatial_stamp` fills `lad_code`/`ctyua_code` for valid points; snapshot default + temporal window
      option; missing-boundary tables tolerated (warn, codes NULL).
- [ ] `collisions_spatial` gold view = `collisions WHERE geom_valid`.
- [ ] R-Tree `collisions_geom_rtree` on `collisions.geom`.
- [ ] End-to-end build (boundaries + stats19) stamps ≥1 collision with a real LAD code, sentinel points
      left unstamped, all three Step 2 invariants pass.
- [ ] Re-build against the same on-disk DB is idempotent (no doubled rows, one index).
- [ ] `lad_code`/`ctyua_code` carry **no** quality dimension (a NULL code for an out-of-area point is not
      a defect).
- [ ] `python -m pytest -q` fully green; `pyproject.toml` deps unchanged.

## End State / Handoff
STATS19 ingestion is complete: `collisions`/`vehicles`/`casualties` silver populated from real DfT CSVs;
collision coordinates cast to EPSG:27700 with sentinels flagged and logged (retained); datetime assembled;
vehicles/casualties linked; valid collisions stamped with LAD/CTYUA codes (snapshot default, temporal
optional); `collisions_spatial` clean view and an R-Tree in place; every build audited green by the Step 2
invariants. This satisfies the master plan's Step 4 "Done when" and gives Step 5 (console) and Step 6
(weather) a populated, spatially-joined collision layer to build on. The spec §8 flow
`build(years=[...], spatial_grain="local_authority")` now produces a queryable collision database.

## Failure Modes & Rollback
- **Nothing stamps in the e2e (`stamped >= 1` fails):** the collision fixture does not intersect the LAD
  fixture — re-trim per step C (or widen the ONS sample). The synthetic tests still prove the logic.
- **Duplicate-match nondeterminism:** if a point matched multiple polygons the `min(area_code)` aggregate
  keeps it deterministic; if it happens in real data, the boundary layer has overlaps (a Step 3 data
  issue), not a Step 4 bug.
- **`UPDATE ... FROM` unsupported / errors:** DuckDB supports `UPDATE ... FROM (subquery)`; if a version
  quirk appears, rebuild `collisions` with a `LEFT JOIN` to the stamp mapping in a `CREATE OR REPLACE`
  instead (note the deviation; keep the R-Tree built afterward).
- **Boundary R-Tree not used / join slow at scale:** verify Step 3's boundary R-Tree exists; the point-in-
  polygon join relies on it (spec §5). This is a performance concern, not correctness.
- **Rollback:** remove `_spatial_stamp`/`_boundary_predicate`/`_table_exists`, the stamp/gold/index lines
  in `transform_and_load`, and the new tests; restore the Stage-01 collision fixture. The suite returns to
  the Stage 03 state.
