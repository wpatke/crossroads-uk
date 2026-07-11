# Stage 01 — Versioning & Release Metadata
> Part of Release Preparation (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

No prior stage required. Verify the starting repo state first:

```bash
cd /Users/will/Documents/Code/Crossroads
grep -n 'version' pyproject.toml                    # inspect the [project] + [tool.hatch.version] blocks
grep -n '__version__' src/crossroads/__init__.py    # how the runtime version is set today
grep -n 'Development Status' pyproject.toml          # expect a classifier line
ls CHANGELOG.md 2>/dev/null || echo "no CHANGELOG yet"
git tag --list 'v*'                                  # are there any release tags yet? (likely none)
```

This stage may be run against a repo where an earlier draft hardcoded `__version__ = "1.0.0"` and pointed Hatchling at the file (`[tool.hatch.version] path = ...`). If you see that, this stage **replaces** that file-based mechanism with a git-derived one — adapt and note it.

## Objective

Make the version **derived from git**, not hand-written in a file, using the standard `setuptools-scm` engine via its Hatchling wrapper `hatch-vcs` (default scheme). The maintainer owns `MAJOR.MINOR` by cutting a GitHub Release (a git tag such as `v1.1.0`): on the tag the version is a clean `1.1.0`, and any commit after it automatically carries the commit distance and the exact git hash, e.g. `1.1.1.dev3+g1a2b3c4` (a build with uncommitted changes also gets a `.dYYYYMMDD` suffix). The computed version is frozen into the installed package metadata, and `crossroads.__version__` reads it back at runtime — so it is available with or without git present. A `CHANGELOG.md` records the release policy, and tests lock the single-source invariant without pinning a literal number.

**Why this design (context for the executor):** a research tool must let a user record exactly what produced their database. The git commit is the exact-code identity, but reading git *at runtime* fails for installed artifacts with no `.git`. The standard Python fix (setuptools-scm, wrapped here by `hatch-vcs`) resolves the version from git **once, at install/build time**, and bakes it into the package metadata, which is always readable at runtime. MAJOR/MINOR stay a human judgement (breaking vs additive); the build number is mechanical and therefore automated.

## Implementation Steps

**Step 1 — Read the version from package metadata in `src/crossroads/__init__.py`.**
Replace any hardcoded `__version__ = "..."` line with a metadata read plus a graceful fallback:
```python
from importlib.metadata import PackageNotFoundError, version as _pkg_version

# ... existing imports / __all__ ...

# The version is derived from git tags (via hatch-vcs) and frozen into the
# installed package's metadata at install/build time. We read it back here rather
# than hardcoding it, so it can never drift and is available at runtime whether or
# not git is present. If the package is not installed (running straight from a
# source checkout with no install), fall back to a clearly-marked placeholder.
try:
    __version__ = _pkg_version("crossroads-uk")
except PackageNotFoundError:  # not installed; no metadata to read
    __version__ = "0.0.0+unknown"
```
Expected result: `crossroads.__version__` reflects whatever `hatch-vcs` computed for the current git state, with no version string literal living in the source.

**Step 2 — Point Hatchling at git via `hatch-vcs` in `pyproject.toml`.**

2a. Add `hatch-vcs` to the build backend requirements:
```toml
[build-system]
requires = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"
```

2b. In `[project]`, ensure the version is dynamic (remove any hardcoded `version = "..."`):
```toml
dynamic = ["version"]
```

2c. Replace any file-based `[tool.hatch.version]` block with a VCS source using the **default** scheme (place it next to the wheel-target block). The default keeps the commit hash in dev-build versions on purpose — it is what makes each dev build identify its exact source:
```toml
# Derive the version from git via hatch-vcs (the Hatchling wrapper around the
# standard setuptools-scm engine), default scheme:
#   - On the exact release tag the version is clean, e.g. "1.1.0".
#   - Between releases it appends the commit distance AND the exact git hash,
#     e.g. "1.1.1.dev3+g1a2b3c4"; an uncommitted (dirty) build also gets a
#     ".dYYYYMMDD" suffix. The hash pins the exact source (reproducibility).
#   - fallback_version: used only when there is no git at all (e.g. building from a
#     source archive with no history), so a build never fails.
[tool.hatch.version]
source = "vcs"
raw-options = { fallback_version = "0.0.0" }
```
> Do **not** copy the common web snippet that sets `build-backend = "setuptools.build_meta"` and a `[tool.setuptools-scm]` table — that is the setuptools wiring. This project builds with **Hatchling**, so `hatch-vcs` provides the identical `setuptools-scm` behaviour without changing the backend.

2d. Update the Development-Status classifier in `[project].classifiers`:
```toml
  "Development Status :: 4 - Beta",
```
(replacing `"Development Status :: 2 - Pre-Alpha",`).
> **Decided:** `4 - Beta` (confirmed by the user). The first public release is `0.9.0`, a deliberate pre-1.0 posture — the schema is not yet frozen. `5 - Production/Stable` and the stable-schema contract arrive at `1.0.0`.

2e. Add `[project.urls]` (helps GitHub now; preps the deferred PyPI plan):
```toml
[project.urls]
Homepage = "https://github.com/wpatke/crossroads-uk"
Repository = "https://github.com/wpatke/crossroads-uk"
Issues = "https://github.com/wpatke/crossroads-uk/issues"
Changelog = "https://github.com/wpatke/crossroads-uk/blob/main/CHANGELOG.md"
```

Expected result: `pip install -e .` computes the version from git. With no tag yet it uses the fallback (or a `0.x.devN` guess); after a `v1.0.0` tag it reports `1.0.0`.

**Step 2f — Add a `crossroads --version` flag.** File: `src/crossroads/console.py`, function `main(argv=None)`. Handle `--version`/`-V` (and `--help`/`-h`) before starting the wizard, using the single-sourced constant:
```python
def main(argv=None):
    import sys
    from crossroads import __version__
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] in ("--version", "-V"):
        print(f"crossroads {__version__}")
        return 0
    if args and args[0] in ("--help", "-h"):
        print("Usage: crossroads            # run the interactive build wizard\n"
              "       crossroads --version  # print the version and exit")
        return 0
    # ... existing wizard wiring unchanged below ...
```
Keep the rest of `main` (the `reader`/`writer` wiring, `KeyboardInterrupt`/`EOFError` handling, `client.close()`) exactly as-is. Expected result: `crossroads --version` prints `crossroads <derived-version>` and exits 0 without prompting.

**Step 3 — Reinstall so the editable metadata picks up the derived version.**
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
pip install -e ".[dev]"           # fetches hatch-vcs the first time (needs network once)
pip show crossroads-uk | grep -E "^(Name|Version)"
```
Expected: `Name: crossroads-uk`, and a `Version:` matching the git state (e.g. `1.0.0` if the `v1.0.0` tag exists, else a dev value like `0.0.1.dev30+g0585288` or the fallback with no git). The metadata is a snapshot from install time — reinstall after tagging to refresh it (see Failure Modes).

**Step 4 — Add `CHANGELOG.md`** at the repo root, in *Keep a Changelog* format:
```markdown
# Changelog

All notable changes to Crossroads-UK are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), read for this project as:

