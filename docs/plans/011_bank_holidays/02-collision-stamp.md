# Stage 02 — Collision `is_bank_holiday` stamp
> Part of GOV.UK Bank Holidays. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
- **Stage 01 is complete:** `src/crossroads/transformers/bank_holidays.py` exists and builds a
  `bank_holidays` silver table `(source_row_key, date DATE, division, title, notes, bunting)`.
  Verify: `pytest -q tests/test_bank_holidays.py` is green.
- Confirm the STATS19 stamping patterns you will copy in
  [`src/crossroads/transformers/stats19.py`](../../../src/crossroads/transformers/stats19.py):
  - `depends_on = ("era5_weather", "ons_lad", "ons_ctyua")` (class attribute, ~line 73).
  - The collision silver `CREATE OR REPLACE TABLE {self.COLLISION_SILVER}` SELECT (~lines 523–568),
    where stamped columns are pre-declared as `CAST(NULL AS …) AS …` (e.g. `lad_code`,
    `temperature_c`, `solar_elevation_deg`).
  - `_spatial_stamp`, `_weather_stamp`, `_solar_stamp` methods (~lines 693–860) and the calls to them
    in `transform_and_load` (~lines 451–463).
  - `self._table_exists(con, name)` helper (used by `_weather_stamp`).
  - Run `grep -n "lad_code\|_solar_stamp\|_table_exists\|depends_on\|CAST(NULL AS DOUBLE) AS solar" src/crossroads/transformers/stats19.py`
    to pin the exact current line numbers before editing.

