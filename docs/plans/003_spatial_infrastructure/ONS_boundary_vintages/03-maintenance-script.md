# Stage 03 — Maintenance Script (discover & validate editions)
> Part of "ONS Boundary Vintage Registry & Update Workflow". You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stages 01–02 are done: `ons_boundaries.json` holds the full registry (15 LAD + 11 CTYUA) and the suite is
green. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```

## Objective
Add a committed, stdlib-only command-line tool `scripts/update_ons_boundaries.py` that a maintainer runs
when ONS releases new data. It has two jobs:
- **`--validate`** — check every vintage already in the manifest is still live and that its `code_col` /
  `name_col` exist on the live ArcGIS layer (reporting exact case). Non-zero exit on any failure.
- **`--discover`** — query the ONS ArcGIS portal for boundary editions **not yet in the manifest**, fetch
  their field names, and print proposed manifest rows; with `--write`, append them to the manifest.

The script is a developer/maintenance tool: it is **not** imported by the package, **not** run by
`build()`, and the test suite stays offline (the script's network calls are exercised only by an opt-in,
network-marked test).

## Background: the ONS ArcGIS REST endpoints the script uses
- **Layer metadata** (for validation): `GET <feature_server>/0?f=json` returns JSON with a `fields` array;
  each field has a `name`. Confirm `code_col`/`name_col` are present (case-insensitive compare, report the
  actual case).
- **Feature count** (pagination guard): `GET <feature_server>/0/query?where=1=1&returnCountOnly=true&f=json`
  returns `{"count": N}`. Compare against the layer's `maxRecordCount`.
- **Content search** (for discovery):
  `GET https://www.arcgis.com/sharing/rest/search?q=<query>&f=json&num=100&start=<n>` where the query is
  `owner:ONSGeography_data AND title:"Local Authority Districts" AND title:"Boundaries UK BGC"` (and the
  CTYUA equivalent `title:"Counties and Unitary Authorities"`). Each result item has `id`, `title`,
  `type`, and `url` (the FeatureServer URL for `type == "Feature Service"`). Paginate via the response's
  `nextStart` (a value of `-1` means no more pages).
- **Title → label / valid_from:** parse the month and year from the title's `(<Month> <Year>)` group, e.g.
  "Local Authority Districts (December 2025) Boundaries UK BGC" → month `December`, year `2025` →
  `label = "2025-12"`, `valid_from = "2025-12-01"`.

## Implementation Steps

### A. Create `scripts/update_ons_boundaries.py`
Create the file with the structure below. Keep it simple, stdlib-only, and well-commented. Use
`urllib.request`/`urllib.parse` for HTTP and `json` for parsing. Resolve the manifest path relative to the
repo root so the script works from anywhere.

