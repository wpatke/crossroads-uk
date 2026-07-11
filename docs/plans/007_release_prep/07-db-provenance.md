# Stage 07 — Database Provenance Stamp (`crossroads_meta` + `schema_version`)
> Part of Release Preparation (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Depends on Stage 01 (the version is single-sourced in `src/crossroads/__init__.py`). Verify:
```bash
cd /Users/will/Documents/Code/Crossroads
grep -n "__version__\|SCHEMA_VERSION" src/crossroads/__init__.py   # expect __version__; NO SCHEMA_VERSION yet
grep -n "def build" src/crossroads/client.py                       # the stamp call goes here
grep -n "def ensure_quality_tables\|def run_invariants" src/crossroads/quality.py
python -m pytest -q                                                # baseline green
```
Today the build writes **no** record of what produced a database — there is no metadata/provenance
table (only per-row `ingested_at` stamps). This stage adds one.

## Objective

Make every database self-describing: stamp a single-row `crossroads_meta` table into each build
carrying the Crossroads version, a monotonic integer `schema_version`, a UTC build timestamp, and the
build parameters. A researcher holding a `.db` file can then run `SELECT * FROM crossroads_meta` to
learn exactly what shape it is and what made it. This is the durable home of the "schema number"
concept — the package version follows SemVer (Stage 01), the *database* carries its own schema
version.

## Implementation Steps

**Step 1 — Add a `SCHEMA_VERSION` constant** to `src/crossroads/__init__.py`, alongside the
`__version__` read (which comes from package metadata, not a literal — see Stage 01). Add it after
the existing `__version__` assignment:
```python
# ... __version__ is read from importlib.metadata above (Stage 01) ...

# Monotonic integer describing the physical shape of the built database (tables, columns,
# views). Increment by 1 on ANY schema change (new column, new table, new datasource,
# renamed/removed field). It is a plain literal here (hand-maintained), independent of the
# git-derived package version. A schema change is also a MINOR (or MAJOR, if breaking)
# release — see CHANGELOG.md.
SCHEMA_VERSION = 1
```
Add `"SCHEMA_VERSION"` to `__all__` if `__all__` lists data constants (it currently lists `Client`,
`init_engine`; adding the constant is optional but tidy).

**Step 2 — Add `write_build_metadata` to `src/crossroads/quality.py`** (which already owns the shared,
non-audited tables). Place it near `ensure_quality_tables`:
```python
def write_build_metadata(con, *, parameters):
    """Stamp a single-row crossroads_meta table describing what built this database.

    Provenance only — like the reference tables (codebook, manifest, boundaries), it has no
    source_id, no bronze/silver pair, and is NOT part of the conservation invariant. Re-running
    build() replaces the row (CREATE OR REPLACE), so the table always reflects the latest build.
    """
    import json
    import crossroads  # lazy import: avoids a circular import at module load time

    con.execute(
        "CREATE OR REPLACE TABLE crossroads_meta ("
        " crossroads_version VARCHAR,"
        " schema_version INTEGER,"
        " built_at_utc TIMESTAMP,"      # UTC provenance stamp; see reproducibility note below
        " parameters VARCHAR)"          # JSON of the build parameters (datasets, years, ...)
    )
    con.execute(
        "INSERT INTO crossroads_meta VALUES (?, ?, now() AT TIME ZONE 'UTC', ?)",
        [crossroads.__version__, crossroads.SCHEMA_VERSION,
         json.dumps(parameters, default=str, sort_keys=True)],
    )
```
> `now() AT TIME ZONE 'UTC'` yields a naive `TIMESTAMP` holding UTC wall-clock, matching the spec's
> provenance-timestamp convention (§3B). If `quality.py` already has a house style for UTC stamps
> (e.g. how `ingested_at` is produced), match it and note the choice.
>
> **Reproducibility note (state it in the docstring or a comment):** `built_at_utc` differs run-to-run
> by design, so it is a *provenance* field explicitly excluded from spec §2's "structurally identical
> database" guarantee (which the spec already carves out for machine-stamped provenance timestamps).
> It does not weaken reproducibility; it records it.

**Step 3 — Call it at the end of a successful build.** File: `src/crossroads/client.py`, method
`build`. After the invariants pass and before `return self`:
```python
        quality.run_invariants(self.con, specs, default_ceiling=default_ceiling)
        # Stamp build provenance LAST, so only a database that passed the invariants is recorded.
        quality.write_build_metadata(self.con, parameters=kwargs)
        return self
```
> Passing the raw `kwargs` captures exactly what the caller requested (datasets, years,
> boundary_mode, and any `reject_ceiling`). That is the honest record of the build.

Expected result: after any build, `SELECT * FROM crossroads_meta` returns one row with the current
version, `schema_version = 1`, a UTC timestamp, and the parameters JSON.

## Testing & Verification

**Fast default-suite test (PRIMARY — runs in CI).** The existing full-build offline tests are
`@pytest.mark.integration` (deselected by default), so add a fast, real-DuckDB test that runs in the
default suite. Create `tests/test_provenance.py`:
```python
"""crossroads_meta provenance stamp — fast, offline, real DuckDB (no fixtures needed)."""
import json

import duckdb

import crossroads
from crossroads import quality


def test_write_build_metadata_single_row():
    con = duckdb.connect(":memory:")
    params = {"datasets": ["stats19"], "years": [2023], "boundary_mode": "snapshot"}
    quality.write_build_metadata(con, parameters=params)

    rows = con.execute(
        "SELECT crossroads_version, schema_version, built_at_utc, parameters FROM crossroads_meta"
    ).fetchall()
    assert len(rows) == 1
    version, schema_version, built_at, params_json = rows[0]
    assert version == crossroads.__version__      # git-derived; not pinned to a literal
    assert schema_version == crossroads.SCHEMA_VERSION
    assert built_at is not None                          # UTC stamp present
    assert json.loads(params_json)["years"] == [2023]    # parameters captured faithfully


def test_write_build_metadata_is_idempotent():
    # A re-build must not accumulate rows — CREATE OR REPLACE keeps exactly one.
    con = duckdb.connect(":memory:")
    quality.write_build_metadata(con, parameters={"datasets": ["stats19"]})
    quality.write_build_metadata(con, parameters={"datasets": ["stats19", "era5_weather"]})
    n = con.execute("SELECT count(*) FROM crossroads_meta").fetchone()[0]
    assert n == 1
    latest = con.execute("SELECT parameters FROM crossroads_meta").fetchone()[0]
    assert "era5_weather" in latest                       # reflects the LATEST build
```
Run:
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
python -m pytest tests/test_provenance.py -q
```
Expected: 2 passed.

**Integration test (deselected; run deliberately) — the stamp appears after a real build.** Extend
`tests/test_console.py`'s existing offline build (`test_wizard_builds_populated_database_offline`) or
add an integration test that reuses `_seed_full_cache`, then asserts:
```python
    # crossroads_meta stamped by the real build
    row = client.con.execute(
        "SELECT crossroads_version, schema_version FROM crossroads_meta"
    ).fetchone()
    assert row == (crossroads.__version__, crossroads.SCHEMA_VERSION)
```
Run with: `python -m pytest -m integration tests/test_console.py -q`.

**Invariant safety check.** Confirm the new table did not break the conservation invariant (it must
be exempt, like the reference tables):
```bash
python -m pytest -q          # full offline suite green, including quality tests
python -m pytest -m integration tests/test_console.py -q   # real builds still pass invariants
```
Expected: green. If an invariant now trips on `crossroads_meta`, it was wrongly wired into the audited
set — it must have **no** `source_id` and must not be counted as a source table (see `quality.py`'s
source enumeration).

**Stage ship-readiness checklist:**
- [ ] `SCHEMA_VERSION = 1` in `src/crossroads/__init__.py`
- [ ] `quality.write_build_metadata` creates a single-row `crossroads_meta` (version, schema_version, UTC ts, params JSON)
- [ ] `client.build` calls it after invariants pass, before `return self`
- [ ] `tests/test_provenance.py` passes in the **default** suite (so CI covers it)
- [ ] full `python -m pytest` green; `-m integration` builds still pass invariants

## End State / Handoff

Every built database carries a queryable `crossroads_meta` row: version, schema version, UTC build
time, and parameters. The "schema number" now has a durable, machine-checkable home independent of the
package version. Future migration/compatibility logic can key off `schema_version`. No other stage
depends on this, but it materially strengthens the reproducibility story the README/`docs/methodology.md` make.

## Failure Modes & Rollback

- **Circular import** (`ImportError` at build time). The `import crossroads` inside
  `write_build_metadata` must be *inside the function*, not at module top — `quality.py` is imported
  while `crossroads/__init__.py` is still initialising. Keep it lazy.
- **`now() AT TIME ZONE 'UTC'` rejected by the DuckDB version.** Fall back to stamping from Python:
  `datetime.now(timezone.utc).replace(tzinfo=None)` passed as a bind parameter. Note the change.
- **Conservation invariant trips on `crossroads_meta`.** It was accidentally counted as a source —
  ensure it is created outside any per-source loop and carries no `source_id`. It is metadata, not a
  source.
- **Rollback:** remove the `write_build_metadata` call from `client.py`, delete the function from
  `quality.py`, remove `SCHEMA_VERSION` from `__init__.py`, delete `tests/test_provenance.py`. No
  data-shape change to any other table.
