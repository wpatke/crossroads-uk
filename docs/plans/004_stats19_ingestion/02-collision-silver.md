# Stage 02 — Collision Silver: Coordinates & Datetime
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map + Approach first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stage 01 is done: `stats19.py` has `Stats19Transformer` building 3 bronze + 3 **minimal** silver tables;
the quality engine supports multiple audit units; the committed sample lives in `tests/fixtures/stats19/`.
Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: `collisions` silver has only `source_row_key`, `accident_index`, `accident_year`,
`accident_reference`; the collision `SourceQuality` has **no dimensions**; there is no `geom` /
`datetime_local` column yet.

## Objective
Enrich the **collision** silver derivation: cast `location_easting_osgr` / `location_northing_osgr` into a
typed `easting` / `northing` and an EPSG:27700 `geom` point (flagging the `-1`/`0`/blank/non-numeric
"missing" sentinels — `geom NULL`, `geom_valid = FALSE`, logged, **row retained**); build a naive
`datetime_local TIMESTAMP` from `date` + `time` with a `datetime_valid` flag; add placeholder
`lad_code` / `ctyua_code` columns (filled in Stage 04); and declare the `geom` and `datetime` dimensions on
the collision `SourceQuality`. Vehicle/casualty and the spatial join are untouched here.

## Implementation Steps

### A. Rewrite `_derive_collision_silver` (`src/crossroads/transformers/stats19.py`)

Replace the minimal Stage-01 derivation with the full one (identity + typed coords + geom + datetime +
placeholders), then write the ledger rows for the FALSE flags. Keep it one method so tests drive it
directly against a synthetic bronze (the `spatial.py` `_derive_silver_and_ledger` pattern).

```python
    # Ledger rules (also referenced by quality_spec's dimensions).
    COORD_RULE = "stats19.coord.sentinel"
    DATETIME_RULE = "stats19.datetime.invalid"

    def _derive_collision_silver(self, con):
        """Collision silver: keep-in-place 1:1, with typed coordinates, an EPSG:27700
        geom point, a naive local datetime, and the ledger rows for missing/invalid
        values. Bad values are flagged + logged, never dropped (spec §9)."""
        acc = self._coalesce_present(con, self.COLLISION_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        yr = self._coalesce_present(con, self.COLLISION_BRONZE,
                                    ["collision_year", "accident_year"], "accident_year")
        ref = self._coalesce_present(con, self.COLLISION_BRONZE,
                                     ["collision_reference", "accident_reference"], "accident_reference")
        idx_expr = acc.replace(" AS accident_index", "")

        # Two-level select: the inner CTE types the coordinates and parses dates (SQL
        # cannot reference a sibling alias in the same SELECT list), the outer builds
        # geom / datetime / flags from those typed values. OSGR eastings/northings ARE
        # EPSG:27700, so ST_Point casts them directly — no reprojection (spec §3A).
        # A coordinate that is a sentinel ('-1'/'0'), blank, or non-numeric becomes
        # NULL (TRY_CAST) -> geom NULL -> geom_valid FALSE. date is DfT 'DD/MM/YYYY',
        # time 'HH:MM' (may be blank); a missing time falls back to midnight and is not
        # a rejection — only an unparseable DATE nulls datetime_local.
        con.execute(
            f"CREATE OR REPLACE TABLE {self.COLLISION_SILVER} AS "
            f"WITH typed AS ("
            f"  SELECT "
            f"    ({idx_expr}) AS source_row_key, {acc}, {yr}, {ref}, "
            f"    location_easting_osgr  AS easting_raw, "
            f"    location_northing_osgr AS northing_raw, "
            f"    CASE WHEN location_easting_osgr IN ('-1','0','') THEN NULL "
            f"         ELSE TRY_CAST(location_easting_osgr AS DOUBLE) END AS easting, "
            f"    CASE WHEN location_northing_osgr IN ('-1','0','') THEN NULL "
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
            # Prefer the full datetime; fall back to midnight when only the date parsed.
            f"  COALESCE(datetime_parsed, date_parsed) AS datetime_local, "
            f"  (date_parsed IS NOT NULL) AS datetime_valid, "
            # Filled by the Stage 04 spatial stamp; present now so the schema is stable.
            f"  CAST(NULL AS VARCHAR) AS lad_code, "
            f"  CAST(NULL AS VARCHAR) AS ctyua_code "
            f"FROM typed"
        )

        # --- LEDGER: one reject_dimension row per FALSE flag, so flag/ledger agreement
        # holds. Aggregate scan + a small Python loop over the (bounded) FALSE rows.
        bad_geom = con.execute(
            f"SELECT source_row_key, easting_raw, northing_raw FROM {self.COLLISION_SILVER} "
            f"WHERE geom_valid = FALSE").fetchall()
        for key, e, n in bad_geom:
            log_exclusion(
                con, source_id=self.COLLISION_SID, source_row_key=key,
                column_name="geom", rule_id=self.COORD_RULE,
                rule_desc="easting/northing missing or out of range "
                          "(sentinel -1/0, blank, or non-numeric)",
                severity="reject_dimension", raw_value=f"{e},{n}")

        bad_dt = con.execute(
            f"SELECT source_row_key, date_raw FROM {self.COLLISION_SILVER} "
            f"WHERE datetime_valid = FALSE").fetchall()
        for key, d in bad_dt:
            log_exclusion(
                con, source_id=self.COLLISION_SID, source_row_key=key,
                column_name="datetime_local", rule_id=self.DATETIME_RULE,
                rule_desc="collision date is missing or unparseable",
                severity="reject_dimension", raw_value=str(d))
```