```python
#!/usr/bin/env python3
"""Maintenance tool for the ONS boundary vintage registry.

Run this when ONS publishes a new boundary edition, or to check the existing
registry is still live. It is a developer tool — it is NOT used by the package
at runtime and never runs during `build()` or the test suite.

Examples:
    python scripts/update_ons_boundaries.py --validate
    python scripts/update_ons_boundaries.py --discover
    python scripts/update_ons_boundaries.py --discover --write

Uses only the Python standard library (no new dependencies).
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

# The manifest the package reads. Resolved relative to this script's location.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(
    REPO_ROOT, "src", "crossroads", "transformers", "ons_boundaries.json"
)

# ONS ArcGIS Online org that owns the boundary datasets.
ONS_OWNER = "ONSGeography_data"
ARCGIS_SEARCH = "https://www.arcgis.com/sharing/rest/search"

# Which products we manage, keyed by manifest source_id. The title fragment is
# used both to search and to recognise an edition's product.
PRODUCTS = {
    "ons_lad":   "Local Authority Districts",
    "ons_ctyua": "Counties and Unitary Authorities",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _get_json(url):
    """GET a URL and parse JSON. Raises urllib errors on failure (caught by caller)."""
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def _load_manifest():
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_manifest(manifest):
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def parse_title(title):
    """Extract (label, valid_from) from an ONS dataset title, or (None, None).

    'Local Authority Districts (December 2025) Boundaries UK BGC'
        -> ('2025-12', '2025-12-01')
    """
    m = re.search(r"\(([A-Za-z]+)\s+(\d{4})\)", title)
    if not m:
        return None, None
    month_name, year = m.group(1).lower(), int(m.group(2))
    month = MONTHS.get(month_name)
    if not month:
        return None, None
    return f"{year:04d}-{month:02d}", f"{year:04d}-{month:02d}-01"


def field_names(feature_server):
    """Return the lowercase->actual mapping of field names on layer 0."""
    meta = _get_json(feature_server.rstrip("/") + "/0?f=json")
    return {f["name"].lower(): f["name"] for f in meta.get("fields", [])}


def find_code_name_cols(feature_server, source_id):
    """Discover the code/name fields on a layer, e.g. LAD25CD / LAD25NM.

    ONS uses '<PREFIX><yy>CD' / '<PREFIX><yy>NM'; the prefix is LAD or CTYUA.
    Match on the suffix so casing/year are taken straight from the live layer.
    """
    prefix = "lad" if source_id == "ons_lad" else "ctyua"
    fields = field_names(feature_server)  # lower -> actual
    code = name = None
    for lower, actual in fields.items():
        if lower.startswith(prefix) and lower.endswith("cd") and "nmw" not in lower:
            code = actual
        if lower.startswith(prefix) and lower.endswith("nm") and not lower.endswith("nmw"):
            name = actual
    return code, name


def validate(manifest):
    """Check every manifest vintage is live and its fields exist. Returns ok bool."""
    ok = True
    for source_id, vintages in manifest.items():
        for v in vintages:
            label = v["label"]
            try:
                fields = field_names(v["feature_server"])
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"FAIL {source_id} {label}: unreachable layer ({exc})")
                ok = False
                continue
            for col_key in ("code_col", "name_col"):
                col = v[col_key]
                if col.lower() not in fields:
                    print(f"FAIL {source_id} {label}: missing field {col}")
                    ok = False
                elif fields[col.lower()] != col:
                    print(f"WARN {source_id} {label}: {col_key} case is "
                          f"'{fields[col.lower()]}' on the live layer (manifest: '{col}')")
            # Pagination guard: count vs maxRecordCount.
            try:
                meta = _get_json(v["feature_server"].rstrip("/") + "/0?f=json")
                cnt = _get_json(v["feature_server"].rstrip("/") +
                                "/0/query?where=1=1&returnCountOnly=true&f=json")
                limit = meta.get("maxRecordCount", 0)
                n = cnt.get("count", 0)
                if limit and n > limit:
                    print(f"WARN {source_id} {label}: {n} features exceed "
                          f"maxRecordCount {limit} (download would paginate)")
            except Exception:  # noqa: BLE001 - count is best-effort
                pass
            if ok:
                print(f"OK   {source_id} {label}")
    return ok


def discover(manifest, write=False):
    """List editions on the portal not already in the manifest. Optionally append."""
    added_any = False
    for source_id, title_fragment in PRODUCTS.items():
        known = {v["label"] for v in manifest.get(source_id, [])}
        items = search_items(title_fragment)
        for item in items:
            title = item.get("title", "")
            # Only UK-wide BGC Feature Services for this product.
            if item.get("type") != "Feature Service":
                continue
            if "BGC" not in title or "UK" not in title:
                continue
            if title_fragment not in title:
                continue
            label, valid_from = parse_title(title)
            if not label or label in known:
                continue
            feature_server = item.get("url", "")
            if not feature_server:
                continue
            code, name = find_code_name_cols(feature_server, source_id)
            if not code or not name:
                print(f"SKIP {source_id} {label}: could not find code/name fields "
                      f"on {feature_server}")
                continue
            row = {
                "label": label,
                "title": title,
                "feature_server": feature_server,
                "code_col": code,
                "name_col": name,
                "valid_from": valid_from,
                "source_file": f"{source_id}_{label}.geojson",
            }
            print(f"NEW  {source_id} {label}:")
            print(json.dumps(row, indent=2))
            if write:
                manifest.setdefault(source_id, []).append(row)
                known.add(label)
                added_any = True
    if write and added_any:
        _save_manifest(manifest)
        print(f"\nManifest updated: {MANIFEST_PATH}")
        print("Review the diff, then run the maintenance steps in "
              "docs/maintenance/updating-ons-boundaries.md.")
    elif not added_any:
        print("No new editions found.")


def search_items(title_fragment):
    """Search the ArcGIS content API for one product's items (paginated)."""
    q = f'owner:{ONS_OWNER} AND title:"{title_fragment}" AND title:"Boundaries UK BGC"'
    items, start = [], 1
    while start != -1:
        params = urllib.parse.urlencode({"q": q, "f": "json", "num": 100, "start": start})
        data = _get_json(f"{ARCGIS_SEARCH}?{params}")
        items.extend(data.get("results", []))
        start = data.get("nextStart", -1)
    return items


def main(argv=None):
    p = argparse.ArgumentParser(description="Maintain the ONS boundary vintage registry.")
    p.add_argument("--validate", action="store_true",
                   help="check every manifest vintage is live and its fields exist")
    p.add_argument("--discover", action="store_true",
                   help="find portal editions not yet in the manifest")
    p.add_argument("--write", action="store_true",
                   help="with --discover, append new editions to the manifest")
    args = p.parse_args(argv)

    manifest = _load_manifest()
    if args.validate:
        return 0 if validate(manifest) else 1
    if args.discover:
        discover(manifest, write=args.write)
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> The field-discovery heuristic (`find_code_name_cols`) and the title parser (`parse_title`) hold the
> ONS-specific assumptions. If ONS changes its naming, these are the two functions to adjust — call that
> out in the runbook (Stage 04).

### B. Make `scripts/` importable for unit tests
The script's pure functions (`parse_title`, and the field-matching logic) are worth unit-testing without
the network. To import the module from a test, ensure there is a way to load it. Simplest: in the new test
file, add the repo root to `sys.path` and import by module path. (Do **not** add an `__init__.py` to
`scripts/` — it is a tools directory, not a package.)

### C. Add tests
Create `tests/test_update_script.py`:
```python
import os
import sys
import importlib.util