- **MAJOR** — a breaking change to the stable contract (a removed/renamed database column or
  table, or a CLI/`init_engine`/`build` API break). Stays `1` while schema changes are additive.
  Bumped by hand, by cutting a new GitHub Release (a git tag such as `v2.0.0`).
- **MINOR** — additive schema/feature changes: a new datasource, column, or table. Bumped by hand,
  by cutting a GitHub Release such as `v1.1.0`.
- **Build identity** (automatic, dev builds only) — set by `hatch-vcs` from git. A release (git tag)
  is a clean number such as `1.1.0`; any commit after it reads e.g. `1.1.1.dev3+g1a2b3c4` — the
  commit distance plus the exact git hash, so a dev build always pins its exact source. Never
  hand-edited.

The physical database shape also carries its own monotonic `schema_version` integer in the
`crossroads_meta` table. Because reproducibility depends on the runtime stack, **any change to
declared dependencies or ingestion behaviour is a release** and is recorded here; the versions each
release was tested against are listed in [docs/methodology.md](docs/methodology.md).

## [0.9.0] - 2026-07-10

First public release. **Pre-1.0 (Beta):** the pipeline is usable and reproducible, but the
DuckDB schema and public API are not yet frozen — they may still change before `1.0.0`. The
stable-contract guarantees described above take effect at `1.0.0`.

### Added
- Reproducible build pipeline unifying DfT STATS19 road-safety data, Copernicus
  ERA5-Land weather, and ONS boundaries into a single local DuckDB database.
- Keep-in-place data-quality model (bronze/silver/gold) with a queryable
  `data_quality_log` exclusion ledger and build-time conservation invariants.
- Interactive data-compilation wizard (`crossroads`).
- Spatial standardisation to EPSG:27700 with R-Tree indices; snapshot and temporal
  boundary modes.

