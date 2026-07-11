# Release Preparation (v1.0.0) — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Tidy Crossroads-UK into a shippable, citable, legally-clean first public release (`0.9.0`, Beta/pre-1.0): a git-derived version, a documented data-licensing/attribution story, researcher-facing docs (README, methodology, citation), and repository hygiene.

## Context & Objective

Crossroads-UK is a reproducible Python pipeline that downloads, cleanses, and unifies UK road-safety (DfT STATS19), weather (Copernicus ERA5-Land), and ONS boundary data into a local DuckDB database. The engine, quality model, and interactive wizard are built; the code is functional. What is missing is the *release wrapper*: version discipline, licence/attribution documentation, user-facing docs, and cleanup of stale references and stray build artifacts.

**What exists today:**
- `pyproject.toml` — package `crossroads-uk`, `version = "0.0.1"` (hardcoded), classifier `Development Status :: 2 - Pre-Alpha`, build backend `hatchling`.
- `src/crossroads/__init__.py` — a *second* hardcoded `__version__ = "0.0.1"` (duplication; drift risk).
- `README.md` — 20 lines, "early foundation" status, dev-only instructions, no usage/data/licence/citation sections.
- `docs/spec.md` — the full product definition (§3 conversions, §9 quality model). Its §7 "Repository Blueprint" still lists `AI_DISCLOSURE.md` at the repo **root**, but the file now lives at `docs/AI_DISCLOSURE.md` (moved this session). The `[AI_DISCLOSURE.md](AI_DISCLOSURE.md)` links at spec.md:9 and :269 *happen* to resolve because spec.md is itself in `docs/`.
- `docs/AI_DISCLOSURE.md` — exists; this release **renames it to `docs/ai-disclosure.md`** (lower-case) per the naming scheme in Cross-Cutting Constraints, and updates the two links in `spec.md` accordingly.
- `src/crossroads/console.py` — the interactive wizard. `run_wizard` gathers params, prints a summary via `format_summary`, confirms, and builds. No licence notice today.
- `src/crossroads/transformers/weather.py` — already surfaces friendly errors when the Copernicus API key is missing or the ERA5-Land licence has not been accepted on the CDS portal (`_missing_key_message`, `_licence_message`).
- `src/crossroads/reference/README.md` — already documents STATS19 provenance and its OGL v3.0 licence.
- `LICENSE` — MIT, `Copyright (c) 2026 wpatke`.
- Git remote: `https://github.com/wpatke/crossroads-uk.git`.
- Stray files in the working tree: `test.duckdb` (78 MB, matched by `.gitignore` `*.duckdb` — already ignored), `crossroads.db` (matched by `*.db` — ignored), and historically a `weather_test` file with **no extension** (matched by *nothing* in `.gitignore` — would be committed).

**The goal:** a `0.9.0` (Beta) release that a researcher can install, run, understand (methodology), cite, and comply with the upstream data licences for — with no dangling internal references.

## Approach / Architecture — shared by all stages

Four decisions, locked with the user, govern every stage:

