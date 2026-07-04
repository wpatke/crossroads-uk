# Stage 05 — Reference Data (Codebook + Column Manifest)
> Part of Stats19 Collision Ingestion & Normalization. You have the overview + this stage; implement ONLY this stage.
> Read the overview's **"Enhancement Phase (post-Stage 04)"** section first — this stage is 05 of the revised
> descriptive-layer plan (05–09), which supersedes the original Stage Map rows 05–07.
> End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stages 01–04 are **implemented and committed** (read-only). `src/crossroads/transformers/stats19.py` has a
working `Stats19Transformer`:
- three bronze tables (`stats19_{collision,vehicle,casualty}_raw`, faithful all-string copies);
- three silver tables — currently the **structural** layer only:
  - `collisions` (~16 cols): `source_row_key`, `accident_index`, `accident_year`, `accident_reference`,
    `easting_raw`, `northing_raw`, `easting`, `northing`, `geom`, `geom_valid`, `date_raw`, `time_raw`,
    `datetime_local`, `datetime_valid`, `lad_code`, `ctyua_code`;
  - `vehicles` (~4 cols): `source_row_key`, `accident_index`, `vehicle_reference`, `link_valid`;
  - `casualties` (~5 cols): `source_row_key`, `accident_index`, `vehicle_reference`, `casualty_reference`,
    `link_valid`;
- gold views `collisions_spatial`, `vehicles_clean`, `casualties_clean`; the spatial stamp; the R-Tree.

Verify green before starting:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Observable state: there is **no** `src/crossroads/reference/` directory, **no** `codebook` table, **no**
`column_manifest` table. This stage adds shared reference data only — it does **not** change any silver
derivation (that is Stage 06).

## Objective
Introduce the two committed reference files that will drive the descriptive-layer clean, and load them into
the build database:
- `src/crossroads/reference/stats19_codebook.csv` — `variable, code, label, is_missing` — the missing-set +
  labels for **every** integer-coded variable across the three files. **Verified size: ~1130 rows across
  ~71 variables** (from the 2024 guide; see the derivation script below).
- `src/crossroads/reference/stats19_columns.csv` — `table, column, kind, dtype` — a classification of
  **every** column of every file. **Verified size: exactly 99 rows** (44 collision + 32 vehicle + 23 casualty
  headers) — the complete, ready-to-commit table is embedded in **Appendix A** below.

Both are derived independently from DfT's published guide, ship inside the package, and load into the
`codebook` and `column_manifest` reference tables at the **top** of `transform_and_load`. Neither is an
audited source (no `source_id`, no bronze/silver pair, no conservation obligation) — they are static lookups,
like `transformers/ons_boundaries.json`. Nothing consumes them yet; Stages 06–09 are the consumers.

> **Research status (resolved 2026-07-04).** The DfT guide has been located, downloaded, and analysed; the
> derivation script below was **run end-to-end** and its outputs verified. This stage is no longer gated on
> external research — the URL, vintage, licence, the exact missing-marker vocabulary, the severity code lists,
> and the full 99-row column classification are all pinned below. The executor's job is to *reproduce* the
> committed CSVs from the pinned recipe, not to re-derive anything from scratch.

## Implementation Steps

### A. Produce & commit the two reference CSVs (do this first — everything downstream depends on them)

**Source of truth (pinned).** The guide is the **Road Safety Open Dataset Data Guide**, published by the
Department for Transport under the **Open Government Licence v3.0** — the same publisher/licence as the
collision CSVs.
- **Download URL (stable, verified HTTP 200):**
  `https://data.dft.gov.uk/road-accidents-safety-data/dft-road-casualty-statistics-road-safety-open-dataset-data-guide-2024.xlsx`
  (identical bytes are mirrored at `assets.publishing.service.gov.uk/media/691c6440e39a085bda43eed6/…-data-guide-2024.xlsx`).
- **Vintage:** the **2024 edition**, published **25 September 2025**; covers 1979–present. Record this exact
  vintage in the README; the guide is re-issued annually (~late September) so a future refresh is a deliberate,
  reviewed step, not an automatic fetch.
