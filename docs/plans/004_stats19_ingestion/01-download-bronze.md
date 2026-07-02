# Stage 01 — Download & Bronze + Multi-Spec Quality Engine
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map + Approach first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Steps 1–3 are merged and green. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # all green
```
Observable state: `src/crossroads/transformers/spatial.py` exists (the reference transformer); there is
**no** `src/crossroads/transformers/stats19.py`; `quality.py` has `UNDECIDED_QUALITY_SPEC_IS_FATAL = True`
and `resolve_quality_specs` handles a **single** `SourceQuality`/`QualityExemption`/`None` per transformer;
`client.build()` calls `quality.reset_source_audit(self.con, transformer.source_id)` once per transformer.

## Objective
Two things, together:
1. **Generalize the quality engine** so a transformer may declare **several** audit units — the change the
   Step 3 overview deferred to Step 4.
2. Deliver `stats19.py` with a `Stats19Transformer` that **downloads** (offline-seedable) the Collision,
   Vehicle, and Casualty CSVs into **bronze** and builds a **minimal keep-in-place silver** (identity +
   `source_row_key`, **no validation dimensions yet**), declaring three `SourceQuality`. Commit tiny real
   DfT fixtures. Conservation must hold; the whole suite stays green.

Coordinate geometry, datetime, linkage, and the spatial join arrive in Stages 02–04. This stage proves
the plumbing: download → bronze → 1:1 silver → three audited sources.

## Implementation Steps

### A. Generalize the quality engine (`src/crossroads/quality.py`)

1. **Flatten multi-spec returns in `resolve_quality_specs`.** Replace the per-transformer body so a
   `quality_spec()` returning a **tuple/list** of `SourceQuality`/`QualityExemption` is handled
   element-by-element. A lone `SourceQuality`/`QualityExemption`/`None` behaves exactly as before
   (backward compatible — boundaries are unaffected).
   ```python
   def resolve_quality_specs(con, transformers,
                             undecided_fatal=UNDECIDED_QUALITY_SPEC_IS_FATAL):
       """Coverage gate ... (keep the existing docstring, then add:)

       A transformer may declare MORE THAN ONE audit unit by returning a tuple/list
       of SourceQuality / QualityExemption (e.g. STATS19's collision/vehicle/casualty).
       Each element is resolved independently; a lone value behaves as before.
       """
       log = logging.getLogger("crossroads.quality")
       specs = []
       for transformer in transformers:
           decision = transformer.quality_spec()
           # Normalise to a list so one transformer can declare several audit units.
           items = list(decision) if isinstance(decision, (tuple, list)) else [decision]
           for item in items:
               if isinstance(item, SourceQuality):
                   specs.append(item)
               elif isinstance(item, QualityExemption):
                   record_exemption(con, transformer.source_id, item.reason)
                   log.info("[%s] quality exemption recorded: %s",
                            transformer.source_id, item.reason)
               elif item is None:
                   msg = (f"[{transformer.source_id}] quality_spec() is undecided "
                          f"(returned None): an active source must return a "
                          f"SourceQuality(...) to be audited or a "
                          f"QualityExemption(reason=...) to opt out explicitly.")
                   if undecided_fatal:
                       raise UndecidedQualitySpecError(msg)
                   log.warning("%s [interim: warning only]", msg)
               else:
                   raise TypeError(
                       f"[{transformer.source_id}] quality_spec() must return "
                       f"SourceQuality, QualityExemption, None, or a tuple/list of "
                       f"those; got {type(item).__name__}.")
       return specs
   ```
   > An **empty** tuple/list yields no specs — treat it as a conscious "audits nothing" (rare; document
   > if you rely on it). Stats19 always returns three, so this edge case does not arise here.

2. **Add `declared_source_ids(transformer)`** — the `source_id`s a transformer writes audit rows under,
   so the build loop can reset them all. Place it next to `resolve_quality_specs`.
   ```python
   def declared_source_ids(transformer):
       """The audit source_ids a transformer will write rows under.

       For a transformer declaring SourceQuality(s), these are the specs' source_ids
       (a transformer may own several — e.g. STATS19). For a QualityExemption or an
       undecided (None) spec there is no separate audit source, so fall back to the
       transformer's own source_id. Used by Client.build to reset the shared audit
       tables for every source the transformer touches before it (re)runs.
       """
       decision = transformer.quality_spec()
       items = list(decision) if isinstance(decision, (tuple, list)) else [decision]
       ids = [item.source_id for item in items if isinstance(item, SourceQuality)]
       return ids or [transformer.source_id]
   ```

### B. Reset per declared source (`src/crossroads/client.py`)

3. In `Client.build()`, replace the single reset line with a loop over the declared source_ids. Current:
   ```python
   for transformer in active:
       quality.reset_source_audit(self.con, transformer.source_id)
       transformer.extract(self.cache_dir, **kwargs)
       transformer.transform_and_load(self.con, self.cache_dir)
   ```
   becomes:
   ```python
   for transformer in active:
       # A transformer may write audit rows under several source_ids (e.g. STATS19's
       # collision/vehicle/casualty). Reset each so a re-build stays idempotent.
       for source_id in quality.declared_source_ids(transformer):
           quality.reset_source_audit(self.con, source_id)
       transformer.extract(self.cache_dir, **kwargs)
       transformer.transform_and_load(self.con, self.cache_dir)
   ```
   This still names no concrete source (provider-plugin purity holds).

### C. Commit tiny real DfT fixtures (do this before the transformer tests)

The suite must run **offline** against committed real CSVs. Produce trimmed samples once, deterministically,
from a networked machine. Use a modern year whose files use the **`accident_*`** naming (e.g. **2023**).

1. Download the three real per-year files to a scratch dir (confirm the live filenames):
   ```
   https://data.dft.gov.uk/road-accidents-safety-data/dft-road-casualty-statistics-collision-2023.csv
   https://data.dft.gov.uk/road-accidents-safety-data/dft-road-casualty-statistics-vehicle-2023.csv
   https://data.dft.gov.uk/road-accidents-safety-data/dft-road-casualty-statistics-casualty-2023.csv
   ```
2. Trim to a handful of collisions **with referential integrity preserved** (only keep vehicle/casualty
   rows whose `accident_index` is in the collision sample), keeping the real headers. Pick collisions with
   **valid** coordinates (so later stages' e2e stays under the reject ceiling); the sentinel/FALSE-branch
   cases are proven by synthetic tests, not the committed sample.
   ```bash
   source .venv/bin/activate
   mkdir -p tests/fixtures/stats19
   python - <<'PY'
   import duckdb
   c = duckdb.connect()
   SRC = "/scratch"   # where the three real 2023 CSVs were downloaded
   OUT = "tests/fixtures/stats19"
   # Read all-string to preserve raw values exactly.
   c.execute(f"CREATE TABLE col AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-collision-2023.csv', all_varchar=true)")
   c.execute(f"CREATE TABLE veh AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-vehicle-2023.csv', all_varchar=true)")
   c.execute(f"CREATE TABLE cas AS SELECT * FROM read_csv_auto('{SRC}/dft-road-casualty-statistics-casualty-2023.csv', all_varchar=true)")
   # 8 collisions with valid coordinates, deterministic order.
   c.execute("""CREATE TABLE cols AS
     SELECT * FROM col
     WHERE location_easting_osgr NOT IN ('-1','0','')
       AND location_northing_osgr NOT IN ('-1','0','')
     ORDER BY accident_index LIMIT 8""")
   # Child rows for exactly those collisions (SEMI JOIN keeps referential integrity).
   c.execute("CREATE TABLE vehs AS SELECT v.* FROM veh v SEMI JOIN cols USING (accident_index)")
   c.execute("CREATE TABLE cass AS SELECT k.* FROM cas k SEMI JOIN cols USING (accident_index)")
   for tbl, name in (("cols","collision"),("vehs","vehicle"),("cass","casualty")):
       c.execute(f"COPY {tbl} TO '{OUT}/dft-road-casualty-statistics-{name}-2023.csv' (HEADER, DELIMITER ',')")
   print("collisions", c.execute("SELECT count(*) FROM cols").fetchone()[0],
         "vehicles", c.execute("SELECT count(*) FROM vehs").fetchone()[0],
         "casualties", c.execute("SELECT count(*) FROM cass").fetchone()[0])
   PY
   ```
   The filenames match `Stats19Transformer._filename(...)` so the offline seed path finds them.
3. Write `tests/fixtures/stats19/README.md`: publisher (DfT), dataset (Road Safety Data, 2023 tranche),
   licence (**Open Government Licence v3.0**), that these are **trimmed samples for testing only**, and how
   they were produced (the recipe above). Record the exact row counts printed.

> **If the live 2023 files use `collision_*` naming instead of `accident_*`:** either pick a year that uses
> `accident_*`, or write the fixtures with `accident_*` headers by aliasing in the `COPY SELECT`. Keep the
> committed fixture on the **`accident_*`** convention so the tests exercise the canonical path; the
> `collision_*` alias branch is covered by a small synthetic test (step F5).

### D. `src/crossroads/transformers/stats19.py` — the transformer (bronze + minimal silver)

Create the module. Mirror `spatial.py`'s structure and comment density. **Standard library only** for
download.

```python
"""DfT STATS19 road collision ingestion — Collision, Vehicle, and Casualty
(spec §3, §5 Phase 2).

One module, one concrete transformer (Stats19Transformer) that owns all three
related tables. STATS19 must be built in dependency order (collision silver first,
then vehicle/casualty linkage, then the spatial stamp of collisions), so a single
transformer drives the whole pipeline and declares THREE audit units to the quality
engine via a tuple from quality_spec() (see docs/plans/004_stats19_ingestion).

Coordinates are OSGR eastings/northings — natively EPSG:27700, so cast (never
reproject). Downloads use only the standard library (urllib); CSVs are read
in-database with read_csv. For offline tests the cache is pre-seeded with the
committed sample CSVs, so no network access occurs.
"""