1. **Version derived from git via `hatch-vcs` (the standard `setuptools-scm` engine), PEP 440, with schema-as-MINOR semantics.** The **first public release is `0.9.0` (Beta, pre-1.0)**: the pipeline is usable and reproducible, but the DuckDB schema, the CLI, and the `init_engine`/`build` public API are **not yet frozen** — they may still change before `1.0.0`. The stable *contract* (a removed/renamed column becomes a breaking change) takes effect **at `1.0.0`**, not now. This is **not** a claim of bug-freeness. **Only MAJOR and MINOR are maintained by hand — the build identity is automatic:**
   - **MAJOR** — a genuinely *breaking* change (a removed/renamed column or table that breaks existing researcher queries, a CLI/API break). Rare; stays `1` in practice as long as schema changes are additive. Maintainer-owned: bumped by cutting a `v2.0.0` **GitHub Release** (which creates the git tag).
   - **MINOR** — *additive* schema/feature changes: new datasource, new column, new table. This is the maintainer's "schema" digit. Maintainer-owned: bumped by cutting a `v1.1.0` GitHub Release.
   - **Between releases — automatic and self-identifying.** On the exact release tag the version is clean (`1.1.0`). For any commit *after* a tag, `setuptools-scm`'s default scheme appends the commit distance **and the exact git commit hash**, e.g. `1.1.1.dev3+g1a2b3c4` (3 commits past `v1.1.0`, at commit `1a2b3c4`); a build with uncommitted changes also gets a `.dYYYYMMDD` dirty-date suffix. No per-commit hand-editing — the commit hash in the version *is* the exact-source identity, which is what makes a result reproducible.

   **The version is single-sourced from git tags, not a file.** `pyproject.toml` declares `dynamic = ["version"]` with `[tool.hatch.version] source = "vcs"` (default scheme — the hash is intentionally kept); `hatch-vcs` computes the version at build/install time and **freezes it into the installed package metadata**, so at runtime `crossroads.__version__` reads it back via `importlib.metadata.version("crossroads-uk")` — available **whether or not git is present**. This is the resolution to the "the git commit isn't always available at runtime" problem: identity is resolved once, at install time, then baked into the distributed artifact. A `fallback_version` keeps builds/installs working when there is no git history at all. A **released** version maps to its git tag (a clean number); a **dev** version carries the commit hash directly. Either way the version fully identifies the code. Stage 07 additionally stamps this version (plus the in-DB `schema_version` and build time) into every database, so each `.db` self-reports what produced it. (Note: the `+g<hash>` "local version" segment is dev-only and is never uploaded to PyPI, which only receives clean tagged releases.)

   **Reproducibility policy:** a "given version" only yields a "structurally identical database" if the *dependency* versions are also known — `duckdb>=1.5` is a floating floor, so a user installing `1.0.0` a year apart can silently resolve a different DuckDB/PROJ and get drifted reprojection. Therefore v1 (a) **documents the exact tested versions** (Python 3.12.13, DuckDB 1.5.4 at authoring time; weather-extra versions captured when installed) so a user can pin them, and (b) adopts the rule that **any change to declared dependencies or ingestion behaviour is a release** (recorded in `CHANGELOG.md`). No lockfile is shipped in v1.

   **Schema version lives in the database, not only the version string.** The "schema number" is also stamped as a monotonic integer `schema_version` into a `crossroads_meta` provenance table in every build (Stage 07), so a `.db` file is self-describing independently of the package version. The package MINOR digit communicates to humans/pip; the in-DB `schema_version` is the durable machine signal. (No branch number: git tracks branches; PEP 440 `.devN`/local `+labels` cover non-release builds if ever needed.)

2. **No licence gate in the wizard.** Copernicus acceptance happens on the CDS portal (a working API key implies it; the un-accepted case is already handled in `weather.py`). OGL v3.0 (STATS19, ONS) has **no click-through** — it is an *attribution* licence whose obligation falls on the researcher at publication time. Therefore: **inform, don't gate.** Deliver `docs/data-sources.md` with exact attribution strings, and a single **non-blocking** one-line pointer in the wizard output. No per-source Y/N.

3. **`docs/methodology.md` is a distillation, not a duplicate.** It summarises how sources are joined, reprojected, temporally aligned, and quality-flagged, and links into `docs/spec.md` for full detail. `spec.md` stays the canonical product definition.

4. **`CITATION.cff`, `CHANGELOG.md`, `docs/data-sources.md`, and a non-affiliation disclaimer are in scope.** `CITATION.cff` is kept — it is the Python equivalent of the `codemeta.json` + `DESCRIPTION` + JOSS-paper citation metadata that R's `citation()` reads for stats19; Python has no built-in equivalent, so the `.cff` file is how GitHub's "Cite this repository" button and machine-readable citation are provided. (It is also the single easiest thing to cut later if unwanted.) Zenodo/DOI is **out of scope** for v1.