- **Format (verified):** a single-sheet `.xlsx` (~72 KB, sheet name `2024_code_list`, 1820 rows) with columns
  **`table`, `field name`, `code/format`, `label`, `note`**. `table` is `collision`/`vehicle`/`casualty`
  (lower-case); one row per `(field name, code)`. This is clean and **fully scriptable** — the codebook
  derivation below reads it directly. Reading `.xlsx` needs DuckDB's `excel` extension (`INSTALL excel; LOAD
  excel;`), but **only for this one-time offline authoring step** — the shipped transformer reads the committed
  **CSV** via `read_csv`, so there is **no runtime dependency** on the `excel` extension.

> **`code/format` is not purely a code list — do not trust it blindly for `kind`.** The column mixes three
> things: real integer code lists (`collision_severity` 1/2/3), format strings (`date` → `(DD/MM/YYYY)`,
> `first_road_number` → `1 to 9999`), and the `-1` missing sentinel that also sits on numeric fields
> (`age_of_casualty`, `engine_capacity_cc`). The codebook script therefore keeps **only integer-castable
> codes** (`TRY_CAST(code AS INTEGER) IS NOT NULL`), and the `kind` classification in `stats19_columns.csv` is
> authored by judgement (Appendix A), **not** by "does it appear with a code".

> **Licence guard.** The reference `../stats19/` package (GPL-3) ships a similar code-list table
> (`data-raw/stats19_schema.csv`). It is **licence-incompatible** with this MIT project — do **not** copy or
> adapt it. Derive independently from the DfT guide above. `../stats19/` may be read only to understand field
> meanings and to *inform judgement calls* (see the `is_missing` decision below, which follows stats19's
> documented `missing_labels` behaviour), never as a data source.

**A1. `stats19_codebook.csv`** — header `variable,code,label,is_missing`:

| column | type | meaning |
|--------|------|---------|
| `variable` | text | STATS19 column name the codes belong to, lower-case, matching the CSV headers |
| `code` | text | the raw code exactly as in the data (`2`, `-1`, `07`) |
| `label` | text | the DfT English label (`Serious`) |
| `is_missing` | `true`/`false` | whether this code is a missing/unknown sentinel |

Set `is_missing = true` where the code is `-1` **or** the label (trimmed, case-insensitive) matches the
missing-marker vocabulary below. This vocabulary is **not a guess** — these are exactly the "not recorded"
labels that occur in the 2024 guide, and the set matches the missing-value handling of the widely-used
stats19 package (consulted as prior art only, never copied):
```
MISSING_LABELS = {                       # lower-cased; matched by exact equality on trim(label)
  "data missing or out of range",        # 56 variables — the dominant sentinel
  "unknown",                             # 5 variables (the BARE 'unknown')
  "undefined",                           # 1 variable
  "not known",                           # 3 variables
  "code deprecated",                     # 1 variable (sits on code -1)
}
# PLUS the numeric sentinel rule: code == '-1' is ALWAYS missing, regardless of its label
# (this also catches the two '-1' markers 'Record predates use of local_authority_* codes').
```
> **Deliberately NOT in the missing set (each verified against the 2024 guide + stats19 behaviour):**
> - **`"unknown (self reported)"` (codes `9`/`99`, 27 variables) → is_missing = FALSE.** stats19 pointedly
>   omits this label from its `missing_labels`, keeping 9/99 as real coded values; we match that (decision
>   confirmed with the user, informed by `../stats19/`). The distinction is meaningful: `-1`/"Data missing" =
>   *not recorded*; `9`/`99` "unknown (self reported)" = *recorded as unknown*. Nulling these would silently
>   drop 27 columns' worth of a legitimate category. Raw values remain in bronze regardless.
> - **`"none"` (always code `0`, 6 variables: `carriageway_hazards`, `skidding_and_overturning`, …) → FALSE.**
>   "None" is a real category ("no hazard"), not a missing marker.
> - **`"unclassified"` (code `6`, `first_road_class`/`second_road_class` only) → FALSE.** In this guide
>   `Unclassified` is a **real road class** and appears on *no other* variable, so — unlike the original plan
>   feared — no per-variable split is needed: it is simply not-missing everywhere it occurs.
> - **`"other"`** wherever it appears → a real category, **not** missing.
> - **`"data cannot be anonymised"`** — the original plan guessed this; it does **not** appear in the 2024
>   guide. Omit it (a future vintage may reintroduce it — the README records that this set is vintage-pinned).

**Coverage is a hard ship requirement.** The codebook must cover **all** integer-coded variables across
collision/vehicle/casualty completely. `collision_severity` and `casualty_severity` are trivially exhaustive
in the guide — **each is exactly `1=Fatal, 2=Serious, 3=Slight`, with no `-1` or other sentinel in its code
list** (so both are always `is_missing = false` and Stage 07's severity `*_valid` is driven by parseability,
not by a missing code). Unique on `(variable, code)` (a dup fans out the Stage 09 label join).

> **Verified codebook shape (from the pinned script):** **~1130 rows, ~71 variables, 0 duplicate
> `(variable, code)`, ~68 rows flagged `is_missing` (of which ~6 are non-`-1` label matches).** Treat these as
> the expected magnitudes when reviewing the committed CSV — a large deviation means the guide vintage or the
> filter changed.

**A2. `stats19_columns.csv`** — header `table,column,kind,dtype`:

| column | type | meaning |
|--------|------|---------|
| `table` | text | `collision` \| `vehicle` \| `casualty` |
| `column` | text | exact modern CSV header name, lower-case |
| `kind` | text | `identity` \| `geo` \| `datetime` \| `coded` \| `numeric` \| `text` |
| `dtype` | text | target type for `numeric` (`INTEGER`/`DOUBLE`) and `coded` (`INTEGER`); blank otherwise |

Classify **every** column of all three files (complete coverage is a ship requirement — a test checks it).
**The complete, verified 99-row classification is embedded in Appendix A — commit it verbatim.** The rules
below document *how* it was derived (and how to extend it for a future column); you should not need to
re-derive it. Verified breakdown: **identity 12, geo 4, datetime 2, coded 60, numeric 14, text 7.** Rules:
- `identity`: `accident_index`, `accident_year`, `accident_reference`, `vehicle_reference`,
  `casualty_reference` (the code handles the `collision_*` legacy names via `_coalesce_present`).
- `geo`: `location_easting_osgr`, `location_northing_osgr`, `longitude`, `latitude`.
- `datetime`: `date`, `time`. (`day_of_week` is `coded`.)
- `coded`: any column whose codes are a real **integer** DfT code list (`collision_severity`, `police_force`,
  `road_type`, `first_road_class`, `junction_detail`, `light_conditions`, `weather_conditions`,
  `road_surface_conditions`, `urban_or_rural_area`, `speed_limit`, `casualty_class`, `casualty_type`,
  `casualty_severity`, `sex_of_casualty`, `vehicle_type`, `vehicle_manoeuvre`, `age_band_of_*`, `day_of_week`,
  `local_authority_district` (integer DfT codes), the `*_historic` variants, and the newer
  `enhanced_casualty_severity`, `collision_injury_based`/`casualty_injury_based`, `escooter_flag`,
  `*_distance_banding`). `dtype = INTEGER`.
  > **Edge case — `enhanced_severity_collision`.** It is a genuine integer category but its code list is
  > **absent from the 2024 guide** (only `enhanced_casualty_severity` is published). Classify it `coded`
  > (it cleans/types correctly), but note it will have **no codebook rows**, so its Stage 09 label column is
  > NULL until DfT publishes the list. This is why Stage 09 decodes by *intersect* (a coded column with no
  > codebook coverage still yields a valid view — labels just come back NULL — and must not trip the
  > "no undecoded codes" check). Record this caveat in the README.
- `numeric`: continuous quantities with a `-1` sentinel and no code list (`age_of_casualty`, `age_of_driver`,
  `engine_capacity_cc`, `age_of_vehicle`, `number_of_vehicles`, `number_of_casualties`, `first_road_number`,
  `second_road_number`, the `*_imd_decile` fields; `longitude`/`latitude` are `geo`). `dtype = INTEGER`, **or
  `DOUBLE` for the four probabilistic `*_adjusted_severity_serious`/`*_adjusted_severity_slight` weights**
  (stats19 types these `numeric`; decision confirmed with the user).
- `text`: free-text / opaque / **string-coded** with no integer code list (`generic_make_model`,
  `lsoa_of_accident_location`, `lsoa_of_driver`, `lsoa_of_casualty`, and the **ONS-string-coded** LA fields
  `local_authority_ons_district`, `local_authority_highway`, `local_authority_highway_current` — these carry
  ONS codes like `E06000036`, not integers, so the integer codebook cannot decode them). Carried **raw**, not
  missing-cleaned (stats19 types every `local_authority_*` field `character`; decision confirmed with the
  user). ONS-code→name decoding is deferred (overview Open Questions).

> Inspect the real fixture headers so the classification matches reality:
> ```bash
> for t in collision vehicle casualty; do echo "== $t =="; head -1 tests/fixtures/stats19/dft-road-casualty-statistics-$t-2023.csv | tr ',' '\n'; done
> ```
> When a column is genuinely ambiguous (e.g. a newer `*_injury_based`/`enhanced_*` field), pick the kind the
> guide's description implies and record the call. Tagging a handful of edge columns `text` (carried raw) is
> safe; mis-typing free-text as numeric is not.

**A3. Reproducible derivation — committed as runnable scripts (auditability requirement).** Both CSVs are
regenerated by committed developer tools under `scripts/` (not shipped in the wheel, never run at build/test
time), each with a `--check` mode that regenerates and fails on any drift from the committed file:
- `scripts/build_stats19_codebook.py` — downloads the pinned DfT guide, derives `stats19_codebook.csv`.
  `--check` proves the committed CSV byte-matches a fresh derivation from the live guide.
- `scripts/build_stats19_column_manifest.py` — applies the documented classification rule sets to the fixture
  headers to derive `stats19_columns.csv`. The rule sets in that script are the manifest's audit trail.

The codebook logic (embedded in that script) is below for reference. Run it offline once to regenerate the
committed codebook CSV: it downloads the pinned guide, reads the single sheet, keeps only integer-castable
codes, and flags `is_missing` with the pinned vocabulary. Column names in the sheet are literally
`field name`, `code/format`, `label` (with spaces/slash — quote them):
```bash
source .venv/bin/activate
mkdir -p src/crossroads/reference
GUIDE_URL="https://data.dft.gov.uk/road-accidents-safety-data/dft-road-casualty-statistics-road-safety-open-dataset-data-guide-2024.xlsx"
curl -sSL -A "crossroads-build" -o /tmp/stats19-guide-2024.xlsx "$GUIDE_URL"
python - <<'PY'
import duckdb
c = duckdb.connect()
c.execute("INSTALL excel; LOAD excel;")          # xlsx read: authoring-only, NOT a runtime dep
GUIDE = "/tmp/stats19-guide-2024.xlsx"
# Pinned missing-marker set: the "not recorded" labels present in the 2024 guide (lower-cased).
MISSING = {"data missing or out of range","unknown","undefined","not known","code deprecated"}
c.execute(f"""CREATE TABLE g AS
  SELECT lower(trim("field name")) AS variable, trim("code/format") AS code, trim("label") AS label
  FROM read_xlsx('{GUIDE}', sheet='2024_code_list', all_varchar=true, header=true)
  WHERE "field name" IS NOT NULL AND "code/format" IS NOT NULL AND "label" IS NOT NULL""")
