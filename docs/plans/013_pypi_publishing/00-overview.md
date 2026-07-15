# PyPI Publishing (v1.0.0) — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Publish Crossroads-UK to PyPI as version **1.0.0** using PyPI Trusted Publishing (OIDC —
no API tokens), validated first against TestPyPI.

## Context & Objective

Crossroads-UK is a reproducible Python pipeline (`src/crossroads/`) that builds a local
DuckDB database from public UK data sources. It is **already packaging-shaped**:

- `pyproject.toml` uses the **Hatchling** build backend with **hatch-vcs** for a
  git-derived version (`dynamic = ["version"]`, `[tool.hatch.version] source = "vcs"`).
  Full `[project]` metadata is present: `name = "crossroads-uk"`, `description`,
  `readme = "README.md"`, `requires-python = ">=3.11"`, `license = { file = "LICENSE" }`,
  classifiers, keywords, `[project.optional-dependencies]` (`dev`, `weather`),
  `[project.scripts] crossroads = "crossroads.console:main"`, and `[project.urls]`.
- The distribution name **`crossroads-uk` is available on PyPI** (verified: pypi.org
  returns 404 for it). The *import* name `crossroads` is taken on PyPI by an unrelated
  LGPL package — this does **not** affect us (we publish `crossroads-uk`); it is noted only
  as a downstream import-collision caveat, out of scope here.
- Reference data ships **inside** the package at `src/crossroads/reference/`
  (`ons_boundaries.json`, `stats19_codebook.csv`, `stats19_columns.csv`, `README.md`) and
  is loaded at runtime via `os.path.dirname(__file__)` joins (see
  `src/crossroads/transformers/spatial.py`, `.../stats19.py`). These files **must** be
  present in the built wheel and sdist or an installed build breaks.
- CI exists: `.github/workflows/tests.yml` runs the offline suite on Python 3.11 + 3.12.
- A `v0.9.0` git tag already exists; the git remote is
  `https://github.com/wpatke/crossroads-uk.git`.
- `crossroads --version` / `-V` is already wired in `src/crossroads/console.py`.

**The gap is only the publish itself**, and cutting the 1.0.0 release that accompanies it.
This plan does four things: (1) bump the project to 1.0.0 in the source metadata,
(2) prove the built artifacts are correct, (3) add a Trusted-Publishing release workflow,
and (4) run the TestPyPI → PyPI release sequence.

**Target version: 1.0.0.** This is the first stable release: after publishing (and testing
via TestPyPI), the project is considered ready for release, and the stable-contract
guarantees the CHANGELOG describes take effect. The `v1.0.0` tag is what makes hatch-vcs
stamp a clean `1.0.0`.

## Approach / Architecture

**Publishing mechanism — PyPI Trusted Publishing (OIDC).** A GitHub Actions workflow
(`.github/workflows/publish.yml`) builds the sdist + wheel and uploads them using
`pypa/gh-action-pypi-publish` with `permissions: id-token: write`. PyPI/TestPyPI are
configured with a **pending publisher** that trusts *this repo + this workflow file +
this environment*, so no password or API token is ever stored. Rejected alternatives:
an API token stored as a GitHub secret (a long-lived credential to rotate and protect) and
a manual local `twine upload` (no repeatable, auditable process; handles a token by hand).

**One artifact, two indexes.** The version is derived from the git tag, so we tag `v1.0.0`
**before** publishing. The dry run (`workflow_dispatch`) uploads the resulting clean
`1.0.0` build to **TestPyPI**; after verifying an install from TestPyPI, publishing a
GitHub **Release** on the same tag uploads the *identical* `1.0.0` build to **PyPI**.
TestPyPI and PyPI are independent indexes, so reusing `1.0.0` across them is fine, and the
dry run validates the exact artifact that ships.

Data flow (release):
```
version-bump commit (CHANGELOG/CITATION/test pin/classifier)  ── Stage 01
        │  + publish.yml workflow committed                    ── Stage 03
        ▼
   git tag v1.0.0  (hatch-vcs → clean "1.0.0")                 ── Stage 04
        │
   workflow_dispatch ──► build ──► TestPyPI  (verify install)  ── Stage 04
        │
   GitHub Release published ──► build ──► PyPI (verify install) ── Stage 04
```

## Cross-Cutting Constraints

- **No commits, tags, or pushes by the executor without explicit user permission**
  (CLAUDE.md). Stages 01–03 *prepare files*; Stage 04 is a **runbook the maintainer runs**.
  The executor never runs `git commit`, `git tag`, `git push`, or creates a GitHub Release.