[0.9.0]: https://github.com/wpatke/crossroads-uk/releases/tag/v0.9.0
```
> Use the release date the user actually tags on; `2026-07-10` is a safe placeholder. Do not invent a different date.

**Step 5 — Document the release step for the maintainer (do NOT run it).** The executor must **not** create tags/releases or commit (CLAUDE.md). The maintainer performs the release, which is what sets the version, **after** reviewing and committing:
```bash
# Preferred: cut a GitHub Release named "v0.9.0" in the repo UI
#   (Releases -> Draft a new release -> tag "v0.9.0" -> Publish).
# The Release creates the git tag; hatch-vcs then reports the version as exactly 0.9.0.
#
# Equivalent from the command line:
git tag -a v0.9.0 -m "Crossroads-UK 0.9.0"
git push origin v0.9.0
```
After the tag exists, a fresh `pip install -e .` (or any wheel build) reports `0.9.0`.

## Testing & Verification

**Version-agreement test (PRIMARY).** Create/replace `tests/test_release.py`:
```python
"""Release-invariant tests: the version is single-sourced from git and reaches the CLI."""
import importlib.metadata

import crossroads


def test_version_single_sourced():
    # crossroads.__version__ must equal the installed package metadata, proving it
    # reads back the hatch-vcs-derived value (no drift). We do NOT pin a literal:
    # the version is git-derived and varies with tag distance ("1.1.0",
    # "1.1.1.dev3+g1a2b3c4", or the fallback when there is no git).
    runtime = crossroads.__version__
    packaged = importlib.metadata.version("crossroads-uk")
    assert runtime == packaged
    assert isinstance(runtime, str) and runtime != ""


def test_cli_version_flag(capsys):
    # A researcher must be able to record the exact version they ran.
    from crossroads import console
    rc = console.main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert crossroads.__version__ in out
```
Run:
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
pip install -e ".[dev]"     # MUST reinstall first so metadata reflects the derived version
python -m pytest tests/test_release.py -q
```
Expected: passes. If `test_version_single_sourced` fails because `importlib.metadata.version` raises `PackageNotFoundError`, the editable install did not complete — rerun `pip install -e ".[dev]"`.

**Full-suite regression.**
```bash
python -m pytest -q
```
Expected: the entire offline suite still green (the version change touches nothing behavioural). If any existing test hardcodes a version literal like `"0.0.1"`/`"1.0.0"`, update it to read `crossroads.__version__` and note it.

**Stage ship-readiness checklist:**
- [ ] `src/crossroads/__init__.py` reads `__version__` from `importlib.metadata` (no hardcoded literal)
- [ ] `pyproject.toml`: `[build-system].requires` includes `hatch-vcs`; `[project]` has `dynamic = ["version"]`; `[tool.hatch.version]` uses `source = "vcs"` (default scheme, only `fallback_version` set); **no** hardcoded `version =` and **no** `[tool.hatch.version] path`; backend is still `hatchling.build`
- [ ] classifier is `5 - Production/Stable`
- [ ] `[project.urls]` added (Homepage/Repository/Issues/Changelog)
- [ ] `crossroads --version` prints `crossroads <derived-version>` and exits 0
- [ ] `CHANGELOG.md` exists with a `[0.9.0]` entry and the release/build-identity policy
- [ ] `tests/test_release.py` passes; full suite green
- [ ] Maintainer knows the release step (cut a `v1.0.0` GitHub Release) — documented, not executed

## End State / Handoff

The next stage may assume: `crossroads.__version__` is single-sourced from git via `hatch-vcs` and read at runtime from package metadata; `CHANGELOG.md` exists at the repo root; `tests/test_release.py` exists and passes. Stage 03 will reference the version in the README and `CITATION.cff` (as a per-release static field the maintainer updates) and may cite `CHANGELOG.md`. Stage 07 stamps the exact commit + version into the database provenance table.

## Failure Modes & Rollback

- **`pip install -e .` fails with "unable to detect version" / setuptools-scm error.** No git tag and no fallback — confirm `fallback_version = "0.0.0"` is present in `raw-options`, and that the command runs inside the git working tree.
- **Version reports `0.x.devN` or the fallback, not `1.0.0`.** Expected before the first release tag exists. Cut the `v1.0.0` GitHub Release (Step 5), then rerun `pip install -e ".[dev]"` to refresh the editable metadata.
- **Metadata looks stale after new commits (editable install).** `importlib.metadata` reflects the version computed at the last install, not live git. This is fine for the reproducibility use case (researchers use installed releases); refresh in dev with `pip install -e .` when you need the current number.
- **Rollback:** restore a hardcoded `version = "0.0.1"` in `[project]`, remove `dynamic`/`hatch-vcs`/`[tool.hatch.version]`, restore a literal `__version__` in `__init__.py`, delete `CHANGELOG.md` and `tests/test_release.py`, reinstall. The system returns to its pre-stage state.