miss = " OR ".join(f"lower(trim(label)) = '{m}'" for m in MISSING)
c.execute(f"""COPY (
  SELECT DISTINCT variable, code, label, (code = '-1' OR {miss}) AS is_missing
  FROM g
  WHERE TRY_CAST(code AS INTEGER) IS NOT NULL          -- integer codes only: drop ONS codes, formats, ranges
  ORDER BY variable, TRY_CAST(code AS INTEGER))
  TO 'src/crossroads/reference/stats19_codebook.csv' (HEADER, DELIMITER ',')""")
# Sanity-check the written CSV against the expected magnitudes.
con = duckdb.connect()
con.execute("CREATE TABLE cb AS SELECT * FROM read_csv('src/crossroads/reference/stats19_codebook.csv', header=true)")
g = lambda q: con.execute(q).fetchone()[0]
print("rows:", g("SELECT count(*) FROM cb"), "vars:", g("SELECT count(DISTINCT variable) FROM cb"),
      "dups:", g("SELECT count(*)-count(DISTINCT(variable||'|'||code)) FROM cb"),
      "missing:", g("SELECT count(*) FROM cb WHERE is_missing"),
      "non-(-1) missing:", g("SELECT count(*) FROM cb WHERE is_missing AND code<>'-1'"))
PY
```
Expected sanity output: `rows: ~1130 vars: ~71 dups: 0 missing: ~68 non-(-1) missing: ~6`.

`stats19_columns.csv` is **not** derived from the guide (the guide can't cleanly separate coded from
numeric/text — see the note above). It is generated by `scripts/build_stats19_column_manifest.py`, which
applies the Rules above (as explicit set constants) to the fixture headers; the committed file is reproduced
verbatim in Appendix A. If a future-year fixture adds a column, update the rule sets in that script and re-run
it (then `--check` in CI keeps the committed CSV honest).

**A4. `src/crossroads/reference/README.md`** records, for **both** files: publisher (DfT); dataset (Road
Safety Open Dataset); the exact guide URL + vintage (**2024 edition, published 25 September 2025**); licence
(**OGL v3.0**); the `MISSING_LABELS` vocabulary (the five labels above, from the 2024
guide; consistent with the stats19 package's prior art) + the numeric `-1` rule; every judgement call (self-reported `9`/`99` kept, `None`/`Other`/
`Unclassified` kept, `data cannot be anonymised` absent-in-vintage); the `enhanced_severity_collision`
no-codebook-coverage caveat; the `kind`/`dtype` rationale (esp. the four `*_adjusted_severity_*` → `DOUBLE`
and the ONS-string LA fields → `text`); the derivation recipe (script above, run **2026-07-04**); and each
file's committed row count (**codebook ≈1130, columns = 99**). Mirrors `tests/fixtures/**/README.md`.
> **Packaging (no `pyproject.toml` change).** `[tool.hatch.build.targets.wheel] packages =
> ["src/crossroads"]` ships all files under the package tree (proven by `transformers/ons_boundaries.json`).
> If a wheel build ever omits the CSVs, co-locate them next to `stats19.py` and note the deviation — do
> **not** add a dependency.

### B. Add the reference constants + loaders (`src/crossroads/transformers/stats19.py`)

Near the module constants (beside `DFT_BASE_URL`):
```python
# Reference data (shared lookups), derived independently from DfT's published data guide (OGL v3.0)
# and committed under src/crossroads/reference/. They ship in the wheel like transformers/
# ons_boundaries.json and load once per build. NEITHER is an audited source — static lookups.
_REFERENCE_DIR = os.path.join(os.path.dirname(__file__), "..", "reference")
_CODEBOOK_PATH = os.path.join(_REFERENCE_DIR, "stats19_codebook.csv")
_COLUMN_MANIFEST_PATH = os.path.join(_REFERENCE_DIR, "stats19_columns.csv")
```
In the `--- table names ---` block:
```python
    CODEBOOK_TABLE = "codebook"
    COLUMN_MANIFEST_TABLE = "column_manifest"
