# Stage 09 — Labelled Views (all coded columns)
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's **"Enhancement Phase (post-Stage 04)"** section first. This is 09 of the revised plan.
> End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stages 05–08 are done: all three silver tables are full keep-in-place (every coded column cleaned to an
`INTEGER` code, codes kept); `codebook(variable, code, label, is_missing)` loads at the top of
`transform_and_load` and is unique on `(variable, code)`; `column_manifest` classifies every column; the two
severities are audited; `stats19_completeness` reports the rest. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: there are **no** `*_labelled` views; no stored table has any `*_label` column.

## Objective
Add three opt-in labelled **views** — `collisions_labelled`, `vehicles_labelled`, `casualties_labelled` —
that expose a `<col>_label` for **every** coded column of that table by joining the codebook, **alongside**
the canonical coded silver table. Stored tables keep integer codes only. There is no global on/off flag and
no stored label column: the default surface is the coded table; "translation on" means querying the
`*_labelled` view.

## Implementation Steps

### A. Add the labelled-view builder (`src/crossroads/transformers/stats19.py`)

Add `_create_labelled_views` as a new method on `Stats19Transformer`. Note there is **no** dedicated
gold-view method to sit beside — the gold views are three inline `create_clean_view(...)` calls inside
`transform_and_load` (`vehicles_clean`, `casualties_clean`, `collisions_spatial`); place the new method
alongside the other `_derive_*`/helper methods. For each table it reads the manifest's
**coded** columns present in the silver table and builds a view: `SELECT s.*, <one label per coded column>`,
each label from a `LEFT JOIN codebook` **aliased per column**. The join is on the **cleaned** code
(`INTEGER` → `VARCHAR`), so a missing value (cleaned `NULL`) yields a `NULL` label. Because the codebook is
unique on `(variable, code)` and each join filters `variable`, every join is at most 1:1 → the view row count
equals the silver row count (no fan-out, no loss).

```python
    def _create_labelled_views(self, con):
        """Opt-in code->label translation, at full breadth. Labels are NEVER stored: each
        *_labelled view joins the codebook to expose a <col>_label for every coded column
        ALONGSIDE the canonical coded silver table. Default surface = the coded table;
        'translation off' = query the silver table. No global flag, no stored label column.

        One LEFT JOIN per coded column, aliased, ON cb.variable='<col>' AND
        cb.code = CAST(s.<col> AS VARCHAR). codebook is unique on (variable, code) and each
        join filters variable, so joins are 1:1 -> the view row count == silver row count.
        Column/variable identifiers come from the trusted manifest (interpolated); no row
        values are interpolated. Views are lazy: the join cost is paid only when queried.

        After each view is built, REPORT (never halt) any undecoded codes -- a code that
        is present but whose label came back NULL -- in the codebook-COVERED coded columns,
        via warnings.warn. This is the build-time 'report loudly and continue': the build
        always succeeds, but a systematic decode gap (e.g. a future zero-padded code the
        INTEGER->VARCHAR join misses) or a stray junk code is surfaced, not silent. Uncovered
        columns (no codebook rows, e.g. enhanced_severity_collision) are skipped -- their
        NULL labels are expected. Mirrors the non-fatal warnings.warn already used in
        _spatial_stamp for a missing boundary table."""
        for silver, table_kind in ((self.COLLISION_SILVER, "collision"),
                                   (self.VEHICLE_SILVER,   "vehicle"),
                                   (self.CASUALTY_SILVER,  "casualty")):
            silver_cols = self._bronze_columns(con, silver)     # reuse info_schema helper
            coded = [c for (c,) in con.execute(
                f"SELECT col FROM {self.COLUMN_MANIFEST_TABLE} "
                f"WHERE tbl = ? AND kind = 'coded' ORDER BY col", [table_kind]).fetchall()
                if c.lower() in silver_cols]
            selects, joins = ["s.*"], []
            for i, col in enumerate(coded):
                a = f"cb{i}"
                selects.append(f"{a}.label AS {col}_label")
                joins.append(
                    f"LEFT JOIN {self.CODEBOOK_TABLE} {a} "
                    f"  ON {a}.variable = '{col}' AND {a}.code = CAST(s.{col} AS VARCHAR)")
            con.execute(
                f"CREATE OR REPLACE VIEW {silver}_labelled AS "
                f"SELECT {', '.join(selects)} FROM {silver} s {' '.join(joins)}")

            # --- REPORT (non-fatal): warn on undecoded codes in COVERED columns. ---
            # A covered column has >=1 codebook row; only those are expected to decode.
            covered = [c for c in coded if con.execute(
                f"SELECT count(*) FROM {self.CODEBOOK_TABLE} WHERE variable = ?",
                [c]).fetchone()[0] > 0]
            if covered:
                # One scan of the view: an undecoded count per covered column.
                exprs = ", ".join(
                    f"count(*) FILTER (WHERE {c} IS NOT NULL AND {c}_label IS NULL)"
                    for c in covered)
                counts = con.execute(f"SELECT {exprs} FROM {silver}_labelled").fetchone()
                bad = [(c, n) for c, n in zip(covered, counts) if n]
                if bad:
                    warnings.warn(
                        f"stats19: {silver}_labelled has undecoded codes (code present but "
                        f"label NULL) in covered columns: "
                        + ", ".join(f"{c}={n}" for c, n in bad)
                        + ". Those rows show a blank label; check the codebook covers every "
                        f"code in use (e.g. a new or zero-padded code).", stacklevel=2)
```
> **`warnings` is already imported** at the top of `stats19.py` (used by `_spatial_stamp`), so no new
> import is needed. The report is one extra aggregate scan per view, paid once at build time.
> **View names.** `self.COLLISION_SILVER` is `"collisions"`, so the views are `collisions_labelled`,
> `vehicles_labelled`, `casualties_labelled`. A table with no coded columns still yields a valid view.