import os
import urllib.request

from crossroads.transformers.base import BaseTransformer
from crossroads.quality import (
    SourceQuality, Dimension, create_clean_view,
    record_source_rows, log_exclusion,
)

# DfT publishes per-year CSVs under this base. The per-year filename template is
# code-controlled; verify the live names at implementation (see the plan).
DFT_BASE_URL = "https://data.dft.gov.uk/road-accidents-safety-data"
_FILE_TEMPLATE = "dft-road-casualty-statistics-{ftype}-{year}.csv"

# Missing/out-of-range coordinate sentinels (spec §9). A coordinate equal to any of
# these — or blank / non-numeric — is treated as missing: typed value NULL, geom NULL,
# geom_valid FALSE, logged, and the row is retained (never deleted).
COORD_SENTINELS = ("-1", "0")

# British National Grid envelope, for verifying geometry really is EPSG:27700 in tests.
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000


class Stats19Transformer(BaseTransformer):
    """Ingests STATS19 Collision/Vehicle/Casualty into three bronze/silver pairs.

    The engine calls extract() then transform_and_load() back-to-back on this same
    instance, so extract() stashes the resolved build parameters (years, boundary
    mode) on self for transform_and_load() to read — the same hand-off spatial.py uses.
    """

    source_id = "stats19"          # registry identity; audit units are the three below

    # --- audit source_ids (one per bronze/silver pair) ---
    COLLISION_SID = "stats19_collision"
    VEHICLE_SID = "stats19_vehicle"
    CASUALTY_SID = "stats19_casualty"

    # --- table names ---
    COLLISION_BRONZE, COLLISION_SILVER = "stats19_collision_raw", "collisions"
    VEHICLE_BRONZE, VEHICLE_SILVER = "stats19_vehicle_raw", "vehicles"
    CASUALTY_BRONZE, CASUALTY_SILVER = "stats19_casualty_raw", "casualties"

    def is_active(self, **kwargs):
        # Nothing to ingest without years; a no-years build (e.g. boundary-only)
        # simply skips STATS19. A real build passes years (spec §8 target flow).
        return bool(kwargs.get("years"))

    def _filename(self, ftype, year):
        return _FILE_TEMPLATE.format(ftype=ftype, year=year)

    def extract(self, cache_dir, **kwargs):
        os.makedirs(cache_dir, exist_ok=True)
        self._years = [int(y) for y in (kwargs.get("years") or [])]
        self._boundary_mode = kwargs.get("boundary_mode", "snapshot")
        for year in self._years:
            for ftype in ("collision", "vehicle", "casualty"):
                path = os.path.join(cache_dir, self._filename(ftype, year))
                # Offline-friendly: if already cached (or test-seeded), skip download.
                if not os.path.exists(path):
                    url = f"{DFT_BASE_URL}/{self._filename(ftype, year)}"
                    urllib.request.urlretrieve(url, path)

    def _cached_files(self, cache_dir, ftype):
        """Cached CSV paths for one file type across the resolved years (existing only)."""
        paths = [os.path.join(cache_dir, self._filename(ftype, y)) for y in self._years]
        return [p for p in paths if os.path.exists(p)]

    def _load_bronze(self, con, bronze_table, files):
        """Faithful all-string bronze from one or more CSVs.

        read_csv with union_by_name lets historical (accident_*) and modern
        (collision_*) tranches coexist (absent columns become NULL); all_varchar
        preserves raw values exactly. Paths are cache-derived (trusted); values
        are never interpolated.
        """
        if not files:
            raise FileNotFoundError(
                f"[stats19] no cached CSVs for {bronze_table}; extract() must run "
                f"first (years={self._years}).")
        paths_sql = "[" + ", ".join(f"'{p}'" for p in files) + "]"
        con.execute(
            f"CREATE OR REPLACE TABLE {bronze_table} AS "
            f"SELECT * FROM read_csv({paths_sql}, union_by_name=true, all_varchar=true)"
        )

    def _coalesce_present(self, con, table, candidates, alias):
        """Build `COALESCE(<present candidates>) AS alias` over only columns that
        exist in `table` (else `NULL AS alias`). Handles the accident_*/collision_*
        rename without erroring on an absent column. Identifiers are code-controlled.
        """
        existing = {r[0].lower() for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall()}
        present = [c for c in candidates if c.lower() in existing]
        if not present:
            return f"NULL AS {alias}"
        if len(present) == 1:
            return f"{present[0]} AS {alias}"
        return "COALESCE(" + ", ".join(present) + f") AS {alias}"

    def transform_and_load(self, con, cache_dir):
        years = getattr(self, "_years", None) or []
        if not years:
            return   # defensive: is_active gates on years, so this is unreachable in practice

        # --- BRONZE (×3): faithful copies; record rows read for conservation. ---
        for sid, bronze, ftype in (
            (self.COLLISION_SID, self.COLLISION_BRONZE, "collision"),
            (self.VEHICLE_SID, self.VEHICLE_BRONZE, "vehicle"),
            (self.CASUALTY_SID, self.CASUALTY_BRONZE, "casualty"),
        ):
            self._load_bronze(con, bronze, self._cached_files(cache_dir, ftype))
            n = con.execute(f"SELECT count(*) FROM {bronze}").fetchone()[0]
            record_source_rows(con, sid, n)

        # --- SILVER (×3): minimal keep-in-place 1:1 (Stage 01). Identity + key only;
        # coordinates/datetime/linkage/dimensions arrive in Stages 02–04. ---
        self._derive_collision_silver(con)
        self._derive_vehicle_silver(con)
        self._derive_casualty_silver(con)

    # --- silver derivations (factored so tests can drive them on a synthetic bronze) ---
    def _derive_collision_silver(self, con):
        acc = self._coalesce_present(con, self.COLLISION_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        yr = self._coalesce_present(con, self.COLLISION_BRONZE,
                                    ["collision_year", "accident_year"], "accident_year")
        ref = self._coalesce_present(con, self.COLLISION_BRONZE,
                                     ["collision_reference", "accident_reference"], "accident_reference")
        # accident_index doubles as the source_row_key (globally unique).
        idx_expr = acc.replace(" AS accident_index", "")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.COLLISION_SILVER} AS "
            f"SELECT ({idx_expr}) AS source_row_key, {acc}, {yr}, {ref} "
            f"FROM {self.COLLISION_BRONZE}"
        )

    def _derive_vehicle_silver(self, con):
        acc = self._coalesce_present(con, self.VEHICLE_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx_expr = acc.replace(" AS accident_index", "")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.VEHICLE_SILVER} AS "
            f"SELECT ({idx_expr}) || '|' || vehicle_reference AS source_row_key, "
            f"       {acc}, vehicle_reference "
            f"FROM {self.VEHICLE_BRONZE}"
        )

    def _derive_casualty_silver(self, con):
        acc = self._coalesce_present(con, self.CASUALTY_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx_expr = acc.replace(" AS accident_index", "")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CASUALTY_SILVER} AS "
            f"SELECT ({idx_expr}) || '|' || vehicle_reference || '|' || casualty_reference "
            f"         AS source_row_key, "
            f"       {acc}, vehicle_reference, casualty_reference "
            f"FROM {self.CASUALTY_BRONZE}"
        )

    def quality_spec(self):
        # Three audit units. Dimensions are empty in Stage 01 (no validation yet);
        # Stages 02–03 add geom/datetime/link dimensions to the right specs.
        return (
            SourceQuality(self.COLLISION_SID, self.COLLISION_BRONZE, self.COLLISION_SILVER,
                          dimensions=(), key_column="source_row_key"),
            SourceQuality(self.VEHICLE_SID, self.VEHICLE_BRONZE, self.VEHICLE_SILVER,
                          dimensions=(), key_column="source_row_key"),
            SourceQuality(self.CASUALTY_SID, self.CASUALTY_BRONZE, self.CASUALTY_SILVER,
                          dimensions=(), key_column="source_row_key"),
        )
