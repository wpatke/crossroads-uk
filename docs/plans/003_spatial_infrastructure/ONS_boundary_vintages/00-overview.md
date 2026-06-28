# ONS Boundary Vintage Registry & Update Workflow — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Move the per-transformer boundary **vintage registry** out of hard-coded Python and into a committed
**JSON manifest**, populate it with **every published ONS BGC edition** for Local Authority Districts
(LAD) and Counties & Unitary Authorities (CTYUA) — chaining each edition's `valid_from`/`valid_to`
validity window by date — add a committed **maintenance script** that discovers and validates editions
against the ONS Open Geography Portal, and write a **runbook document** describing how to update the
registry when ONS releases new data.

## Context & Objective

**What exists today.**
- `src/crossroads/transformers/spatial.py` defines the boundary ingestion: a frozen `Vintage` dataclass
  (`label`, `url`, `source_file`, `code_col`, `name_col`, `valid_from`, `valid_to`), an abstract
  `_BoundaryTransformer` (shared download / `ST_Read` / bronze→silver→gold / ledger / quality logic), and
  two concrete transformers `LADBoundaryTransformer` (`source_id = "ons_lad"`) and
  `CTYUABoundaryTransformer` (`source_id = "ons_ctyua"`).
- Each transformer currently hard-codes a **single** vintage (December 2024) as a `Vintage(...)` tuple,
  with the download URL built into two module constants (`_LAD_2024_URL`, `_CTYUA_2024_URL`) pointing at
  the ONS ArcGIS **FeatureServer GeoJSON query endpoint**
  (`…/FeatureServer/0/query?where=1%3D1&outFields=<code>,<name>&outSR=27700&f=geojson`).
- Vintage selection is **snapshot-only**: `_BoundaryTransformer._vintages_for()` returns
  `(self.vintages[-1],)` — i.e. the newest vintage (vintages are ordered newest-last). Multi-vintage
  "temporal" loading is a separate, not-yet-implemented concern and is **out of scope here**.
- `extract(cache_dir)` downloads each wanted vintage's `url` to `cache_dir/<source_file>` with
  `urllib.request.urlretrieve`, **skipping the download if the file already exists** (this is how the
  offline tests work — the cache is pre-seeded with committed fixtures).
- `transform_and_load(con, cache_dir)` builds bronze (`ST_Read` of each vintage file, `UNION ALL`,
  tagged with `vintage`), silver (composite `source_row_key = area_code || '|' || vintage`, typed
  `area_code`/`area_name`, `geom_valid`, `valid_from`/`valid_to` via a CASE map), the ledger rows for
  invalid geometry, conservation accounting, and the gold `_clean` view.
- `tests/test_spatial.py` runs fully **offline** against committed GeoJSON fixtures under
  `tests/fixtures/ons/lad_2024/lad_sample.geojson` and `tests/fixtures/ons/ctyua_2024/ctyua_sample.geojson`.
  `_seed_cache` copies those fixtures into the build cache so `extract()` finds them and never downloads.
- `pyproject.toml`: package `crossroads-uk`, Hatchling, wheel target `packages = ["src/crossroads"]`,
  runtime dep `duckdb>=1.0`, dev dep `pytest`. **No `requests`/`geopandas`/`fiona` — stdlib only.**

**The problem.** ONS publishes a new BGC boundary edition roughly twice a year (a May and a December
edition), each on its own ArcGIS FeatureServer with its own service name and its own attribute field
names (and the field-name **casing changes** between older and newer editions). Today, adding an edition
means editing Python. The registry should instead be **data**, populated with the full back-catalogue
and trivially extendable when ONS releases the next edition.

**The goal.** After this plan:
1. The vintage registry lives in a committed JSON manifest that `spatial.py` loads at import time.
2. The manifest contains every verified LAD edition (15) and CTYUA edition (11), with correct field
   names (exact case), FeatureServer URLs, effective dates, and chained validity windows.
3. A committed maintenance script (`scripts/update_ons_boundaries.py`) can **validate** the manifest
   against the live ONS portal and **discover** newly-published editions not yet in the manifest.
