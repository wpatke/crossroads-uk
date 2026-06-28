# Stage 02 — Populate the Full Vintage Registry
> Part of "ONS Boundary Vintage Registry & Update Workflow". You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stage 01 is done: `spatial.py` loads its registry from `ons_boundaries.json` (currently one Dec 2024
vintage per type), and the suite is green. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```

## Objective
Replace the manifest contents with **every published ONS BGC edition** — 15 Local Authority District
(LAD) editions and 11 Counties & Unitary Authorities (CTYUA) editions — labelled `YYYY-MM`, with verified
FeatureServer URLs, exact-case field names, effective dates, and per-vintage cache filenames. The loader
(unchanged) will sort them by date, chain validity windows, and expose December 2025 as the newest
snapshot for both types. Make the offline tests robust to "which edition is newest," and add pure-data
tests asserting the registry's contents.

All URLs and field names below were verified against the live ArcGIS layer JSON. The FeatureServer base
for every entry is `https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/<service_name>/FeatureServer`.

## Implementation Steps

### A. Replace `ons_boundaries.json` with the full registry
Overwrite `src/crossroads/transformers/ons_boundaries.json` with the content below. **Field-name casing
matters** — copy it exactly (several older editions are lowercase). `source_file` follows the convention
`<source_id>_<label>.geojson`.

```json
{
  "ons_lad": [
    {"label": "2016-12", "title": "Local Authority Districts (December 2016) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_December_2016_GCB_in_the_UK_2022/FeatureServer", "code_col": "lad16cd", "name_col": "lad16nm", "valid_from": "2016-12-01", "source_file": "ons_lad_2016-12.geojson"},
    {"label": "2018-12", "title": "Local Authority Districts (December 2018) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/LAD_December_2018_Boundaries_UK_BGC_2022/FeatureServer", "code_col": "lad18cd", "name_col": "lad18nm", "valid_from": "2018-12-01", "source_file": "ons_lad_2018-12.geojson"},
    {"label": "2019-04", "title": "Local Authority Districts (April 2019) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_April_2019_UK_BGC_2022/FeatureServer", "code_col": "LAD19CD", "name_col": "LAD19NM", "valid_from": "2019-04-01", "source_file": "ons_lad_2019-04.geojson"},
    {"label": "2019-12", "title": "Local Authority Districts (December 2019) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_December_2019_GCB_UK_2022/FeatureServer", "code_col": "lad19cd", "name_col": "lad19nm", "valid_from": "2019-12-01", "source_file": "ons_lad_2019-12.geojson"},
    {"label": "2020-12", "title": "Local Authority Districts (December 2020) Boundaries UK BGC (V2)", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/LAD_DEC_2020_UK_BGC/FeatureServer", "code_col": "LAD20CD", "name_col": "LAD20NM", "valid_from": "2020-12-01", "source_file": "ons_lad_2020-12.geojson"},
    {"label": "2021-05", "title": "Local Authority Districts (May 2021) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_May_2021_UK_BGC_2022/FeatureServer", "code_col": "LAD21CD", "name_col": "LAD21NM", "valid_from": "2021-05-01", "source_file": "ons_lad_2021-05.geojson"},
    {"label": "2021-12", "title": "Local Authority Districts (December 2021) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_December_2021_UK_BGC_2022/FeatureServer", "code_col": "LAD21CD", "name_col": "LAD21NM", "valid_from": "2021-12-01", "source_file": "ons_lad_2021-12.geojson"},
    {"label": "2022-05", "title": "Local Authority Districts (May 2022) Boundaries UK BGC (V3)", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_May_2022_UK_BGC_V3_2022/FeatureServer", "code_col": "LAD22CD", "name_col": "LAD22NM", "valid_from": "2022-05-01", "source_file": "ons_lad_2022-05.geojson"},
    {"label": "2022-12", "title": "Local Authority Districts (December 2022) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_December_2022_UK_BGC_V2/FeatureServer", "code_col": "LAD22CD", "name_col": "LAD22NM", "valid_from": "2022-12-01", "source_file": "ons_lad_2022-12.geojson"},
    {"label": "2023-05", "title": "Local Authority Districts (May 2023) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_May_2023_UK_BGC_V2/FeatureServer", "code_col": "LAD23CD", "name_col": "LAD23NM", "valid_from": "2023-05-01", "source_file": "ons_lad_2023-05.geojson"},
    {"label": "2023-12", "title": "Local Authority Districts (December 2023) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_December_2023_Boundaries_UK_BGC/FeatureServer", "code_col": "LAD23CD", "name_col": "LAD23NM", "valid_from": "2023-12-01", "source_file": "ons_lad_2023-12.geojson"},
    {"label": "2024-05", "title": "Local Authority Districts (May 2024) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_May_2024_Boundaries_UK_BGC/FeatureServer", "code_col": "LAD24CD", "name_col": "LAD24NM", "valid_from": "2024-05-01", "source_file": "ons_lad_2024-05.geojson"},
    {"label": "2024-12", "title": "Local Authority Districts (December 2024) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_December_2024_Boundaries_UK_BGC/FeatureServer", "code_col": "LAD24CD", "name_col": "LAD24NM", "valid_from": "2024-12-01", "source_file": "ons_lad_2024-12.geojson"},
    {"label": "2025-05", "title": "Local Authority Districts (May 2025) Boundaries UK BGC (V2)", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/LAD_MAY_2025_UK_BGC_V2/FeatureServer", "code_col": "LAD25CD", "name_col": "LAD25NM", "valid_from": "2025-05-01", "source_file": "ons_lad_2025-05.geojson"},
    {"label": "2025-12", "title": "Local Authority Districts (December 2025) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_DEC_2025_Boundaries_UK_BGC/FeatureServer", "code_col": "LAD25CD", "name_col": "LAD25NM", "valid_from": "2025-12-01", "source_file": "ons_lad_2025-12.geojson"}
  ],
  "ons_ctyua": [
    {"label": "2018-12", "title": "Counties and Unitary Authorities (December 2018) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2018_GCB_UK_2022/FeatureServer", "code_col": "ctyua18cd", "name_col": "ctyua18nm", "valid_from": "2018-12-01", "source_file": "ons_ctyua_2018-12.geojson"},
    {"label": "2019-04", "title": "Counties and Unitary Authorities (April 2019) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_April_2019_Generalised_Boundaries_UK_2022/FeatureServer", "code_col": "ctyua19cd", "name_col": "ctyua19nm", "valid_from": "2019-04-01", "source_file": "ons_ctyua_2019-04.geojson"},
    {"label": "2019-12", "title": "Counties and Unitary Authorities (December 2019) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2019_GCB_UK_2022/FeatureServer", "code_col": "ctyua19cd", "name_col": "ctyua19nm", "valid_from": "2019-12-01", "source_file": "ons_ctyua_2019-12.geojson"},
    {"label": "2020-12", "title": "Counties and Unitary Authorities (December 2020) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2020_UK_BGC_2022/FeatureServer", "code_col": "CTYUA20CD", "name_col": "CTYUA20NM", "valid_from": "2020-12-01", "source_file": "ons_ctyua_2020-12.geojson"},
    {"label": "2021-05", "title": "Counties and Unitary Authorities (May 2021) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_May_2021_UK_BGC_2022/FeatureServer", "code_col": "CTYUA21CD", "name_col": "CTYUA21NM", "valid_from": "2021-05-01", "source_file": "ons_ctyua_2021-05.geojson"},
    {"label": "2021-12", "title": "Counties and Unitary Authorities (December 2021) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2021_UK_BGC_2022/FeatureServer", "code_col": "CTYUA21CD", "name_col": "CTYUA21NM", "valid_from": "2021-12-01", "source_file": "ons_ctyua_2021-12.geojson"},
    {"label": "2022-12", "title": "Counties and Unitary Authorities (December 2022) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2022_UK_BGC/FeatureServer", "code_col": "CTYUA22CD", "name_col": "CTYUA22NM", "valid_from": "2022-12-01", "source_file": "ons_ctyua_2022-12.geojson"},
    {"label": "2023-05", "title": "Counties and Unitary Authorities (May 2023) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_May_2023_UK_BGC/FeatureServer", "code_col": "CTYUA23CD", "name_col": "CTYUA23NM", "valid_from": "2023-05-01", "source_file": "ons_ctyua_2023-05.geojson"},
    {"label": "2023-12", "title": "Counties and Unitary Authorities (December 2023) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2023_Boundaries_UK_BGC/FeatureServer", "code_col": "CTYUA23CD", "name_col": "CTYUA23NM", "valid_from": "2023-12-01", "source_file": "ons_ctyua_2023-12.geojson"},
    {"label": "2024-12", "title": "Counties and Unitary Authorities (December 2024) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2024_Boundaries_UK_BGC/FeatureServer", "code_col": "CTYUA24CD", "name_col": "CTYUA24NM", "valid_from": "2024-12-01", "source_file": "ons_ctyua_2024-12.geojson"},
    {"label": "2025-12", "title": "Counties and Unitary Authorities (December 2025) Boundaries UK BGC", "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2025_Boundaries_UK_BGC/FeatureServer", "code_col": "CTYUA25CD", "name_col": "CTYUA25NM", "valid_from": "2025-12-01", "source_file": "ons_ctyua_2025-12.geojson"}
  ]
}
```

**Editions deliberately excluded (verified absent / out of UK scope):**
- CTYUA **May 2024**, **May 2025**, **May 2022** — ONS published no CTYUA BGC edition for these.
- CTYUA **December 2016** — only an England-&-Wales extent exists (no UK-wide BGC), so it is excluded to
  keep the registry UK-wide. (If a UK 2016 CTYUA is ever needed, add it explicitly with its own row.)

> Sanity check the resulting validity chaining once loaded: LAD `2016-12` should chain to
> `valid_to = 2018-12-01` (the 2017 gap is absorbed), and both `ons_lad`/`ons_ctyua` `2025-12` should be
> `valid_to = None`.

### B. Make the offline build tests robust to "newest edition"
The build tests seed the committed fixtures, but `extract()` now looks for the **newest** vintage's
`source_file` (`ons_lad_2025-12.geojson` / `ons_ctyua_2025-12.geojson`), which differs from the committed
fixture filenames. Rather than download a new sample, seed the existing committed fixture **under the
newest vintage's filename** so the offline build still works and adapts to future editions automatically.

In `tests/test_spatial.py`, replace `_seed_cache` with:
```python
from crossroads.transformers.spatial import (
    LADBoundaryTransformer,
    CTYUABoundaryTransformer,
)

def _seed_cache(cache_dir):
    """Copy each committed GeoJSON fixture into the build cache under the name the
    newest vintage expects, so the snapshot build runs fully offline regardless of
    which edition is currently newest in the manifest. The fixture is real ONS BGC
    geometry (Dec 2024) used as a structural stand-in; the tests assert row counts,
    the EPSG:27700 envelope, and quality invariants, not edition identity.
    """
    os.makedirs(cache_dir, exist_ok=True)
    seeds = (
        ("lad_2024", "lad_sample", LADBoundaryTransformer),
        ("ctyua_2024", "ctyua_sample", CTYUABoundaryTransformer),
    )
    for sub, stem, cls in seeds:
        src = os.path.join(FIXTURES, sub, stem + ".geojson")
        dest_name = cls().vintages[-1].source_file   # newest vintage's cache filename
        shutil.copy(src, os.path.join(cache_dir, dest_name))
```
The existing `_boundary_client`, `test_build_ingests_boundaries_end_to_end`,
`test_boundary_geometry_is_epsg_27700`, and `test_boundary_build_passes_step2_invariants` then keep
working unchanged (still asserting `lad == 3`, `ctyua == 2`, etc., because the same fixture geometry is
loaded — just under a new filename).

### C. Fix the hard-coded label in the invalid-geometry test
`test_invalid_geometry_is_flagged_and_logged` inserts synthetic bronze rows tagged `'2024'` and asserts
`source_row_key == "E99000002|2024"`. With the full registry the newest label is `"2025-12"`. The test
builds its own bronze, so make it use the newest vintage's label so it stays self-consistent:
```python
def test_invalid_geometry_is_flagged_and_logged(con):
    con.execute("LOAD spatial")
    ensure_quality_tables(con)

    t = LADBoundaryTransformer()
    vintages = t._vintages_for()
    label = vintages[-1].label          # newest vintage label, e.g. "2025-12"

    vf_case, vt_case = t._validity_case_sql(vintages)
    con.execute(
        f"CREATE TABLE {t.bronze_table} ("
        f"  vintage VARCHAR, area_code VARCHAR, area_name VARCHAR, geom GEOMETRY"
        f")"
    )
    con.execute(
        f"INSERT INTO {t.bronze_table} VALUES "
        f"(?, 'E99000001', 'Valid Area', "
        f"  ST_GeomFromText('POLYGON((0 0,100 0,100 100,0 100,0 0))')), "
        f"(?, 'E99000002', 'Null Geom Area', NULL)",
        [label, label],
    )

    t._derive_silver_and_ledger(con, vintages)

    valid_n = con.execute(
        f"SELECT count(*) FROM {t.silver_table} WHERE geom_valid = TRUE"
    ).fetchone()[0]
    invalid_n = con.execute(
        f"SELECT count(*) FROM {t.silver_table} WHERE geom_valid = FALSE"
    ).fetchone()[0]
    assert valid_n == 1 and invalid_n == 1

    ledger = con.execute(
        "SELECT source_row_key, rule_id, severity "
        "FROM data_quality_log WHERE source_id = ?",
        ["ons_lad"],
    ).fetchall()
    assert len(ledger) == 1
    key, rule, sev = ledger[0]
    assert key == "E99000002|" + label
    assert rule == LADBoundaryTransformer.GEOM_RULE
    assert sev == "reject_dimension"
```

### D. Add pure-data registry tests
Append to `tests/test_spatial.py` (no network, no build):
```python
def test_full_registry_loaded():
    lad = LADBoundaryTransformer().vintages
    ctyua = CTYUABoundaryTransformer().vintages
    assert len(lad) == 15
    assert len(ctyua) == 11
    # Newest edition is December 2025 for both types.
    assert lad[-1].label == "2025-12" and lad[-1].valid_to is None
    assert ctyua[-1].label == "2025-12" and ctyua[-1].valid_to is None


def test_validity_windows_chain_by_date():
    lad = LADBoundaryTransformer().vintages
    # Sorted ascending by valid_from; each valid_to equals the next valid_from.
    for earlier, later in zip(lad, lad[1:]):
        assert earlier.valid_from < later.valid_from
        assert earlier.valid_to == later.valid_from
    # Spot-check the 2017 gap is absorbed: Dec 2016 -> Dec 2018.
    by_label = {v.label: v for v in lad}
    assert by_label["2016-12"].valid_to == "2018-12-01"


def test_field_name_casing_preserved():
    by_label = {v.label: v for v in LADBoundaryTransformer().vintages}
    assert by_label["2019-12"].code_col == "lad19cd"   # older editions lowercase
    assert by_label["2024-12"].code_col == "LAD24CD"   # newer editions uppercase
```

## Testing & Verification
```bash
source .venv/bin/activate
python -m pytest -q
```
Expected: **all green**, including the updated build tests and the new registry tests. The end-to-end
build test proves a real offline `build()` ingests the newest edition through bronze→silver→gold with
EPSG:27700 verified and the quality invariants passing.

**Stage ship-readiness checklist:**
- [ ] `ons_boundaries.json` holds 15 LAD + 11 CTYUA vintages with exact-case field names.
- [ ] `LADBoundaryTransformer().vintages[-1].label == "2025-12"` and the same for CTYUA.
- [ ] Validity windows chain (each `valid_to` == next `valid_from`; newest `valid_to is None`).
- [ ] `_seed_cache` seeds fixtures under the newest vintage's `source_file`; build tests still green.
- [ ] The invalid-geometry test uses the newest label and passes.
- [ ] New registry tests (`test_full_registry_loaded`, `test_validity_windows_chain_by_date`,
      `test_field_name_casing_preserved`) pass.
- [ ] Full `python -m pytest -q` green.

## End State / Handoff
The manifest holds the full LAD/CTYUA back-catalogue; the snapshot `build()` ingests December 2025; the
registry is correct and tested. Stage 03 can build the maintenance script against this manifest schema and
contents.

## Failure Modes & Rollback
- **`ST_Read`/binder error on a field name during a real download:** a `code_col`/`name_col` is wrong or
  wrong-cased for that edition. The offline tests will not catch this (they read the committed fixture);
  the Stage 03 `--validate` script is what catches it. Fix the offending manifest row.
- **`test_full_registry_loaded` count mismatch:** a row was dropped or duplicated when pasting the JSON;
  re-check against the tables above (15 / 11).
- **Chaining test fails:** two vintages share a `valid_from`, or a `valid_from` is mis-typed; ensure each
  is unique and `YYYY-MM-DD`.
- **Rollback:** restore the Stage 01 single-vintage `ons_boundaries.json` and revert the
  `tests/test_spatial.py` edits. The suite returns to the Stage 01 state.
</content>