### B. Add the geom + datetime dimensions to the collision spec

In `quality_spec()`, give the **collision** `SourceQuality` its two dimensions (vehicle/casualty stay
dimension-less until Stage 03):
```python
        SourceQuality(
            self.COLLISION_SID, self.COLLISION_BRONZE, self.COLLISION_SILVER,
            dimensions=(
                Dimension("geom", "geom_valid", (self.COORD_RULE,)),
                Dimension("datetime", "datetime_valid", (self.DATETIME_RULE,)),
            ),
            key_column="source_row_key"),
```

> **Reject-rate note.** The committed sample was trimmed to valid-coordinate collisions, so both reject
> rates are ~0 and stay under the 5% default. Deep historical tranches can exceed it — leave the ceiling at
> default and document that a real deep-history build can pass `build(reject_ceiling=...)` or a
> per-`Dimension` `reject_ceiling` if needed (do **not** hard-code a higher ceiling here).

## Testing & Verification
Add to `tests/test_stats19.py`.

**Integration (offline, real sample):** extend the end-to-end checks.
```python
def test_collision_geometry_is_epsg_27700(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    # Every non-null collision point sits inside the British National Grid envelope.
    row = client.con.execute(
        "SELECT min(ST_X(geom)), max(ST_X(geom)), min(ST_Y(geom)), max(ST_Y(geom)) "
        "FROM collisions WHERE geom IS NOT NULL").fetchone()
    assert 0 <= row[0] and row[1] <= 700_000       # easting band
    assert 0 <= row[2] and row[3] <= 1_300_000     # northing band
    # The clean sample has all-valid geometry.
    assert client.con.execute(
        "SELECT count(*) FROM collisions WHERE geom_valid = FALSE").fetchone()[0] == 0
    client.close()


def test_collision_datetime_local_built(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    # datetime_local is a real TIMESTAMP and all sample rows parsed.
    bad = client.con.execute(
        "SELECT count(*) FROM collisions WHERE datetime_valid = FALSE").fetchone()[0]
    assert bad == 0
    dtype = {r[0]: r[1] for r in client.con.execute("DESCRIBE collisions").fetchall()}
    assert dtype["datetime_local"].startswith("TIMESTAMP")
    assert dtype["geom"] == "GEOMETRY"     # bare geometry (RTREE-ready in Stage 04)
    client.close()
```