```

> `_coalesce_present(...).replace(" AS accident_index", "")` is a small trick to reuse one helper for both
> the aliased select item and the bare expression used in the key. If you find it fragile, add a
> `_present_expr(con, table, candidates)` returning just the bare `COALESCE(...)`/`col`/`NULL` and derive
> the aliased form from it. Either way, keep it readable and commented.

### E. Fix the now-misnamed no-op build test (`tests/test_client.py`)

`test_empty_build_is_noop_in_memory` builds with the **default registry** and `years=[2023]`. With
`Stats19Transformer` active on `years`, that would attempt a **real DfT download**. Update the test to use
an explicit empty registry so it deterministically tests the true no-op path (this also removes the latent
boundary-download dependency it already had):
```python
def test_empty_build_is_noop_in_memory():
    client = crossroads.init_engine()
    client.registry._transformers = []   # no sources: a genuine no-op build (offline, deterministic)
    result = client.build(years=[2023], include_weather=True, spatial_grain="local_authority")
    assert result is client
    assert client.con is not None
    assert client.con.execute("SELECT 42").fetchone()[0] == 42
    client.close()
    assert client.con is None
```

### F. Tests — `tests/test_stats19.py` (new) and `tests/test_quality.py` (add)

Create `tests/test_stats19.py` mirroring `test_spatial.py`'s offline-seed pattern.
```python
"""Tests for the STATS19 transformer (stats19.py).

Offline: the cache is pre-seeded with committed sample CSVs so extract() finds the
files and performs no network download.
"""
import os
import shutil