## Objective
Stamp a tri-state `is_bank_holiday BOOLEAN` onto every collision — TRUE on a bank holiday for that
collision's nation, FALSE on a known in-coverage non-holiday, and **NULL** where the answer is
unknown (division undeterminable, date unparsed, or date outside the feed's coverage). The nation is
derived from the collision's `lad_code` prefix. Guarded so a build without `bank_holidays` leaves the
column NULL.

## Implementation Steps

### 1. Declare the column in collision silver — `stats19.py` (~line 563)
In the collision silver `SELECT` (the `_derive_collision_silver` method), add the new NULL-initialised
column alongside the other stamped columns. Insert it right **after** the two solar columns
(`solar_azimuth_deg`), keeping the existing trailing comma structure intact:

```python
            f"  CAST(NULL AS DOUBLE) AS solar_elevation_deg, "
            f"  CAST(NULL AS DOUBLE) AS solar_azimuth_deg, "
            # Filled by _bank_holiday_stamp when a bank_holidays table exists: TRUE if the
            # collision's date is a bank holiday in its nation, FALSE if a known non-holiday
            # in-coverage, NULL if unknown (no/unknown nation, no date, or date outside the
            # feed's coverage for that nation). NULL is a first-class "unknown", not a reject.
            f"  CAST(NULL AS BOOLEAN) AS is_bank_holiday, "
```
Expected result: the collision silver table always carries an `is_bank_holiday` column (NULL until
stamped), so the schema is stable whether or not `bank_holidays` is built — exactly like `lad_code`.

### 2. Add `"bank_holidays"` to `depends_on` — `stats19.py` (~line 73)
So the registry orders `bank_holidays` before STATS19 when both are active (the stamp reads the
`bank_holidays` table). The edge is optional — if `bank_holidays` isn't selected, STATS19 still runs
and the stamp's guard leaves the column NULL.

```python
    depends_on = ("bank_holidays", "era5_weather", "ons_lad", "ons_ctyua")
```
Update the adjacent comment to mention that `bank_holidays` is also consumed by the stamp. Expected
result: `Stats19Transformer().depends_on` includes `"bank_holidays"`.

### 3. Add the stamp method — `stats19.py`
Add `_bank_holiday_stamp` next to `_weather_stamp` / `_solar_stamp`. It mirrors `_weather_stamp`'s
missing-table guard and set-based `UPDATE`. Uses `EXISTS` (not a join) so it cannot fan out even if a
`(division, date)` appeared twice.

```python
    def _bank_holiday_stamp(self, con):
        """Stamp is_bank_holiday onto collisions from the bank_holidays dimension (Stage 02).

        Tri-state, per the locked requirement — "known not a holiday" must be distinct from
        "no data":
          • NULL  if the nation can't be determined (no/unknown lad_code), the date didn't
                  parse (datetime_local NULL), or the date is OUTSIDE the feed's coverage for
                  that nation.
          • TRUE  if the date is a bank holiday in that nation.
          • FALSE only if the date is within coverage for that nation and is not a holiday.

        Nation comes from the ONS LAD code prefix (a stable GSS convention):
          E…/W… -> england-and-wales   S… -> scotland   N… -> northern-ireland   else -> NULL.
        Coverage is per division: the [min(year), max(year)] span of that division's events in
        the feed (the feed publishes contiguous whole years). Set-based UPDATE, no row loop.

        Guarded like _weather_stamp: if bank_holidays was not built this run (source not
        selected), warn and leave the column NULL — collisions still build."""
        if not self._table_exists(con, "bank_holidays"):
            warnings.warn(
                "stats19: bank_holidays table not found; is_bank_holiday left NULL "
                "(build the bank_holidays dataset alongside stats19 to enable the flag).",
                stacklevel=2)
            return
        con.execute(
            f"UPDATE {self.COLLISION_SILVER} AS c SET is_bank_holiday = m.val "
            f"FROM ("
            f"  WITH cov AS ("                              # per-division coverage years
            f"    SELECT division, min(year(date)) AS min_y, max(year(date)) AS max_y "
            f"    FROM bank_holidays GROUP BY division"
            f"  ), cold AS ("                               # each collision -> its nation + date
            f"    SELECT source_row_key AS k, "
            f"           CAST(datetime_local AS DATE) AS cdate, "
            f"           CASE "
            f"             WHEN lad_code LIKE 'E%' OR lad_code LIKE 'W%' THEN 'england-and-wales' "
            f"             WHEN lad_code LIKE 'S%' THEN 'scotland' "
            f"             WHEN lad_code LIKE 'N%' THEN 'northern-ireland' "
            f"             ELSE NULL END AS division "
            f"    FROM {self.COLLISION_SILVER}"
            f"  ) "
            f"  SELECT cold.k AS k, "
            f"    CASE "
            f"      WHEN cold.division IS NULL THEN NULL "                 # unknown nation
            f"      WHEN cold.cdate IS NULL THEN NULL "                    # no parsed date
            f"      WHEN cov.min_y IS NULL THEN NULL "                     # nation absent from feed
            f"      WHEN year(cold.cdate) NOT BETWEEN cov.min_y AND cov.max_y THEN NULL "  # out of coverage
            f"      WHEN EXISTS (SELECT 1 FROM bank_holidays bh "
            f"                   WHERE bh.division = cold.division AND bh.date = cold.cdate) THEN TRUE "
            f"      ELSE FALSE "                                           # in coverage, not a holiday
            f"    END AS val "
            f"  FROM cold LEFT JOIN cov ON cov.division = cold.division"
            f") m WHERE c.source_row_key = m.k"
        )
```
Notes for the executor:
- `year(...)` is DuckDB's date part function (equivalent to `EXTRACT(year FROM …)`).
- `cold` is 1:1 with collisions and `cov` joins 1:1 on division, so `m` has exactly one row per
  collision — the `UPDATE` sets `is_bank_holiday` for every row (writing NULL where undetermined,
  which is harmless as the column is already NULL).
- No ledger / `data_quality_log` write — NULL is legitimate "unknown", not a rejection (same stance
  as `_weather_stamp`/`_solar_stamp`, which add no audit dimension).

### 4. Call the stamp in `transform_and_load` — `stats19.py` (~after line 460)
It must run **after** `_spatial_stamp` (it reads `lad_code`). Place it right after `_solar_stamp`,
before the `collisions_spatial` gold view:

```python
        # --- SOLAR STAMP ... (existing)
        self._solar_stamp(con)

        # --- BANK-HOLIDAY STAMP (optional): fill is_bank_holiday from the bank_holidays
        # dimension if it was built this run. Needs lad_code (set by _spatial_stamp above).
        self._bank_holiday_stamp(con)

        # --- GOLD: the valid-geometry collision projection ... (existing)
        create_clean_view(con, "collisions_spatial", self.COLLISION_SILVER, ["geom_valid"])
```
Expected result: after a combined STATS19 + `bank_holidays` build, `collisions.is_bank_holiday` is
populated (tri-state); after a STATS19-only build it is all NULL with a warning.

### 5. Tests — `tests/test_stats19.py`
Add three tests. Reuse the file's existing helpers (`_stats19_client`, `_seed_cache`,
`_seed_ons_cache`, `YEARS`, boundary-transformer imports) — confirm their exact names at the top of
the file before writing.

**(a) Unit — tri-state + division routing, deterministic (no real calendar).**
Drive `_bank_holiday_stamp` directly on synthetic tables:

```python
def test_bank_holiday_stamp_tri_state_and_division(con):
    from crossroads.transformers.stats19 import Stats19Transformer
    # Synthetic bank_holidays: coverage = 2023 for all divisions. Deliberate divergence:
    #   Easter Monday 2023-04-10 -> england-and-wales ONLY
    #   2nd January  2023-01-03 -> scotland ONLY
    con.execute(
        "CREATE TABLE bank_holidays AS SELECT * FROM (VALUES "
        "  ('england-and-wales', DATE '2023-04-10', 'Easter Monday'), "
        "  ('england-and-wales', DATE '2023-01-02', 'New Year'), "
        "  ('scotland',          DATE '2023-01-03', '2nd January'), "
        "  ('scotland',          DATE '2023-01-02', 'New Year') "
        ") AS t(division, date, title)")
    # Synthetic collisions: one row per case. is_bank_holiday starts NULL.
    con.execute(
        "CREATE TABLE collisions AS SELECT * FROM (VALUES "
        "  ('A', 'E06000001', TIMESTAMP '2023-04-10 09:00'), "  # Eng holiday      -> TRUE
        "  ('B', 'E06000001', TIMESTAMP '2023-06-15 09:00'), "  # Eng non-holiday  -> FALSE
        "  ('C', 'S12000033', TIMESTAMP '2023-04-10 09:00'), "  # same date, Scot  -> FALSE (routing)
        "  ('D', 'E06000001', TIMESTAMP '1999-04-10 09:00'), "  # out of coverage  -> NULL
        "  ('E', NULL,        TIMESTAMP '2023-04-10 09:00'), "  # unknown nation   -> NULL
        "  ('F', 'S12000033', TIMESTAMP '2023-01-03 09:00'), "  # Scot-only holiday-> TRUE
        "  ('W', 'W06000001', TIMESTAMP '2023-04-10 09:00')  "  # Wales->eng cal   -> TRUE
        ") AS t(source_row_key, lad_code, datetime_local) ")
    con.execute("ALTER TABLE collisions ADD COLUMN is_bank_holiday BOOLEAN")

    Stats19Transformer()._bank_holiday_stamp(con)

    got = dict(con.execute(
        "SELECT source_row_key, is_bank_holiday FROM collisions").fetchall())
    assert got["A"] is True
    assert got["B"] is False
    assert got["C"] is False        # division routing: Eng holiday is NOT a Scot holiday
    assert got["D"] is None         # outside feed coverage -> unknown, not FALSE
    assert got["E"] is None         # unknown nation -> unknown
    assert got["F"] is True
    assert got["W"] is True         # Wales resolves to the england-and-wales calendar
```
(If DuckDB returns SQL booleans as something other than Python `True/False/None`, assert on the
values it does return — e.g. compare against `1/0/None` — but keep the three-way distinction.)

**(b) Combined build (offline) — column present, stamped, row count unchanged.**
Model on `test_stats19_plus_weather_stamps_collisions_offline` (seed STATS19 + ONS + the
bank-holidays fixture, restrict the registry, build both):

```python
def test_stats19_plus_bank_holidays_stamps_collisions_offline(tmp_path):
    import shutil, os
    from crossroads.transformers.bank_holidays import BankHolidaysTransformer
    cache = str(tmp_path / "cache")
    _seed_cache(cache); _seed_ons_cache(cache)
    shutil.copy(
        os.path.join(os.path.dirname(__file__), "fixtures", "bank_holidays",
                     "bank-holidays-sample.json"),
        os.path.join(cache, "bank-holidays.json"))
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [
        CTYUABoundaryTransformer(), LADBoundaryTransformer(),
        Stats19Transformer(), BankHolidaysTransformer()]      # get_active orders bh first
    client.build(datasets=["stats19", "bank_holidays"], years=YEARS)
    try:
        cols = [r[0] for r in client.con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='collisions'").fetchall()]
        assert "is_bank_holiday" in cols
        total = client.con.execute("SELECT count(*) FROM collisions").fetchone()[0]
        assert total > 0                                        # row count unchanged by an UPDATE
        # At least one collision resolves to a determinable value: the 2023 GB fixture
        # collisions have E/S/W lad_codes and 2023 dates within the fixture's 2023 coverage.
        determinable = client.con.execute(
            "SELECT count(*) FROM collisions WHERE is_bank_holiday IS NOT NULL").fetchone()[0]
        assert determinable >= 1, (
            "No collision resolved to TRUE/FALSE — check that the committed STATS19 collision "
            "fixture has valid lad_codes (spatial join) and 2023 dates within the bank-holidays "
            "fixture's coverage.")
    finally:
        client.close()
```

