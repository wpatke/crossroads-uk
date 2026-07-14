# Stage 04 — Documentation, Schema Version & README Showcase
> Part of DfT AADF Traffic Counts. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

- Stages 01–03 complete: `aadf` source green (boundary-mode-aware stamping), showcase test
  green, `docs/plans/012_aadf_traffic/showcase-output.txt` contains real M1 query output,
  and the wizard warns on temporal+aadf.
- Verify: `python -m pytest && python -m pytest -m integration` green.

## Objective

Make every user-facing document tell the truth about the new source: schema dictionary
(+ version bump), data-sources/licence page, README with the verified showcase, CHANGELOG.
Documentation must also state the boundary-mode behaviour accurately (snapshot vs temporal,
mid-year attribution) — no stale "snapshot-only" limitation.

## Implementation Steps

### Step 1 — Schema version bump

Adding tables is an additive schema change (see the versioning rules at the top of
`CHANGELOG.md`), so:

1. `src/crossroads/__init__.py` line ~25: `SCHEMA_VERSION = 2` → `3`.
2. `docs/schema.md` header (~line 9): `**Schema version:** 2` → `3`.
   These two must change together — `tests/test_schema_doc.py`'s fast test asserts the
   doc declares the current constant.

### Step 2 — `docs/schema.md`: document the aadf tables

Follow the existing per-table format exactly (illustrative `CREATE TABLE` block +
per-column meaning/derivation). Add:

1. **`aadf` (silver)** — every column Stage 01 created, in order, including:
   `source_row_key` (count_point_id|year), the raw twins (`easting_raw`,
   `northing_raw`, `all_motor_vehicles_raw`), typed values, `geom` (EPSG:27700 point,
   NULL when invalid), `geom_valid`, `count_valid`, `link_length_km`,
   `estimation_method` (+`_detailed` — note that `Estimated` rows are retained and
   flagged as such by DfT, not by us), the per-class volume columns, and
   `lad_code`/`ctyua_code`. For `lad_code`/`ctyua_code`, state the boundary-mode
   behaviour accurately: **stamped point-in-polygon honouring the build's `boundary_mode`
   — snapshot uses the latest vintage; temporal uses the vintage in force at a mid-year
   (1 July) date derived from the count's `year`** (exact except in a year the boundary
   changed, when the annual average straddles both districts). Do NOT describe this as
   "latest vintage only" — that was an earlier design that no longer holds.
   Get the authoritative column list from a fixture build: `DESCRIBE aadf`.
2. **`aadf_clean` (gold view)** — `WHERE geom_valid AND count_valid` projection.
3. **`aadf_raw`** — one line in the bronze category (bronze tables are documented as a
   category, not column-by-column).
4. Mention the R-Tree index alongside however the other geometry indexes are noted.

### Step 3 — Wire aadf into the schema drift guard

`tests/test_schema_doc.py`:

1. Add `"aadf"` and `"aadf_clean"` to `CORE_TABLES`; add `"aadf"` to `COLUMN_GUARDED`
   (mirror how `collisions` vs `collisions_spatial` are split between the two lists).
2. Add `"aadf_"` to `EXCLUDED_PREFIXES` (covers the `aadf_raw` bronze; the prefix
   requires the underscore so the silver table `aadf` itself is NOT excluded).
3. The drift-guard build (~line 73) must now include aadf: scripted pick `"3-4"` →
   `"1,3-4"` (menu: 1=aadf, 2=bank holidays, 3=era5_weather, 4=stats19) — update the
   adjacent comment. `_seed_full_cache` already seeds the aadf fixture (Stage 01).
4. Run `python -m pytest -m integration -k schema` — the guard now fails on any `aadf`
   column missing from the doc; fix the doc until green. This is the mechanism that
   proves Step 2 is complete, so do not hand-tick it.

### Step 4 — `docs/data-sources.md`: section 5

Follow the existing section format (publisher / dataset / licence / attribution /
loaded-into / caveats):

