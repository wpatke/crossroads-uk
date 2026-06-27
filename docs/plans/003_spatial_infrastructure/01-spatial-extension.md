# Stage 01 — Spatial Extension (Central Load)
> Part of Spatial Infrastructure & Boundaries. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Steps 1 & 2 are merged. Verify before starting:
```bash
source .venv/bin/activate
python -m pytest -q          # all green
python -c "import duckdb; c=duckdb.connect(); c.execute('INSTALL spatial'); c.execute('LOAD spatial'); print(c.execute(\"SELECT ST_AsText(ST_Point(1,2))\").fetchone())"
# -> ('POINT (1 2)',)  (confirms the spatial extension is installable/loadable locally)
```
Observable state: `src/crossroads/client.py` exists with `Client.build()` that opens `self.con`,
calls `quality.ensure_quality_tables`, runs the transformer loop, then the coverage gate + invariants.
`build()` does **not** load the spatial extension yet.

## Objective
Make the DuckDB Spatial Extension available on every build connection by loading it centrally in
`Client.build()`, so later stages (and Step 6 weather) can use `ST_Read`, `ST_Transform`, `ST_IsValid`,
etc. without each transformer loading it. No boundary logic in this stage.

## Implementation Steps

1. **`src/crossroads/client.py` — load spatial in `build()`.**
   Immediately after `self.con = duckdb.connect(self.database_path)` and **before**
   `quality.ensure_quality_tables(self.con)`, add:
   ```python
   # Load the DuckDB Spatial Extension once, as foundational infrastructure
   # (spec §5 Phase 1). It is generic (names no data source, so provider-plugin
   # purity holds), idempotent, and cheap. INSTALL needs the network only on the
   # first run on a machine; thereafter the extension is cached locally. Every
   # spatial source (boundaries now, weather later) relies on this being loaded.
   self.con.execute("INSTALL spatial")
   self.con.execute("LOAD spatial")
   ```
   Expected result: after any `build()`, spatial SQL functions are callable on `self.con`.

2. **No other file changes.** Do not touch `registry.py`, `quality.py`, or `base.py` in this stage.

## Testing & Verification

Add to a **new** `tests/test_spatial.py`:

```python
import crossroads


def test_build_loads_spatial_extension():
    # After an (empty) build, spatial functions must be available on the connection.
    client = crossroads.init_engine()  # in-memory
    client.build()
    # ST_Point + ST_AsText prove the extension loaded.
    assert client.con.execute(
        "SELECT ST_AsText(ST_Point(1, 2))"
    ).fetchone()[0] == "POINT (1 2)"
    # Reprojection EPSG:27700 -> EPSG:4326 must work (used by later steps).
    lon_lat = client.con.execute(
        "SELECT ST_X(g), ST_Y(g) FROM ("
        "  SELECT ST_Transform(ST_Point(530000, 180000), 'EPSG:27700', 'EPSG:4326') AS g"
        ")"
    ).fetchone()
    # Central London-ish: latitude ~51.5, longitude ~ -0.13 (axis order lon=X,lat=Y).
    assert 50.0 < lon_lat[0] < 53.0     # X here is latitude under EPSG:4326 axis order
    client.close()


def test_existing_empty_build_still_succeeds():
    # Loading spatial must not break the zero-transformer no-op build.
    client = crossroads.init_engine()
    client.build()
    assert client.con.execute(
        "SELECT count(*) FROM data_quality_log"
    ).fetchone()[0] == 0
    client.close()
```

> Note on axis order: under EPSG:4326 DuckDB returns coordinates in (latitude, longitude) =
> (X, Y) order for `ST_Transform`. The assertion checks the latitude band to stay robust; if your
> DuckDB build orders them (lon, lat), adapt the assertion to the longitude band (`-1.0 < x < 1.0`)
> and note the deviation. The essential proof is that reprojection runs and yields a sane GB coordinate.

Run:
```bash
source .venv/bin/activate
python -m pytest -q
```
Expected: **all tests pass**, including the pre-existing Step 1/2 suite (now exercising a
spatial-loaded `build()`). If `INSTALL spatial` fails for network reasons, confirm the machine can
reach the DuckDB extension repository once; after the first success it is cached.

**Stage ship-readiness checklist:**
- [ ] `client.py` loads spatial in `build()` before `ensure_quality_tables`.
- [ ] `tests/test_spatial.py` proves spatial functions + reprojection are available post-build.
- [ ] Pre-existing Step 1/2 tests remain green.
- [ ] No new dependency added; `registry.py`/`quality.py`/`base.py` untouched.

## End State / Handoff
`Client.build()` guarantees the spatial extension is loaded on `self.con`. Stage 02 may assume
`ST_Read`, `ST_Transform`, `ST_IsValid`, `ST_Point`, `ST_X`/`ST_Y`, `ST_Extent`, and
`CREATE INDEX ... USING RTREE` are all available inside any transformer's `transform_and_load`.

## Failure Modes & Rollback
- **`INSTALL spatial` blocked (offline/firewalled):** first build on a fresh machine fails. Mitigation:
  run the install once with network access (cached thereafter). Not a code bug.
- **Existing build() tests fail after the change:** indicates an ordering issue — ensure spatial loads
  *before* `ensure_quality_tables` and that nothing else changed. Rollback = remove the two
  `self.con.execute(...)` lines; `build()` returns to its Step 2 behaviour.