5. **CI is added, and it is free.** GitHub Actions runs on GitHub-hosted runners (not the maintainer's machine): unlimited minutes for public repos, 2,000 min/month free if private. A minimal workflow runs the offline `pytest` suite on push/PR across Python 3.11 and 3.12 — no local infrastructure, no cost.

6. **PyPI publishing is deferred to a separate future plan (`008_pypi_release`).** This plan does **not** build wheels, check name availability, verify wheel data inclusion, or push to TestPyPI/PyPI. It *does* add `[project.urls]` metadata now (helps GitHub, preps for PyPI).

7. **Pre-existing internal references are scrubbed and doc/code drift is fixed.** Committed source docstrings/comments reference invisible planning artifacts ("master-plan Step 5", "Stage 07", "Step 3"); the spec's headline API example (§8) uses parameters that don't exist on the real `build()`. Both are corrected so a first-time public reader hits no dangling reference or broken example.

8. **A `docs/schema.md` data dictionary is shipped, guarded against drift.** Researchers get annotated `CREATE TABLE`-style blocks (illustrative — never executed) with a per-column meaning/derivation, **authored from a real fixture build** so it starts accurate. Because a hand-written schema doc is the highest drift-risk artifact in the repo (tables are built dynamically via `CREATE OR REPLACE ... AS SELECT`, not literal DDL), it ships with a drift-guard test: a full offline build is introspected and every real column of the silver/provenance/reference tables must appear in the doc, and `docs/schema.md`'s declared schema version must equal `crossroads.SCHEMA_VERSION`. Gold views are documented by derivation rule; bronze `*_raw` tables as a category (their columns are the upstream source's).

**Data flow of the docs themselves** (why the stage order matters): the version number (Stage 01) is referenced by `CITATION.cff` and the README badge/status (Stage 03); `docs/data-sources.md` (Stage 02) is referenced by both the README and the wizard notice (Stages 02–03). Stages 01 and 02 have no dependencies and can run in either order; Stage 03 depends on both; Stage 04 is independent.

## Cross-Cutting Constraints