import pytest

# Load scripts/update_ons_boundaries.py as a module (it is not an installed package).
_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "update_ons_boundaries.py",
)
_spec = importlib.util.spec_from_file_location("update_ons_boundaries", _SCRIPT)
uob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uob)


# --- pure-logic unit tests (no network) ---

def test_parse_title_december():
    assert uob.parse_title(
        "Local Authority Districts (December 2025) Boundaries UK BGC"
    ) == ("2025-12", "2025-12-01")


def test_parse_title_may():
    assert uob.parse_title(
        "Counties and Unitary Authorities (May 2023) Boundaries UK BGC"
    ) == ("2023-05", "2023-05-01")


def test_parse_title_april():
    assert uob.parse_title(
        "Local Authority Districts (April 2019) Boundaries UK BGC"
    ) == ("2019-04", "2019-04-01")


def test_parse_title_unparseable():
    assert uob.parse_title("Some Lookup Table 2024") == (None, None)


def test_manifest_path_points_at_package():
    assert uob.MANIFEST_PATH.endswith(
        os.path.join("crossroads", "transformers", "ons_boundaries.json")
    )


# --- opt-in network test (skipped unless explicitly enabled) ---

@pytest.mark.skipif(
    os.environ.get("CROSSROADS_NETWORK_TESTS") != "1",
    reason="network test; set CROSSROADS_NETWORK_TESTS=1 to run",
)
def test_validate_against_live_portal():
    manifest = uob._load_manifest()
    # Validate just the newest LAD vintage to keep the call light.
    newest = {"ons_lad": [manifest["ons_lad"][-1]]}
    assert uob.validate(newest) is True
```

Register the marker so pytest does not warn. Append to `pyproject.toml` under
`[tool.pytest.ini_options]`:
```toml
markers = [
  "network: tests that reach the internet (opt in with CROSSROADS_NETWORK_TESTS=1)",
]
```

## Testing & Verification
Core suite stays offline and green:
```bash
source .venv/bin/activate
python -m pytest -q
```
Expected: all green; `test_validate_against_live_portal` shows as **skipped**.

Manually verify the network behaviour once (requires internet):
```bash
CROSSROADS_NETWORK_TESTS=1 python -m pytest -q tests/test_update_script.py
python scripts/update_ons_boundaries.py --validate    # every vintage prints OK
python scripts/update_ons_boundaries.py --discover     # "No new editions found." today
```
`--validate` should print `OK` for all 26 vintages (it may print `WARN` lines for the older lowercase
editions if the live case differs from the manifest — those are informational, not failures, and the exit
code stays 0). `--discover` should report no new editions (the registry is current as of December 2025).

**Stage ship-readiness checklist:**
- [ ] `scripts/update_ons_boundaries.py` exists, stdlib-only, with `--validate` / `--discover` / `--write`.
- [ ] `tests/test_update_script.py` unit-tests `parse_title` and the manifest path; all offline.
- [ ] The `network` marker is registered in `pyproject.toml`; the live test is skipped by default.
- [ ] Manual run: `--validate` exits 0 and prints OK for every vintage; `--discover` finds nothing new.
- [ ] Full `python -m pytest -q` green (live test skipped).

## End State / Handoff
A maintainer can validate the registry and discover new editions with one command. The manifest schema and
the script's flags are now fixed, so Stage 04 can document the exact update procedure against them.

## Failure Modes & Rollback
- **`--validate` reports FAIL for a current vintage:** either the manifest URL/field is wrong (fix the
  manifest) or ONS moved/renamed the service (fix the manifest and note it). A real failure must keep the
  non-zero exit.
- **`--discover` misses or mis-tags an edition:** ONS changed its title format or published a non-UK or
  non-BGC variant matching the filters. Adjust `parse_title` / the filters in `discover()`; never let the
  script silently write a wrong row — `--discover` without `--write` prints first for human review.
- **Search API returns nothing:** the org name or query syntax changed. The runbook (Stage 04) documents
  the manual portal fallback.
- **Rollback:** delete `scripts/update_ons_boundaries.py` and `tests/test_update_script.py`, and revert
  the `markers` addition in `pyproject.toml`.
</content>
