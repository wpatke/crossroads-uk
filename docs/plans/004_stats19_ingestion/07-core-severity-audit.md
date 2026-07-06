# Stage 07 — Core Severity Audit
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's **"Enhancement Phase (post-Stage 04)"** section first. This is 07 of the revised plan.
> End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stages 05–06 are done: reference tables load at the top of `transform_and_load`; all three silver tables are
full keep-in-place (coded/numeric cleaned + typed, text raw). `collision_severity` (in `collisions`) and
`casualty_severity` (in `casualties`) are already cleaned `INTEGER` codes (full missing set → NULL) but carry
**no** `*_raw` twin, **no** `*_valid` flag, **no** ledger, **no** `Dimension`. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: the collision `SourceQuality` has `geom` + `datetime` dimensions; vehicle + casualty each
have `link`. `stats19.py` imports `Dimension`, `log_exclusion` (from Stages 02–03). Confirm they are imported.

## Objective
Promote the two headline severity outcomes out of the broad loop into the **formal audit** — the same rigour
geom/datetime/link already have. Each gains a `<col>_raw` twin (verbatim), keeps the cleaned `INTEGER`
(unchanged semantics), and gains a `<col>_valid` flag, a `reject_dimension` ledger rule, a `severity`
`Dimension` on its `SourceQuality`, and the reject-rate gate. This is "rigour where it is load-bearing." No
other coded field gets a formal dimension (Stage 08 reports the rest). Vehicles, the linkage, and the spatial
join are untouched.

> **Real severity code lists (2024 guide, verified).** Both `collision_severity` and `casualty_severity` are
> exactly `1=Fatal, 2=Serious, 3=Slight` — **no `-1` and no other sentinel in the code list** (so the clean
> guards a bare `-1` directly, exactly as the broad clean does, instead of relying on the codebook to list it
> for this variable). So on real,
> well-formed data `<col>_valid` is TRUE for every row and the reject rate is `0`; `*_valid = FALSE` only
> fires on a value that is blank/non-numeric or outside 1–3 (i.e. genuinely unparseable). The synthetic unit
> tests below deliberately inject `-1`/`9` missing codes into a *stub* codebook to exercise the FALSE branch
> — that is a mechanism test, not a claim that the real guide lists those codes for severity.

## Implementation Steps

### A. Add the CORE constants + helper (`src/crossroads/transformers/stats19.py`)

Near the other rule constants:
```python
    # CORE severity ledger rules (referenced by quality_spec's severity dimensions).
    COLLISION_SEVERITY_RULE = "stats19.collision_severity.missing"
    CASUALTY_SEVERITY_RULE  = "stats19.casualty_severity.missing"
```
Add the CORE-clean helper next to `_clean_fragment` (Stage 06). It reuses the codebook missing-set (identical
semantics to the broad coded clean) but also emits the `*_raw` twin and `*_valid` flag, and coalesces legacy
aliases so a pre-2024 tranche's `accident_severity` still populates `collision_severity`:
```python
    def _core_severity_fragments(self, con, bronze_table, column, aliases):
        """Return (raw_expr, cleaned_expr, valid_expr) for a CORE severity column.

        Same missing-set semantics as the broad coded clean: a bare -1 (DfT's universal
        "missing" sentinel, guarded even when the codebook omits it for this variable) OR
        any codebook is_missing code -> NULL, else the code is kept as INTEGER. Keeping the
        -1 guard matches _clean_fragment exactly, so a codebook gap can never leak a raw -1
        into the cleaned severity code. PLUS a <col>_raw twin (the raw code inline, for the
        ledger) and a <col>_valid flag. `aliases` handles the pre/post-2024 rename via
        COALESCE over the present columns. column/aliases are trusted constants
        (interpolated); no row values are interpolated.
        """
        raw = self._coalesce_present(con, bronze_table, aliases, f"{column}_raw")
        raw_ex = raw.replace(f" AS {column}_raw", "")        # bare expression, no alias
        cleaned = (f"CASE WHEN ({raw_ex}) = '-1' OR ({raw_ex}) IN (SELECT code FROM {self.CODEBOOK_TABLE} "
                   f"WHERE variable = '{column}' AND is_missing) "
                   f"THEN NULL ELSE TRY_CAST(({raw_ex}) AS INTEGER) END")
        return (f"({raw_ex}) AS {column}_raw",
                f"{cleaned} AS {column}",
                f"({cleaned}) IS NOT NULL AS {column}_valid")

    def _log_missing_codes(self, con, silver_table, source_id, column, rule_id):
        """One reject_dimension ledger row per <column>_valid = FALSE row, so flag/ledger
        agreement holds for the CORE field. Bounded per-row Python over the FALSE set; the
        scan itself is aggregate SQL."""
        bad = con.execute(
            f"SELECT source_row_key, {column}_raw FROM {silver_table} "
            f"WHERE {column}_valid = FALSE").fetchall()
        for key, raw in bad:
            log_exclusion(
                con, source_id=source_id, source_row_key=key,
                column_name=column, rule_id=rule_id,
                rule_desc="severity code is a missing/unknown sentinel or unparseable",
                severity="reject_dimension", raw_value=str(raw))
```

