# Update Runbook
> Part of "ONS Boundary Vintage Registry & Update Workflow". You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
Stages 01–03 are done: the manifest holds the full registry, `spatial.py` loads it, the maintenance
script exists and works, and the suite is green. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```

## Objective
Write `docs/maintenance/updating-ons-boundaries.md` — a durable, self-contained **runbook** a future
maintainer follows when ONS releases a new boundary edition (or to re-validate the registry). This is the
"document containing a plan that can be executed in the future to update spatial.py" the user asked for.
It is operational documentation, not an implementation plan, and lives outside `docs/plans/`.

## Implementation Steps

### A. Create the runbook
Create `docs/maintenance/updating-ons-boundaries.md` with the content below. Keep it accurate to what
Stages 01–03 actually delivered (manifest path, schema fields, script flags, test commands). Adjust any
detail that drifted during implementation.

````markdown
# Updating ONS boundary vintages

The LAD and CTYUA boundary transformers in `src/crossroads/transformers/spatial.py` read their list of
published editions ("vintages") from a JSON manifest:

```
src/crossroads/transformers/ons_boundaries.json
```

ONS publishes a new BGC ("Generalised, clipped to coastline") boundary edition on the
[Open Geography Portal](https://geoportal.statistics.gov.uk) roughly twice a year (a May and a December
edition). When that happens, add the edition to the manifest. No Python changes are needed for a normal
update — the registry is data.

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

`valid_to` is **not** stored — `spatial.py` derives it by sorting vintages by `valid_from` and chaining
each edition's `valid_to` to the next edition's `valid_from` (the newest edition is open-ended). So adding
a newer edition automatically closes the previous one's window. The newest edition is what a default
(snapshot) `build()` ingests.

## Routine update (new edition released)

From the repo root with the virtualenv active (`source .venv/bin/activate`):

1. **Discover the new edition automatically:**
   ```bash
   python scripts/update_ons_boundaries.py --discover
   ```
   This searches the ONS portal for LAD/CTYUA "Boundaries UK BGC" editions not yet in the manifest and
   prints a ready-made manifest row for each (FeatureServer URL, field names with correct case, label,
   `valid_from`, `source_file`). Review the output.

2. **Apply it** — either copy the printed row into `ons_boundaries.json` by hand, or let the script append
   it:
   ```bash
   python scripts/update_ons_boundaries.py --discover --write
   ```
   Always review the resulting diff. Confirm: the FeatureServer URL is the **UK-wide BGC** Feature Service
   (not England-only, Map Service, or a different generalisation), and the field names/case match the live
   layer.

3. **Validate the whole registry is live:**
   ```bash
   python scripts/update_ons_boundaries.py --validate
   ```
   Every vintage should print `OK`. `WARN` lines (e.g. live field casing differing from the manifest, or a
   feature count exceeding `maxRecordCount`) are informational — read them and decide if action is needed.
   A `FAIL` (unreachable layer or missing field) must be fixed before committing.

4. **Run the test suite:**
   ```bash
   python -m pytest -q
   ```
   The registry tests confirm the new edition is picked up and the validity windows still chain. The
   offline build test automatically uses the new newest edition's `source_file` (it seeds the committed
   sample fixture under that name), so it keeps passing without a new fixture download.

5. **Commit** the manifest change (ask the repo owner first per project policy).

## Manual fallback (script can't find it / ONS changed something)

The script encodes ONS-specific assumptions in two places — `parse_title()` (title → label/date) and
`find_code_name_cols()` / the filters in `discover()` (which item is the right UK BGC Feature Service, and
which fields are the code/name). If ONS changes its title format, service naming, or field naming, the
script may need a small edit there. To find an edition by hand:

1. Search the content API (replace the product title as needed):
   ```
   https://www.arcgis.com/sharing/rest/search?q=owner:ONSGeography_data%20AND%20title:%22Local%20Authority%20Districts%22%20AND%20title:%22Boundaries%20UK%20BGC%22&f=json&num=100
   ```
   Find the result whose `type` is `Feature Service` and whose `title` matches the edition; its `url` is
   the `feature_server` value.

2. Confirm the field names and case from the live layer:
   ```
   <feature_server>/0?f=json
   ```
   Look in `fields` for the `…CD` (code) and `…NM` (name) entries — copy their exact `name`.

3. Add the row to `ons_boundaries.json` using the format above, then run steps 3–5 of the routine update.

## Notes

- **Field-name casing varies by edition.** Newer editions use uppercase (`LAD25CD`); several older ones
  use lowercase (`lad19cd`, `ctyua18cd`). Store exactly what the live layer reports — the code matches
  case-insensitively but the manifest should be faithful.
- **Not every edition exists for both types.** CTYUA has no May editions for some years and no UK-wide 2016
  edition; that is expected. Validity chaining absorbs the gaps.
- **Download format.** `spatial.py` builds the GeoJSON query URL from `feature_server` + the field names
  (`/0/query?where=1=1&outFields=<code>,<name>&outSR=27700&f=geojson`). Boundaries are EPSG:27700 native;
  the `outSR=27700` keeps them in British National Grid. There is no separate URL to maintain.
- **Tests never hit the network.** Only `scripts/update_ons_boundaries.py` and the opt-in
  `network`-marked test do. Run the live validation manually as part of an update.
````

### B. Cross-check the runbook against reality
After writing, confirm every concrete reference in the runbook is correct:
- The manifest path `src/crossroads/transformers/ons_boundaries.json` exists.
- `scripts/update_ons_boundaries.py` supports `--discover`, `--discover --write`, and `--validate`.
- The schema field names in the table match the manifest exactly.
- The test command and the `network` marker exist as described.

## Testing & Verification
This stage delivers documentation; "tests" are a correctness cross-check rather than `pytest`.

```bash
# The runbook exists.
test -f docs/maintenance/updating-ons-boundaries.md && echo "runbook present"

# Every path/flag the runbook names actually exists.
ls src/crossroads/transformers/ons_boundaries.json
grep -q -- "--discover" scripts/update_ons_boundaries.py && echo "--discover ok"
grep -q -- "--validate" scripts/update_ons_boundaries.py && echo "--validate ok"
grep -q "network:" pyproject.toml && echo "marker ok"

# The suite is still green (nothing in this stage touches code).
source .venv/bin/activate
python -m pytest -q
```
Expected: the runbook is present, every grep matches, and the suite is green.

**Stage ship-readiness checklist:**
- [ ] `docs/maintenance/updating-ons-boundaries.md` exists and is self-contained.
- [ ] It documents the manifest schema, the routine update (discover → write → validate → test → commit),
      and a manual portal fallback.
- [ ] Every path, flag, command, and schema field it names matches what Stages 01–03 delivered.
- [ ] `python -m pytest -q` still green.

## End State / Handoff
The repository contains: a manifest-driven boundary registry populated with the full LAD/CTYUA
back-catalogue, a maintenance script to validate and discover editions, and a runbook describing exactly
how to update the registry when ONS releases new data. The user's two requests are satisfied —
`spatial.py` is updated (via the manifest + loader, plus the script that supports future updates), and the
future-update document exists.

## Failure Modes & Rollback
- **Runbook drifts from reality:** if Stages 01–03 deviated from this plan (different paths, flags, or
  schema), update the runbook to match what was actually built — the cross-check in Step B catches this.
- **Rollback:** delete `docs/maintenance/updating-ons-boundaries.md`. No code is affected.
</content>