> **Decode-by-intersect — a coded column with *no* codebook coverage is expected, not an error.** One real
> case exists in the 2024 data: `enhanced_severity_collision` is classified `coded` (Stage 05) but its code
> list is **absent from the guide**, so the codebook has zero rows for it. The `LEFT JOIN` above handles this
> for free — every row's `enhanced_severity_collision_label` comes back `NULL`, the view still builds, and the
> row count is unaffected. **Do not** filter such columns out and **do not** treat their NULL labels as a
> failure. The "no undecoded codes" invariant below therefore applies **only to columns that have codebook
> coverage** (≥1 codebook row) — the integration test checks every *covered* coded column but skips
> uncovered ones like `enhanced_severity_collision`, so it is never a blanket check across *all* coded
> columns. When DfT later publishes the `enhanced_severity_collision` list, adding its rows to the codebook
> lights up its labels automatically — and the same test starts holding that column to the invariant too, no
> view change.

### B. Wire it into `transform_and_load`

Append the call at the **very end** of `transform_and_load`, after the R-Tree index creation (the codebook
and all three silver tables already exist by then):
```python
        # --- LABELS (opt-in, NEVER stored): code->label views alongside the coded tables. ---
        self._create_labelled_views(con)
```
For reference, the actual current order of `transform_and_load` is: load codebook + column manifest →
bronze (×3) → silver derivations (×3) → **completeness writes** → gold clean views (`vehicles_clean`,
`casualties_clean`) → spatial stamp → `collisions_spatial` → R-Tree index. The labelled views go after all
of it. Exact placement is not load-bearing — the views read only the silver tables + codebook, both of
which exist well before the end — so just tack the call on last and don't rely on the precise position of
the earlier steps.

## Testing & Verification
Add to `tests/test_stats19.py`.

**Unit — labels come from the view, never the stored table (synthetic, authoritative):**
```python
def test_labelled_view_decodes_and_never_stores(con):
    t = Stats19Transformer()
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('casualty_severity','1','Fatal',FALSE),('casualty_severity','2','Serious',FALSE),"
        "  ('casualty_severity','-1','Data missing or out of range',TRUE),"
        "  ('sex_of_casualty','1','Male',FALSE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('casualty','casualty_severity','coded','INTEGER'),"
        "  ('casualty','sex_of_casualty','coded','INTEGER'),"
        "  ('casualty','age_of_casualty','numeric','INTEGER')) AS t(tbl,col,kind,dtype)")   # numeric -> not labelled
    con.execute("CREATE TABLE casualties AS SELECT * FROM (VALUES "
        "  ('k1', 2,    1, 34),('k2', NULL, 1, NULL)"
        ") AS t(source_row_key, casualty_severity, sex_of_casualty, age_of_casualty)")
    t._create_labelled_views(con)
    rows = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT source_row_key, casualty_severity_label, sex_of_casualty_label "
        "FROM casualties_labelled").fetchall()}
    assert rows["k1"] == ("Serious", "Male")
    assert rows["k2"][0] is None                 # cleaned-NULL severity -> NULL label
    cols = {r[0] for r in con.execute("DESCRIBE casualties").fetchall()}
    assert not any(c.endswith("_label") for c in cols)       # labels NOT stored
    view_cols = {r[0] for r in con.execute("DESCRIBE casualties_labelled").fetchall()}
    assert "age_of_casualty_label" not in view_cols          # numeric not labelled
    assert con.execute("SELECT count(*) FROM casualties_labelled").fetchone()[0] == 2   # no fan-out
```

