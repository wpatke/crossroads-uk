# Stage 04 — R-Tree Spatial Indices
> Part of Spatial Infrastructure & Boundaries. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stages 01–03 are done: `spatial.py` ingests LAD+CTYUA boundaries (snapshot + temporal modes) through
bronze → silver → gold, EPSG:27700 verified, Step 2 invariants passing. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
python -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL spatial'); c.execute('LOAD spatial'); c.execute('CREATE TABLE t(g GEOMETRY)'); c.execute('CREATE INDEX i ON t USING RTREE (g)'); print('RTREE ok')"
```
Confirm `transform_and_load` currently builds bronze → silver → ledger → `record_source_rows` → gold
view (no index step yet).

## Objective
Build a bounding-box **R-Tree** index on each boundary silver table's `geom` column, inside
`transform_and_load` (so every build has it and re-builds stay idempotent). Verify the indices exist and
that all Step 2 invariants still pass. This is the index that makes Step 4's collision point-in-polygon
joins fast instead of an unindexed cross-join (spec §5).

## Implementation Steps

1. **`src/crossroads/transformers/spatial.py` — add index creation to `transform_and_load`.**
   As the **last** step of `transform_and_load` (after the gold view), add an R-Tree index on the silver
   geometry. Use a deterministic index name per source and drop-then-create so re-builds are idempotent
   (DuckDB has no `CREATE INDEX IF NOT EXISTS` for custom indexes, and `CREATE OR REPLACE TABLE` for
   silver drops any existing index with it — but be explicit and safe):
   ```python
   # Build the bounding-box R-Tree over the silver geometry. This is what makes
   # Step 4's point-in-polygon joins fast (spec §5: avoids an unindexed spatial
   # cross-join). The silver table was just (re)created via CREATE OR REPLACE, so
   # no prior index survives; we create a fresh one with a deterministic name.
   # Identifiers are code-controlled constants (trusted interpolation).
   index_name = f"{self.silver_table}_geom_rtree"
   con.execute(f"DROP INDEX IF EXISTS {index_name}")
   con.execute(
       f"CREATE INDEX {index_name} ON {self.silver_table} USING RTREE (geom)"
   )
   ```
   > NULL geometry is fine here. A flagged-invalid boundary carries `geom IS NULL` (spec §9), and
   > DuckDB's RTREE skips NULL rows: the index builds without error and the NULL row is simply absent
   > from spatial results (verified on DuckDB 1.5.4; guarded by `test_rtree_index_tolerates_null_geometry`).
   > The `duckdb>=1.5` lower bound in `pyproject.toml` locks in a version with this behaviour.

2. **No other code changes.** `client.py`, `registry.py`, `quality.py`, `base.py` untouched.

## Testing & Verification

Add to `tests/test_spatial.py`:

```python
def test_rtree_index_exists_on_boundary_tables(tmp_path):
    client = _boundary_client(tmp_path)
    client.build()
    # duckdb_indexes() lists user indexes; assert an index exists on each silver table.
    idx = client.con.execute(
        "SELECT table_name, index_name FROM duckdb_indexes() "
        "WHERE table_name IN ('lad_boundaries', 'ctyua_boundaries')"
    ).fetchall()
    tables_with_index = {r[0] for r in idx}
    assert "lad_boundaries" in tables_with_index
    assert "ctyua_boundaries" in tables_with_index
    # The index name follows the deterministic convention.
    names = {r[1] for r in idx}
    assert "lad_boundaries_geom_rtree" in names
    client.close()


def test_rebuild_against_same_file_keeps_one_index(tmp_path):
    # A second build against the SAME on-disk database must not error on a
    # duplicate index and must leave exactly one index per silver table.
    db_path = str(tmp_path / "b.db")
    cache = str(tmp_path / "cache")
    _seed_cache(cache)

    def run_once():
        client = crossroads.init_engine(database_path=db_path, cache_dir=cache)
        client.registry._transformers = [
            CTYUABoundaryTransformer(), LADBoundaryTransformer(),
        ]
        client.build()
        return client

    first = run_once(); first.close()
    second = run_once()           # must not raise on the duplicate-index path
    count = second.con.execute(
        "SELECT count(*) FROM duckdb_indexes() WHERE table_name = 'lad_boundaries'"
    ).fetchone()[0]
    assert count == 1
    # Invariants still pass (index creation changes no row counts).
    assert second.con.execute("SELECT count(*) FROM lad_boundaries").fetchone()[0] == 3
    second.close()