**(c) Guard — STATS19-only build leaves the flag NULL and warns.**

```python
def test_bank_holiday_flag_null_without_source(tmp_path):
    import warnings
    client = _stats19_client(tmp_path)                          # registry = [Stats19] only
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        client.build(years=YEARS)
    cols = [r[0] for r in client.con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='collisions'").fetchall()]
    assert "is_bank_holiday" in cols                            # column always present
    nonnull = client.con.execute(
        "SELECT count(*) FROM collisions WHERE is_bank_holiday IS NOT NULL").fetchone()[0]
    assert nonnull == 0                                         # no bank_holidays -> all NULL
    assert any("bank_holidays table not found" in str(x.message) for x in w)
    client.close()
```

### 6. Document the column
- **`docs/schema.md`** — in the `### `collisions`` `CREATE TABLE` block, add an `is_bank_holiday`
  line right after `solar_azimuth_deg` (matching the existing inline-comment style):
  ```sql
      is_bank_holiday        BOOLEAN,     -- TRUE if the collision's date is a bank holiday in its nation (from lad_code prefix); FALSE if a known non-holiday in-coverage; NULL if unknown (no/unknown nation, no date, or date outside the gov.uk feed's coverage)
  ```
  If `tests/test_schema_doc.py` checks documented-vs-actual columns, run it and reconcile.
- **Optional (`README.md` / spec §8 example):** if the README shows a `client.build(datasets=[...])`
  example, you may add `"bank_holidays"` to the list to advertise the flag. Do **not** edit
  `docs/spec.md` (product definition, out of scope here).

## Testing & Verification

**Primary (this stage):**
```
pip install -e '.[dev]'
pytest -q tests/test_stats19.py -k "bank_holiday"
```
Expected: the three new tests pass — tri-state + division routing (unit), combined-build stamping,
and the missing-source NULL guard.

**No regressions in STATS19 or the suite:**
```
pytest -q tests/test_stats19.py
pytest -q
```
Expected: green. (`test_schema_doc.py` green after the `docs/schema.md` edit.)

**Stage ship-readiness checklist:**
- [ ] `is_bank_holiday BOOLEAN` declared in collision silver (NULL default).
- [ ] `"bank_holidays"` added to `Stats19Transformer.depends_on`.
- [ ] `_bank_holiday_stamp` added, guarded, called after `_spatial_stamp`/`_solar_stamp`.
- [ ] Unit test proves TRUE / FALSE / NULL, cross-division routing, and Wales→eng-wales.
- [ ] Combined build stamps a determinable value; row count unchanged; STATS19-only build all NULL.
- [ ] `docs/schema.md` updated; `test_schema_doc.py` green.
- [ ] `pytest -q` green.

## End State / Handoff (the contract)
- Every collision carries `is_bank_holiday BOOLEAN` with the tri-state semantics:
  TRUE (holiday in nation) / FALSE (known in-coverage non-holiday) / NULL (unknown — undeterminable
  nation, unparsed date, or date outside the feed's coverage for that nation).
- The flag is division-correct: the same date can be TRUE for an England collision and FALSE/NULL for
  a Scotland collision.
- A STATS19-only build leaves the column NULL and warns; a STATS19 + `bank_holidays` build populates
  it. Row counts and all §9 invariants are unchanged (the stamp adds no audit dimension).
- The feature is complete: standalone `bank_holidays` table (Stage 01) + the per-collision flag.

## Failure Modes & Rollback
- **`bank_holidays` table absent** (source not selected) → guard warns, column stays NULL. Verified by
  test (c).
- **`lad_code` NULL** (collision outside all boundaries, or boundaries not built) → division unknown
  → NULL (safe). Not a regression: `lad_code` is already NULL in stats19-only builds.
- **Date outside feed coverage** → NULL, never FALSE — the key correctness property; test (a) case D.
- **Stamp ordering bug** (called before `_spatial_stamp`) → `lad_code` NULL → everything NULL. Guard:
  the call is placed after `_solar_stamp`, which is itself after `_spatial_stamp`.
- **Rollback:** remove the `_bank_holiday_stamp` method + its call, drop the `is_bank_holiday` silver
  column line, remove `"bank_holidays"` from `depends_on`, delete the three tests, and revert the
  `docs/schema.md` line. Stage 01 (the standalone table) is unaffected.