- **Publisher:** UK Department for Transport (DfT), Road Traffic Statistics.
- **Dataset:** Annual Average Daily Flow (AADF) by count point — daily traffic volumes
  per road link per year, major roads counted, minor roads partly estimated
  (downloaded as one national zipped CSV from https://roadtraffic.dft.gov.uk/downloads).
- **Licence:** Open Government Licence v3.0 (same link format as sections 1–2).
- **Required attribution:** "Contains public sector information licensed under the Open
  Government Licence v3.0. Source: Department for Transport, Road Traffic Statistics."
- **Loaded into:** the `aadf` table (link to schema.md); note the LAD/CTYUA stamping and
  that the FULL year history (2000 onward) is always loaded regardless of the build's
  `years` (with one sentence on why: single source artifact, denominator data).
- **Caveats:**
  - `estimation_method` distinguishes DfT-counted from DfT-estimated flows; both are
    retained (keep-in-place), so filter on it when your analysis needs counted-only volumes.
  - **Boundary attribution:** in temporal mode each annual count is attributed to the area
    boundaries in force at its mid-year (1 July) point; this is exact except in a year the
    boundary itself changed. For a risk metric, compare like years so collisions and counts
    resolve to the same boundary vintage. The wizard shows this note and asks to confirm
    when temporal mode is chosen together with traffic counts.

Optionally extend `tests/test_release.py::test_data_sources_doc_lists_every_source`'s
token list with `"AADF"` — cheap and keeps the doc honest.

### Step 5 — `README.md`: the showcase

1. In the **Usage** Python example, extend `datasets=["stats19"]` to
   `datasets=["stats19", "aadf"]` with a short comment (`# aadf = DfT traffic counts`).
2. In **What you get**, add one bullet: DfT AADF traffic volumes, LAD-stamped, enabling
   per-vehicle-km risk denominators.
3. New section after **What you get**, titled e.g. **Example: real risk, not raw
   counts** — three parts, keep it tight:
   - Two sentences of setup: raw collision counts mislead (busy roads "look" dangerous);
     joining collisions to traffic volume gives collisions per million vehicle-km, and
     both datasets are already in your database sharing road names and LAD codes.
   - The canonical query from Stage 02 in its motorway form (M1), verbatim the same
     shape as the tested query.
   - The REAL output table from `docs/plans/012_aadf_traffic/showcase-output.txt`,
     formatted as a small markdown table, followed by one honest-limitations sentence:
     road-level grain within each local authority (not per-junction), major-road
     coverage, some flows DfT-estimated (`estimation_method`), collisions matched by
     recorded road identity — not by point-to-line snapping — and, for cross-year work,
     compare like years so both sides share the same boundary vintage.
4. Do NOT touch the Citing section (CITATION.cff version is updated at tag time — out of
   scope here; note for the releaser: `tests/test_release.py` pins the CITATION version
   string and must be updated in the same commit as the tag).

### Step 6 — `CHANGELOG.md`

Add an entry under the current unreleased/next-release heading (match the existing
entry style): Added — DfT AADF traffic-count source (`aadf` dataset: national count-point
volumes 2000-onward, LAD/CTYUA-stamped honouring `boundary_mode`, `aadf_clean` view,
R-Tree index); README risk showcase; wizard temporal-mode warning; schema_version 3.

## Testing & Verification

```bash
python -m pytest                    # fast suite (schema version declaration check)
python -m pytest -m integration     # schema drift guard now covers aadf
```
Ship-readiness checklist:
- [ ] Drift guard green WITH `aadf` in `COLUMN_GUARDED` (Step 3.4 — the real proof).
- [ ] README query is character-identical in shape to the tested query (eyeball diff).
- [ ] README output table matches `showcase-output.txt` verbatim.
- [ ] `docs/schema.md` `lad_code`/`ctyua_code` description states the boundary-mode /
      mid-year behaviour and does NOT say "latest vintage only".
- [ ] `docs/data-sources.md` renders correctly (check in a markdown preview).
- [ ] `grep -ri aadf docs/schema.md docs/data-sources.md README.md CHANGELOG.md` shows
      all four files mention it.

## End State / Handoff

- `SCHEMA_VERSION = 3` in code and doc; `aadf`/`aadf_clean`/`aadf_raw` fully documented
  (with accurate boundary-mode wording); drift guard enforcing it; data-sources section 5
  with OGL attribution + the boundary caveat; README carries the verified showcase with
  real output; CHANGELOG entry present.
- The whole plan (Stages 01–04) is complete: full suite green, and the feature is
  documented exactly as tested.

## Failure Modes & Rollback

- **Drift guard names undocumented columns** → that's it working; document them (or fix
  the silver SELECT if a column shouldn't exist).
- **Fast schema test fails after the bump** → code constant and doc header disagree;
  they change together (Step 1).
- **Showcase output looks wrong at README time** → stop; re-run the Stage 02 Step 3
  sanity checks. Never paste unverified numbers.
- **Rollback:** revert this stage's edits (all documentation + two test-list edits +
  the version constant); Stages 01–03 remain valid and green.
