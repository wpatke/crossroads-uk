# Updating ONS boundary vintages

The LAD and CTYUA boundary transformers in `src/crossroads/transformers/spatial.py` read their list of
published editions ("vintages") from a JSON manifest:

```
src/crossroads/transformers/ons_boundaries.json
```

ONS publishes a new BGC ("Generalised, clipped to coastline") boundary edition on the
[Open Geography Portal](https://geoportal.statistics.gov.uk) roughly twice a year (a May and a December
edition). When that happens, add the new edition to the manifest. No Python changes are needed for a
normal update — the registry is data.

## Manifest format

The manifest is keyed by `source_id` (`ons_lad`, `ons_ctyua`); each holds a list of vintage objects:

| Field            | Meaning                                                                         |
|------------------|---------------------------------------------------------------------------------|
| `label`          | Unique edition id, `"YYYY-MM"` (e.g. `"2025-12"`).                               |
| `title`          | The exact ONS dataset title.                                                     |
| `feature_server` | The ArcGIS REST FeatureServer **base** URL (no `/0/query…` suffix).             |
| `code_col`       | Area-code field, **exact case** (e.g. `LAD24CD`, or lowercase `lad19cd`).        |
| `name_col`       | Area-name field, **exact case**.                                                 |
| `valid_from`     | `"YYYY-MM-DD"`, the first day of the edition's month.                            |
| `source_file`    | Cache filename, `<source_id>_<label>.geojson`.                                   |

`valid_to` is **not** stored — `spatial.py` derives it automatically by sorting vintages by `valid_from`
and chaining: each edition's `valid_to` equals the next edition's `valid_from`; the newest edition is
open-ended (`valid_to = None`). So adding a newer edition automatically closes the previous one's
validity window. The newest edition is what a default (snapshot) `build()` ingests.

## Routine update (new edition released)

From the repo root with the virtualenv active (`source .venv/bin/activate`):

**1. Discover the new edition automatically:**
```bash
python scripts/update_ons_boundaries.py --discover
```
This searches the ONS portal for LAD/CTYUA "Boundaries UK BGC" editions not yet in the manifest and
prints a ready-made manifest row for each — FeatureServer URL, field names with correct case, label,
`valid_from`, `source_file`. Review the output carefully.

**2. Apply it** — either copy the printed row into `ons_boundaries.json` by hand, or let the script
append it:
```bash
python scripts/update_ons_boundaries.py --discover --write
```
Always review the resulting diff before committing. Confirm: the FeatureServer URL is the **UK-wide BGC**
Feature Service (not England-only, MapServer, or a different generalisation), and the field names and
their casing match the live layer.

**3. Validate the whole registry is live:**
```bash
python scripts/update_ons_boundaries.py --validate
```
Every vintage should print `OK`. `WARN` lines are informational — read them and decide if action is
needed (e.g. a live field casing that differs from the manifest, or a feature count approaching the
download limit). A `FAIL` (unreachable layer or missing field) must be fixed before committing.

**4. Add a fixture for the new edition:**

The offline test suite seeds a committed GeoJSON fixture under the newest vintage's `source_file` name.
When a new edition is added and it becomes the newest, the tests will look for a fixture directory named
`tests/fixtures/ons/<type>_<year>/` containing `<type>_sample.geojson` (e.g. `lad_2026/lad_sample.geojson`).

Create it by taking the previous year's fixture and renaming the column properties to match the new
edition's `code_col`/`name_col`:
```bash
python - <<'PY'
import json, os

# Adjust these for the new edition:
prev_year = "2025"
new_year  = "2026"
old_code, new_code = "LAD25CD", "LAD26CD"
old_name, new_name = "LAD25NM", "LAD26NM"

for prefix in ("lad", "ctyua"):
    with open(f"tests/fixtures/ons/{prefix}_{prev_year}/{prefix}_sample.geojson") as f:
        data = json.load(f)
    for feat in data["features"]:
        p = feat["properties"]
        # Adapt to CTYUA names if needed; adjust old_*/new_* for ctyua above.
        if old_code in p:
            p[new_code] = p.pop(old_code)
        if old_name in p:
            p[new_name] = p.pop(old_name)
    os.makedirs(f"tests/fixtures/ons/{prefix}_{new_year}", exist_ok=True)
    with open(f"tests/fixtures/ons/{prefix}_{new_year}/{prefix}_sample.geojson", "w") as f:
        json.dump(data, f)
    print(f"wrote {prefix}_{new_year}")
PY
```

**5. Run the test suite:**
```bash
python -m pytest -q
```
The registry tests confirm the new edition is present and validity windows still chain correctly. The
offline build test uses the new fixture automatically.

**6. Commit** the manifest change and new fixture (ask the repo owner first per project policy).

## Manual fallback (if the script can't find an edition)

The script encodes two ONS-specific assumptions:
- `parse_title()` — parses `(Month Year)` from the dataset title to produce the `label` and `valid_from`.
- `find_code_name_cols()` and filters in `discover()` — identify the right UK BGC Feature Service and
  match the code/name fields by prefix (`lad`/`ctyua`) and suffix (`cd`/`nm`).

If ONS changes its title format, service naming, or field naming conventions, the script may need a small
edit in those functions. To find an edition manually:

1. Search the ArcGIS content API (replace product fragment as needed):
   ```
   https://www.arcgis.com/sharing/rest/search?q=owner:ONSGeography_data%20AND%20title:%22Local%20Authority%20Districts%22%20AND%20title:%22Boundaries%20UK%20BGC%22&f=json&num=100
   ```
   Find the result with `"type": "Feature Service"` whose `title` matches the new edition; its `url` is
   the `feature_server` value.

2. Confirm the field names and casing from the live layer:
   ```
   <feature_server>/0?f=json
   ```
   Look in the `fields` array for the `…CD` (code) and `…NM` (name) entries — copy their exact `name`.

3. Add the row to `ons_boundaries.json` following the format above, then run steps 3–6 of the routine
   update.

## Notes

- **Field-name casing varies by edition.** Newer editions use uppercase (`LAD25CD`); several older ones
  use lowercase (`lad19cd`, `ctyua18cd`). The manifest stores exactly what the live layer reports — the
  transformer uses the exact case to SELECT from the downloaded GeoJSON.

- **Not every edition exists for both boundary types.** CTYUA has no May editions for several years and no
  UK-wide 2016 edition. That is expected; validity chaining absorbs the gaps automatically.

- **Download format.** `spatial.py` builds the GeoJSON query URL from `feature_server` plus the field
  names (`/0/query?where=1=1&outFields=<code>,<name>&outSR=27700&f=geojson`). Boundaries are returned in
  EPSG:27700 (British National Grid) by the server. There is no separate URL field to maintain.

- **Tests never hit the network.** Only `scripts/update_ons_boundaries.py` and the opt-in
  `network`-marked test in `tests/test_update_script.py` do. Run the live validation manually as part of
  every update.
