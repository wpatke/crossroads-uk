# Stage 03 — Vehicle & Casualty Silver & Linkage
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map + Approach first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stage 02 is done: collision silver has typed coordinates, `geom` (EPSG:27700), `datetime_local`, and the
`geom`/`datetime` dimensions. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: `vehicles` / `casualties` silver still carry only `source_row_key`, `accident_index`,
`vehicle_reference` (+ `casualty_reference`); their `SourceQuality` entries have **no dimensions**; there
are no `vehicles_clean` / `casualties_clean` gold views.

## Objective
Cleanse vehicle and casualty silver and **relationally link** them to collisions: add a `link_valid`
dimension (the row's `accident_index` resolves to a `collisions` row), flag + log orphans (retained, never
dropped), declare the `link` dimension on both specs, and expose `vehicles_clean` / `casualties_clean`
gold views. This completes the three-table STATS19 relational model; the spatial join is Stage 04.

The essential Step 4 deliverable here is **referential linkage** (`link_valid`). Additional per-field
typing (e.g. `age_of_driver`, `casualty_severity`) follows the Stage-02 coordinate pattern and can be
added as downstream analyses require — it is intentionally **not** required for this stage, to keep the
derivations robust against optional/renamed columns.

## Implementation Steps

### A. Rewrite the vehicle & casualty derivations (`src/crossroads/transformers/stats19.py`)

```python
    # Linkage ledger rules (referenced by quality_spec's dimensions).
    VEHICLE_LINK_RULE = "stats19.link.orphan_vehicle"
    CASUALTY_LINK_RULE = "stats19.link.orphan_casualty"

    def _derive_vehicle_silver(self, con):
        """Vehicle silver: keep-in-place 1:1, linked to collisions by accident_index.
        A vehicle whose accident_index has no collision is flagged link_valid = FALSE
        and logged (orphan), never dropped."""
        acc = self._coalesce_present(con, self.VEHICLE_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx = acc.replace(" AS accident_index", "")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.VEHICLE_SILVER} AS "
            f"SELECT ({idx}) || '|' || vehicle_reference AS source_row_key, "
            f"       {acc}, vehicle_reference, "
            f"       (({idx}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) AS link_valid "
            f"FROM {self.VEHICLE_BRONZE}"
        )
        self._log_orphans(con, self.VEHICLE_SILVER, self.VEHICLE_SID, self.VEHICLE_LINK_RULE)

    def _derive_casualty_silver(self, con):
        """Casualty silver: keep-in-place 1:1, linked to collisions by accident_index
        (carries vehicle_reference for the finer casualty→vehicle link). Orphans flagged."""
        acc = self._coalesce_present(con, self.CASUALTY_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx = acc.replace(" AS accident_index", "")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CASUALTY_SILVER} AS "
            f"SELECT ({idx}) || '|' || vehicle_reference || '|' || casualty_reference "
            f"         AS source_row_key, "
            f"       {acc}, vehicle_reference, casualty_reference, "
            f"       (({idx}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) AS link_valid "
            f"FROM {self.CASUALTY_BRONZE}"
        )
        self._log_orphans(con, self.CASUALTY_SILVER, self.CASUALTY_SID, self.CASUALTY_LINK_RULE)

    def _log_orphans(self, con, silver_table, source_id, rule_id):
        """Write one reject_dimension ledger row per link_valid = FALSE row."""
        orphans = con.execute(
            f"SELECT source_row_key, accident_index FROM {silver_table} "
            f"WHERE link_valid = FALSE").fetchall()
        for key, acc_idx in orphans:
            log_exclusion(
                con, source_id=source_id, source_row_key=key,
                column_name="accident_index", rule_id=rule_id,
                rule_desc="accident_index has no matching collision row",
                severity="reject_dimension", raw_value=str(acc_idx))
```

> `_derive_vehicle_silver` / `_derive_casualty_silver` **read the `collisions` silver table**, so
> `transform_and_load` must derive collision silver **before** them (it already does — keep that order).
> In tests that drive these helpers directly, create a `collisions` table (even a 1-column stub with the
> matching `accident_index` values) first.

### B. Add gold views (in `transform_and_load`, after the silver derivations)

```python
        # --- GOLD: valid-link projections (spec §9 clean views). collisions_spatial
        # is added in Stage 04 once lad_code/ctyua_code are stamped.
        create_clean_view(con, "vehicles_clean", self.VEHICLE_SILVER, ["link_valid"])
        create_clean_view(con, "casualties_clean", self.CASUALTY_SILVER, ["link_valid"])
```

### C. Add the `link` dimension to the vehicle & casualty specs