4. A runbook (`docs/maintenance/updating-ons-boundaries.md`) tells a future maintainer exactly how to use
   the script and update the manifest — and notes the script may itself need adjusting if ONS changes its
   naming or API.
5. The default `build()` behaviour is **unchanged except for the latest snapshot**: snapshot mode still
   loads exactly the newest edition (now December 2025), and all existing tests still pass offline.

## Approach / Architecture

### Registry as a JSON manifest (locked decision)
A new file `src/crossroads/transformers/ons_boundaries.json` holds the registry keyed by `source_id`:

```json
{
  "ons_lad":  [ { vintage }, { vintage }, ... ],
  "ons_ctyua": [ { vintage }, ... ]
}
```

Each **vintage object** has these fields (all strings):

| Field            | Meaning                                                                              |
|------------------|-------------------------------------------------------------------------------------|
| `label`          | Unique, sortable edition id `"YYYY-MM"`, e.g. `"2025-12"`, `"2019-04"`.              |
| `title`          | The exact ONS dataset title (human reference; used by the discovery script).         |
| `feature_server` | The ArcGIS REST **FeatureServer base URL** (no `/0/query…` suffix).                  |
| `code_col`       | The area-code attribute field, **exact case** (e.g. `LAD24CD`, `lad19cd`).           |
| `name_col`       | The area-name attribute field, **exact case** (e.g. `LAD24NM`, `lad19nm`).           |
| `valid_from`     | `"YYYY-MM-DD"`, the first day of the edition's month (effective date).               |
| `source_file`    | The cache filename `extract()` reads/writes, e.g. `"ons_lad_2025-12.geojson"`.       |

`valid_to` is **not stored** — it is **derived** by `spatial.py` at load time (see below), so adding an
edition never requires editing another edition's row.

**Why JSON, not inline constants or a database.** The user's explicit requirement is "this will have to
be updated when new data is released." A committed data file makes an update a data edit (or an
auto-generated patch from the script), reviewable in a diff, with zero Python changes. JSON is in the
stdlib, requires no dependency, and stays deterministic (a committed file → identical builds). Rejected:
*inline Python `Vintage` tuples* (every update is a code edit; the script would have to emit and a human
paste code); *an external network call at build time* (breaks determinism and offline tests); *a
separate DB table* (over-engineered for ~26 static rows).

### Loader in `spatial.py` (locked decision)
`spatial.py` gains a small `_load_vintages(source_id)` function that:
1. Reads `ons_boundaries.json` from **next to the module** (`os.path.dirname(__file__)`), so it works in
   both source checkouts and installed wheels without `importlib.resources` ceremony.
2. **Sorts the vintages ascending by `valid_from`** so `vintages[-1]` is always the newest edition (the
   snapshot). The manifest may be written in any order; the loader imposes the order.
3. **Chains `valid_to` by date**: for each vintage, `valid_to = next vintage's valid_from`; the newest
   vintage gets `valid_to = None` ("current"). This implements "include all editions, chain by date" —
   each edition is valid from its effective date until the next edition supersedes it. Gaps in the ONS
   release cadence (e.g. no 2017 edition, no May 2024 CTYUA) are simply absorbed into the preceding
   window, which is the correct behaviour for later point-in-time joins.
4. **Builds the download `url`** from `feature_server` + `code_col`/`name_col` using
   `urllib.parse.urlencode`, producing the same GeoJSON query endpoint shape the code uses today:
   `<feature_server>/0/query?where=1%3D1&outFields=<code>,<name>&outSR=27700&f=geojson`.
5. Returns a `tuple[Vintage, ...]` — the exact type the rest of `spatial.py` already consumes.

The two concrete classes change from a hard-coded `vintages = (Vintage(...),)` to
`vintages = _load_vintages("ons_lad")` / `_load_vintages("ons_ctyua")`. **Nothing else in
`_BoundaryTransformer` changes** — `extract`, `transform_and_load`, `_validity_case_sql`,
`_derive_silver_and_ledger`, and `quality_spec` all keep working because the `Vintage` shape is identical.
The two module URL constants (`_LAD_2024_URL`, `_CTYUA_2024_URL`) are deleted (the loader builds URLs).