### B. Carve `collision_severity` out of the collision broad loop

In `_derive_collision_silver` (Stage 06): add the severity names to `exclude`, build the CORE fragments,
splice them into the outer `SELECT` after the `lad_code`/`ctyua_code` placeholders and before `{broad_sql}`,
then log after the CREATE:
```python
        exclude = { ...Stage-06 set... , "collision_severity", "accident_severity"}
        sev_raw, sev_clean, sev_valid = self._core_severity_fragments(
            con, self.COLLISION_BRONZE, "collision_severity", ["collision_severity", "accident_severity"])
        broad = self._broad_fragments(con, self.COLLISION_BRONZE, "collision", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""
        # ...in the outer SELECT, after `CAST(NULL AS VARCHAR) AS ctyua_code`:
        #     f"  , {sev_raw}, {sev_clean}, {sev_valid} "
        #     f"  {broad_sql} "
        # ...after CREATE OR REPLACE, alongside the geom/datetime ledger writes:
        self._log_missing_codes(con, self.COLLISION_SILVER, self.COLLISION_SID,
                                "collision_severity", self.COLLISION_SEVERITY_RULE)
```
> The `WITH typed AS (SELECT *, …)` CTE (Stage 06) already exposes every raw bronze column, so `raw_ex` (e.g.
> `collision_severity` or the coalesced `accident_severity`) resolves. Order: bespoke → CORE severity → broad.

### C. Carve `casualty_severity` out of the casualty broad loop

In `_derive_casualty_silver` (Stage 06): same pattern (it is a plain `SELECT … FROM bronze`, so `raw_ex`
references the bronze column directly):
```python
        exclude = { ...Stage-06 set... , "casualty_severity"}
        sev_raw, sev_clean, sev_valid = self._core_severity_fragments(
            con, self.CASUALTY_BRONZE, "casualty_severity", ["casualty_severity"])
        broad = self._broad_fragments(con, self.CASUALTY_BRONZE, "casualty", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CASUALTY_SILVER} AS "
            f"SELECT ({idx}) || '|' || vehicle_reference || '|' || casualty_reference AS source_row_key, "
            f"       {acc}, vehicle_reference, casualty_reference, "
            f"       (({idx}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) AS link_valid, "
            f"       {sev_raw}, {sev_clean}, {sev_valid} "
            f"       {broad_sql} "
            f"FROM {self.CASUALTY_BRONZE}")
        self._log_orphans(con, self.CASUALTY_SILVER, self.CASUALTY_SID, self.CASUALTY_LINK_RULE)
        self._log_missing_codes(con, self.CASUALTY_SILVER, self.CASUALTY_SID,
                                "casualty_severity", self.CASUALTY_SEVERITY_RULE)
```

### D. Add the `severity` dimension to both specs

