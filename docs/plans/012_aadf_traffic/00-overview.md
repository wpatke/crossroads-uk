# DfT AADF Traffic Counts — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Add DfT Annual Average Daily Flow (AADF) traffic counts as a new `aadf` data source, stamp
each count point with ONS area codes, and showcase the resulting "collisions per million
vehicle-km" risk metric with a verified README example and a real integration test.

## Context & Objective

**What exists.** Crossroads-UK is a working pipeline (spec: `docs/spec.md`) with five
transformers in `src/crossroads/transformers/`: ONS LAD/CTYUA boundaries (`spatial.py`,
always-on infrastructure), STATS19 collisions (`stats19.py`), ERA5-Land weather
(`weather.py`), and GOV.UK bank holidays (`bank_holidays.py`). The registry
(`src/crossroads/registry.py`) auto-discovers transformers; the wizard
(`src/crossroads/console.py`) builds its dataset menu from `Registry().selectable()`; the
quality engine (`src/crossroads/quality.py`) enforces conservation, flag/ledger agreement,
and reject-rate invariants on every build (spec §9).

**What this plan adds.**

1. A new `aadf` transformer ingesting the DfT national "AADF by count point" dataset —
   one zipped CSV (≈600,000 rows, years 2000–2025, one row per road link per year) from
   `https://storage.googleapis.com/dft-statistics/road-traffic/downloads/data-gov-uk/dft_traffic_counts_aadf.zip`
   (listed on https://roadtraffic.dft.gov.uk/downloads). Bronze → silver → gold with the
   standard keep-in-place quality model.
2. LAD/CTYUA area codes stamped onto each count point via the existing point-in-polygon
   pattern (the same `UPDATE … FROM (… ST_Contains …)` shape as
   `Stats19Transformer._spatial_stamp`, `src/crossroads/transformers/stats19.py`),
   **honouring the build's `boundary_mode`** exactly as stats19 does — snapshot uses the
   latest boundary vintage; temporal uses the vintage in force for each count's year
   (keyed on a mid-year date, see the Temporal note under Cross-Cutting Constraints). This
   keeps a temporal-mode risk join consistent: a 2015 collision and a 2015 count both
   resolve to 2015's boundaries, so they carry the same `lad_code` and join cleanly.
3. A **risk-metric showcase**: because collisions and count points both carry a road
   identity (STATS19 `first_road_class`/`first_road_number`; AADF `road_name`) and both
   get LAD codes, "collisions per million vehicle-km, per road, per local authority" is a
   plain SQL join — no point-to-line snapping. This query goes in the README **with real
   output**, and an offline integration test proves the same join shape end-to-end on
   committed real fixture data.
4. Documentation: `docs/schema.md` (+ `SCHEMA_VERSION` bump), `docs/data-sources.md`,
   `README.md`, `CHANGELOG.md`.

**Why road-identity join, not spatial snapping.** Snapping collision points to road-link
geometry needs a road-network dataset this project does not ingest, plus a defensible
tolerance/tie-break methodology. A wrong snap silently misattributes the denominator —
exactly the failure mode spec §2 exists to prevent. Joining on (road name × LAD) uses
only facts both sources state directly. Rejected alternatives:

- *Nearest-count-point snap* — ambiguous on minor roads, invents precision; rejected.
- *Per-LA or per-region CSV downloads* — hundreds of files to manage; the single national
  zip is one atomic artifact; rejected.
- *Filtering AADF to the build's `years`* — the file is one artifact covering all years;
  slicing it would discard denominator data for no size benefit (≈600k rows is trivial
  for DuckDB). We land the full history; `is_active` still gates on `years` only so that
  boundary-only builds don't trigger a 40 MB download.

## Cross-Cutting Constraints

- **No new dependencies.** Download with `urllib.request`, unzip with stdlib `zipfile`.
- **Keep-in-place quality model** (spec §9): bronze is a faithful `all_varchar` copy;
  silver is 1:1 with bronze; failed validations are flagged + ledger-logged, never
  deleted; `record_source_rows` feeds the conservation invariant.
- **EPSG:27700 only** (spec §3A): AADF eastings/northings are already British National
  Grid — `ST_Point` directly, no reprojection, R-Tree index on silver geometry.
- **Deterministic** (spec §2): no randomness; stable `source_row_key`; registry ordering
  handled via `depends_on` (see Stage 01 — alphabetically `aadf` would otherwise run
  *before* the boundary transformers it stamps against).
- **Simple, commented code**: the maintainer reads every line before commit. Mirror the
  style of `bank_holidays.py` (the most recent, smallest transformer).
- **Menu ripple**: a new `user_selectable` source renumbers the wizard menu and activates
  on any `years=`-bearing build without a `datasets` list. Known hardcoded pick sites:
  `tests/test_console.py:306` (`"3"` = stats19) and `tests/test_schema_doc.py:73`
  (`"2-3"`). Every cache-seeding helper must also seed the AADF fixture.
- **Boundary-mode consistency (Temporal)**: AADF area stamping honours the build's
  `boundary_mode` the same way stats19 does — captured in `extract()` as
  `self._boundary_mode` and read back in the stamp method (mirror
  `Stats19Transformer`, `src/crossroads/transformers/stats19.py:159` and `:709`). The
  temporal predicate needs a *date*, but AADF only has a *year*, so we look up the vintage
  using **`make_date(year, 7, 1)`** — 1 July, which sits after the usual 1 April boundary
  changeover and so picks the vintage in force for most of the count year. This is a
  documented approximation, exact except in the single year a boundary actually changed.
  Snapshot mode is unchanged (`valid_to IS NULL`).
- **Committed docs hygiene**: plan and code comments must not reference internal
  planning artifacts (enforced by `tests/test_no_internal_refs.py` for `src/`).

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | AADF source | Real-data fixture, `transformers/aadf.py` (bronze/silver/gold, **boundary-mode-aware** area stamping, R-Tree, quality spec), all test-suite ripple fixes | `aadf` builds offline from fixtures in both boundary modes; full suite (incl. `-m integration`) green | — | `01-aadf-source.md` |
| 02 | Risk showcase test | Offline integration test running the README-shape risk query on fixture data; opt-in live download test; real-build runbook producing the README's output table | Query verified end-to-end offline; real national build performed and output captured | 01 | `02-risk-showcase-test.md` |
| 03 | Wizard temporal warning | Wizard shows a mid-year-approximation warning + `Y/n` confirm (default yes) only when the user picks **temporal AND aadf**; `n` aborts the wizard | Warning fires on temporal+aadf only; Enter proceeds; `n` aborts; console tests green | 01 | `03-wizard-temporal-warning.md` |
| 04 | Docs & README | `SCHEMA_VERSION` 2→3, `schema.md`, `data-sources.md`, README showcase section with real output, CHANGELOG, boundary-mode caveat | All docs accurate; schema drift guard covers `aadf`; suite green | 01, 02, 03 | `04-docs-readme.md` |

## Global Testing & Ship

- **Primary proof (Stage 02):** `pytest -m integration` builds a real database offline
  from committed real DfT/ONS fixture data (STATS19 Hartlepool collisions on the A689 and
  A179 + real AADF count points for those same roads) and asserts the README's risk query
  returns the expected per-road, per-LAD figures.
- **Live proof (Stage 02, opt-in):** a `@pytest.mark.live` test downloads the real
  national zip and sanity-checks shape/row count — run deliberately before release with
  `CROSSROADS_RUN_LIVE=1 pytest -m live`.
- **Boundary-mode proof (Stage 01):** a fast unit test drives `_stamp_area_codes` against
  a hand-built two-vintage boundary table (distinct area codes per window) and asserts
  temporal mode stamps each AADF row with the vintage matching its `year`, while snapshot
  stamps every row with the latest vintage. This proves the mid-year predicate without
  needing real multi-vintage ONS fixtures.
- **Wizard-gate proof (Stage 03):** console tests assert the warning + confirm appears
  only on temporal+aadf, that Enter (default yes) proceeds, and that `n` aborts.
- **Standing gates:** the spec §9 invariants (conservation, flag/ledger, reject ceiling)
  run inside every build the tests perform; the Stage 04 schema drift guard keeps
  `docs/schema.md` in lockstep with the real `aadf` columns.
- Each stage leaves `python -m pytest` and `python -m pytest -m integration` green.

## Open Questions / Risks

- **Zip member name unknown.** The national zip's internal CSV filename is unverified.
  Stage 01's `extract()` therefore extracts *the single `.csv` member whatever its name*
  and writes it to a canonical cache filename. If the zip ever contains multiple CSVs,
  fail loudly.
- **National header may differ from the per-LA sample.** Column names below were read
  from a real per-LA AADF file (31 columns). Stage 01 verifies the national header
  before finalizing the silver SELECT; the bronze layer is header-driven
  (`read_csv(..., all_varchar=true)`) so it adapts automatically, and a missing column
  in the silver SELECT fails loudly at build time.
- **Fixture points must fall inside the committed sample polygons.** The ONS fixture
  boundaries are generalised; Stage 01 verifies each chosen fixture count point stamps
  to `E06000001` (Hartlepool) and swaps points if any miss.
- **Temporal boundary mode:** AADF stamping honours the build's `boundary_mode` (Stage 01).
  Snapshot uses the latest vintage; temporal resolves each count to the vintage in force at
  a **mid-year (1 July) date** derived from its `year`. The only real approximation is the
  single transition year in which a boundary changed (the annual average straddles both
  districts); this is documented in Stage 04, not hidden. Chosen over snapshot-only because
  snapshot mis-stamps *every* historical year for a reorganised area and silently
  disagrees with temporal-mode stats19 stamping — corrupting the risk join for exactly the
  areas where boundaries moved.
- **Out of scope:** PyPI publishing (next plan), population/demographic sources
  (deferred), any road-network geometry or point-to-line snapping.
