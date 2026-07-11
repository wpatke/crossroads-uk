# Stage 03 — User-Facing Docs (README, methodology, CITATION)
> Part of Release Preparation (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Depends on Stage 01 (git-derived version; first release is `0.9.0`) and Stage 02 (`docs/data-sources.md` exists). Verify:
```bash
cd /Users/will/Documents/Code/Crossroads
python -c "import crossroads; print(crossroads.__version__)"   # expect 0.9.0 once tagged (else a dev value)
ls docs/data-sources.md CHANGELOG.md docs/spec.md
ls docs/AI_DISCLOSURE.md    # still UPPER-CASE at the start of this stage; Step 0 renames it
ls docs/methodology.md CITATION.cff 2>/dev/null || echo "not created yet (expected)"
```
If Stage 01/02 outputs are absent, create the files they reference as you go and note the deviation, but prefer running the stages in order.

## Objective

Turn the 20-line "early foundation" README into a real release README, add a researcher-facing `docs/methodology.md` that distils (not duplicates) `docs/spec.md`, and add a `CITATION.cff` so GitHub shows a "Cite this repository" button. First (Step 0) rename `docs/AI_DISCLOSURE.md` → `docs/ai-disclosure.md` so the README's link to it is valid the moment it is written.

## Implementation Steps

**Step 0 — Rename the AI-disclosure file to lower-case (do this FIRST, before writing the README).**
The README written in Step 1 links `docs/ai-disclosure.md`, so the file must already carry that name —
otherwise the link is broken on case-sensitive filesystems (Linux/CI). Rename it and fix the two links
inside `spec.md` in the same step, so no reference is left dangling:
```bash
cd /Users/will/Documents/Code/Crossroads
mv docs/AI_DISCLOSURE.md docs/ai-disclosure.md      # plain mv, NOT git mv (git mv stages the change)
```
> Use plain `mv`, **not** `git mv` — staging without explicit user permission is forbidden
> (CLAUDE.md). The user stages/commits the rename themselves.

Then update the two links inside `docs/spec.md` (`spec.md:9` and `:269`), which currently read
`[AI_DISCLOSURE.md](AI_DISCLOSURE.md)`. The target is now `ai-disclosure.md` (same `docs/` directory),
so change each to:
```markdown
[AI_DISCLOSURE.md](ai-disclosure.md)
```
(the link *text* may stay `AI_DISCLOSURE.md` or become "AI disclosure"; only the `(target)` must
change). After this, `grep -n "](AI_DISCLOSURE.md)" docs/spec.md` returns nothing. The stale §7
*blueprint tree* in `spec.md` (a `text` diagram, not a link) is corrected separately in Stage 04.

**Step 1 — Rewrite `README.md`.** Replace the whole file with:

````markdown
# Crossroads-UK

A reproducible Python pipeline that downloads, cleanses, and unifies UK road-safety
(DfT STATS19), meteorological (Copernicus ERA5-Land), and ONS boundary data into a
single local DuckDB database — built on the fly from version-controlled code.

Crossroads-UK does not ship a pre-baked database. You choose what to build; the pipeline
fetches the raw public sources and compiles a DuckDB file on your machine, so the result
is fresh, reproducible, and exactly scoped to your query.

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .              # add ".[weather]" for the ERA5-Land weather source
```

## Usage

Run the interactive wizard:

```bash
crossroads
```

It asks for an output database path, which datasets to build, which years to ingest, and
the boundary mode, then compiles the database. Or drive it from Python:

```python
import crossroads as cr

client = cr.init_engine(database_path="local_analytics.db")
client.build(datasets=["stats19"], years=[2022, 2023, 2024], boundary_mode="snapshot")
client.close()
```

The **weather** source additionally needs the `weather` extra installed and a free
Copernicus CDS API key — the build prints setup steps if it is missing.

## What you get

- A **keep-in-place** data model (bronze/silver/gold): raw rows are never deleted; records
  that fail validation are flagged with a reason in a queryable `data_quality_log`.
- Spatial standardisation to the British National Grid (EPSG:27700) with R-Tree indices.
- Snapshot or temporally-sliced boundary joins.
- Build-time conservation invariants that halt the build if any row goes unaccounted for.

See **[docs/methodology.md](docs/methodology.md)** for how the data is joined, converted, and
quality-flagged, and **[docs/spec.md](docs/spec.md)** for the full product definition.

## Data & licences

Crossroads-UK downloads data directly from DfT, ONS, and Copernicus. You are responsible
for honouring each source's licence when you publish. See **[docs/data-sources.md](docs/data-sources.md)**
for each source, its licence, and the exact attribution to reproduce.

## Citing

If you use Crossroads-UK in research, please cite it — see **[CITATION.cff](CITATION.cff)**
(GitHub's "Cite this repository" button) or the `[0.9.0]` entry in
**[CHANGELOG.md](CHANGELOG.md)**.

## Development

```bash
pip install -e ".[dev]"
python -m pytest                 # fast, offline suite
python -m pytest -m integration  # slow / networked tests (run deliberately)
```

## Licence & AI disclosure

Crossroads-UK is released under the [MIT Licence](LICENSE). This project is **not
affiliated with or endorsed by** the Department for Transport, the Office for National
Statistics, Ordnance Survey, or Copernicus/ECMWF. AI usage in development is documented in
**[docs/ai-disclosure.md](docs/ai-disclosure.md)**.
````
> Keep every relative link accurate — the Stage 04 link-integrity test will fail the build if any target is missing. From the README (at repo root) these link targets must all exist: `docs/spec.md`, `docs/methodology.md`, `docs/data-sources.md`, `docs/ai-disclosure.md`, `CITATION.cff`, `CHANGELOG.md`, `LICENSE`. (`[weather]` is the pip extra, not a link.)

**Step 2 — Create `docs/methodology.md`** (in `docs/`, alongside `spec.md`). A concise, researcher-facing "how the data was made" page that *links into* `spec.md` rather than repeating it. **Because this file lives in `docs/`, it links to `spec.md` as a bare sibling name, and to root files with `../` (e.g. `../CHANGELOG.md`) and to source with `../src/…`:**

````markdown
# Methodology

How Crossroads-UK turns raw public datasets into a single analysable database. This is a
summary for researchers; the authoritative detail lives in [spec.md](spec.md).

## Sources

| Source | Publisher | Native format | Native CRS / time |
|--------|-----------|---------------|-------------------|
| STATS19 collisions/vehicles/casualties | DfT | CSV | EPSG:27700 / UK local time |
| LAD & CTYUA boundaries | ONS | Shapefile (BGC) | EPSG:27700 |
| ERA5-Land weather | Copernicus | NetCDF | EPSG:4326 / UTC, hourly |

## Spatial join

All geometries are reprojected **once at ingestion** to the British National Grid
(EPSG:27700) and never at query time; R-Tree indices are built on disk. Collision points
are matched to boundary polygons by point-in-polygon. Coordinate sentinels (`0`/`-1`,
DfT's "data missing or out of range") set `geom = NULL` and `geom_valid = FALSE` and are
logged — never deleted. Detail: [spec.md §3A](spec.md), [spec.md §5](spec.md).

## Temporal alignment

Every record carries a `*_local` column in UK civil time (`Europe/London`). Sources that
natively record a true instant (ERA5-Land, UTC) also carry `*_utc`; a `*_utc` is never
reconstructed from local time. Weather is matched to collisions at the hourly grain.
Detail: [spec.md §3B](spec.md).

## Weather value handling

ERA5-Land 2 m temperature (Kelvin) and total precipitation (metres, an hourly
accumulation) are ingested; precipitation is converted to millimetres and stored as
published (no de-accumulation — a documented simplification). Sea cells outside the land
model carry `NULL` metrics by domain, kept in place. Detail:
[spec.md §5 Phase 4](spec.md).

## Boundary drift

Two modes: **snapshot** evaluates every event against the latest ONS boundaries;
**temporal** appends `valid_from`/`valid_to` so an event maps to the boundaries that
existed on its date. Detail: [spec.md §3C](spec.md).

## Data quality (keep-in-place)

No source row is ever deleted. Bad values are nulled in the typed "silver" columns, the
raw value is preserved, a boolean flag records the failure, and a `data_quality_log` row
explains it. "Gold" views filter to valid-only. Three invariants are asserted on every
build — conservation (`source == clean + quarantined`), flag/ledger agreement, and a
reject-rate ceiling — and the build halts if any row is unaccounted for. Full model:
[spec.md §9](spec.md).

## Reproducibility

A given Crossroads-UK version, with the same parameters and the same pinned source
vintages, produces a structurally identical database. Reference tables (STATS19 codebook,
column manifest, ONS boundary manifest) are version-pinned and regenerable by committed
scripts. See [../src/crossroads/reference/README.md](../src/crossroads/reference/README.md).

### Tested with (v0.9.0)

Reproducibility depends on the runtime stack, and `duckdb>=1.5` is a floating floor. To
reproduce the exact `0.9.0` behaviour (notably coordinate reprojection, which rides on
DuckDB Spatial + PROJ), pin these versions:

| Component | Tested version |
|-----------|----------------|
| Python | 3.11 and 3.12 (authored on 3.12.13) |
| DuckDB | 1.5.4 |
| xarray / cdsapi / netCDF4 (weather extra) | *record the installed versions when the weather extra is set up* |

Any change to these — or to ingestion behaviour — is a new release (see
[../CHANGELOG.md](../CHANGELOG.md)).
````
> Fill the weather-extra row with the actual versions from `pip show xarray cdsapi netCDF4`
> after installing `.[weather]`; leave the italic placeholder only if the extra is not part
> of the release you are cutting.
> Each `[spec.md §N](spec.md)` links to the file (a sibling in `docs/`); the `§N` is descriptive text. Stage 04's strengthened link test verifies both that `spec.md` resolves **and** that every cited `§N` is a real section number in `spec.md`, so a spec renumber can't silently orphan these.

**Step 3 — Create `CITATION.cff`** at the repo root (Citation File Format 1.2.0 — this filename must stay exactly `CITATION.cff` at the root; GitHub only recognises that):
```yaml
cff-version: 1.2.0
message: "If you use Crossroads-UK in your research, please cite it as below."
title: "Crossroads-UK: A Reproducible Pipeline for Unifying UK Road-Safety, Weather, and Boundary Data"
abstract: >-
  Crossroads-UK is an open-source Python pipeline that downloads, cleanses, and unifies
  UK road-safety (DfT STATS19), meteorological (Copernicus ERA5-Land), and ONS boundary
  data into a single reproducible local DuckDB database.
type: software
authors:
  - given-names: Will
    family-names: Patke
    # orcid: "https://orcid.org/0000-0000-0000-0000"   # add if available
version: "0.9.0"
date-released: "2026-07-10"
license: MIT
repository-code: "https://github.com/wpatke/crossroads-uk"
url: "https://github.com/wpatke/crossroads-uk"
keywords:
  - UK road safety
  - STATS19
  - DuckDB
  - reproducible research
  - ETL
```
> **Name confirmed:** the author is **Will Patke** (`given-names: Will`, `family-names: Patke`) — use exactly this. Add an ORCID only if the user supplies one (leave the commented placeholder otherwise). `date-released` and `version` must match the actual release (the first release is `0.9.0` — Beta/pre-1.0; today's date is a placeholder — use the real tag date).

## Testing & Verification

**CITATION.cff validity test.** Add to `tests/test_release.py`:
```python
def test_citation_cff_is_valid():
    """Prefer cffconvert's validator; fall back to a YAML parse + required-keys check
    so the suite never hard-depends on an optional tool."""
    import os
    root = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(root, "CITATION.cff")
    assert os.path.exists(path)
    try:
        from cffconvert.cli.create_citation import create_citation
        from cffconvert.cli.validate_or_write_output import validate_or_write_output
        citation = create_citation(path, None)
        validate_or_write_output(None, "bibtex", False, citation)  # raises on invalid
    except ImportError:
        import yaml  # PyYAML is a common transitive dep; skip cleanly if truly absent
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        for key in ("cff-version", "message", "title", "authors", "version"):
            assert key in data, f"CITATION.cff missing {key!r}"
        assert data["version"] == "0.9.0"
```
> If neither `cffconvert` nor `yaml` is importable, add `import pytest; pytest.importorskip("yaml")` at the top of the fallback branch so the test skips rather than errors. Do **not** add a new hard dependency to `pyproject.toml` for this.

**Docs render check (manual, quick).** Open `README.md` and `docs/methodology.md` and confirm the tables and code fences render. The automated link check lives in Stage 04 and runs repo-wide.

Run:
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
python -m pytest tests/test_release.py -q
```

**Stage ship-readiness checklist:**
- [ ] `docs/AI_DISCLOSURE.md` renamed to `docs/ai-disclosure.md` (Step 0); the two `spec.md` links target `ai-disclosure.md`; `grep -n "](AI_DISCLOSURE.md)" docs/spec.md` is empty
- [ ] `README.md` rewritten; every relative link target exists (including `docs/ai-disclosure.md`)
- [ ] `docs/methodology.md` created; links to `spec.md` (sibling) sections; no duplication of spec prose
- [ ] `CITATION.cff` created, valid, `version: "0.9.0"`, author `Will Patke`
- [ ] `python -m pytest` green

## End State / Handoff

The next stage (04) may assume `docs/ai-disclosure.md` (lower-case) exists with its `spec.md` links updated, and that `README.md`, `docs/methodology.md`, and `CITATION.cff` exist and are internally linked. Stage 04 corrects the remaining stale §7 blueprint tree and adds the repo-wide link-integrity test that covers all of them.

## Failure Modes & Rollback

- **`cffconvert` produces an obscure schema error.** Its error text names the offending
  key; fix the YAML. The fallback branch already covers the no-tool case.
- **A README link points at a not-yet-created file** (e.g. running this before Stage 02's
  `docs/data-sources.md`). Create the missing file or run stages in order; the Stage 04 test
  will otherwise fail.
- **Case-sensitive link failure.** If the README links `docs/ai-disclosure.md` but Step 0's rename
  was skipped, the link resolves on macOS (case-insensitive) but breaks on Linux/CI. Do Step 0 first.
- **Rollback:** `mv docs/ai-disclosure.md docs/AI_DISCLOSURE.md` and revert the `spec.md` link edits;
  restore the original `README.md` (git); delete `docs/methodology.md`, `CITATION.cff`, and the
  `test_citation_cff_is_valid` test.
