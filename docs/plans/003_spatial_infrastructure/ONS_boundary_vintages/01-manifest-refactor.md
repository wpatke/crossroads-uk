# Stage 01 — Manifest-Driven Registry (Behaviour-Preserving Refactor)
> Part of "ONS Boundary Vintage Registry & Update Workflow". You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Verify before you start:
```bash
source .venv/bin/activate
python -m pytest -q          # all green
```
Observable state to confirm by reading the file:
- `src/crossroads/transformers/spatial.py` defines `Vintage`, `_BoundaryTransformer`,
  `LADBoundaryTransformer`, `CTYUABoundaryTransformer`, and two module constants `_LAD_2024_URL` and
  `_CTYUA_2024_URL`.
- Each concrete class has a hard-coded `vintages = (Vintage(label="2024", url=_LAD_2024_URL,
  source_file="lad_sample.geojson", code_col="LAD24CD", name_col="LAD24NM", valid_from="2024-12-01",
  valid_to=None),)` (and the CTYUA equivalent with `CTYUA24CD`/`CTYUA24NM`/`ctyua_sample.geojson`).
- There is **no** `src/crossroads/transformers/ons_boundaries.json` yet.

## Objective
Move the vintage registry into a committed JSON manifest loaded at import time, **without changing any
behaviour**: each transformer must end up with exactly the same single December 2024 vintage it has today,
the same download URL shape, and the same `source_file`. All existing tests must stay green. This stage
lays the foundation; Stage 02 adds the full back-catalogue.

## Implementation Steps

### A. Create the manifest with ONLY the current data
Create `src/crossroads/transformers/ons_boundaries.json` with exactly the two vintages that exist today.
Keep `label` as `"2024"` and `source_file` as the current fixture names so behaviour is identical:

```json
{
  "ons_lad": [
    {
      "label": "2024",
      "title": "Local Authority Districts (December 2024) Boundaries UK BGC",
      "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_December_2024_Boundaries_UK_BGC/FeatureServer",
      "code_col": "LAD24CD",
      "name_col": "LAD24NM",
      "valid_from": "2024-12-01",
      "source_file": "lad_sample.geojson"
    }
  ],
  "ons_ctyua": [
    {
      "label": "2024",
      "title": "Counties and Unitary Authorities (December 2024) Boundaries UK BGC",
      "feature_server": "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Counties_and_Unitary_Authorities_December_2024_Boundaries_UK_BGC/FeatureServer",
      "code_col": "CTYUA24CD",
      "name_col": "CTYUA24NM",
      "valid_from": "2024-12-01",
      "source_file": "ctyua_sample.geojson"
    }
  ]
}
```

> Note: `valid_to` is intentionally absent — the loader derives it (here, a single vintage ⇒
> `valid_to = None`, matching today).

### B. Add the loader to `spatial.py`
1. Add imports near the top of `src/crossroads/transformers/spatial.py` (alongside `import os`):
   ```python
   import json
   import urllib.parse
   ```
2. Add a module-level manifest path and loader function **above** the concrete classes (e.g. just below
   the `Vintage` dataclass). Keep the comments plain and matching the file's existing style:
   ```python
   # The vintage registry lives in a committed JSON manifest next to this module,
   # so adding a new ONS edition is a data change, not a code change. The loader
   # below turns each manifest row into a Vintage, chains validity windows by date,
   # and builds the FeatureServer GeoJSON query URL.
   _MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "ons_boundaries.json")


   def _build_query_url(feature_server, code_col, name_col):
       """Build the ONS FeatureServer GeoJSON query endpoint for one vintage.

       Same shape used previously: layer 0, all features, only the code/name
       columns, reprojected server-side to EPSG:27700, returned as GeoJSON.
       """
       query = urllib.parse.urlencode({
           "where": "1=1",
           "outFields": f"{code_col},{name_col}",
           "outSR": "27700",
           "f": "geojson",
       })
       return feature_server.rstrip("/") + "/0/query?" + query


   def _load_vintages(source_id):
       """Load one source's vintages from the JSON manifest, newest LAST.

       Vintages are sorted ascending by valid_from so vintages[-1] is the latest
       edition (the snapshot). valid_to is derived by chaining: each vintage is
       valid until the next edition's valid_from; the latest vintage is open-ended
       (valid_to = None).
       """
       with open(_MANIFEST_PATH, encoding="utf-8") as f:
           rows = json.load(f)[source_id]
       rows = sorted(rows, key=lambda r: r["valid_from"])
       vintages = []
       for i, r in enumerate(rows):
           valid_to = rows[i + 1]["valid_from"] if i + 1 < len(rows) else None
           vintages.append(Vintage(
               label=r["label"],
               url=_build_query_url(r["feature_server"], r["code_col"], r["name_col"]),
               source_file=r["source_file"],
               code_col=r["code_col"],
               name_col=r["name_col"],
               valid_from=r["valid_from"],
               valid_to=valid_to,
           ))
       return tuple(vintages)
   ```

