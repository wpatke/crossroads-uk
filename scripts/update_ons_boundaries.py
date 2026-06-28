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

# Which products we manage, keyed by manifest source_id.
# The title fragment is used both to search and to recognise an edition's product.
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
            # Pagination guard: warn if feature count exceeds the layer limit.
            try:
                meta = _get_json(v["feature_server"].rstrip("/") + "/0?f=json")
                cnt = _get_json(
                    v["feature_server"].rstrip("/") +
                    "/0/query?where=1=1&returnCountOnly=true&f=json"
                )
                limit = meta.get("maxRecordCount", 0)
                n = cnt.get("count", 0)
                if limit and n > limit:
                    print(f"WARN {source_id} {label}: {n} features exceed "
                          f"maxRecordCount {limit} (download would paginate)")
            except Exception:  # noqa: BLE001 - count check is best-effort
                pass
            if ok:
                print(f"OK   {source_id} {label}")
    return ok


def search_items(title_fragment):
    """Search the ArcGIS content API for one product's items (paginated)."""
    q = f'owner:{ONS_OWNER} AND title:"{title_fragment}" AND title:"Boundaries UK BGC"'
    items, start = [], 1
    while start != -1:
        params = urllib.parse.urlencode(
            {"q": q, "f": "json", "num": 100, "start": start}
        )
        data = _get_json(f"{ARCGIS_SEARCH}?{params}")
        items.extend(data.get("results", []))
        start = data.get("nextStart", -1)
    return items


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
        print("Review the diff, then follow docs/maintenance/updating-ons-boundaries.md.")
    elif not added_any:
        print("No new editions found.")


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
