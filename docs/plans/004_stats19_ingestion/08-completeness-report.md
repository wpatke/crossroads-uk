# Stage 08 — Completeness Report
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's **"Enhancement Phase (post-Stage 04)"** section first. This is 08 of the revised plan.
> End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stages 05–07 are done: all three silver tables are full keep-in-place (coded/numeric cleaned + typed, text
raw); the two severities are formally audited; `codebook` + `column_manifest` load at the top of
`transform_and_load`. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: there is **no** `stats19_completeness` table. The only per-column quality signal today is
the formal `Dimension` on geom/datetime/link/severity — every **other** cleaned column has its missing values
nulled but **no queryable record of how many**.

## Objective
Build one queryable **completeness** table, `stats19_completeness`, with one row per cleaned column (`kind`
coded or numeric) per source: `n_total`, `n_present`, `n_missing`, `missing_rate`. This is the "report
everywhere else" half of the audit strategy — no reject gate (legitimately-sparse columns must not fail a
build), no per-cell ledger (would explode across ~50 columns) — just a queryable answer to "how complete is
column X?" computed with **one aggregate scan per silver table** plus a bounded insert loop. It is a
stats19-owned report table, not an audited source.

> **Expected row counts (from the committed 99-row manifest — `kind IN ('coded','numeric')`):** **collision
> 31, vehicle 26, casualty 17 → 74 total.** The e2e test derives these from `column_manifest` at runtime, so
> it stays correct if the manifest changes; the numbers here are for review sanity.

## Implementation Steps

### A. Add the table name + writer (`src/crossroads/transformers/stats19.py`)

Near the table-name constants:
```python
    COMPLETENESS_TABLE = "stats19_completeness"
```
Add the creator + per-source writer next to the other helpers. A cleaned coded/numeric column is `NULL`
exactly for its missing/unparseable values, so `count(col)` (which ignores `NULL`) is the present count and
`count(*) - count(col)` is the missing count — one scan gives every column's counts at once:
```python
    def _ensure_completeness_table(self, con):
        """Create the completeness report table if absent (idempotent). stats19-owned
        report table, NOT an audited source. One row per cleaned column per source.
        missing_rate is n_missing/n_total (0..1)."""
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {self.COMPLETENESS_TABLE} ("
            f"  source_id VARCHAR, column_name VARCHAR, kind VARCHAR, "
            f"  n_total BIGINT, n_present BIGINT, n_missing BIGINT, missing_rate DOUBLE)")

    def _write_completeness(self, con, silver_table, source_id, table_kind):
        """Write one completeness row per cleaned column (kind coded|numeric) of one file.

        Reads column_manifest for the cleaned columns present in the silver table, runs a
        SINGLE aggregate scan (count(*) + count(col) per column), then a bounded Python
        loop inserts ~one row per column. Idempotent per source: existing rows for this
        source_id are cleared first. Column identifiers come from the trusted manifest
        (interpolated); counts/values are bound with ?.
        """
        silver_cols = self._bronze_columns(con, silver_table)   # reuse the info_schema helper
        cols = [(c, k) for c, k in con.execute(
            f"SELECT col, kind FROM {self.COLUMN_MANIFEST_TABLE} "
            f"WHERE tbl = ? AND kind IN ('coded','numeric') ORDER BY col", [table_kind]).fetchall()
            if c.lower() in silver_cols]
        con.execute(f"DELETE FROM {self.COMPLETENESS_TABLE} WHERE source_id = ?", [source_id])
        if not cols:
            return
        selects = ["count(*) AS n_total"] + [f"count({c}) AS present_{i}"
                                             for i, (c, _) in enumerate(cols)]
        agg = con.execute(f"SELECT {', '.join(selects)} FROM {silver_table}").fetchone()
        n_total = agg[0]
        for i, (col, kind) in enumerate(cols):
            n_present = agg[i + 1]
            n_missing = n_total - n_present
            rate = (n_missing / n_total) if n_total else 0.0
            con.execute(
                f"INSERT INTO {self.COMPLETENESS_TABLE} "
                f"(source_id, column_name, kind, n_total, n_present, n_missing, missing_rate) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                [source_id, col, kind, n_total, n_present, n_missing, rate])
```
> `missing_rate` is the **missing** fraction — the number a researcher checks before using a column. The CORE
> severities (also `coded`) appear here too: they get both the formal dimension AND a completeness row, which
> is fine and consistent.

### B. Wire it into `transform_and_load`

After the three silver derivations (it only reads the silver tables), add:
```python
        # --- COMPLETENESS: one queryable row per cleaned column per source. Rigour lives in
        # the formal dimensions; this is the broad "how complete is column X?" report. ---
        self._ensure_completeness_table(con)
        for silver, sid, kind in (
            (self.COLLISION_SILVER, self.COLLISION_SID, "collision"),
            (self.VEHICLE_SILVER,   self.VEHICLE_SID,   "vehicle"),
            (self.CASUALTY_SILVER,  self.CASUALTY_SID,  "casualty"),
        ):
            self._write_completeness(con, silver, sid, kind)
```
No `quality_spec()`/`client.py`/`registry.py` change — `stats19_completeness` is a stats19-owned report
table, not an audited source, and does not participate in the invariants.