### Snapshot behaviour is preserved
`_vintages_for()` still returns `(self.vintages[-1],)`. The only observable change after population is
that the newest vintage is now **December 2025**, not December 2024. The offline tests are made
robust to "which edition is newest" by seeding the committed fixture under the **newest vintage's**
`source_file` name (read from the transformer), so they never break when a new edition is appended.

### The maintenance script (`scripts/update_ons_boundaries.py`)
A standalone, stdlib-only CLI (run by the maintainer, **not** part of `build()` or the test suite). Two
modes:
- `--validate` — for every vintage in the manifest, GET `<feature_server>/0?f=json` and confirm the
  layer is reachable and that `code_col` and `name_col` exist in its `fields` list (reporting the exact
  case found); also GET `…/0/query?where=1=1&returnCountOnly=true&f=json` and warn if the count exceeds
  the layer's `maxRecordCount` (a pagination risk). Exit non-zero if any vintage fails.
- `--discover` — query the ArcGIS content search API for items owned by `ONSGeography_data` whose title
  matches the boundary product (LAD / CTYUA) and "Boundaries UK BGC", list editions **not already in the
  manifest**, fetch each one's fields, and print proposed manifest rows (with `--write`, append them).

The script encodes the ONS-specific knowledge (search org, title patterns, query shape) so the runbook
can simply say "run this." It is acknowledged that ONS may change naming/API; the runbook flags that the
script may need adjusting, and the script is written defensively (clear errors, never a silent pass).

### The runbook (`docs/maintenance/updating-ons-boundaries.md`)
A durable operational document (NOT an implementation plan): when ONS releases a new edition, how to run
the script, verify its output, add the row to the manifest, confirm the new edition becomes the snapshot,
and run the test suite. Includes the manual ArcGIS-portal fallback (exact search/REST URLs) in case the
script needs fixing.

## Cross-Cutting Constraints (every stage follows these)
- **No new dependencies.** `duckdb` + `pytest` only. The manifest uses stdlib `json`; downloads/HTTP use
  stdlib `urllib`. Do not add `requests`, `geopandas`, `fiona`, `pyogrio`.
- **One transformer module.** All boundary *transformer* code stays in
  `src/crossroads/transformers/spatial.py`. The manifest is data; the maintenance script is a dev tool
  under `scripts/`; the runbook is docs.
- **Offline, deterministic tests.** The test suite never touches the network. Any network behaviour
  (script validation/discovery) is exercised only by an **opt-in / network-marked** test or not at all.
  Same committed manifest + same fixtures → identical tables.
- **Behaviour-preserving refactor first.** Stage 01 introduces the manifest with only the *existing*
  December 2024 data and must leave every current test green before any new editions are added.
- **Field-name casing is data, not convention.** Store `code_col`/`name_col` exactly as ONS publishes
  them (newer editions UPPERCASE, several older ones lowercase). Do not normalise in the manifest.
- **Provider-plugin purity.** `client.py` and `registry.py` are not touched. The manifest names sources,
  but it is loaded by the source-specific module, not the engine.
- **Keep-in-place / quality model unchanged.** No change to bronze→silver→gold, the `geom_valid`
  dimension, the ledger, conservation accounting, or the quality engine. This plan only changes *which
  vintages* and *how the registry is stored*.
- **Git discipline.** Never stage or commit without explicit user permission (CLAUDE.md).
- **Style.** Plain-language comments, simple code; match the comment density of the existing `spatial.py`.