```
Add the two loaders next to `_load_bronze`:
```python
    def _load_codebook(self, con):
        """Load the committed codebook CSV into the `codebook` reference table.

        codebook(variable, code, label, is_missing) maps STATS19 integer codes to DfT
        labels and marks missing/unknown sentinels. Reference data, NOT an audited
        source. Read all-string then cast so `code` keeps '-1'/'07' exactly and
        is_missing is a real BOOLEAN. CREATE OR REPLACE keeps a same-file rebuild
        idempotent. Path is code-controlled (trusted); no row values are interpolated.
        """
        if not os.path.exists(_CODEBOOK_PATH):
            raise FileNotFoundError(
                f"[stats19] codebook reference file missing: {_CODEBOOK_PATH}. "
                f"It ships in the package under src/crossroads/reference/.")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CODEBOOK_TABLE} AS "
            f"SELECT CAST(variable AS VARCHAR) AS variable, "
            f"       CAST(code AS VARCHAR)     AS code, "
            f"       CAST(label AS VARCHAR)    AS label, "
            f"       CAST(is_missing AS BOOLEAN) AS is_missing "
            f"FROM read_csv('{_CODEBOOK_PATH}', header=true, all_varchar=true)")

    def _load_column_manifest(self, con):
        """Load the committed column manifest into the `column_manifest` reference table.

        column_manifest(tbl, col, kind, dtype) classifies EVERY column of every file:
        kind in {identity, geo, datetime, coded, numeric, text}; dtype is the target
        type for numeric/coded. Single source of truth for how the keep-in-place silver
        (Stage 06) treats each column. Reference data, NOT an audited source. The CSV
        headers are `table,column,...`; alias them to tbl/col (both reserved-ish).
        """
        if not os.path.exists(_COLUMN_MANIFEST_PATH):
            raise FileNotFoundError(
                f"[stats19] column manifest missing: {_COLUMN_MANIFEST_PATH}.")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.COLUMN_MANIFEST_TABLE} AS "
            f'SELECT CAST("table" AS VARCHAR)  AS tbl, '
            f'       CAST("column" AS VARCHAR) AS col, '
            f"       CAST(kind AS VARCHAR)     AS kind, "
            f"       CAST(dtype AS VARCHAR)    AS dtype "
            f"FROM read_csv('{_COLUMN_MANIFEST_PATH}', header=true, all_varchar=true)")