## Testing & Verification
Add to `tests/test_stats19.py`.

**Unit — counts are correct (synthetic, authoritative):**
```python
def test_completeness_counts(con):
    t = Stats19Transformer()
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('casualty','casualty_severity','coded','INTEGER'),"
        "  ('casualty','age_of_casualty','numeric','INTEGER'),"
        "  ('casualty','lsoa_of_casualty','text','')) AS t(tbl,col,kind,dtype)")   # text -> NOT reported
    con.execute("CREATE TABLE casualties AS SELECT * FROM (VALUES "
        "  (2,    34,   'E01'),(3, NULL, 'E02'),(NULL, NULL, 'E03')"
        ") AS t(casualty_severity, age_of_casualty, lsoa_of_casualty)")
    t._ensure_completeness_table(con)
    t._write_completeness(con, "casualties", "stats19_casualty", "casualty")
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT column_name, kind, n_total, n_present, n_missing, missing_rate "
        "FROM stats19_completeness WHERE source_id='stats19_casualty' ORDER BY column_name").fetchall()}
    assert rows["casualty_severity"] == ("coded", 3, 2, 1, 1/3)
    assert rows["age_of_casualty"]   == ("numeric", 3, 1, 2, 2/3)
    assert "lsoa_of_casualty" not in rows        # text columns are not reported


def test_completeness_idempotent(con):
    t = Stats19Transformer()
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('casualty','casualty_severity','coded','INTEGER')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE casualties AS SELECT * FROM (VALUES (2)) AS t(casualty_severity)")
    t._ensure_completeness_table(con)
    t._write_completeness(con, "casualties", "stats19_casualty", "casualty")
    t._write_completeness(con, "casualties", "stats19_casualty", "casualty")
    assert con.execute("SELECT count(*) FROM stats19_completeness "
                       "WHERE source_id='stats19_casualty'").fetchone()[0] == 1
```

**Integration — populated report over the real sample:**
```python
def test_completeness_report_end_to_end(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    for sid, kind in (("stats19_collision","collision"),("stats19_vehicle","vehicle"),
                      ("stats19_casualty","casualty")):
        expected = client.con.execute(
            "SELECT count(*) FROM column_manifest WHERE tbl=? AND kind IN ('coded','numeric')",
            [kind]).fetchone()[0]
        got = client.con.execute(
            "SELECT count(*) FROM stats19_completeness WHERE source_id=?", [sid]).fetchone()[0]
        assert got == expected and got > 0, f"{sid}: {got} rows, expected {expected}"
    assert client.con.execute(
        "SELECT count(*) FROM stats19_completeness WHERE missing_rate < 0 OR missing_rate > 1").fetchone()[0] == 0
    sev = client.con.execute(
        "SELECT missing_rate FROM stats19_completeness "
        "WHERE source_id='stats19_casualty' AND column_name='casualty_severity'").fetchone()[0]
    assert sev == 0.0        # mandatory field, clean sample
    client.close()
```

Run:
```bash
source .venv/bin/activate
python -m pytest -q          # expected: all green
```

**Stage ship-readiness checklist:**
- [ ] `stats19_completeness(source_id, column_name, kind, n_total, n_present, n_missing, missing_rate)`
      created; one row per cleaned coded/numeric column per source; `text` columns not reported.
- [ ] Counts correct (`n_present = count(col)`, `n_missing = n_total - n_present`); `missing_rate` in [0,1];
      one aggregate scan per silver table + a bounded insert loop.
- [ ] Idempotent per source; stats19-owned report table, **not** an audited source; no reject gate; no
      per-cell ledger.
- [ ] `python -m pytest -q` fully green; `pyproject.toml` untouched.

## End State / Handoff
A build produces `stats19_completeness` — a queryable, citable per-column missingness report across all three
sources, computed cheaply. Combined with the formal dimensions on geom/datetime/link/severity, the audit
strategy is complete: rigour where load-bearing, a report everywhere else. Stage 09 adds the opt-in labelled
views.

## Failure Modes & Rollback
- **Row count ≠ expected in the e2e:** a manifest coded/numeric column is absent from silver (a keep-in-place
  gap from Stage 06) or the manifest lists a column the fixture lacks. `_write_completeness` intersects with
  the silver columns, so the mismatch points at a real coverage gap — fix the silver derivation or the manifest.
- **Suspiciously low missingness for a known-sparse column:** the manifest mis-classified it (e.g. `text`
  instead of `coded`) so it was not cleaned — fix the manifest (Stage 05).
- **Doubled rows after rebuild:** the per-source `DELETE` before insert was skipped — keep it.
- **Rollback:** remove `_ensure_completeness_table`/`_write_completeness` + their calls + the constant, delete
  the new tests. The suite returns to the Stage 07 state.