### C. Switch the concrete classes to the loader
1. Delete the two module constants `_LAD_2024_URL` and `_CTYUA_2024_URL` (and their explanatory comment
   block) — the loader builds URLs now.
2. In `LADBoundaryTransformer`, replace the hard-coded `vintages = (Vintage(...),)` with:
   ```python
   vintages = _load_vintages("ons_lad")
   ```
3. In `CTYUABoundaryTransformer`, replace it with:
   ```python
   vintages = _load_vintages("ons_ctyua")
   ```
4. Leave everything else in `spatial.py` unchanged (`_BoundaryTransformer`, `extract`,
   `transform_and_load`, `_validity_case_sql`, `_derive_silver_and_ledger`, `quality_spec`, the BNG
   envelope constants, the `Vintage` dataclass).

> The `vintages = _load_vintages(...)` assignment runs at class-definition (import) time, reading the JSON
> once. That is fine: the manifest is a static committed file. If you prefer lazy loading, a `@property`
> returning `_load_vintages(...)` also works — but the plain class attribute keeps `vintages[-1]` and
> `_vintages_for()` working exactly as today, so prefer it.

### D. Ensure the JSON ships in the package
Hatchling's wheel target is `packages = ["src/crossroads"]`. Confirm the manifest is included:
```bash
source .venv/bin/activate
python -m pip install --quiet build
python -m build --wheel --outdir /tmp/cr_wheel_check 2>/dev/null
python - <<'PY'
import zipfile, glob
whl = sorted(glob.glob("/tmp/cr_wheel_check/*.whl"))[-1]
names = zipfile.ZipFile(whl).namelist()
hit = [n for n in names if n.endswith("ons_boundaries.json")]
print("MANIFEST IN WHEEL:", hit)
assert hit, "ons_boundaries.json missing from wheel"
PY
```
If the assertion fails (the data file was not included), add this to `pyproject.toml` and re-run the
check:
```toml
[tool.hatch.build.targets.wheel.force-include]
"src/crossroads/transformers/ons_boundaries.json" = "crossroads/transformers/ons_boundaries.json"
```
(If the file is already present in the wheel, change nothing in `pyproject.toml`.)

## Testing & Verification
This stage is behaviour-preserving, so the existing suite is the primary proof. Run:
```bash
source .venv/bin/activate
python -m pytest -q
```
Expected: **all green**, identical to the starting state. In particular `tests/test_spatial.py` still
passes because each transformer still exposes a single Dec 2024 `Vintage` with `source_file`
`lad_sample.geojson` / `ctyua_sample.geojson` and `valid_to = None`.

Add one small **pure-data** test to `tests/test_spatial.py` proving the registry now comes from the
manifest and the URL/validity derivation works (no network, no build):
```python
def test_vintages_loaded_from_manifest():
    from crossroads.transformers.spatial import LADBoundaryTransformer
    vintages = LADBoundaryTransformer().vintages
    assert len(vintages) >= 1
    latest = vintages[-1]
    # URL is derived from the FeatureServer base + columns.
    assert latest.url.endswith("f=geojson")
    assert "outSR=27700" in latest.url
    assert latest.code_col in latest.url
    # The latest (newest) vintage is open-ended.
    assert latest.valid_to is None
```

**Stage ship-readiness checklist:**
- [ ] `src/crossroads/transformers/ons_boundaries.json` exists with the two Dec 2024 vintages.
- [ ] `spatial.py` loads `vintages` via `_load_vintages(...)`; `_LAD_2024_URL`/`_CTYUA_2024_URL` deleted.
- [ ] `_build_query_url` reproduces the existing query endpoint shape (`/0/query?…outSR=27700…f=geojson`).
- [ ] The JSON is present in a built wheel (Step D), with `force-include` added only if needed.
- [ ] `python -m pytest -q` is fully green, including the new `test_vintages_loaded_from_manifest`.

## End State / Handoff
`spatial.py` reads its vintage registry from `ons_boundaries.json`. The registry is functionally
identical to before (one Dec 2024 vintage per type, same URL, same `source_file`, `valid_to = None`), and
the whole suite is green. Stage 02 may now replace the manifest contents with the full back-catalogue,
relying on: the loader sorting by `valid_from` (newest last), chaining `valid_to`, and building URLs from
`feature_server` + columns.

## Failure Modes & Rollback
- **`KeyError` for a manifest field:** the loader expects `label`, `title`, `feature_server`, `code_col`,
  `name_col`, `valid_from`, `source_file` on every row. Fix the JSON; do not make the loader tolerant of
  missing required fields (a missing field should fail loudly).
- **URL shape drift breaks the opt-in download:** if a later download fails, compare the
  `_build_query_url` output to the previous `_LAD_2024_URL` literal — they must be equivalent (`where=1=1`
  url-encodes to `where=1%3D1`, which ArcGIS accepts identically).
- **Wheel missing the JSON:** add the `force-include` block (Step D) and re-verify.
- **Rollback:** restore the two URL constants and the hard-coded `vintages` tuples, delete
  `ons_boundaries.json` and the loader, and revert any `pyproject.toml` change. The suite returns to the
  starting state.
</content>