## Stage Map (sequential — do in order)

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Manifest-driven registry (behaviour-preserving) | Add `ons_boundaries.json` containing only the current Dec 2024 LAD+CTYUA vintages; add `_load_vintages()`; switch both classes to load from it; delete the two URL constants. | `spatial.py` builds its `vintages` from the JSON manifest; the registry is identical to today (one Dec 2024 vintage each); **all existing `tests/` pass offline**; packaging includes the JSON. | (current `spatial.py`) | `01-manifest-refactor.md` |
| 02 | Populate full vintage registry | Replace the manifest with **all 15 LAD + 11 CTYUA** editions, labelled `YYYY-MM`, validity chained by date; make tests robust to "newest edition" and add manifest-content tests. | The manifest holds the full back-catalogue; `vintages[-1]` is Dec 2025 for both types; validity windows chain correctly; the snapshot `build()` ingests Dec 2025 offline; new manifest tests + all existing tests pass. | Stage 01 | `02-populate-vintages.md` |
| 03 | Maintenance script | Add `scripts/update_ons_boundaries.py` (`--validate` / `--discover [--write]`), stdlib-only, operating on the manifest; add an opt-in network test. | Running the script `--validate` confirms every manifest vintage against the live portal; `--discover` lists/append new editions; the core suite stays offline and green. | Stage 02 | `03-maintenance-script.md` |
| 04 | Update runbook document | Write `docs/maintenance/updating-ons-boundaries.md`: the end-to-end procedure to update the registry on a new ONS release, referencing the script and manifest, with a manual fallback. | A self-contained runbook exists and accurately matches the manifest schema, script flags, and test commands delivered in 01–03. | Stages 01–03 | `04-update-runbook.md` |

## Global Testing & Ship
All tests are **real and runnable** and stay **offline**. From the repo root with the venv active:

```bash
source .venv/bin/activate
python -m pytest -q
```

Expected at the end of **every** stage: all tests pass, zero failures/errors (including the pre-existing
boundary, quality, client, registry, and package tests).

- **Stage 01** ships the refactor: the manifest drives the registry with no behavioural change — proven
  by the unchanged, still-green `tests/test_spatial.py`.
- **Stage 02** ships the full registry: a real offline `build()` ingests the newest edition end-to-end
  (bronze→silver→gold, EPSG:27700 verified, quality invariants pass), plus new pure-data tests assert the
  manifest has the expected editions, the newest is Dec 2025, and validity windows chain with no overlap
  or gap in ordering.
- **Stage 03** ships the maintenance tool: unit tests cover the script's pure logic (title→`label`/
  `valid_from` parsing, field-presence check) using **local fixture JSON**, and a single
  **network-marked, opt-in** test exercises `--validate` against the live portal (skipped by default).
- **Stage 04** ships the runbook: verified by a documentation cross-check (every command, path, flag, and
  schema field named in the runbook exists exactly as delivered).

## Open Questions / Risks
- **Newest edition has no committed fixture.** The committed fixtures are Dec 2024 geometry. Rather than
  download a fresh Dec 2025 sample, the offline tests seed the existing fixture under the **newest
  vintage's** `source_file` name (real ONS BGC geometry is fine as a structural stand-in — the tests
  assert counts/envelope/invariants, not edition identity). This keeps execution fully offline. Documented
  in Stage 02.
- **ONS service-name / field-name churn.** Service names are inconsistent (V2/V3 suffixes, `DEC` vs
  `December`, `GCB` tokens) and field casing varies by year. These are captured per-vintage in the
  manifest exactly as verified; the discovery script must not assume a single naming pattern.
- **FeatureServer pagination.** Default `maxRecordCount` is 2000; LAD (~361) and CTYUA (~218) fit one
  request, so the single-request download stands. The `--validate` mode warns if a future edition's count
  exceeds the limit. If that ever happens, paging is a follow-up, not part of this plan.
- **CTYUA has fewer editions than LAD.** No CTYUA BGC exists for May 2024, May 2025, or May 2022, and Dec
  2016 CTYUA is England-&-Wales only (no UK extent) — so it is **excluded** (UK-wide only). This is
  expected, not a defect; the validity chaining absorbs the gaps.
- **Packaging the JSON.** The wheel must ship `ons_boundaries.json`. Stage 01 verifies inclusion and adds
  Hatchling config only if the default build omits it.
- **Temporal multi-vintage loading is out of scope.** This plan only expands and re-homes the registry and
  keeps snapshot behaviour. Any `boundary_mode="temporal"` work is separate.
</content>
</invoke>