- **The executor cannot create accounts or enter credentials** — creating PyPI/TestPyPI
  accounts and confirming publisher config is the maintainer's job (documented, not done).
- **hatch-vcs needs full git history + tags.** Any checkout that builds the package must use
  `fetch-depth: 0` (fetches tags too). Without it the version silently falls back to a dev
  or `0.0.0` value. This is the single most likely release-day failure.
- **Reference data must ship.** Every packaging check verifies `reference/*.csv` and
  `reference/*.json` appear in both the wheel and the sdist.
- **Keep it simple; comment in plain language** (CLAUDE.md). Pin action versions.
- **Python floor is 3.11**; the workflow builds on 3.12 (a single wheel — the package is
  pure Python, so one build serves all supported versions).
- Do not install the `weather` extra in CI or the publish build — it is heavy and not needed
  to build or metadata-check the package.

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Version bump to 1.0.0 | Promote CHANGELOG `[Unreleased]`→`[1.0.0]`; set CITATION.cff to `1.0.0`/release date; update the one hardcoded version literal in `tests/test_release.py`; bump the `Development Status` classifier. | Working tree edited to describe 1.0.0; `pytest -q` green **after** the CITATION pin is updated; **nothing committed**. | — | `01-version-bump.md` |
| 02 | Packaging validation & hardening | Build sdist + wheel, run `twine check`, prove reference CSV/JSON ship in **both** artifacts, install the built wheel into a clean venv and smoke-test. | A verified, installable `dist/` locally; any packaging gap fixed in `pyproject.toml`. | 01 | `02-packaging-validation.md` |
| 03 | Trusted-Publishing workflow | Add `.github/workflows/publish.yml` (OIDC, TestPyPI on dispatch / PyPI on release). Document the PyPI + TestPyPI pending-publisher and GitHub Environment setup. | Workflow file present and valid; maintainer setup steps written. | 02 | `03-publish-workflow.md` |
| 04 | Release runbook (maintainer-executed) | Ordered sequence: commit → CI green → tag `v1.0.0` → dispatch to TestPyPI → verify install → publish GitHub Release → PyPI → final verify. | `crossroads-uk 1.0.0` live and pip-installable from PyPI. | 01, 02, 03 | `04-release-runbook.md` |

## Global Testing & Ship

The whole feature ships when a fresh machine can `pip install crossroads-uk`, get `1.0.0`,
and run it. Concretely, in order:

1. **Metadata/build correctness (Stage 02):** `python -m build` produces an sdist + wheel;
   `twine check dist/*` passes; `tar tzf dist/*.tar.gz` and `unzip -l dist/*.whl` both list
   `crossroads/reference/ons_boundaries.json` and the two CSVs.
2. **Clean-install smoke test (Stage 02):** in a throwaway venv, `pip install dist/*.whl`
   then `crossroads --version` prints the derived version and `python -c "import crossroads;
   from crossroads.transformers import spatial"` imports without a missing-file error.
3. **TestPyPI end-to-end (Stage 04):** after tagging `v1.0.0`, a `workflow_dispatch` run
   uploads to TestPyPI; `pip install -i https://test.pypi.org/simple/
   --extra-index-url https://pypi.org/simple crossroads-uk==1.0.0` in a clean venv installs,
   and `crossroads --version` prints `crossroads 1.0.0`.
4. **PyPI end-to-end (Stage 04):** publishing the GitHub Release uploads to PyPI; a clean
   `pip install crossroads-uk` resolves `1.0.0` and `crossroads --version` prints
   `crossroads 1.0.0`. The PyPI project page renders the README.

## Open Questions / Risks

- **Classifier bump (`4 - Beta` → `5 - Production/Stable`).** Included in Stage 01 because
  the CHANGELOG states the stable contract begins at 1.0.0. If you prefer to stay Beta for
  the first PyPI appearance, skip that one edit — it is isolated and reversible.
- **README relative links on the PyPI page.** `README.md` links to `docs/schema.md`,
  `docs/methodology.md`, etc. with repo-relative paths; these render as broken links on the
  PyPI project page (they resolve fine on GitHub). Cosmetic only. If it matters, convert the
  doc links to absolute `https://github.com/wpatke/crossroads-uk/blob/main/...` URLs — noted,
  not required for shipping.
- **License metadata style.** `license = { file = "LICENSE" }` is valid and passes
  `twine check`. Newer PEP 639 style (`license = "MIT"` + `license-files = ["LICENSE"]`) is
  optional modernisation; not required, and left out to avoid churn on release day.