import pytest
import crossroads
from crossroads.transformers.stats19 import Stats19Transformer

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
YEARS = [2023]


def _seed_cache(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    for ftype in ("collision", "vehicle", "casualty"):
        name = f"dft-road-casualty-statistics-{ftype}-2023.csv"
        shutil.copy(os.path.join(FIXTURES, name), os.path.join(cache_dir, name))


def _stats19_client(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_cache(cache)
    client = crossroads.init_engine(cache_dir=cache)     # in-memory DB, seeded cache
    client.registry._transformers = [Stats19Transformer()]   # stats19 only this stage
    return client


def test_bronze_and_minimal_silver_end_to_end(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)     # runs invariants; raises if conservation fails

    for bronze, silver in (
        ("stats19_collision_raw", "collisions"),
        ("stats19_vehicle_raw", "vehicles"),
        ("stats19_casualty_raw", "casualties"),
    ):
        b = client.con.execute(f"SELECT count(*) FROM {bronze}").fetchone()[0]
        s = client.con.execute(f"SELECT count(*) FROM {silver}").fetchone()[0]
        assert b > 0 and b == s, f"{bronze}={b} must equal {silver}={s} (keep-in-place)"

    # Three audit units recorded a source-row count each.
    sids = {r[0] for r in client.con.execute(
        "SELECT DISTINCT source_id FROM source_ingest_log").fetchall()}
    assert {"stats19_collision", "stats19_vehicle", "stats19_casualty"} <= sids
    client.close()


def test_source_row_keys_are_unique(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    for silver in ("collisions", "vehicles", "casualties"):
        dupes = client.con.execute(
            f"SELECT count(*) - count(DISTINCT source_row_key) FROM {silver}"
        ).fetchone()[0]
        assert dupes == 0, f"{silver} has duplicate source_row_key values"
    client.close()


def test_identity_normalized_to_accident_index(tmp_path):
    # Canonical silver identity is accident_index, never NULL for the sample.
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    nulls = client.con.execute(
        "SELECT count(*) FROM collisions WHERE accident_index IS NULL"
    ).fetchone()[0]
    assert nulls == 0
    client.close()


def test_stats19_inactive_without_years():
    # No years -> nothing to ingest -> transformer is skipped.
    assert Stats19Transformer().is_active() is False
    assert Stats19Transformer().is_active(years=[2023]) is True


def test_quality_spec_declares_three_units():
    specs = Stats19Transformer().quality_spec()
    assert len(specs) == 3
    ids = {s.source_id for s in specs}
    assert ids == {"stats19_collision", "stats19_vehicle", "stats19_casualty"}


def test_collision_reference_alias_branch(con):
    # Prove the collision_* -> accident_* normalization on a synthetic modern-schema
    # bronze (no committed collision_* fixture needed). Independent reimplementation of
    # the reference package's index-normalization behaviour (inspiration only, no copy).
    from crossroads.quality import ensure_quality_tables
    ensure_quality_tables(con)
    con.execute(
        "CREATE TABLE stats19_collision_raw AS "
        "SELECT * FROM (VALUES ('2024A1','2024','ref1')) "
        "AS t(collision_index, collision_year, collision_reference)")
    t = Stats19Transformer()
    t._derive_collision_silver(con)
    row = con.execute(
        "SELECT accident_index, accident_year, source_row_key FROM collisions").fetchone()
    assert row[0] == "2024A1" and row[1] == "2024" and row[2] == "2024A1"
```

In `tests/test_quality.py`, add coverage for the multi-spec generalization:
```python
def test_resolve_quality_specs_flattens_tuple(con):
    from crossroads.quality import (
        ensure_quality_tables, resolve_quality_specs, declared_source_ids,
        SourceQuality,
    )
    from crossroads.transformers.base import BaseTransformer
    ensure_quality_tables(con)

    class MultiUnit(BaseTransformer):
        source_id = "multi"
        def extract(self, cache_dir, **kwargs): pass
        def transform_and_load(self, con, cache_dir): pass
        def quality_spec(self):
            return (
                SourceQuality("multi_a", "a_raw", "a", key_column="source_row_key"),
                SourceQuality("multi_b", "b_raw", "b", key_column="source_row_key"),
            )

    t = MultiUnit()
    specs = resolve_quality_specs(con, [t])
    assert {s.source_id for s in specs} == {"multi_a", "multi_b"}
    assert declared_source_ids(t) == ["multi_a", "multi_b"]
```

### G. Run the suite
```bash
source .venv/bin/activate
python -m pytest -q          # expected: all green, including the new stats19 + quality tests
```

## Testing & Verification
**Integration (PRIMARY, offline, real data):** `test_bronze_and_minimal_silver_end_to_end` runs a real
`build(years=[2023])` over the committed DfT sample: three bronze tables, three 1:1 silver tables, three
audited sources, conservation enforced by the real engine. `test_source_row_keys_are_unique` and
`test_identity_normalized_to_accident_index` prove the key/identity shape.

**Unit:** `test_collision_reference_alias_branch` (collision_*→accident_* on synthetic bronze),
`test_stats19_inactive_without_years`, `test_quality_spec_declares_three_units`,
`test_resolve_quality_specs_flattens_tuple` (engine generalization + `declared_source_ids`).

**Opt-in real download (add, marked so it is deselected by default):**
```python
@pytest.mark.integration
def test_download_real_dft_sample(tmp_path):
    t = Stats19Transformer()
    cache = str(tmp_path / "cache")
    t.extract(cache, years=[2023])
    assert os.path.exists(os.path.join(
        cache, "dft-road-casualty-statistics-collision-2023.csv"))
```

**Stage ship-readiness checklist:**
- [ ] `quality.resolve_quality_specs` flattens tuple/list returns; `declared_source_ids` added; both tested.
- [ ] `client.build()` resets audit rows per declared source_id (no source named).
- [ ] `stats19.py` defines `Stats19Transformer` (discovered by the registry) with `is_active` gated on `years`.
- [ ] A real `build(years=[2023])` over the committed sample builds 3 bronze + 3 silver (1:1), 3 audited
      sources, conservation passing.
- [ ] `tests/fixtures/stats19/*.csv` (real, trimmed, referential-integrity preserved) + README committed.
- [ ] `test_empty_build_is_noop_in_memory` updated to an explicit empty registry (offline, deterministic).
- [ ] No new dependency; `pyproject.toml` untouched.
- [ ] `python -m pytest -q` fully green.

## End State / Handoff
`stats19.py` exists with a discovered `Stats19Transformer`. A `build(years=[...])` produces
`stats19_{collision,vehicle,casualty}_raw` bronze and `collisions`/`vehicles`/`casualties` silver
(minimal, keep-in-place 1:1), each an audited `SourceQuality` with **no dimensions yet**; conservation
holds. The quality engine and `client.build()` support multiple audit units per transformer. Stage 02 may
assume this silver exists and enrich the collision derivation with typed coordinates, geometry, and datetime.

## Failure Modes & Rollback
- **`read_csv` column-name mismatch / missing identity column:** the binder errors on
  `vehicle_reference`/`casualty_reference`/etc. Fix: confirm the real headers; the `_coalesce_present`
  helper already tolerates absent `accident_*`/`collision_*` variants, but `vehicle_reference` /
  `casualty_reference` are assumed present in vehicle/casualty files — verify and adjust if a tranche
  differs.
- **A default-registry build unexpectedly downloads:** any test doing `init_engine().build(years=...)`
  with the default registry now activates Stats19. Use an explicit `_transformers` list (as the stats19
  tests do) or seed the cache. `test_empty_build_is_noop_in_memory` is the known case (fixed in step E).
- **Conservation fails (bronze != silver):** a silver derivation filtered rows — it must be a straight 1:1
  `SELECT` (keep-in-place). Check no `WHERE`/`DISTINCT`/`GROUP BY` crept in.
- **Rollback:** delete `stats19.py`, `tests/test_stats19.py`, `tests/fixtures/stats19/`; revert the
  `quality.py`/`client.py` and `test_client.py`/`test_quality.py` edits. The suite returns to the Step 3 state.