```
Call both at the **top** of `transform_and_load`, right after the `years` guard and **before** the bronze
loop (so both exist before any Stage-06 consumer):
```python
        years = getattr(self, "_years", None) or []
        if not years:
            return   # defensive: is_active gates on years

        # --- REFERENCE: load the codebook + column manifest before any silver clean. ---
        self._load_codebook(con)
        self._load_column_manifest(con)

        # --- BRONZE (×3): faithful copies; record rows read for conservation. ---
        ...   # (unchanged)
```
No `quality_spec()`/`client.py`/`registry.py` change — neither table is an audited source.

## Testing & Verification
Add to `tests/test_stats19.py`.

**Unit — reference tables load & decode (authoritative):**
```python
def test_reference_tables_load(con):
    t = Stats19Transformer()
    t._load_codebook(con); t._load_column_manifest(con)

    lab = con.execute("SELECT label FROM codebook WHERE variable='casualty_severity' AND code='2'").fetchone()
    assert lab and lab[0].lower().startswith("serious")
    # A -1 sentinel is flagged missing. NB: use a variable that HAS a -1 code — the severities are
    # 1/2/3 only (no -1), so 'casualty_severity AND code=-1' returns no row and would crash on None.
    assert con.execute("SELECT is_missing FROM codebook WHERE variable='age_band_of_casualty' AND code='-1'"
                       ).fetchone()[0] is True
    # The FULL missing set is flagged, not just -1 (2024 guide: ~6 non-(-1) missing rows).
    assert con.execute("SELECT count(*) FROM codebook WHERE is_missing AND code <> '-1'").fetchone()[0] >= 1
    # Self-reported '9'/'99' unknowns are KEPT (is_missing = FALSE), matching stats19's behaviour.
    speed99 = con.execute("SELECT is_missing FROM codebook WHERE variable='speed_limit' AND code='99'").fetchone()
    if speed99:                                      # present in the 2024 guide
        assert speed99[0] is False
    # Both audited severities are covered and exactly 1/2/3 (Fatal/Serious/Slight) — no sentinel in the list.
    for v in ("collision_severity", "casualty_severity"):
        codes = {r[0] for r in con.execute("SELECT code FROM codebook WHERE variable=?", [v]).fetchall()}
        assert {"1", "2", "3"} <= codes
        assert con.execute("SELECT count(*) FROM codebook WHERE variable=? AND is_missing", [v]).fetchone()[0] == 0
    # Unique on (variable, code); is_missing is a real BOOLEAN.
    assert con.execute("SELECT count(*)-count(DISTINCT (variable||'\\x1f'||code)) FROM codebook").fetchone()[0] == 0
    assert {r[0]: r[1] for r in con.execute("DESCRIBE codebook").fetchall()}["is_missing"] == "BOOLEAN"


