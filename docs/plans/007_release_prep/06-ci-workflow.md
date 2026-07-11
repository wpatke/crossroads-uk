# Stage 06 — Continuous Integration (GitHub Actions)
> Part of Release Preparation (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Depends on Stage 01 (the package installs cleanly via `pip install -e ".[dev]"` with the dynamic
version). Verify:
```bash
cd /Users/will/Documents/Code/Crossroads
ls .github/workflows/ 2>/dev/null || echo "no workflows yet (expected)"
python -m pytest -q            # the suite this workflow will run must already pass locally
grep -n "requires-python\|3.11\|3.12" pyproject.toml
```
`requires-python = ">=3.11"` and the classifiers advertise 3.11 + 3.12, so CI must prove both.

## Objective

Add a free GitHub Actions workflow that runs the offline test suite on every push and pull request,
across Python 3.11 and 3.12, on GitHub-hosted runners — so the advertised support matrix is
continuously proven and the maintainer never has to remember to run it. No local infrastructure and
no cost (unlimited minutes for public repos; 2,000 free minutes/month if the repo is private — this
suite uses seconds).

## Implementation Steps

**Step 1 — Create `.github/workflows/tests.yml`:**
```yaml
name: tests

# Run on pushes to main and on every pull request.
on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12"]

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}

      - name: Install (with dev extra)
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"

      - name: Run the offline test suite
        run: python -m pytest -q
```
Notes for the executor:
- The default `pytest` config already deselects `-m integration` (see `[tool.pytest.ini_options]`
  in `pyproject.toml`), so CI stays offline and deterministic — it does **not** reach DfT/ONS/CDS.
  Do not add `-m integration` here.
- Do **not** install the `weather` extra in CI: `cdsapi`/`netCDF4` are heavy and the offline suite
  is designed to pass without them (weather tests seed a synthetic `.nc`). Keeping CI on `[dev]`
  only mirrors the default local suite.
- Pin action versions (`@v4`, `@v5`) as shown for reproducible CI runs.

**Step 2 — Add a CI status badge to the README.**
In `README.md` (rewritten in Stage 03), add directly under the `# Crossroads-UK` title:
```markdown
[![tests](https://github.com/wpatke/crossroads-uk/actions/workflows/tests.yml/badge.svg)](https://github.com/wpatke/crossroads-uk/actions/workflows/tests.yml)
```
> The badge renders as "no status" until the workflow has run once on `main`; that is expected
> before the first push.

**Step 3 — Document that CI is the release gate.**
The green Actions run on the release commit is the final ship signal (overview Global Testing #6).
If a `RELEASING`/checklist note exists, add: "wait for the `tests` workflow to pass on `main`, then
tag `v1.0.0`." Otherwise this lives in the overview and Stage 01's tag step.

## Testing & Verification

**Local sanity — the workflow runs exactly what CI will run:**
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
```
Expected: green. CI runs this same command on 3.11 and 3.12; if it passes locally on 3.12 and the
code uses no 3.12-only syntax, both legs pass.

**YAML validity (offline check, no new dependency required):**
```bash
python -c "import importlib.util as u; print('pyyaml' if u.find_spec('yaml') else 'no pyyaml')"
# If pyyaml is available (often transitively present):
python -c "import yaml; yaml.safe_load(open('.github/workflows/tests.yml')); print('workflow YAML OK')"
```
Expected: `workflow YAML OK` (or skip cleanly if PyYAML is genuinely absent — GitHub validates it on
push regardless).

**End-to-end verification (requires a push — the human does this):**
1. Commit the workflow and push the branch / open a PR.
2. Open the repo's **Actions** tab; confirm the `tests` workflow runs and both matrix legs
   (3.11, 3.12) go green.
3. Confirm the README badge turns green after the run on `main`.
> The executor must **not** push or commit without explicit user permission (CLAUDE.md). Prepare the
> files; the human performs the push that triggers the first CI run.

**Stage ship-readiness checklist:**
- [ ] `.github/workflows/tests.yml` exists and is valid YAML
- [ ] matrix covers Python 3.11 and 3.12; installs `.[dev]`; runs `python -m pytest -q` (offline)
- [ ] README has the `tests` status badge
- [ ] (after the human pushes) both matrix legs are green in the Actions tab

## End State / Handoff

Every push/PR is automatically tested on the advertised Python matrix, on GitHub's runners, for
free. Combined with Stages 01–05, the release is version-clean, licence-documented, well-described,
reference-clean, and continuously verified — ready for the human to tag `v1.0.0` once the workflow is
green on `main`.

## Failure Modes & Rollback

- **CI fails on 3.11 but passes locally on 3.12.** The code likely uses a 3.12-only feature. Either
  fix the code to the `requires-python = ">=3.11"` floor, or (only if intentional) raise the floor to
  `>=3.12` and update the classifiers/matrix — note the decision.
- **`pip install -e ".[dev]"` fails in CI but works locally.** Usually a version-source/build issue
  from Stage 01; confirm `[tool.hatch.version]` points at `src/crossroads/__init__.py` and that
  `__version__` is a plain literal.
- **Badge shows "no status".** The workflow has not yet run on `main` — expected until the first push.
- **Rollback:** delete `.github/workflows/tests.yml` and remove the README badge line. No source or
  test code is affected.
