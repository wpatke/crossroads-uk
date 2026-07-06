# Stage 06 — Keep-in-Place Silver (Broad Clean)
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's **"Enhancement Phase (post-Stage 04)"** section first. This is 06 of the revised plan.
> End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stage 05 is done: `transform_and_load` loads `codebook` + `column_manifest` at the top; both reference tables
exist. Stages 01–04 remain the committed baseline. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: silver is still the **structural** layer only — `collisions` ~16 cols, `vehicles` ~4,
`casualties` ~5 (schemas listed in Stage 05's Prerequisites). The ~50 descriptive coded/numeric/text columns
(`collision_severity`, `road_type`, `weather_conditions`, `speed_limit`, `vehicle_type`, `sex_of_casualty`,
`age_of_casualty`, …) are present only in bronze. `stats19.py` already imports `warnings` (Stage 04);
confirm it does.

## Objective
Widen the three `_derive_*_silver` methods (an **in-place enhancement** of the committed code) so **every**
bronze column reaches silver: coded/numeric columns cleaned (the full missing set → `NULL`) and typed under
their canonical names via a manifest-driven generator; text columns carried raw. Keep all existing bespoke
logic (identity, `easting`/`northing`/`geom`/`datetime_local`, `link_valid`, the geom/datetime/link ledger)
and dimensions unchanged. The cleaned value stays a **code/number**, never a label. `collision_severity` and
`casualty_severity` are cleaned here as ordinary coded columns; Stage 07 promotes them to the formal audit.

> **What Stage 05's data already decides for this stage (mostly table-driven; one manifest correction is called out below):**
> - **The missing set is the codebook's `is_missing` codes PLUS a bare `-1`.** The coded clean nulls the
>   codebook's `is_missing = TRUE` codes and, in addition, any bare `-1`. `-1` is DfT's universal "Data missing
>   or out of range" sentinel, so every codebook-covered variable already flags it; adding it in code means a
>   codebook **gap** can't silently leak a `-1` into a cleaned code — e.g. `enhanced_severity_collision`, which
>   has **no** codebook rows and is all `-1` in the 2023 sample. It never nulls a real value: no STATS19 coded
>   field uses `-1` as a meaningful category. Per Stage 05 the codebook's missing set is `-1` + five labels;
>   crucially the `9`/`99` "unknown (self reported)" codes are `is_missing = FALSE`, so this stage **keeps** them
>   as real codes across ~27 columns (matching the reference `stats19` package). Do **not** add a separate
>   `9`/`99` rule — only `-1` is universal.
> - **`DOUBLE` numerics flow through the numeric path.** The four `*_adjusted_severity_serious/slight` weights
>   are `numeric`/`DOUBLE` in the manifest, so `_clean_fragment` emits `TRY_CAST(... AS DOUBLE)` with the
>   `-1`/blank sentinel nulled — no special-casing.
> - **ONS-string LA fields are `text`.** `local_authority_ons_district`/`_highway`/`_highway_current` are
>   `text` in the manifest → carried raw, **not** missing-cleaned (their codes are strings like `E06000036`,
>   which the integer codebook can't decode). `local_authority_district` (integer DfT codes) is `coded` and
>   cleans normally.
> - **`longitude`/`latitude` are numeric (`DOUBLE`), not `geo` — MANIFEST CORRECTION REQUIRED.** They ship in
>   Stage 05's `stats19_columns.csv` as `kind = geo` with no dtype, which this stage's broad clean would
>   otherwise drop (see the keep-in-place objective — silver must carry **every** bronze column). Correct their
>   two manifest rows to `kind = numeric`, `dtype = DOUBLE` (a one-line reference-data fix, not code). They then
>   flow through the numeric path like `easting`/`northing`: `-1`/blank → `NULL`, else `TRY_CAST(... AS DOUBLE)`.
>   The OSGR `location_easting_osgr`/`location_northing_osgr` stay `geo` — they are consumed bespokely (turned
>   into `easting`/`northing`/`geom`) and listed in the collision `exclude`, so they never hit the broad clean.
> - **This reclassification changes a pinned count in an existing Stage 05 test — update it (decided: keep the
>   reclassification, fix the test).** `test_column_manifest_covers_every_fixture_column` asserts the exact
>   kind breakdown, currently `geo: 4, numeric: 14`. Moving `longitude`/`latitude` makes it `geo: 2,
>   numeric: 16` — change those two numbers in that assertion (total stays 99; the "every coded/numeric row
>   has a dtype" assertion still passes because we set `dtype = DOUBLE`). We considered instead typing the two
>   columns inside this stage's code and leaving the manifest as `geo`, but chose the manifest fix so the
>   reference list reads truthfully (longitude/latitude ARE numbers).

## Implementation Steps

### A. Add the broad-clean SQL generator (`src/crossroads/transformers/stats19.py`)

Place these next to `_coalesce_present` (same "only touch what's present, from a trusted manifest" idea). Add
the numeric-sentinel constant near `COORD_SENTINELS`:
```python
# Sentinels for the broad numeric clean (a coded clean uses the codebook's is_missing set instead).
NUMERIC_SENTINELS = ("-1", "")
```
```python
    def _bronze_columns(self, con, table):
        """Lower-cased set of column names present in a table (bronze or silver)."""
        return {r[0].lower() for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall()}

    def _clean_fragment(self, col, kind, dtype):
        """SELECT fragment that carries one bronze column into silver, cleaned per kind.

        coded   -> codebook is_missing set OR a bare -1 -> NULL, else INTEGER code kept.
        numeric -> NUMERIC_SENTINELS/blank/non-numeric -> NULL, else typed to dtype
                   (this is the path longitude/latitude take once reclassified to DOUBLE).
        text    -> carried verbatim (free-text; normalization deferred).
        other   -> identity/any unrecognised kind carried raw (e.g. a child table's
                   accident_year/accident_reference, which no bespoke SELECT emits).
        The cleaned value stays a CODE/NUMBER, never a label; NULL means 'empty cell'.
        col/kind/dtype come from the trusted manifest (interpolated); no row values are.
        """
        if kind == "coded":
            # Null the codebook's is_missing codes, PLUS a bare -1 (DfT's universal missing
            # sentinel) so a codebook gap can't leak a -1 into a cleaned code. Never nulls a
            # real value; no coded field uses -1 as a meaningful category.
            return (f"CASE WHEN {col} = '-1' OR {col} IN (SELECT code FROM {self.CODEBOOK_TABLE} "
                    f"WHERE variable = '{col}' AND is_missing) "
                    f"THEN NULL ELSE TRY_CAST({col} AS INTEGER) END AS {col}")
        if kind == "numeric":
            typ = dtype or "INTEGER"
            sent = ", ".join(f"'{s}'" for s in NUMERIC_SENTINELS)
            return (f"CASE WHEN {col} IN ({sent}) THEN NULL "
                    f"ELSE TRY_CAST({col} AS {typ}) END AS {col}")
        return f"{col} AS {col}"   # text/identity (and any unrecognised kind): carry raw

    def _placeholder_fragment(self, col, kind, dtype):
        """Typed NULL for a manifest column ABSENT from this bronze (a future-year drop or
        the pre/post-2024 rename), so silver's schema stays stable across year selections."""
        typ = "INTEGER" if kind == "coded" else (dtype or "INTEGER") if kind == "numeric" else "VARCHAR"
        return f"CAST(NULL AS {typ}) AS {col}"

    def _broad_fragments(self, con, bronze_table, table_kind, exclude):
        """Deterministic list of broad-clean SELECT fragments for one file.

        Reads column_manifest for `table_kind` and skips the columns the bespoke path
        already emits, listed in `exclude` — the SINGLE authority for what bespoke owns
        (renamed or verbatim). Every other manifest column gets a cleaned fragment (or a
        typed NULL placeholder if absent from this bronze). Invariant: a column reaches
        silver via exactly ONE path — bespoke (in `exclude`) OR broad. We do NOT filter by
        kind: an earlier draft skipped identity/geo/datetime kinds, but that silently
        dropped columns no bespoke SELECT actually produced (longitude/latitude, and the
        child tables' accident_year/accident_reference). ORDER BY col keeps rebuilds
        structurally identical (spec §2)."""
        present = self._bronze_columns(con, bronze_table)
        exclude = {c.lower() for c in exclude}
        rows = con.execute(
            f"SELECT col, kind, dtype FROM {self.COLUMN_MANIFEST_TABLE} "
            f"WHERE tbl = ? ORDER BY col", [table_kind]).fetchall()
        frags = []
        for col, kind, dtype in rows:
            if col.lower() in exclude:
                continue
            frags.append(self._clean_fragment(col, kind, dtype) if col.lower() in present
                         else self._placeholder_fragment(col, kind, dtype))
        return frags
```
> **Trust boundary.** `col`/`kind`/`dtype`/`table_kind` all originate from the committed manifest (code-
> controlled), so interpolating them is safe — the same trusted-identifier rule `quality.py`/`spatial.py`
> follow. Row VALUES are never interpolated. The `variable = '{col}'` filter is a manifest identifier.

### B. Widen the collision derivation

The committed `_derive_collision_silver` uses a `WITH typed AS (SELECT …)` CTE. Change the CTE to
`SELECT *, …` so every raw bronze column is in scope for the broad fragments, and append the broad fragments
to the outer `SELECT` after the existing bespoke columns. List the columns the bespoke logic consumes in
`exclude` so the broad loop does not also carry them. **Everything else in the method (the sentinel handling,
geom, datetime, the two ledger writes) is unchanged.**

```python
    def _derive_collision_silver(self, con):
        """Collision silver: FULL keep-in-place. Existing identity/geom/datetime/lad/ctyua
        logic UNCHANGED; PLUS every remaining bronze column carried and cleaned per the
        column manifest (coded/numeric missing set -> NULL + typed; text raw). Codes kept,
        never labelled."""
        acc = self._coalesce_present(con, self.COLLISION_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        yr = self._coalesce_present(con, self.COLLISION_BRONZE,
                                    ["collision_year", "accident_year"], "accident_year")
        ref = self._coalesce_present(con, self.COLLISION_BRONZE,
                                     ["collision_ref_no", "collision_reference", "accident_reference"],
                                     "accident_reference")
        idx_expr = acc.replace(" AS accident_index", "")
        sentinels_sql = ", ".join(f"'{s}'" for s in COORD_SENTINELS)

        # Columns the bespoke logic consumes -> keep OUT of the broad loop. collision_severity
        # is broad-cleaned here as an ordinary coded column; Stage 07 moves it to `exclude`.
        # Note: longitude/latitude are NOT here -> they fall to the broad loop and are carried
        # as DOUBLE (manifest geo->numeric reclassification). Only the OSGR easting/northing,
        # which bespoke turns into easting/northing/geom, are excluded.
        exclude = {"accident_index", "collision_index", "accident_year", "collision_year",
                   "accident_reference", "collision_reference", "collision_ref_no",
                   "location_easting_osgr", "location_northing_osgr", "date", "time"}
        broad = self._broad_fragments(con, self.COLLISION_BRONZE, "collision", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""

        con.execute(
            f"CREATE OR REPLACE TABLE {self.COLLISION_SILVER} AS "
            f"WITH typed AS ("
            f"  SELECT *, "                       # <-- expose every raw bronze column
            f"    ({idx_expr}) AS source_row_key, {acc}, {yr}, {ref}, "
            f"    location_easting_osgr  AS easting_raw, "
            f"    location_northing_osgr AS northing_raw, "
            f"    CASE WHEN location_easting_osgr IN ({sentinels_sql}, '') THEN NULL "
            f"         ELSE TRY_CAST(location_easting_osgr AS DOUBLE) END AS easting, "
            f"    CASE WHEN location_northing_osgr IN ({sentinels_sql}, '') THEN NULL "
            f"         ELSE TRY_CAST(location_northing_osgr AS DOUBLE) END AS northing, "
            f"    date AS date_raw, time AS time_raw, "
            f"    TRY_STRPTIME(date, '%d/%m/%Y') AS date_parsed, "
            f"    TRY_STRPTIME(date || ' ' || time, '%d/%m/%Y %H:%M') AS datetime_parsed "
            f"  FROM {self.COLLISION_BRONZE}"
            f") "
            f"SELECT "
            f"  source_row_key, accident_index, accident_year, accident_reference, "
            f"  easting_raw, northing_raw, easting, northing, "
            f"  CASE WHEN easting IS NULL OR northing IS NULL THEN NULL "
            f"       ELSE ST_Point(easting, northing)::GEOMETRY END AS geom, "
            f"  (easting IS NOT NULL AND northing IS NOT NULL) AS geom_valid, "
            f"  date_raw, time_raw, "
            f"  COALESCE(datetime_parsed, date_parsed) AS datetime_local, "
            f"  (date_parsed IS NOT NULL) AS datetime_valid, "
            f"  CAST(NULL AS VARCHAR) AS lad_code, "
            f"  CAST(NULL AS VARCHAR) AS ctyua_code "
            f"  {broad_sql} "
            f"FROM typed"
        )
        # --- LEDGER (unchanged): geom + datetime FALSE-flag rows. Keep the existing two loops. ---
        ...   # (the committed bad_geom / bad_dt log_exclusion loops, verbatim)
```
> **Why `SELECT *` is safe here:** the outer `SELECT` lists an explicit, deterministic column set (bespoke
> first, then broad `ORDER BY col`), and the raw coordinate/date columns the bespoke logic replaced are in
> `exclude`, so they are not re-exported. The silver schema stays stable and reproducible.

### C. Widen the vehicle & casualty derivations

These are plain `SELECT … FROM bronze` (no CTE), so the raw columns are already in scope — just append the
broad fragments. Keep the identity/`source_row_key`/`link_valid`/`_log_orphans` logic unchanged.

```python
    def _derive_vehicle_silver(self, con):
        """Vehicle silver: FULL keep-in-place. Existing identity + link_valid UNCHANGED;
        PLUS every remaining bronze column carried + cleaned per the manifest."""
        acc = self._coalesce_present(con, self.VEHICLE_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx = acc.replace(" AS accident_index", "")
        # Exclude ONLY the columns the bespoke SELECT below actually emits: accident_index
        # (coalesced from either name) + vehicle_reference. accident_year/accident_reference
        # are NOT bespoke-produced here, so they must fall to the broad loop and be carried
        # (carried raw, matching how collision carries them) — otherwise they vanish.
        exclude = {"accident_index", "collision_index", "vehicle_reference"}
        broad = self._broad_fragments(con, self.VEHICLE_BRONZE, "vehicle", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.VEHICLE_SILVER} AS "
            f"SELECT ({idx}) || '|' || vehicle_reference AS source_row_key, "
            f"       {acc}, vehicle_reference, "
            f"       (({idx}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) AS link_valid "
            f"       {broad_sql} "
            f"FROM {self.VEHICLE_BRONZE}")
        self._log_orphans(con, self.VEHICLE_SILVER, self.VEHICLE_SID, self.VEHICLE_LINK_RULE)

    def _derive_casualty_silver(self, con):
        """Casualty silver: FULL keep-in-place. Existing identity + link_valid UNCHANGED;
        PLUS every remaining bronze column carried + cleaned per the manifest. casualty_severity
        is cleaned here as an ordinary coded column (Stage 07 promotes it to CORE)."""
        acc = self._coalesce_present(con, self.CASUALTY_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx = acc.replace(" AS accident_index", "")
        # As in vehicles: exclude ONLY the bespoke-emitted keys (accident_index +
        # vehicle_reference + casualty_reference). accident_year/accident_reference fall to
        # the broad loop and are carried, never dropped.
        exclude = {"accident_index", "collision_index", "vehicle_reference", "casualty_reference"}
        broad = self._broad_fragments(con, self.CASUALTY_BRONZE, "casualty", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CASUALTY_SILVER} AS "
            f"SELECT ({idx}) || '|' || vehicle_reference || '|' || casualty_reference AS source_row_key, "
            f"       {acc}, vehicle_reference, casualty_reference, "
            f"       (({idx}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) AS link_valid "
            f"       {broad_sql} "
            f"FROM {self.CASUALTY_BRONZE}")
        self._log_orphans(con, self.CASUALTY_SILVER, self.CASUALTY_SID, self.CASUALTY_LINK_RULE)
```

No `quality_spec()` change this stage (dimensions unchanged); no `client.py`/`registry.py` change. The
Stage 04 spatial stamp, `collisions_spatial`, and the R-Tree are unaffected — they read/write `geom` and
`lad_code`/`ctyua_code`, which still exist; the wider schema is transparent to them.

## Testing & Verification
Add to `tests/test_stats19.py`. All existing Stage 01–04 tests must stay green (widening silver keeps row
counts, uniqueness, identity, and the geom/datetime/link flags intact).

> **Existing derivation-driving tests need reference stubs.** A few Stage 01–04 unit tests call
> `_derive_*_silver` on a synthetic bronze without loading the codebook/manifest. The derivations now read
> `column_manifest`, so those tests fail with a missing-table error until they seed it. Add a shared helper that
> creates **empty** `codebook` + `column_manifest` tables (broad loop then returns no fragments -> the bespoke
> columns those tests target are unchanged) and call it before the `_derive_*` call in each:
> ```python
> def _empty_reference_stubs(con):
>     con.execute("CREATE TABLE IF NOT EXISTS codebook"
>                 "(variable VARCHAR, code VARCHAR, label VARCHAR, is_missing BOOLEAN)")
>     con.execute("CREATE TABLE IF NOT EXISTS column_manifest"
>                 "(tbl VARCHAR, col VARCHAR, kind VARCHAR, dtype VARCHAR)")
> ```

**Unit — broad clean keeps codes, nulls the full missing set (synthetic, authoritative):**
```python
def test_collision_broad_clean_keeps_codes_nulls_missing(con):
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('road_type','6','Single carriageway',FALSE),('road_type','-1','Data missing or out of range',TRUE),"
        "  ('road_type','9','Unknown',TRUE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('collision','road_type','coded','INTEGER'),"
        "  ('collision','number_of_vehicles','numeric','INTEGER'),"
        "  ('collision','longitude','numeric','DOUBLE'),"        # geo->numeric reclassification
        "  ('collision','lsoa_of_accident_location','text','')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30','6','2','E01000001','-0.12'),"
        "  ('c2','2023','r2','531000','181000','06/01/2023','09:00','9','-1','E01000002','-1'),"
        "  ('c3','2023','r3','532000','182000','07/01/2023','10:00','x','x','E01000003','x')"
        ") AS t(accident_index,accident_year,accident_reference,location_easting_osgr,"
        "location_northing_osgr,date,time,road_type,number_of_vehicles,lsoa_of_accident_location,longitude)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT accident_index, road_type, number_of_vehicles, longitude, "
        "lsoa_of_accident_location FROM collisions").fetchall()}
    assert rows["c1"][1:] == (6, 2, -0.12, "E01000001")   # codes/number kept; DOUBLE lon; text raw
    assert rows["c2"][1] is None                        # '9=Unknown' -> NULL
    assert rows["c2"][2] is None                        # '-1' INTEGER numeric -> NULL
    assert rows["c2"][3] is None                        # '-1' DOUBLE longitude -> NULL
    assert rows["c3"][1] is None and rows["c3"][2] is None and rows["c3"][3] is None  # non-numeric -> NULL
    assert con.execute("SELECT count(*) FROM collisions").fetchone()[0] == 3   # keep-in-place
    assert con.execute("SELECT count(*) FROM collisions WHERE road_type = -1").fetchone()[0] == 0
    dt = {r[0]: r[1] for r in con.execute("DESCRIBE collisions").fetchall()}
    assert dt["road_type"] == "INTEGER" and dt["lsoa_of_accident_location"] == "VARCHAR"
    assert dt["longitude"] == "DOUBLE"                  # lon/lat carried as real numbers, not dropped/raw
    # Bespoke geom/datetime still there.
    assert "geom" in dt and dt["datetime_local"].startswith("TIMESTAMP")
```

Add a matching `test_casualty_broad_clean` (drive `_derive_casualty_silver` against a `collisions` stub +
codebook/manifest stubs, asserting a coded column keeps its code, a `-1` numeric → NULL, and `link_valid`).

**Integration — full-width silver over the real sample (proves it ships):**
```python
def test_all_silver_tables_full_width(tmp_path):
    import os
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)          # runs all invariants; raises on failure
    FIX = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
    consumed = {"collision": {"location_easting_osgr","location_northing_osgr","date","time"},
                "vehicle": set(), "casualty": set()}
    for table_kind, silver in (("collision","collisions"),("vehicle","vehicles"),("casualty","casualties")):
        with open(os.path.join(FIX, f"dft-road-casualty-statistics-{table_kind}-2023.csv")) as f:
            header = [h.strip().lower() for h in f.readline().strip().split(",")]
        cols = {r[0].lower() for r in client.con.execute(f"DESCRIBE {silver}").fetchall()}
        for c in header:
            if c in consumed[table_kind]:
                continue
            assert c in cols, f"{silver}: column '{c}' missing from silver (not keep-in-place)"
    # No raw '-1' missing marker leaks into ANY cleaned coded/numeric column. Widened from
    # a 3-column spot-check to EVERY coded/numeric column the manifest defines, across all
    # three silver tables — closes the "some coded column silently keeps its -1" gap.
    for table_kind, silver in (("collision","collisions"),("vehicle","vehicles"),("casualty","casualties")):
        cleaned = [r[0] for r in client.con.execute(
            "SELECT col FROM column_manifest WHERE tbl = ? AND kind IN ('coded','numeric')",
            [table_kind]).fetchall()]
        present = {r[0].lower() for r in client.con.execute(f"DESCRIBE {silver}").fetchall()}
        for c in cleaned:
            if c.lower() in present:
                leaked = client.con.execute(
                    f"SELECT count(*) FROM {silver} WHERE {c} = -1").fetchone()[0]
                assert leaked == 0, f"{silver}.{c} still holds {leaked} raw -1 missing markers"
    # longitude/latitude are carried as real numbers (DOUBLE), not dropped or left raw text.
    ctypes = {r[0].lower(): r[1] for r in client.con.execute("DESCRIBE collisions").fetchall()}
    assert ctypes.get("longitude") == "DOUBLE" and ctypes.get("latitude") == "DOUBLE"
    # Geom/datetime dimensions still hold; gold views + spatial join unaffected.
    assert client.con.execute("SELECT count(*) FROM collisions WHERE geom_valid = FALSE").fetchone()[0] == 0
    assert client.con.execute("SELECT count(*) FROM collisions_spatial").fetchone()[0] > 0
    client.close()
```

Run:
```bash
source .venv/bin/activate
python -m pytest -q          # expected: all green (incl. all Stage 01–04 tests)
```

**Stage ship-readiness checklist:**
- [ ] `_broad_fragments`/`_clean_fragment`/`_placeholder_fragment`/`_bronze_columns` added; `NUMERIC_SENTINELS`
      defined. `_broad_fragments` uses `exclude` as the single authority (no `kind` filter).
- [ ] Manifest correction applied: `longitude`/`latitude` are `numeric`/`DOUBLE` (was `geo`), AND the pinned
      kind-breakdown assertion in `test_column_manifest_covers_every_fixture_column` updated to `geo: 2,
      numeric: 16`.
- [ ] `collisions`/`vehicles`/`casualties` carry **every** bronze column: coded/numeric cleaned+typed (full
      missing set → NULL, codes kept), text raw; deterministic column order. `longitude`/`latitude` land as
      `DOUBLE`; the child tables carry `accident_year`/`accident_reference` (not silently dropped).
- [ ] All existing bespoke logic + dimensions unchanged; conservation, flag/ledger agreement, reject-rate
      still pass across all three sources; Stage 04 spatial stamp + gold views unaffected.
- [ ] No raw `-1` leaks into ANY cleaned coded/numeric column (checked across all three tables, every such
      column — not just a spot-check); `9`/`99` self-reported unknowns kept; no `quality_spec()`/`pyproject.toml`
      change.
- [ ] `python -m pytest -q` fully green (including all Stage 01–04 tests).

## End State / Handoff
All three silver tables are now **full keep-in-place**: every bronze column carried, coded/numeric cleaned +
typed with codes kept, text raw. Nothing is dropped except the columns bespoke logic transforms in place (OSGR
`easting`/`northing` → `easting`/`northing`/`geom`; `date`/`time` → `datetime_local`). `longitude`/`latitude`
are carried as `DOUBLE`; the vehicle/casualty tables carry their own `accident_year`/`accident_reference`.
`collision_severity` and `casualty_severity` are present as ordinary cleaned `INTEGER` codes. Stage 07 promotes
those two to the formal audit (raw twin + valid flag + ledger + reject gate). Stage 08 reports completeness for
every cleaned column; Stage 09 adds labelled views.

## Failure Modes & Rollback
- **A cleaned column is all-NULL:** usually a manifest mis-kind (free-text tagged numeric/coded) — fix the
  manifest (Stage 05), not the code. NOTE it can also be legitimate: `enhanced_severity_collision` is a `coded`
  column with no codebook rows and is entirely `-1` in the 2023 sample, so it correctly cleans to all-NULL (the
  `-1` sentinel rule handles it). All-NULL means "no data for this field this year", which is the right
  representation — don't force a value.
- **A coded column keeps a raw `-1`:** the codebook has no `is_missing` row for that variable's `-1`. The coded
  clean's bare-`-1` rule catches this regardless, and the widened leak test asserts it across every coded/numeric
  column — this is exactly how `enhanced_severity_collision` surfaced during implementation.
- **A column silently drops out of silver:** it is in `exclude` (or the test's `consumed` set) but no bespoke
  SELECT actually emits it — the exact gap this stage fixes. `exclude` must list ONLY columns bespoke truly
  produces (renamed or verbatim); everything else falls to the broad loop. The `consumed` set in
  `test_all_silver_tables_full_width` is a human-maintained escape hatch — only add a column there if its data
  genuinely survives under a different name (e.g. `location_easting_osgr` → `easting`). A column parked in
  `consumed` without a real replacement is lost data that no test will catch.
- **`SELECT *` name clash** (a bronze column literally named `easting`/`geom`): the fixture has none; if a
  future file does, add the raw name to `exclude` or rename the bespoke alias.
- **Reference tables missing when a derivation runs:** ensure Stage 05's loaders run at the top of
  `transform_and_load`; in unit tests, create `codebook`/`column_manifest` stubs first.
- **An existing Stage 01–04 test fails:** widening should only *add* columns. If a test asserted an exact
  column set, relax it to a subset check (a column is present), not a fixed list — silver is intentionally
  wider now.
- **Rollback:** restore the committed (narrow) `_derive_*_silver` methods, remove the broad-clean helpers +
  `NUMERIC_SENTINELS`, delete the new tests. The suite returns to the Stage 05 state.
