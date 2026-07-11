# Stage 08 — Schema Data Dictionary (`docs/schema.md`)
> Part of Release Preparation (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Depends on Stage 03 (README exists to link into) and Stage 07 (`SCHEMA_VERSION` constant +
`crossroads_meta` table to document). Verify:
```bash
cd /Users/will/Documents/Code/Crossroads
grep -n "SCHEMA_VERSION" src/crossroads/__init__.py     # expect SCHEMA_VERSION = 1 (Stage 07)
ls docs/schema.md 2>/dev/null || echo "no docs/schema.md yet (expected)"
ls README.md docs/methodology.md docs/spec.md
python -m pytest -q                                      # baseline green
```

**Author the doc from a real database, not from memory** — this is the whole point. Build a full
offline fixture database first and introspect it; the introspected column list is the ground truth you
document and the drift-guard test enforces.

## Objective

Ship `docs/schema.md`: a researcher-facing data dictionary of the database Crossroads *can* build —
annotated `CREATE TABLE`-style blocks (illustrative, never executed) with a comment on every column
giving its meaning, units, and derivation — plus a drift-guard test so the doc cannot silently
diverge from what the code produces.

## Ground-truth table inventory (from a full build; weather seeded)

Introspected from a real build (`information_schema.columns`). Column counts will match what your
fixture build produces; treat this as the map, and regenerate the exact list yourself (Step 1).

- **Silver analytical tables (document every column):** `collisions` (~55), `vehicles` (~34),
  `casualties` (~27), `weather` (temperature/precip grid), `lad_boundaries` (~8), `ctyua_boundaries` (~8).
- **Gold views (document by derivation rule, not per-column):** `collisions_spatial`
  (`collisions` filtered to `geom_valid`), `vehicles_clean` / `casualties_clean` /
  `lad_boundaries_clean` / `ctyua_boundaries_clean` / `weather_clean` (silver filtered to its valid
  flag), and `collisions_labelled` / `vehicles_labelled` / `casualties_labelled` (silver + an
  `<column>_label` twin for each coded column, via the `codebook` join).
- **Provenance / quality / reference tables (document every column):** `crossroads_meta` (Stage 07),
  `data_quality_log` (~8), `quarantine_raw` (~4), `source_ingest_log` (~3), `quality_exemptions` (~3),
  `stats19_completeness` (~7), `codebook` (~4), `column_manifest` (~4).
- **Bronze `*_raw` tables (document as a category only):** `stats19_collision_raw`,
  `stats19_vehicle_raw`, `stats19_casualty_raw`, `ons_lad_raw`, `ons_ctyua_raw`, `era5_weather_raw` —
  faithful append-only copies of the source columns. Do **not** re-enumerate their columns (they are
  the upstream source's, catalogued in the DfT guide and `src/crossroads/reference/README.md`);
  document the *category* and point there.

## Implementation Steps

**Step 1 — Build a full fixture DB and dump the real schema.** Use the existing offline harness
(`tests/test_console.py::_seed_full_cache` + the weather `.nc` copy from
`test_wizard_builds_weather_offline`) to build a database with **all** datasets, then dump columns:
```python
# scratch introspection — run once to author from ground truth (not committed)
import duckdb
con = duckdb.connect("PATH/TO/full_fixture.duckdb", read_only=True)
for t, ct in con.execute("""
    SELECT table_name, table_type FROM information_schema.tables
    WHERE table_schema='main' ORDER BY table_type, table_name""").fetchall():
    print(f"\n-- {ct} {t}")
    for col, dtype in con.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position""",[t]).fetchall():
        print(f"   {col} {dtype}")
```
Everything you document must come from this output.

**Step 2 — Write `docs/schema.md`** (in `docs/`, alongside `spec.md`; its sibling links use bare
names like `spec.md`/`methodology.md`, and root/source targets use `../`). Structure:

````markdown
# Database Schema — Data Dictionary

The tables and views a Crossroads-UK build can create, with the meaning and derivation of each
column. The `CREATE TABLE` blocks below are **illustrative** — Crossroads builds these tables
dynamically (`CREATE OR REPLACE ... AS SELECT`), so this file documents the *result*, it is never
executed. Every built database also carries its own machine-readable schema marker:
`SELECT * FROM crossroads_meta` (see [Provenance](#provenance)).

**Schema version:** 1  ·  See [methodology.md](methodology.md) for how the data is produced and
[spec.md §9](spec.md) for the keep-in-place quality model.

## Conventions (keep-in-place model)

Every cleansed field appears **twice**: the preserved raw value and a typed/clean column that is
`NULL` when the source value failed validation, plus a boolean `*_valid` flag. Spatial/severity
analysis runs only where the flag is `TRUE`. Nothing is deleted — failures are logged in
`data_quality_log`. `*_local` columns are UK civil time (`Europe/London`); `*_utc` exists only for
UTC-native sources; machine `ingested_at`/`built_at_utc` stamps are UTC provenance.

---

## Silver: analytical tables

### `collisions`
```sql
CREATE TABLE collisions (
    collision_index        VARCHAR,     -- DfT natural key (accident_index); primary identifier
    easting                INTEGER,     -- raw OSGR easting, EPSG:27700 (preserved; 0/-1 = missing)
    easting_valid          BOOLEAN,     -- FALSE when easting was a 0/-1 sentinel
    geom                   GEOMETRY,    -- POINT(EPSG:27700) derived from easting/northing; NULL if invalid
    geom_valid             BOOLEAN,     -- TRUE only when a real point was derived (drives collisions_spatial)
    collision_severity     INTEGER,     -- cleaned code 1=Fatal 2=Serious 3=Slight (see codebook)
    date_local             DATE,        -- collision date, UK local
    -- ... one commented line PER real column from Step 1 ...
);
```
> Repeat a fully-commented block for `vehicles`, `casualties`, `weather`, `lad_boundaries`,
> `ctyua_boundaries`. Pull STATS19 code meanings from `codebook` / the reference README; pull weather
> semantics (t2m→temperature_c, tp→precipitation_mm, grid join) from `transformers/weather.py`;
> boundary columns (codes, names, valid_from/valid_to in temporal mode) from `transformers/spatial.py`.

## Gold: clean views

Filtered projections researchers query by default — no new data, derived from silver:

- **`collisions_spatial`** — `SELECT * FROM collisions WHERE geom_valid` (valid-geometry collisions).
- **`<source>_clean`** (`vehicles_clean`, `casualties_clean`, `weather_clean`,
  `lad_boundaries_clean`, `ctyua_boundaries_clean`) — the silver table filtered to its validity flag.
- **`<source>_labelled`** (`collisions_labelled`, `vehicles_labelled`, `casualties_labelled`) — the
  silver table plus an `<column>_label` text twin for every coded column, decoded via `codebook`.

## Provenance, quality & reference tables

Document every column of: `crossroads_meta`, `data_quality_log`, `quarantine_raw`,
`source_ingest_log`, `quality_exemptions`, `stats19_completeness`, `codebook`, `column_manifest`
(same annotated-block style).

## Bronze: raw landing tables

`stats19_collision_raw`, `stats19_vehicle_raw`, `stats19_casualty_raw`, `ons_lad_raw`,
`ons_ctyua_raw`, `era5_weather_raw` are faithful, append-only copies of each downloaded source with
original column names and permissive types (spec §9). Their columns are the upstream source's —
catalogued in the [DfT data guide](../src/crossroads/reference/README.md) and the reference README — and
are not re-listed here.
````
> The `collisions` block above is a *shape example*. You must emit one commented line for every real
> column from Step 1's dump — no column omitted, none invented.

**Step 3 — Link `docs/schema.md` from the docs.** In `README.md` (at the repo root; Stage 03), under
the "What you get" or "Data & licences" section, add: `The full table/column data dictionary is in
[docs/schema.md](docs/schema.md).` In `docs/methodology.md` (a sibling in `docs/`), add a one-line
pointer near the top: `See [schema.md](schema.md) for the table-and-column data dictionary.`

**Step 4 — Keep the declared schema version in sync.** `docs/schema.md`'s "Schema version: N" must equal
`crossroads.SCHEMA_VERSION`. When either changes, both change (enforced by the fast test below).

## Testing & Verification

**Fast default-suite test (PRIMARY — runs in CI).** Create `tests/test_schema_doc.py`:
```python
"""docs/schema.md exists, declares the current schema version, and names the core tables."""
import os
import re

import crossroads

ROOT = os.path.dirname(os.path.dirname(__file__))
CORE_TABLES = [
    "collisions", "vehicles", "casualties", "weather", "lad_boundaries", "ctyua_boundaries",
    "collisions_spatial", "crossroads_meta", "data_quality_log", "quarantine_raw",
    "source_ingest_log", "codebook", "column_manifest",
]


def _schema_text():
    with open(os.path.join(ROOT, "docs", "schema.md"), encoding="utf-8") as fh:
        return fh.read()


def test_schema_doc_declares_current_version():
    text = _schema_text()
    m = re.search(r"Schema version:\D*(\d+)", text)
    assert m, "docs/schema.md must state 'Schema version: N'"
    assert int(m.group(1)) == crossroads.SCHEMA_VERSION


def test_schema_doc_mentions_core_tables():
    text = _schema_text()
    missing = [t for t in CORE_TABLES if t not in text]
    assert not missing, f"docs/schema.md does not document core tables: {missing}"
```
Run: `python -m pytest tests/test_schema_doc.py -q` → 2 passed.

**Column-level drift guard (integration; deselected, run before release).** This is the guard that
makes the doc trustworthy. Add to `tests/test_schema_doc.py`, reusing the offline full build:
```python
import shutil
import pytest
import duckdb
from crossroads import console
# reuse the console-test fixtures/harness
from tests.test_console import _seed_full_cache, scripted, STATS19_FIXTURES  # adjust imports as needed

# Tables whose EVERY column must be documented (silver + provenance/quality + reference).
COLUMN_GUARDED = [
    "collisions", "vehicles", "casualties", "weather", "lad_boundaries", "ctyua_boundaries",
    "crossroads_meta", "data_quality_log", "quarantine_raw", "source_ingest_log",
    "quality_exemptions", "stats19_completeness", "codebook", "column_manifest",
]
# Bronze copies of source columns — documented as a category, excluded from the column guard.
EXCLUDED_PREFIXES = ("stats19_", "ons_", "era5_")  # *_raw bronze tables


@pytest.mark.integration
def test_documented_columns_match_built_database(tmp_path):
    cache = str(tmp_path / "cache"); _seed_full_cache(cache)
    # also seed weather so the weather table exists
    shutil.copy(os.path.join(ROOT, "tests", "fixtures", "weather", "era5_land_sample.nc"),
                os.path.join(cache, "era5_land_2023.nc"))
    db = str(tmp_path / "full.duckdb")
    reader, writer, _ = scripted([db, "1-2", "2023", "snapshot", "y"])  # weather+stats19
    client = console.run_wizard(reader, writer, cache_dir=cache)
    try:
        text = _schema_text()
        problems = []
        for table in COLUMN_GUARDED:
            cols = [r[0] for r in client.con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='main' AND table_name=?", [table]).fetchall()]
            assert cols, f"{table} not found in the built DB — fixture/build changed"
            undocumented = [c for c in cols if c not in text]
            if undocumented:
                problems.append(f"{table}: undocumented columns {undocumented}")
        assert not problems, "docs/schema.md drifted from the built database:\n" + "\n".join(problems)
    finally:
        client.close()
```
Run: `python -m pytest -m integration tests/test_schema_doc.py -q`.
> This checks *presence* of each real column name in the doc (cheap, robust). If you want stricter
> per-table scoping (a column documented under the wrong table), parse each `CREATE TABLE <name>`
> block and match within it — optional; the presence check catches the common drift (a new column
> nobody documented). Note any deviation.

**Stale-doc direction.** The fast test's `CORE_TABLES` and the integration guard both fail if a
documented table/column *disappears* only insofar as it's referenced; if you want to catch "documented
but no longer built", extend the integration test to diff the other direction for the guarded tables.

**Stage ship-readiness checklist:**
- [ ] `docs/schema.md` documents every column of the silver + provenance/quality + reference tables (from Step 1's real dump), gold views by derivation rule, bronze as a category
- [ ] "Schema version: N" equals `crossroads.SCHEMA_VERSION`
- [ ] README and `docs/methodology.md` link `docs/schema.md`
- [ ] `tests/test_schema_doc.py` fast tests pass in the default suite (CI-covered)
- [ ] `-m integration` column-drift guard passes against a full build
- [ ] repo-wide markdown link-integrity test (Stage 04) still green

## End State / Handoff

A researcher can read `docs/schema.md` to understand every table and column before writing a query, and a
test prevents the doc from silently drifting from the code. The schema is now documented in three
mutually-reinforcing places: human prose (`docs/schema.md`), a machine integer (`crossroads_meta.schema_version`,
Stage 07), and the SemVer MINOR digit (Stage 01) — all kept consistent by tests and policy.

## Failure Modes & Rollback

- **Integration guard fails on first run** because the doc missed columns — that is the test doing its
  job; add the missing column lines from Step 1's dump.
- **`from tests.test_console import ...` fails** (test-module import path). Either add
  `tests/__init__.py` if absent, or copy the small `_seed_full_cache` helper into `test_schema_doc.py`.
  Note the choice.
- **Weather table absent** in the built DB — the `.nc` fixture was not seeded or the `[weather]`
  extra's parse path differs; ensure the weather fixture copy step ran. If weather is intentionally
  out of the release, drop `weather` from `COLUMN_GUARDED`/`CORE_TABLES` and note it.
- **Rollback:** delete `docs/schema.md` and `tests/test_schema_doc.py`, and remove the two README / `docs/methodology.md`
  links. No source or data change.