**Unit (FALSE branches, synthetic bronze — the authoritative correctness proof):**
```python
def test_sentinel_and_bad_date_flagged_and_logged(con):
    # Drive the collision derivation against a hand-built bronze containing a valid
    # row, a sentinel-coordinate row, and a bad-date row. Proves geom/datetime flags
    # + matching ledger entries without needing a dirty fixture (keeps the sample clean
    # so the e2e reject rate stays 0). Mirrors spatial.py's invalid-geometry test.
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute(
        "CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30'), "   # valid
        "  ('c2','2023','r2','-1','-1','06/01/2023','09:00'), "           # sentinel coords
        "  ('c3','2023','r3','531000','181000','not-a-date','10:00')  "   # bad date
        ") AS t(accident_index, accident_year, accident_reference, "
        "       location_easting_osgr, location_northing_osgr, date, time)")
    t = Stats19Transformer()
    t._derive_collision_silver(con)

    rows = {r[0]: r for r in con.execute(
        "SELECT source_row_key, geom_valid, datetime_valid, geom IS NULL "
        "FROM collisions").fetchall()}
    assert rows["c1"][1] is True and rows["c1"][2] is True        # valid
    assert rows["c2"][1] is False and rows["c2"][3] is True       # geom NULL, flagged
    assert rows["c3"][2] is False                                 # bad date flagged
    # Row is retained (keep-in-place): 3 silver rows, none deleted.
    assert con.execute("SELECT count(*) FROM collisions").fetchone()[0] == 3

    # Ledger has exactly the two rejections with the right rules.
    ledger = con.execute(
        "SELECT source_row_key, rule_id FROM data_quality_log "
        "WHERE source_id = 'stats19_collision' AND severity = 'reject_dimension' "
        "ORDER BY source_row_key").fetchall()
    assert ledger == [("c2", "stats19.coord.sentinel"),
                      ("c3", "stats19.datetime.invalid")]


def test_missing_time_falls_back_to_midnight(con):
    # A blank time is NOT a rejection: datetime_local is that date at 00:00 and valid.
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute(
        "CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c9','2023','r9','530000','180000','07/01/2023','')"
        ") AS t(accident_index, accident_year, accident_reference, "
        "       location_easting_osgr, location_northing_osgr, date, time)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    row = con.execute(
        "SELECT datetime_valid, CAST(datetime_local AS VARCHAR) FROM collisions").fetchone()
    assert row[0] is True and row[1].startswith("2023-01-07 00:00:00")
```

Run:
```bash
source .venv/bin/activate
python -m pytest -q          # expected: all green
```

**Stage ship-readiness checklist:**
- [ ] Collision silver carries `easting`/`northing` (typed), `geom` (bare `GEOMETRY`, EPSG:27700),
      `geom_valid`, `datetime_local` (`TIMESTAMP`), `datetime_valid`, and `lad_code`/`ctyua_code` placeholders.
- [ ] Sentinel/blank/non-numeric coordinates → `geom NULL`, `geom_valid = FALSE`, ledger row
      (`stats19.coord.sentinel`), **row retained**.
- [ ] Unparseable date → `datetime_valid = FALSE`, ledger row (`stats19.datetime.invalid`); missing time →
      midnight, not a rejection.
- [ ] Collision `SourceQuality` declares `geom` + `datetime` dimensions; flag/ledger agreement +
      reject-rate pass on the sample.
- [ ] `python -m pytest -q` fully green.

## End State / Handoff
`collisions` silver now has typed coordinates, an EPSG:27700 `geom`, a naive `datetime_local`, validity
flags, and `lad_code`/`ctyua_code` placeholders; sentinel and bad-date rows are flagged + logged, never
dropped; the collision source passes conservation, flag/ledger agreement, and reject-rate. Stage 03 may
assume this schema and add vehicle/casualty typing + linkage. Stage 04 will fill `lad_code`/`ctyua_code`
via the spatial join and expose `collisions_spatial`.

## Failure Modes & Rollback
- **Coordinate column names differ** (`location_easting_osgr`/`location_northing_osgr`): confirm against
  the real headers; they are stable across STATS19 years but verify. Update the CASE expressions if so.
- **Date format differs** (not `DD/MM/YYYY`): `datetime_valid` would be FALSE for every row and the
  reject-rate tripwire fires. Inspect a raw `date` value and fix the `strptime` format string.
- **Alias reference error** ("Referenced column ... not found"): a sibling alias was used in the same
  SELECT list — keep the two-level CTE (types inner, geom/datetime outer).
- **`geom` reports `GEOMETRY('EPSG:27700')` not `GEOMETRY`**: unlikely from `ST_Point` (it yields bare
  geometry), but the `::GEOMETRY` cast guarantees it for the Stage 04 RTREE. Keep the cast.
- **Rollback:** restore the Stage-01 minimal `_derive_collision_silver` and remove the collision
  dimensions from `quality_spec`; delete the new tests. The suite returns to the Stage 01 state.