- **Keep it simple** (CLAUDE.md). Prefer the explicit small change. Every code change must be human-readable with plain-language comments.
- **No git commits, staging, or tags without explicit user permission** (CLAUDE.md + user memory). Where a stage needs a git tag or commit, it **documents the command for the user to run** — it does not run it.
- **Do not reference `master-plan.md`, "Step N", or the GPL `stats19` code** in any committed artifact (CLAUDE.md). These plans are committed and public.
- **MIT-clean.** Do not copy from the GPL `stats19` R package. Attribution docs describe *DfT's* licence (OGL), which is unrelated to the R package's GPL.
- **Offline, deterministic tests.** New tests must pass with `python -m pytest` (no network, no `-m integration`). The default suite deselects `integration`.
- **Python ≥ 3.11**, Hatchling build backend, DuckDB ≥ 1.5.
- **Documentation file naming & locations (authoritative — every stage follows this).** Only the conventional/tool-recognised meta files stay UPPER-CASE at the repo root; all project-authored content docs are lower-case under `docs/`:

  | File | Location | Case |
  |------|----------|------|
  | `README.md` | root | UPPER (universal; GitHub landing page) |
  | `LICENSE` | root | UPPER (universal; GitHub licence detection) |
  | `CHANGELOG.md` | root | UPPER (Keep a Changelog convention) |
  | `CITATION.cff` | root | UPPER (**required exact name** — GitHub only recognises `CITATION.cff`) |
  | `docs/spec.md` | docs/ | lower (already exists) |
  | `docs/methodology.md` | docs/ | lower (was `METHODOLOGY.md`) |
  | `docs/schema.md` | docs/ | lower (was `SCHEMA.md`) |
  | `docs/data-sources.md` | docs/ | lower (was `DATA_SOURCES.md`) |
  | `docs/ai-disclosure.md` | docs/ | lower (was `docs/AI_DISCLOSURE.md`) |

  **Relative-path consequences (get these right — the link-integrity test enforces them):** a doc in `docs/` links to a sibling doc by bare name (`spec.md`, `schema.md`), and to a root file with `../` (`../LICENSE`, `../CHANGELOG.md`, `../CITATION.cff`), and to source with `../src/...`. `README.md` (at root) links to docs with `docs/…` (`docs/methodology.md`). Runtime strings (e.g. the wizard notice) name the path in prose: `docs/data-sources.md`.

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|------------|------|
| 01 | Versioning & release metadata | Derive the version from git via `hatch-vcs` default scheme (MAJOR.MINOR owned by release tags; dev builds auto-carry commit distance + hash, e.g. `1.1.1.dev3+g1a2b3c4`); read it at runtime from package metadata; update classifier; add `[project.urls]`; add a `crossroads --version` flag; add `CHANGELOG.md` (with the release/build-identity policy); document the GitHub-Release step the maintainer performs. | `crossroads.__version__ == importlib.metadata.version("crossroads-uk")` (single-sourced from git, no hardcoded literal); `crossroads --version` prints it; `CHANGELOG.md` and `[project.urls]` exist; tests assert the agreement without pinning a number. | — | `01-versioning.md` |
| 02 | Data sources & attribution | Author `docs/data-sources.md` (each source: publisher, licence, exact attribution string, "why no gate"). Add a non-blocking one-line licence pointer to the wizard output. | `docs/data-sources.md` exists and lists STATS19/ONS (OGL v3.0) + ERA5-Land (Copernicus); wizard prints a pointer to it and still builds without an extra prompt; a test asserts both. | — | `02-data-licensing.md` |
| 03 | User-facing docs | **Rename `docs/AI_DISCLOSURE.md` → `docs/ai-disclosure.md` + update its two `spec.md` links (Step 0, first)**; rewrite `README.md` (install, usage, data sources, licence, citation, disclaimer); add `docs/methodology.md` (distillation → links to spec.md, incl. a "Tested with" versions table); add `CITATION.cff`. | `docs/ai-disclosure.md` exists (lower-case); README reflects `1.0.0` and links to `docs/methodology.md`, `docs/data-sources.md`, `CITATION.cff`, `docs/ai-disclosure.md`; `CITATION.cff` validates; links resolve. | 01, 02 | `03-user-facing-docs.md` |
| 04 | Reference fixes & hygiene | Fix the stale `AI_DISCLOSURE` location in the spec.md §7 blueprint tree (the rename itself is done in Stage 03); **fix the spec §8/§1 `build()` API example to match the real signature**; align `LICENSE` copyright with the real name; add non-affiliation disclaimer; ignore extensionless stray artifacts in `.gitignore`; **add the strengthened link-integrity test (files + anchors + `§N` citations)**. | spec §7 blueprint shows `docs/ai-disclosure.md`; spec §8 example runs against the real API; `.gitignore` covers `weather_test`; strengthened link-integrity test passes repo-wide. | 03 | `04-reference-fixes-hygiene.md` |
| 05 | Source-comment cleanup | Scrub committed source of references to invisible planning artifacts (`master-plan`, bare `Step N`, `Stage NN`) so public readers hit no dangling references. Behaviour-preserving (comments/docstrings only). | `grep -rn "master-plan\|Step [0-9]\|Stage [0-9]" src/` returns nothing; full suite still green (no code logic changed). | — | `05-source-comment-cleanup.md` |
| 06 | CI workflow | Add `.github/workflows/tests.yml` running the offline `pytest` suite on push/PR across Python 3.11 & 3.12 on GitHub-hosted runners (free). | Workflow file exists and is valid; a push shows a green Actions run; README gains a status badge. | 01 | `06-ci-workflow.md` |
| 07 | DB provenance stamp | Add a `SCHEMA_VERSION` constant and stamp a single-row `crossroads_meta` table (crossroads version, schema_version, UTC build time, params) into every build. | `SELECT * FROM crossroads_meta` returns one row after any build; a fast default-suite test asserts it; the conservation invariant is unaffected. | 01 | `07-db-provenance.md` |
| 08 | Schema data dictionary | Author `docs/schema.md` (annotated `CREATE TABLE`-style blocks + per-column meaning/derivation, from a real fixture build), linked from README + `docs/methodology.md`, with a drift-guard test tying documented columns to the built database and the declared schema version to `SCHEMA_VERSION`. | `docs/schema.md` documents every silver/provenance/reference column and the gold views; fast test (schema-version + core tables) is CI-covered; `-m integration` guard matches the built DB. | 03, 07 | `08-schema-dictionary.md` |

## Global Testing & Ship

Three test surfaces prove the release is real (all offline, all in the default `pytest` run):