```python
        SourceQuality(
            self.VEHICLE_SID, self.VEHICLE_BRONZE, self.VEHICLE_SILVER,
            dimensions=(Dimension("link", "link_valid", (self.VEHICLE_LINK_RULE,)),),
            key_column="source_row_key"),
        SourceQuality(
            self.CASUALTY_SID, self.CASUALTY_BRONZE, self.CASUALTY_SILVER,
            dimensions=(Dimension("link", "link_valid", (self.CASUALTY_LINK_RULE,)),),
            key_column="source_row_key"),
```

> **Reject-rate note.** The committed sample preserves referential integrity (child rows were SEMI-joined
> to the collision sample in Stage 01), so `link_valid` is TRUE for every sample row and the reject rate is
> 0. Real full-year tranches are internally consistent too, so orphans should be rare; if a genuine
> upstream break pushes the rate over 5% the tripwire correctly fires.

## Testing & Verification
Add to `tests/test_stats19.py`.

**Integration (offline, real sample):**
```python
def test_vehicles_and_casualties_link_to_collisions(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    # Every sample child row links to a collision (fixtures preserve integrity).
    for silver in ("vehicles", "casualties"):
        bad = client.con.execute(
            f"SELECT count(*) FROM {silver} WHERE link_valid = FALSE").fetchone()[0]
        assert bad == 0, f"{silver} has unexpected orphan rows"
    # Gold views exist and equal their silver (all-linked sample).
    for silver, view in (("vehicles", "vehicles_clean"), ("casualties", "casualties_clean")):
        s = client.con.execute(f"SELECT count(*) FROM {silver}").fetchone()[0]
        v = client.con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        assert s == v and s > 0
    client.close()
```

**Unit (orphan FALSE branch, synthetic bronze — authoritative proof):**
```python
def test_orphan_vehicle_is_flagged_and_logged(con):
    # collisions has c1 only; a vehicle referencing c1 links, one referencing cX is an orphan.
    from crossroads.quality import ensure_quality_tables
    ensure_quality_tables(con)
    con.execute("CREATE TABLE collisions AS SELECT * FROM (VALUES ('c1')) AS t(accident_index)")
    con.execute(
        "CREATE TABLE stats19_vehicle_raw AS SELECT * FROM (VALUES "
        "  ('c1','1'), ('cX','1')"
        ") AS t(accident_index, vehicle_reference)")
    t = Stats19Transformer(); t._derive_vehicle_silver(con)

    rows = {r[0]: r[1] for r in con.execute(
        "SELECT accident_index, link_valid FROM vehicles").fetchall()}
    assert rows["c1"] is True and rows["cX"] is False
    assert con.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 2   # retained
    ledger = con.execute(
        "SELECT source_row_key, rule_id FROM data_quality_log "
        "WHERE source_id = 'stats19_vehicle' AND severity = 'reject_dimension'").fetchall()
    assert ledger == [("cX|1", "stats19.link.orphan_vehicle")]
```

Run:
```bash
source .venv/bin/activate
python -m pytest -q          # expected: all green
```

**Stage ship-readiness checklist:**
- [ ] `vehicles` / `casualties` silver carry `link_valid`; orphans flagged + logged
      (`stats19.link.orphan_vehicle` / `_casualty`), rows retained.
- [ ] Vehicle/casualty `SourceQuality` declare the `link` dimension; flag/ledger agreement + reject-rate
      pass across all three sources.
- [ ] `vehicles_clean` / `casualties_clean` gold views exist.
- [ ] Vehicle/casualty derivations run **after** collision silver (dependency order preserved).
- [ ] `python -m pytest -q` fully green.

## End State / Handoff
All three STATS19 tables are cleansed and audited: collisions (geom + datetime), vehicles and casualties
(linked to collisions via `link_valid`), with clean gold views for the child tables. Every source passes
conservation, flag/ledger agreement, and reject-rate. Stage 04 may assume the linked model and add the
point-in-polygon boundary stamp + `collisions_spatial`.

## Failure Modes & Rollback
- **`vehicle_reference` / `casualty_reference` absent or renamed in a tranche:** the derivation errors on
  the missing column. Confirm the real headers; both are stable STATS19 columns, but verify and adjust the
  key expression if a tranche differs.
- **Vehicle/casualty derived before collision silver exists:** the `IN (SELECT ... FROM collisions)`
  subquery errors ("table collisions does not exist"). Ensure `transform_and_load` derives collision
  silver first and tests create a `collisions` stub.
- **Unexpected orphans in the real sample push reject-rate over ceiling:** the fixtures lost referential
  integrity — re-trim with the SEMI JOIN recipe (Stage 01 step C) so child rows only reference sampled
  collisions.
- **Rollback:** restore the Stage-02 minimal vehicle/casualty derivations, remove the link dimensions and
  gold views, delete the new tests. The suite returns to the Stage 02 state.