def test_spatial_predicate_works(tmp_path):
    # Functional proof a spatial predicate runs correctly against the indexed
    # table (index present and not breaking queries; not a proof the planner
    # uses it — real perf validation lands in Step 4's point-in-polygon joins).
    client = _boundary_client(tmp_path)
    client.build()
    # A point known to fall inside one of the sample polygons should match exactly
    # that polygon via ST_Contains. (Pick a point from inside a fixture polygon's
    # extent; ST_Centroid of a row is guaranteed inside a convex-ish polygon, and is
    # a safe choice for the sample.)
    hit = client.con.execute(
        "SELECT count(*) FROM lad_boundaries "
        "WHERE ST_Contains(geom, (SELECT ST_Centroid(geom) FROM lad_boundaries LIMIT 1))"
    ).fetchone()[0]
    assert hit >= 1
    client.close()


def test_rtree_index_tolerates_null_geometry():
    # A flagged-invalid boundary carries geom = NULL (spec §9). The R-Tree build
    # must not error on it, and the NULL row must simply be absent from spatial
    # results rather than breaking the query. Guards against a DuckDB version bump
    # changing NULL handling.
    con = duckdb.connect()
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    con.execute("CREATE TABLE t (id INT, geom GEOMETRY)")
    con.execute("INSERT INTO t VALUES "
                "(1, ST_GeomFromText('POLYGON((0 0,0 10,10 10,10 0,0 0))')), "
                "(2, NULL)")                     # the flagged-invalid case
    con.execute("CREATE INDEX t_geom_rtree ON t USING RTREE (geom)")  # must not raise
    hit = con.execute(
        "SELECT id FROM t WHERE ST_Contains(geom, ST_Point(5,5))").fetchall()
    assert hit == [(1,)]                          # valid polygon matched, NULL ignored
```
> `test_rtree_index_tolerates_null_geometry` opens its own `duckdb.connect()` (it tests raw DuckDB
> behaviour, not the client), so add `import duckdb` to the top of `tests/test_spatial.py` if it is not
> already imported. Verified on DuckDB 1.5.4: the RTREE build skips NULL geometries and the NULL row is
> simply absent from spatial results.

> `duckdb_indexes()` column names can vary slightly by version. If `index_name`/`table_name` differ,
> adjust to the actual columns (run `DESCRIBE (SELECT * FROM duckdb_indexes())` once) and note it. The
> essential assertion is that an index exists per boundary silver table and a re-build leaves exactly one.
> If `ST_Centroid` of a concave polygon falls outside it and `test_spatial_predicate_works` is flaky,
> replace the point with a hard-coded easting/northing known to be inside a sample polygon and document it.

Run:
```bash
source .venv/bin/activate
python -m pytest -q
```
Expected: all green.

**Stage ship-readiness checklist:**
- [ ] `transform_and_load` builds an R-Tree index on the silver `geom` after the gold view.
- [ ] Index exists on `lad_boundaries` and `ctyua_boundaries` (verified via `duckdb_indexes()`).
- [ ] Same-file re-build is idempotent — exactly one index per table, no duplicate-index error.
- [ ] Step 2 invariants still pass after indexing; a spatial predicate query works.
- [ ] R-Tree build tolerates a NULL (flagged-invalid) geometry; the NULL row is absent from spatial results, not an error.
- [ ] No new dependency added; the `duckdb` lower bound is raised to `>=1.5` (guarantees RTREE + NULL handling). Only `spatial.py` changed among engine source.

## End State / Handoff
Every `build()` leaves a bounding-box R-Tree index on each boundary silver table's geometry. The Phase 1
spatial base layer is complete: DuckDB Spatial loaded centrally, LAD/CTYUA boundaries ingested in
EPSG:27700 through the audited bronze/silver/gold model with snapshot + temporal drift support, and
R-Tree indices in place. **Step 4 (stats19) may now** cast collision Eastings/Northings to EPSG:27700
geometry and run indexed point-in-polygon joins against `lad_boundaries`/`ctyua_boundaries`
(using `valid_from`/`valid_to` for temporally-sliced joins when `boundary_mode="temporal"`).

## Failure Modes & Rollback
- **Duplicate-index error on re-build:** the `DROP INDEX IF EXISTS` before `CREATE INDEX` prevents it; if
  it still errors, confirm the silver table's `CREATE OR REPLACE` ran first (which removes the old index)
  and that the index name is deterministic, not randomised.
- **RTREE rejects NULL geometry:** not expected — DuckDB 1.5.4 builds the index fine over NULL
  geometries and the `duckdb>=1.5` bound guarantees such a version (verified; covered by
  `test_rtree_index_tolerates_null_geometry`). If a future DuckDB regresses this, that test fails
  loudly; the fallback is to keep invalid geometry non-NULL-but-flagged.
- **`duckdb_indexes()` schema differs:** adapt the test columns as noted above.
- **Rollback:** remove the `DROP INDEX`/`CREATE INDEX` lines from `transform_and_load` and delete the
  index tests. Boundaries still ingest correctly (Stage 03 state); only the spatial index is absent.