In `quality_spec()`, extend the collision + casualty `SourceQuality` (vehicle unchanged):
```python
        SourceQuality(
            self.COLLISION_SID, self.COLLISION_BRONZE, self.COLLISION_SILVER,
            dimensions=(
                Dimension("geom", "geom_valid", (self.COORD_RULE,)),
                Dimension("datetime", "datetime_valid", (self.DATETIME_RULE,)),
                Dimension("severity", "collision_severity_valid", (self.COLLISION_SEVERITY_RULE,)),
            ),
            key_column="source_row_key"),
        # vehicle: unchanged (link only)
        SourceQuality(
            self.CASUALTY_SID, self.CASUALTY_BRONZE, self.CASUALTY_SILVER,
            dimensions=(
                Dimension("link", "link_valid", (self.CASUALTY_LINK_RULE,)),
                Dimension("severity", "casualty_severity_valid", (self.CASUALTY_SEVERITY_RULE,)),
            ),
            key_column="source_row_key"),
```
> **Reject-rate note.** Both severities are mandatory STATS19 fields, so the clean sample has no missing
> severities (rate ≈ 0). Deep-history tranches could differ — leave the ceiling at default and document that a
> real build may pass `build(reject_ceiling=…)` or set a per-`Dimension` `reject_ceiling`. Do **not** hard-code
> a higher ceiling.

## Testing & Verification
Add to `tests/test_stats19.py`.

**Unit — collision severity FALSE branch (synthetic, authoritative):**
```python
def test_collision_severity_core_audit(con):
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('collision_severity','1','Fatal',FALSE),('collision_severity','2','Serious',FALSE),"
        "  ('collision_severity','3','Slight',FALSE),('collision_severity','-1','Data missing or out of range',TRUE),"
        "  ('collision_severity','9','Unknown',TRUE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('collision','collision_severity','coded','INTEGER')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30','2'),"
        "  ('c2','2023','r2','531000','181000','06/01/2023','09:00','-1'),"
        "  ('c3','2023','r3','532000','182000','07/01/2023','10:00','9'),"
        "  ('c4','2023','r4','533000','183000','08/01/2023','11:00','x')"
        ") AS t(accident_index,accident_year,accident_reference,location_easting_osgr,"
        "location_northing_osgr,date,time,collision_severity)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT accident_index, collision_severity, collision_severity_valid, collision_severity_raw "
        "FROM collisions").fetchall()}
    assert rows["c1"][1:] == (2, True, "2")
    assert rows["c2"][1:] == (None, False, "-1")
    assert rows["c3"][1:] == (None, False, "9")
    assert rows["c4"][1:] == (None, False, "x")
    assert con.execute("SELECT count(*) FROM collisions").fetchone()[0] == 4   # keep-in-place
    ledger = con.execute("SELECT source_row_key, rule_id FROM data_quality_log "
        "WHERE rule_id='stats19.collision_severity.missing' ORDER BY source_row_key").fetchall()
    assert ledger == [("c2","stats19.collision_severity.missing"),
                      ("c3","stats19.collision_severity.missing"),
                      ("c4","stats19.collision_severity.missing")]
    dt = {r[0]: r[1] for r in con.execute("DESCRIBE collisions").fetchall()}
    assert dt["collision_severity"] == "INTEGER" and dt["collision_severity_raw"] == "VARCHAR"
    assert dt["collision_severity_valid"] == "BOOLEAN"
```

**Unit — the bare `-1` guard, codebook does NOT list `-1` (regression pin).** The test above injects `-1`
into the stub codebook, so it exercises the FALSE branch *through the codebook* — it would still pass if the
`-1` guard were removed. This test lists **only** the real severity codes (`1/2/3`, no `-1` row), matching the
real 2024 guide, and asserts a raw `-1` still cleans to `NULL` + `valid = FALSE` + a ledger row. It fails iff
the guard is missing, so it locks in the fix:
```python
def test_collision_severity_guards_bare_minus_one_without_codebook(con):
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    # Codebook lists ONLY the real severity codes — no -1 row (mirrors the real guide).
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('collision_severity','1','Fatal',FALSE),('collision_severity','2','Serious',FALSE),"
        "  ('collision_severity','3','Slight',FALSE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('collision','collision_severity','coded','INTEGER')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30','2'),"
        "  ('c2','2023','r2','531000','181000','06/01/2023','09:00','-1')"
        ") AS t(accident_index,accident_year,accident_reference,location_easting_osgr,"
        "location_northing_osgr,date,time,collision_severity)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT accident_index, collision_severity, collision_severity_valid, collision_severity_raw "
        "FROM collisions").fetchall()}
    assert rows["c1"][1:] == (2, True, "2")
    assert rows["c2"][1:] == (None, False, "-1")   # nulled by the -1 guard, NOT the codebook
    ledger = con.execute("SELECT source_row_key FROM data_quality_log "
        "WHERE rule_id='stats19.collision_severity.missing'").fetchall()
    assert ledger == [("c2",)]
```