def test_column_manifest_covers_every_fixture_column(con):
    import os
    t = Stats19Transformer(); t._load_column_manifest(con)
    FIX = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
    for table_kind in ("collision", "vehicle", "casualty"):
        with open(os.path.join(FIX, f"dft-road-casualty-statistics-{table_kind}-2023.csv")) as f:
            header = {h.strip().lower() for h in f.readline().strip().split(",")}
        classified = {r[0].lower() for r in con.execute(
            "SELECT col FROM column_manifest WHERE tbl = ?", [table_kind]).fetchall()}
        missing = header - classified
        assert not missing, f"{table_kind}: unclassified columns {missing}"
    kinds = {r[0] for r in con.execute("SELECT DISTINCT kind FROM column_manifest").fetchall()}
    assert kinds <= {"identity","geo","datetime","coded","numeric","text"}, f"bad kinds {kinds}"
    # Verified size + breakdown for the committed (2024) manifest — Appendix A.
    assert con.execute("SELECT count(*) FROM column_manifest").fetchone()[0] == 99
    by_kind = {r[0]: r[1] for r in con.execute(
        "SELECT kind, count(*) FROM column_manifest GROUP BY kind").fetchall()}
    assert by_kind == {"identity":12, "geo":4, "datetime":2, "coded":60, "numeric":14, "text":7}
    # coded/numeric carry a dtype; identity/geo/datetime/text do not.
    assert con.execute("SELECT count(*) FROM column_manifest "
                       "WHERE kind IN ('coded','numeric') AND (dtype IS NULL OR dtype='')").fetchone()[0] == 0
    # The four probabilistic severity-adjustment weights are DOUBLE numerics.
    # NB: the manifest table renames CSV `column` -> `col` (and `table` -> `tbl`), so filter on `col`.
    assert con.execute("SELECT count(*) FROM column_manifest "
                       "WHERE col LIKE '%adjusted_severity%' AND kind='numeric' AND dtype='DOUBLE'"
                       ).fetchone()[0] == 4
