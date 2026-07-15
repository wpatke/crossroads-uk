# Stage 02 — Packaging Validation & Hardening
> Part of PyPI Publishing (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Stage 01 is done (the working tree describes 1.0.0). Verify:
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
grep -n 'version:' CITATION.cff                    # "1.0.0"
python -m pytest -q                                # green
grep -n 'packages' pyproject.toml                  # [tool.hatch.build.targets.wheel] packages = ["src/crossroads"]
ls src/crossroads/reference/                        # ons_boundaries.json, stats19_codebook.csv, stats19_columns.csv, README.md
```

This stage runs a **real build** and inspects the artifacts. It needs network **once** to
fetch the build front-end (`build`, `hatchling`, `hatch-vcs`, `twine`) if they are not
already cached. It does **not** publish anything and does **not** commit.

## Objective

Prove that `python -m build` produces a correct sdist and wheel: metadata passes
`twine check`, the in-package reference data (`reference/*.json`, `reference/*.csv`) is
present in **both** artifacts, and the built wheel installs into a clean virtual environment
and runs. Fix any packaging gap in `pyproject.toml` if a file is missing. This is the stage
that catches the classic "it works from a source checkout but the installed wheel is missing
its data files" bug **before** it reaches PyPI.

## Implementation Steps

**Step 1 — Build the sdist and wheel.**
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
rm -rf dist build                       # clean any stale artifacts
python -m pip install --upgrade build twine
python -m build
ls -la dist/
```
Expected result: `dist/` contains two files —
`crossroads_uk-<version>.tar.gz` (sdist) and
`crossroads_uk-<version>-py3-none-any.whl` (wheel). The `<version>` will be a hatch-vcs dev
value here (e.g. `0.9.1.dev4+g<hash>`) because the `v1.0.0` tag does not exist yet — that is
fine for validation; Stage 04 rebuilds from the tag for the real `1.0.0` artifact.

> If `python -m build` errors with "unable to detect version" / a setuptools-scm error, the
> build ran outside the git tree or history is missing. Confirm you are in the repo root and
> that `git tag --list` shows `v0.9.0`. `fallback_version = "0.0.0"` in `pyproject.toml`
> guarantees a build still succeeds with no git at all.

**Step 2 — Metadata check (`twine check`).**
```bash
python -m twine check dist/*
```
Expected result: `PASSED` for both files. This validates that the long-description
(`README.md`, content-type `text/markdown`) renders on PyPI and the core metadata is
well-formed. If it complains about the README content type, confirm `readme = "README.md"`
is set in `[project]` (it is) — Hatchling infers `text/markdown` from the `.md` extension.

**Step 3 — Prove the reference data ships in the SDIST.**
```bash
tar tzf dist/*.tar.gz | grep -E 'reference/(ons_boundaries\.json|stats19_codebook\.csv|stats19_columns\.csv)'
```
Expected result: three lines listing the JSON and both CSVs under a
`crossroads_uk-<version>/src/crossroads/reference/` path. If **any** is missing, the sdist is
incomplete — see Step 6 (harden) before continuing.

**Step 4 — Prove the reference data ships in the WHEEL.**
```bash
unzip -l dist/*.whl | grep -E 'reference/(ons_boundaries\.json|stats19_codebook\.csv|stats19_columns\.csv)'
```
Expected result: three lines listing the files under `crossroads/reference/`. The wheel path
has **no** `src/` prefix (Hatchling maps `src/crossroads` → `crossroads`). This is the path
the installed code reads at runtime, so this check is the one that most directly protects
against a broken install. If any file is missing, go to Step 6.

**Step 5 — Clean-venv smoke install (the ship-readiness test).**
Install the built wheel into a throwaway environment — *not* the dev `.venv` — so you test
what a user gets, with no source tree on the path:
```bash
cd /Users/will/Documents/Code/Crossroads
python3 -m venv /tmp/cr_pkgtest
/tmp/cr_pkgtest/bin/pip install --upgrade pip
/tmp/cr_pkgtest/bin/pip install dist/*.whl
# 1) console entry point works and prints the derived version:
/tmp/cr_pkgtest/bin/crossroads --version
# 2) the package imports and can locate its shipped reference data (runs from site-packages,
#    NOT the repo), proving the data files installed alongside the code:
/tmp/cr_pkgtest/bin/python - <<'PY'
import os, crossroads
from crossroads.transformers import spatial, stats19  # import-time reference path joins
# ons_boundaries.json path is resolved in spatial.py relative to the installed package:
p = os.path.join(os.path.dirname(spatial.__file__), "..", "reference", "ons_boundaries.json")
assert os.path.exists(p), f"MISSING shipped reference file: {p}"
print("crossroads", crossroads.__version__, "- reference data present:", os.path.basename(p))
PY
```
Expected result: `crossroads --version` prints `crossroads <derived-version>`; the Python
snippet prints a line ending `reference data present: ons_boundaries.json` with no
`AssertionError`. If the assertion fires, the wheel is missing its data — Step 6.

Clean up when done:
```bash
rm -rf /tmp/cr_pkgtest
```

**Step 6 — Harden `pyproject.toml` ONLY IF a file was missing in Steps 3–5.**
Hatchling includes all files inside the `src/crossroads` package directory in the wheel by
default, and includes VCS-tracked files in the sdist by default, so with the current
`packages = ["src/crossroads"]` the reference data **should** already be present and this
step is usually a no-op. If (and only if) a check above failed, add an explicit
force-include so the data files are unambiguously packaged:
```toml
[tool.hatch.build.targets.wheel.force-include]
"src/crossroads/reference" = "crossroads/reference"

[tool.hatch.build.targets.sdist]
include = [
  "src/crossroads",
  "README.md",
  "LICENSE",
  "CHANGELOG.md",
  "CITATION.cff",
]
```
Then re-run Steps 1–5. Do not add this block speculatively — only if a check proved a gap,
so the config stays minimal (CLAUDE.md: keep it simple). Note in your handoff whether it was
needed.

**Step 7 — Confirm the sdist is not bloated by data or caches.**
```bash
du -h dist/*.tar.gz
tar tzf dist/*.tar.gz | grep -E '\.db$|\.crossroads_cache|crossroads_2022' || echo "clean: no db/cache in sdist"
```
Expected result: the sdist is small (well under 1 MB) and prints `clean: no db/cache in
sdist`. The 5.9 GB `crossroads_2022.db` and `.crossroads_cache/` are git-ignored, so
Hatchling's VCS-based sdist excludes them. If a `*.db` appears, something is tracked that
should not be — stop and investigate before publishing.

## Testing & Verification

The Implementation Steps **are** the tests — they build and exercise the real artifacts.
Consolidated ship-readiness gate for this stage:

```bash
cd /Users/will/Documents/Code/Crossroads && source .venv/bin/activate
rm -rf dist build && python -m build && python -m twine check dist/*
echo "--- sdist reference data ---"; tar tzf dist/*.tar.gz | grep -c 'reference/.*\.\(json\|csv\)'   # expect 3
echo "--- wheel reference data ---"; unzip -l dist/*.whl | grep -c 'reference/.*\.\(json\|csv\)'      # expect 3
```

**Stage ship-readiness checklist:**
- [ ] `python -m build` produces both an sdist (`.tar.gz`) and a wheel (`.whl`)
- [ ] `twine check dist/*` reports `PASSED` for both
- [ ] sdist contains all three `reference/` data files (Step 3)
- [ ] wheel contains all three `reference/` data files under `crossroads/reference/` (Step 4)
- [ ] clean-venv install: `crossroads --version` works and the shipped reference file is
      found from site-packages (Step 5)
- [ ] sdist is small and contains no `*.db` / cache (Step 7)
- [ ] `pyproject.toml` hardened only if a gap was proven (Step 6), and the choice noted
- [ ] nothing committed

## End State / Handoff

`python -m build` is proven to produce a correct, installable, metadata-valid distribution
whose in-package reference data is present in both the sdist and the wheel. Any packaging gap
is fixed in `pyproject.toml`. Stage 03 can now add a CI workflow that runs this same
`build` + `twine check` on GitHub's runners and uploads the result to PyPI, confident the
artifacts are sound. The local `dist/` directory is git-ignored and is not committed; the
real release artifacts are built fresh in CI in Stage 04.

## Failure Modes & Rollback

- **A `reference/` file is missing from the wheel or sdist.** Apply Step 6's force-include,
  rebuild, re-verify. This is the highest-impact bug this stage exists to catch.
- **`twine check` fails on the description.** Ensure `readme = "README.md"` is in `[project]`
  (present); Hatchling sets `text/markdown` automatically. Do not switch to reStructuredText.
- **`crossroads --version` fails in the clean venv with a missing entry point.** Confirm
  `[project.scripts] crossroads = "crossroads.console:main"` is intact in `pyproject.toml`.
- **Build fails with a version-detection error.** Run inside the git tree; `git tag --list`
  should show `v0.9.0`; `fallback_version` covers the no-git case.
- **Rollback:** `rm -rf dist build` and `git checkout -- pyproject.toml` (if Step 6 edited
  it). No commits were made.