Add `test_casualty_severity_core_audit` (mirror, driving `_derive_casualty_silver` against a `collisions`
stub + a `casualty_severity` codebook/manifest) and `test_collision_severity_reads_legacy_accident_severity`
(a bronze with `accident_severity` instead of `collision_severity` still populates `collision_severity`).

**Integration — severities audited end-to-end (real sample):**
```python
def test_severities_audited_end_to_end(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)          # runs all invariants; raises on failure
    for tbl, col in (("collisions","collision_severity"), ("casualties","casualty_severity")):
        dt = {r[0]: r[1] for r in client.con.execute(f"DESCRIBE {tbl}").fetchall()}
        assert dt[col] == "INTEGER" and dt[f"{col}_raw"] == "VARCHAR" and dt[f"{col}_valid"] == "BOOLEAN"
        assert client.con.execute(f"SELECT count(*) FROM {tbl} WHERE {col}_valid = FALSE").fetchone()[0] == 0
        assert client.con.execute(f"SELECT count(*) FROM {tbl} WHERE {col} = -1").fetchone()[0] == 0
    client.close()
```

Run:
```bash
source .venv/bin/activate
python -m pytest -q          # expected: all green
```

**Stage ship-readiness checklist:**
- [ ] `collisions` carries `collision_severity_raw`/`collision_severity`/`collision_severity_valid`;
      `casualties` the same for `casualty_severity`.
- [ ] Missing/unparseable severities → `NULL` + `valid = FALSE` + one ledger row each; rows retained; value
      stays a code.
- [ ] Legacy `accident_severity` coalesces into `collision_severity`.
- [ ] Both severities carved out of the broad loop (no duplicate column definition); collision spec declares
      `geom`+`datetime`+`severity`, casualty declares `link`+`severity`; flag/ledger agreement + reject-rate
      pass across all three sources.
- [ ] Vehicles, linkage, spatial join untouched; `pyproject.toml` untouched; `python -m pytest -q` fully green.

## End State / Handoff
Both headline severities are now formally audited (validated `INTEGER` code, raw kept, `*_valid` flag with
matching ledger rows, `severity` `Dimension` + reject gate). The reusable `_core_severity_fragments` /
`_log_missing_codes` helpers are ready for any future CORE promotion. Stage 08 reports completeness for every
other cleaned column; Stage 09 adds labelled views.

## Failure Modes & Rollback
- **Duplicate column definition:** if a severity is not added to `exclude`, both the broad loop and the CORE
  fragments define it → SQL "duplicate column" error. Ensure it is in `exclude`.
- **Flag/ledger disagreement fails the build:** keep the `cleaned`/`valid` expressions and the ledger scan
  derived from the same missing test (a bare `-1` or a codebook `is_missing` code); `_log_missing_codes`
  scans `<col>_valid = FALSE` directly, so it writes exactly one row per `<col>_valid = FALSE`.
- **`raw_ex` unresolved:** the collision derivation needs the `WITH typed AS (SELECT *, …)` CTE (Stage 06); the
  casualty derivation references bronze directly.
- **Severity column absent from a tranche (implementation note):** `_core_severity_fragments` degrades
  gracefully when NO alias is present — it carries stable typed NULLs with `<col>_valid = TRUE` and warns,
  instead of flagging every row FALSE (which would flood the ledger and trip the reject gate for a column that
  simply is not there). This mirrors the broad `_placeholder_fragment`/`_coalesce_present` convention and keeps
  the bespoke unit tests (which drive the derivations on severity-less synthetic bronze) green. Real STATS19
  always carries both severities, so this branch only guards synthetic/future-drop inputs.
- **Reject-rate tripwire on the sample:** the fixture unexpectedly has missing severities — re-trim to valid
  rows or override the ceiling per the note (do not hard-code a higher default).
- **Rollback:** revert both derivations to broad-cleaning the severities (remove from `exclude`; delete the
  CORE fragments + `_log_missing_codes` calls), remove the two `severity` dimensions + the CORE constants +
  `_core_severity_fragments`, delete the new tests. The suite returns to the Stage 06 state.
