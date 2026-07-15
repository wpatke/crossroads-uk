# Stage 03 — Trusted-Publishing Release Workflow
> Part of PyPI Publishing (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Stage 02 is done (a real `python -m build` is proven to produce correct artifacts). Verify:
```bash
cd /Users/will/Documents/Code/Crossroads
ls .github/workflows/                         # expect: tests.yml (the existing CI)
grep -n 'source = "vcs"' pyproject.toml       # hatch-vcs version source present
git remote -v                                  # origin -> https://github.com/wpatke/crossroads-uk.git
```

This stage adds a workflow file and writes maintainer setup instructions. It does **not**
push, commit, create accounts, or publish.

## Objective

Add `.github/workflows/publish.yml` that builds the distribution and uploads it via **PyPI
Trusted Publishing (OIDC)** — no stored tokens. A manual run (`workflow_dispatch`) publishes
to **TestPyPI** (the dry run); publishing a GitHub **Release** publishes to **PyPI**. Also
document the one-time PyPI/TestPyPI "pending publisher" and GitHub "environment" setup the
maintainer must do for OIDC to be trusted.

## Implementation Steps

**Step 1 — Create `.github/workflows/publish.yml`.**
Exact contents (pin action major versions; keep comments plain-language per CLAUDE.md):
```yaml
name: publish

# Two ways to run:
#   - Manually (workflow_dispatch): builds and uploads to TestPyPI. This is the dry run.
#   - When a GitHub Release is published: builds and uploads to real PyPI.
# Uploads use PyPI Trusted Publishing (OIDC): no API tokens or passwords are stored.
on:
  workflow_dispatch:
  release:
    types: [published]

jobs:
  build:
    name: Build sdist + wheel
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          # hatch-vcs derives the version from git tags, so we need full history
          # AND tags. Without fetch-depth: 0 the version silently falls back.
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - name: Build and metadata-check
        run: |
          python -m pip install --upgrade pip build twine
          python -m build
          python -m twine check dist/*

      - name: Upload built distribution as an artifact
        uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/

  publish-testpypi:
    name: Publish to TestPyPI (dry run)
    # Only on a manual run — this is the pre-release validation upload.
    if: github.event_name == 'workflow_dispatch'
    needs: build
    runs-on: ubuntu-latest
    environment: testpypi
    permissions:
      id-token: write        # required for OIDC Trusted Publishing
    steps:
      - name: Download the built distribution
        uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/

      - name: Publish to TestPyPI
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: https://test.pypi.org/legacy/

  publish-pypi:
    name: Publish to PyPI
    # Only when a GitHub Release is published — the real release.
    if: github.event_name == 'release'
    needs: build
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write        # required for OIDC Trusted Publishing
    steps:
      - name: Download the built distribution
        uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```
Notes for the executor:
- `fetch-depth: 0` on checkout is **mandatory** — it is the release-day footgun. It fetches
  tags too, which is what lets hatch-vcs compute `1.0.0` from the `v1.0.0` tag.
- The default `pypa/gh-action-pypi-publish@release/v1` step (no `repository-url`) targets real
  PyPI; the TestPyPI job overrides `repository-url`.
- One `build` job feeds both publish jobs; only one publish job runs per event, gated by the
  `if:` conditions.
- Do not add the `weather` extra or run tests here — CI (`tests.yml`) already gates tests on
  push/PR; this workflow only builds and publishes.

**Step 2 — Validate the workflow YAML locally (offline).**
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
python -c "import yaml; yaml.safe_load(open('.github/workflows/publish.yml')); print('publish.yml YAML OK')"
```
Expected: `publish.yml YAML OK`. (PyYAML is a `dev` extra dependency, so it is installed.)

**Step 3 — Write the maintainer setup instructions.**
These are actions the **maintainer** performs in a browser; the executor cannot create
accounts or enter credentials. Record them where the maintainer will find them — create
`docs/maintenance/pypi-release.md` (a short runbook stub that Stage 04 completes), starting
with the one-time Trusted-Publishing setup:

```markdown
# PyPI release — one-time Trusted Publishing setup

Trusted Publishing lets GitHub Actions upload to PyPI with no stored token, by trusting a
specific repo + workflow file + environment. Do this once per index (PyPI and TestPyPI).

## GitHub: create two environments
Repo → Settings → Environments → New environment. Create:
- `pypi`
- `testpypi`
(No secrets needed. Optionally add yourself as a required reviewer on `pypi` for a manual
approval gate before the real upload.)

## PyPI (https://pypi.org) — add a pending publisher
1. Create/log in to a PyPI account (maintainer task — cannot be automated).
2. Account → Publishing → "Add a new pending publisher" (GitHub Actions):
   - PyPI Project Name: `crossroads-uk`
   - Owner: `wpatke`
   - Repository name: `crossroads-uk`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
   "Pending" means the project does not exist yet; the first successful upload creates it and
   binds the trust.

## TestPyPI (https://test.pypi.org) — add a pending publisher
TestPyPI is a completely separate site with its own account. Repeat the same steps there:
   - PyPI Project Name: `crossroads-uk`
   - Owner: `wpatke`
   - Repository name: `crossroads-uk`
   - Workflow name: `publish.yml`
   - Environment name: `testpypi`

The workflow file name (`publish.yml`) and environment names (`pypi` / `testpypi`) MUST match
the workflow exactly, or the OIDC exchange is rejected.
```
> If the maintainer chose **not** to use GitHub Environments, remove the `environment:` keys
> from `publish.yml` and leave the "Environment name" blank in both pending publishers. Using
> environments is recommended (it enables an approval gate on the real PyPI upload), so keep
> them unless told otherwise.

## Testing & Verification

Full end-to-end verification requires a push and the maintainer's PyPI setup, so it lives in
Stage 04. What is verifiable now, offline:

**Workflow is present and valid:**
```bash
ls .github/workflows/publish.yml
python -c "import yaml; d=yaml.safe_load(open('.github/workflows/publish.yml')); \
print('jobs:', list(d['jobs'])); \
assert d['jobs']['build']['steps'][0]['with']['fetch-depth'] == 0, 'fetch-depth must be 0'; \
assert d['jobs']['publish-pypi']['permissions']['id-token'] == 'write'; \
assert d['jobs']['publish-testpypi']['permissions']['id-token'] == 'write'; \
print('publish.yml structure OK')"
```
Expected: prints the three job names and `publish.yml structure OK`. The assertions guard the
two things that most commonly break Trusted Publishing: `fetch-depth: 0` and
`id-token: write`.

**Stage ship-readiness checklist:**
- [ ] `.github/workflows/publish.yml` exists and is valid YAML
- [ ] `build` job checks out with `fetch-depth: 0`, builds, and runs `twine check`
- [ ] `publish-testpypi` runs only on `workflow_dispatch`, has `id-token: write`, targets
      `test.pypi.org/legacy/`, environment `testpypi`
- [ ] `publish-pypi` runs only on `release: published`, has `id-token: write`, targets PyPI
      (default), environment `pypi`
- [ ] `docs/maintenance/pypi-release.md` documents the PyPI + TestPyPI pending-publisher and
      the two GitHub environments, with names matching the workflow exactly
- [ ] nothing committed

## End State / Handoff

The repository contains a Trusted-Publishing workflow that will build and upload the package
on demand (TestPyPI) and on release (PyPI), plus a maintenance doc telling the maintainer how
to configure the trust on both indexes. Stage 04 executes the release: it assumes this
workflow file exists and will be committed **at or before** the `v1.0.0` tag (the workflow
must be present in the tagged commit for a tag-ref dispatch to run it).

## Failure Modes & Rollback

- **OIDC upload later rejected ("not a trusted publisher").** The pending-publisher fields do
  not match: check owner `wpatke`, repo `crossroads-uk`, workflow `publish.yml`, and the
  environment name, on the correct index (PyPI vs TestPyPI are separate).
- **Version uploads as `0.0.0`/a dev value instead of `1.0.0`.** `fetch-depth: 0` missing, or
  the run was not from the `v1.0.0` tag ref. Both are handled in Stage 04's runbook.
- **`environment:` set in the workflow but the GitHub environment does not exist.** Create
  `pypi` and `testpypi` under Settings → Environments, or remove the `environment:` keys.
- **Rollback:** delete `.github/workflows/publish.yml` and `docs/maintenance/pypi-release.md`.
  No source, tests, or existing CI are affected.