**Integration — broad labelled views over the real sample (ship proof):**
```python
def test_labelled_views_end_to_end(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    for silver, view, kind in (("collisions","collisions_labelled","collision"),
                               ("vehicles","vehicles_labelled","vehicle"),
                               ("casualties","casualties_labelled","casualty")):
        silver_cols = {r[0].lower() for r in client.con.execute(f"DESCRIBE {silver}").fetchall()}
        coded = [c for (c,) in client.con.execute(
            "SELECT col FROM column_manifest WHERE tbl=? AND kind='coded'", [kind]).fetchall()
            if c.lower() in silver_cols]
        view_cols = {r[0].lower() for r in client.con.execute(f"DESCRIBE {view}").fetchall()}
        for c in coded:
            assert f"{c}_label" in view_cols, f"{view} missing {c}_label"
        s = client.con.execute(f"SELECT count(*) FROM {silver}").fetchone()[0]
        v = client.con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        assert v == s and s > 0                                         # no fan-out/loss
        assert not any(c.endswith("_label") for c in silver_cols), f"{silver} must not store labels"
    lab = client.con.execute(
        "SELECT DISTINCT casualty_severity_label FROM casualties_labelled "
        "WHERE casualty_severity = 2").fetchone()
    assert lab and lab[0].lower().startswith("serious")
    # Every PRESENT code in a codebook-COVERED column must decode -- across ALL coded columns
    # of ALL three views, not just casualty_severity. This is the regression tripwire for a
    # systematic decode break (e.g. a future zero-padded code '07' that the INTEGER->VARCHAR
    # join would silently miss): if any covered column loses its labels, the suite fails loudly
    # and names the column, instead of quietly serving blanks. Scope to covered columns only
    # (>=1 codebook row) so a legitimately-uncovered column like enhanced_severity_collision
    # (all-NULL labels by design) is NOT treated as a failure.
    for silver, view, kind in (("collisions","collisions_labelled","collision"),
                               ("vehicles","vehicles_labelled","vehicle"),
                               ("casualties","casualties_labelled","casualty")):
        silver_cols = {r[0].lower() for r in client.con.execute(f"DESCRIBE {silver}").fetchall()}
        coded = [c for (c,) in client.con.execute(
            "SELECT col FROM column_manifest WHERE tbl=? AND kind='coded'", [kind]).fetchall()
            if c.lower() in silver_cols]
        for c in coded:
            covered = client.con.execute(
                "SELECT count(*) FROM codebook WHERE variable = ?", [c]).fetchone()[0] > 0
            if not covered:
                continue                 # dictionary has nothing for this column yet -> blanks expected
            undecoded = client.con.execute(
                f"SELECT count(*) FROM {view} "
                f"WHERE {c} IS NOT NULL AND {c}_label IS NULL").fetchone()[0]
            assert undecoded == 0, f"{view}.{c} has {undecoded} undecoded codes"
    client.close()
```

Run:
```bash
source .venv/bin/activate
python -m pytest -q          # expected: all green
```

**Stage ship-readiness checklist:**
- [ ] `collisions_labelled`/`vehicles_labelled`/`casualties_labelled` views exist, each exposing a
      `<col>_label` for **every** coded column present in its silver table.
- [ ] Labels join on the cleaned code (unique-key `LEFT JOIN`); view row count == silver row count (no
      fan-out/loss); a missing (cleaned-NULL) value yields a NULL label.
- [ ] Stored `collisions`/`vehicles`/`casualties` carry **no** `*_label` column; numeric columns not labelled.
- [ ] Every present coded value in a **codebook-covered** column decodes (no undecoded codes); a coded column
      with no codebook coverage (e.g. `enhanced_severity_collision`) yields NULL labels without error.