1. **Version-agreement test** (attaches to Stage 01) — `tests/test_release.py::test_version_single_sourced`: `crossroads.__version__` equals `importlib.metadata.version("crossroads-uk")` and is a non-empty string. It does **not** pin a literal like `"1.0.0"`, because the version is git-derived and legitimately varies with tag distance (`1.1.0`, `1.1.1.dev3+g1a2b3c4`, or the `fallback_version` when there is no git). Proves the single-source-from-git wiring holds. Requires an editable reinstall (`pip install -e .`) after the `pyproject.toml` change so the metadata reflects the derived version.
2. **Wizard-notice test** (attaches to Stage 02) — extends `tests/test_console.py`: a scripted `run_wizard` run emits the `docs/data-sources.md` pointer line in its writer output, and the build proceeds with the *same number of prompts as before* (no new gate). Proves "inform, don't block."
3. **Markdown link-integrity test** (attaches to Stage 04, exercised by 03 and 08) — `tests/test_docs_links.py`: scans committed `*.md` (README, docs/, excluding `docs/plans/**` and `.venv/**`) and checks three things: (a) every relative **file** link resolves; (b) every `path#anchor` **fragment** matches a real heading in the target file; (c) every `§N` **spec-section citation** in the docs corresponds to a real section number in `spec.md`. This is the test that would have caught the `AI_DISCLOSURE` move, and — crucially for this release — it fails loudly on any reference left stale by the doc renames, so it *is* the guarantee that all internal references stay accurate. It gives every docs stage a real, runnable pass/fail.
4. **`--version` CLI test** (attaches to Stage 01) — asserts `crossroads --version` prints the same string as `crossroads.__version__` (non-empty) and exits 0, proving the version reaches the CLI surface a researcher records (without pinning a literal, since it is git-derived).
5. **Internal-reference scrub test** (attaches to Stage 05) — a test (or documented `grep`) asserting `src/` contains no `master-plan`, bare `Step N`, or `Stage NN` references, so the drift cannot silently return.
6. **CI (attaches to Stage 06)** — the GitHub Actions run *is* the ship signal for the whole suite on 3.11 + 3.12; a green run on the release commit is the final gate before tagging.
7. **Provenance test** (attaches to Stage 07) — `tests/test_provenance.py`: a fast, real-DuckDB (default-suite, so CI-covered) test that `write_build_metadata` produces a single `crossroads_meta` row with the right version/schema_version/params, plus an integration assertion that a real build stamps it. The existing full-build offline tests are `@pytest.mark.integration` (deselected), so the fast test is what keeps provenance under CI.
8. **Schema-doc drift guard** (attaches to Stage 08) — `tests/test_schema_doc.py`: a fast default-suite test (`docs/schema.md` declares `SCHEMA_VERSION` and names the core tables) plus an `@pytest.mark.integration` guard that builds the full fixture DB and asserts every real column of the silver/provenance/reference tables is documented. The fast tier is CI-covered; the column guard is part of the pre-release `-m integration` gate.

**Ship-readiness (whole release):**
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
pip install -e ".[dev]"          # picks up the dynamic version
python -m pytest                 # full offline suite green, including all new tests
python -c "import crossroads; print(crossroads.__version__)"   # -> the git-derived version (1.0.0 on the v1.0.0 tag, else e.g. 1.0.1.dev3+g1a2b3c4, or the fallback with no git)
```
Then the human performs the release by cutting a **GitHub Release** for `v1.0.0` (which creates the tag; steps documented in Stage 01; **not** run by the executor).

## Open Questions / Risks

- **Release posture — decided (revised).** The first public release is **`0.9.0` with `Development Status :: 4 - Beta`** (confirmed by the user), a deliberate pre-1.0 posture: the schema is **not** frozen yet and may still change before `1.0.0`. (This revises an earlier `1.0.0` / `5 - Production/Stable` decision.) The stable-schema contract — additive changes bump MINOR, breaking changes bump MAJOR — takes effect at `1.0.0`.
- **Author identity — confirmed.** The author is **Will Patke** (used verbatim in `CITATION.cff` and the `LICENSE` copyright line). Only an ORCID remains optional — add it if the user supplies one; otherwise leave the commented placeholder.
- **`cffconvert` availability.** The `CITATION.cff` validation test falls back to a plain YAML-parse + required-keys check if `cffconvert` is not installed, so the suite never hard-depends on it.
- **PyPI is a separate plan.** `pip install crossroads-uk` will not work until `008_pypi_release` is executed; the README must say "install from source" for now (Stage 03 already does).
- **Repo visibility for free CI.** Actions is free-unlimited only for **public** repos; `wpatke/crossroads-uk` is assumed public (MIT, open-source). If private, the 2,000 min/month free tier still covers this suite easily — note it in Stage 06.
- **`Stage NN` scrub breadth (Stage 05).** The `master-plan` and bare `Step N` references are unambiguous cleanup; the internal `Stage NN` shorthand comments are a judgement call (harmless to a reader but reference invisible docs). Default: scrub all three so the grep test can enforce zero drift. Downgrade to master-plan/Step-only if the churn is undesirable, and relax the test accordingly.