```

**Integration — reference tables exist after a real build:**
```python
def test_build_creates_reference_tables(tmp_path):
    client = _stats19_client(tmp_path)     # stats19-only registry helper from Stage 01
    client.build(years=YEARS)
    assert client.con.execute("SELECT count(*) FROM codebook").fetchone()[0] > 0
    assert client.con.execute("SELECT count(*) FROM column_manifest").fetchone()[0] > 0
    client.close()
```

Run:
```bash
source .venv/bin/activate
python -m pytest -q          # expected: all green (incl. all Stage 01–04 tests)
```

**Stage ship-readiness checklist:**
- [ ] `stats19_codebook.csv` committed — all coded variables, unique on `(variable, code)`, both severities
      exhaustive, non-`-1` missing markers flagged.
- [ ] `stats19_columns.csv` committed — every column of all three files classified into a valid `kind`;
      numerics/coded carry a `dtype`.
- [ ] `src/crossroads/reference/README.md` records publisher/URL/vintage/licence, MISSING vocabulary,
      judgement calls, classification rationale, derivation recipe, row counts.
- [ ] Derived from DfT's guide — **not** copied from the GPL package.
- [ ] `_load_codebook` + `_load_column_manifest` called at the **top** of `transform_and_load`; `is_missing`
      is a real `BOOLEAN`; both `CREATE OR REPLACE` (idempotent).
- [ ] No `quality_spec()`/`client.py`/`registry.py`/`pyproject.toml` change; no new dependency; Stages 01–04
      untouched.
- [ ] `python -m pytest -q` fully green.

## End State / Handoff
A build creates the `codebook` + `column_manifest` reference tables at the top of `transform_and_load`, before
any silver derivation. Nothing consumes them yet. Stage 06 uses `column_manifest` (which columns are
coded/numeric/text) + `codebook` (`is_missing`) to widen the three silver tables to full keep-in-place with
the descriptive columns cleaned. Stage 07 uses the codebook for the severity audit; Stage 09 uses `label` for
the labelled views.

## Failure Modes & Rollback
- **Guide URL/format drifted or unreachable:** the CSVs are *committed*, so tests never fetch them. If you
  cannot reach the guide now, hand-author both severities + the manifest faithfully and note it in the README.
- **`read_csv` path not found at runtime:** the `../reference/` relative path assumes `stats19.py` is in
  `src/crossroads/transformers/`. Loaders raise a clear `FileNotFoundError`; fix the path or co-locate the
  CSVs next to `stats19.py` and update the constants.
- **`CAST(is_missing AS BOOLEAN)` fails:** the CSV wrote `1`/`0` or odd casing. Normalise to lower-case
  `true`/`false`, or change the cast to `(is_missing IN ('1','true','True'))`.
- **Duplicate `(variable, code)`:** `test_reference_tables_load` fails — de-duplicate the CSV (keep the
  authoritative label). Must be fixed before Stage 09's label join.
- **Rollback:** delete `src/crossroads/reference/`, remove the constants + both loaders + their calls, delete
  the new tests. The suite returns to the committed Stage 04 state.

## Appendix A — `stats19_columns.csv` (commit verbatim)
Derived on 2026-07-04 from the three fixture headers + the 2024 guide, applying the Rules in step A2. 99 rows.
Save exactly this (including the header) as `src/crossroads/reference/stats19_columns.csv`:
```csv
table,column,kind,dtype
collision,accident_index,identity,
collision,accident_year,identity,
collision,accident_reference,identity,
collision,location_easting_osgr,geo,
collision,location_northing_osgr,geo,
collision,longitude,geo,
collision,latitude,geo,
collision,police_force,coded,INTEGER
collision,collision_severity,coded,INTEGER
collision,number_of_vehicles,numeric,INTEGER
collision,number_of_casualties,numeric,INTEGER
collision,date,datetime,
collision,day_of_week,coded,INTEGER
collision,time,datetime,
collision,local_authority_district,coded,INTEGER
collision,local_authority_ons_district,text,
collision,local_authority_highway,text,
collision,local_authority_highway_current,text,
collision,first_road_class,coded,INTEGER
collision,first_road_number,numeric,INTEGER
collision,road_type,coded,INTEGER
collision,speed_limit,coded,INTEGER
collision,junction_detail_historic,coded,INTEGER
collision,junction_detail,coded,INTEGER
collision,junction_control,coded,INTEGER
collision,second_road_class,coded,INTEGER
collision,second_road_number,numeric,INTEGER
collision,pedestrian_crossing_human_control_historic,coded,INTEGER
collision,pedestrian_crossing_physical_facilities_historic,coded,INTEGER
collision,pedestrian_crossing,coded,INTEGER
collision,light_conditions,coded,INTEGER
collision,weather_conditions,coded,INTEGER
collision,road_surface_conditions,coded,INTEGER
collision,special_conditions_at_site,coded,INTEGER
collision,carriageway_hazards_historic,coded,INTEGER
collision,carriageway_hazards,coded,INTEGER
collision,urban_or_rural_area,coded,INTEGER
collision,did_police_officer_attend_scene_of_accident,coded,INTEGER
collision,trunk_road_flag,coded,INTEGER
collision,lsoa_of_accident_location,text,
collision,enhanced_severity_collision,coded,INTEGER
collision,collision_injury_based,coded,INTEGER
collision,collision_adjusted_severity_serious,numeric,DOUBLE
collision,collision_adjusted_severity_slight,numeric,DOUBLE
vehicle,accident_index,identity,
vehicle,accident_year,identity,
vehicle,accident_reference,identity,
vehicle,vehicle_reference,identity,
vehicle,vehicle_type,coded,INTEGER
vehicle,towing_and_articulation,coded,INTEGER
vehicle,vehicle_manoeuvre_historic,coded,INTEGER
vehicle,vehicle_manoeuvre,coded,INTEGER
vehicle,vehicle_direction_from,coded,INTEGER
vehicle,vehicle_direction_to,coded,INTEGER
vehicle,vehicle_location_restricted_lane_historic,coded,INTEGER
vehicle,vehicle_location_restricted_lane,coded,INTEGER
vehicle,junction_location,coded,INTEGER
vehicle,skidding_and_overturning,coded,INTEGER
vehicle,hit_object_in_carriageway,coded,INTEGER
vehicle,vehicle_leaving_carriageway,coded,INTEGER
vehicle,hit_object_off_carriageway,coded,INTEGER
vehicle,first_point_of_impact,coded,INTEGER
vehicle,vehicle_left_hand_drive,coded,INTEGER
vehicle,journey_purpose_of_driver_historic,coded,INTEGER
vehicle,journey_purpose_of_driver,coded,INTEGER
vehicle,sex_of_driver,coded,INTEGER
vehicle,age_of_driver,numeric,INTEGER
vehicle,age_band_of_driver,coded,INTEGER
vehicle,engine_capacity_cc,numeric,INTEGER
vehicle,propulsion_code,coded,INTEGER
vehicle,age_of_vehicle,numeric,INTEGER
vehicle,generic_make_model,text,
vehicle,driver_imd_decile,numeric,INTEGER
vehicle,lsoa_of_driver,text,
vehicle,escooter_flag,coded,INTEGER
vehicle,driver_distance_banding,coded,INTEGER
casualty,accident_index,identity,
casualty,accident_year,identity,
casualty,accident_reference,identity,
casualty,vehicle_reference,identity,
casualty,casualty_reference,identity,
casualty,casualty_class,coded,INTEGER
casualty,sex_of_casualty,coded,INTEGER
casualty,age_of_casualty,numeric,INTEGER
casualty,age_band_of_casualty,coded,INTEGER
casualty,casualty_severity,coded,INTEGER
casualty,pedestrian_location,coded,INTEGER
casualty,pedestrian_movement,coded,INTEGER
casualty,car_passenger,coded,INTEGER
casualty,bus_or_coach_passenger,coded,INTEGER
casualty,pedestrian_road_maintenance_worker,coded,INTEGER
casualty,casualty_type,coded,INTEGER
casualty,casualty_imd_decile,numeric,INTEGER
casualty,lsoa_of_casualty,text,
casualty,enhanced_casualty_severity,coded,INTEGER
casualty,casualty_injury_based,coded,INTEGER
casualty,casualty_adjusted_severity_serious,numeric,DOUBLE
casualty,casualty_adjusted_severity_slight,numeric,DOUBLE
casualty,casualty_distance_banding,coded,INTEGER
```
> If a fixture is later re-trimmed to add/drop a column, update this table and the `== 99` / breakdown
> assertions in `test_column_manifest_covers_every_fixture_column` together.