- [ ] A build that hits undecoded codes in a covered column **warns** (naming the column + count) and
      **still succeeds** — non-fatal `warnings.warn`, never a hard failure.
- [ ] No global translation flag; default surface is the coded table; no `pyproject.toml`/dependency change.
- [ ] `python -m pytest -q` fully green.

## End State / Handoff
STATS19 now offers an opt-in labelled surface across **every** coded column: the three `*_labelled` views
translate codes to DfT labels via the codebook, while the stored tables keep integer codes only. The
descriptive-layer enhancement is complete: silver carries the whole dataset cleaned (codes kept, full missing
set nulled), the two severities are formally audited, `stats19_completeness` reports the rest, and labels are
available on demand without ever being stored. Future work promotes another CORE field (Stage 07 pattern) or
normalizes free-text (`generic_make_model`) — neither changes the stored coded schema, and any new coded
column is labelled automatically once classified in the manifest and covered by the codebook.

## Failure Modes & Rollback
- **View row count ≠ silver (fan-out):** the codebook has a duplicate `(variable, code)` for some coded
  column. Fix the codebook CSV (Stage 05 uniqueness), not the view.
- **Build warns about undecoded codes in a covered column:** expected behaviour, not a failure — the build
  continues and the warning names the column + count. In real data this is usually a stray/junk source code
  (leave it — the row simply shows a blank label) or, if a *whole* column is undecoded, a systematic gap
  (e.g. a new or zero-padded code) — extend the codebook (Stage 05). The integration test's `undecoded == 0`
  invariant catches the systematic case on the clean fixture; the warning is what surfaces it in real builds.
- **`undecoded > 0` on a covered column:** the codebook is missing a code that appears in the sample for that
  variable. Extend the codebook (Stage 05) — coverage of all *guide-listed* coded variables is a ship
  requirement. (Exception: `enhanced_severity_collision` has no guide code list, so all-NULL labels there are
  expected — scope the `undecoded == 0` assertion to a covered column, per the Objective note.)
- **A `*_label` column appears in a stored table:** someone materialised a label into silver — revert; labels
  live only in views. The `DESCRIBE` assertions guard this.
- **View errors at query time ("codebook does not exist"):** views are lazy; keep `_load_codebook` at the top
  of `transform_and_load` so a rebuilt DB always has it.
- **Too many joins slow an unfiltered view scan:** each coded column adds one `LEFT JOIN` (~30 for
  collisions), a cost paid only when the labelled view is scanned unfiltered; researchers usually filter. If a
  specific wide scan is hot, build a narrow labelled projection on demand — do not store labels on silver.
- **Rollback:** remove `_create_labelled_views` + its call, delete the new tests. The suite returns to the
  Stage 08 state; stored tables are unchanged (this stage stored nothing).

## Implementation notes (deviations from the plan as written)
- **`speed_limit` reclassified `coded` → `numeric` (Stage 05 manifest).** The new "every covered coded column
  decodes" test correctly caught that `speed_limit` (classified `coded`) had undecoded values (30/40/60 mph):
  it is a literal quantity, not a labelled code list, and DfT's guide gives it no discrete value labels (only
  its `-1`/`99` sentinels reach the codebook). It fits the manifest's own `NUMERIC_INT` definition exactly
  (like `age_of_driver`/`number_of_vehicles`). Fixed at the root — added `speed_limit` to `NUMERIC_INT` in
  `scripts/build_stats19_column_manifest.py`, regenerated `stats19_columns.csv` (now `coded 59, numeric 17`),
  and updated the breakdown assertion + README. `speed_limit` is now cleaned as a number and excluded from the
  labelled views. (Decided with the user.)
- **Latent script bug fixed in passing.** Regenerating the manifest revealed the script's `GEO` set wrongly
  included `longitude`/`latitude` (they must be `numeric DOUBLE`, as the code + committed CSV + tests already
  assumed); the committed CSV had silently drifted from the script because `--check` isn't run in CI. Corrected
  the script so a fresh derivation reproduces the intended CSV (`--check` now passes) with `speed_limit` as the
  only real change.
- **`_create_labelled_views` skips absent silver tables** (a `_table_exists` guard, mirroring `_spatial_stamp`)
  so the synthetic unit test — which builds only `casualties` — works; a real build has all three tables.
