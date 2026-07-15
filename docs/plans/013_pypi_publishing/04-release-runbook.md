# Stage 04 — Release Runbook (maintainer-executed)
> Part of PyPI Publishing (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Stages 01–03 are complete and their edits are in the working tree (not yet committed):
```bash
cd /Users/will/Documents/Code/Crossroads
git status                                            # shows the 01–03 edits, uncommitted
grep -n '## \[1.0.0\]' CHANGELOG.md                   # Stage 01
grep -n 'version:' CITATION.cff                        # "1.0.0" (Stage 01)
ls .github/workflows/publish.yml                       # Stage 03
ls docs/maintenance/pypi-release.md                    # Stage 03
python -m pytest -q                                    # green
python -m build && python -m twine check dist/*        # Stage 02 checks pass
```
The maintainer must also have completed the one-time **Trusted Publishing setup** on both
PyPI and TestPyPI, and created the `pypi` / `testpypi` GitHub environments — see
`docs/maintenance/pypi-release.md` (written in Stage 03).

## Objective

Execute the release: land the version-bump + workflow on `main`, tag `v1.0.0`, validate the
exact `1.0.0` artifact on TestPyPI, then publish it to PyPI — and verify a real
`pip install crossroads-uk` gives `1.0.0`.

> **This stage is performed by the maintainer.** It contains `git commit`, `git tag`,
> `git push`, and GitHub Release actions, which the AI executor must NOT run without explicit
> per-action permission (CLAUDE.md). The AI's role here is to *walk the maintainer through it*
> and to complete `docs/maintenance/pypi-release.md` as the durable runbook — not to press the
> buttons.

## Implementation Steps

**Ordering constraint (why the sequence matters):** the version is derived from the git tag,
and the workflow file must exist *in the tagged commit* for a tag-ref dispatch to run it. So
everything (Stage 01 edits + Stage 03 workflow) goes into one commit, that commit is tagged
`v1.0.0`, and all publishing runs from that tag. Same `1.0.0` build → TestPyPI, then → PyPI.

**Step 1 — Commit the release changes (maintainer).**
Bundle the Stage 01 metadata edits and the Stage 03 workflow into a single release commit:
```bash
cd /Users/will/Documents/Code/Crossroads
git add CHANGELOG.md CITATION.cff pyproject.toml tests/test_release.py \
        .github/workflows/publish.yml docs/maintenance/pypi-release.md \
        docs/plans/013_pypi_publishing
git commit -m "Release 1.0.0: PyPI publishing workflow + version bump"
git push origin main
```
(If a Stage 02 `pyproject.toml` force-include was added, it is already part of the staged
`pyproject.toml`.)

**Step 2 — Wait for CI to pass on `main` (release gate).**
Open the repo's **Actions** tab; confirm the existing `tests` workflow is green on the
release commit for both Python 3.11 and 3.12. Do not tag until it is green — a red release
commit must not become `1.0.0`.

**Step 3 — Tag `v1.0.0` and push the tag (maintainer).**
```bash
git tag -a v1.0.0 -m "Crossroads-UK 1.0.0"
git push origin v1.0.0
```
This is the tag hatch-vcs reads to stamp a clean `1.0.0`. Because the tag points at the
commit from Step 1, that commit contains `publish.yml`, so the tag ref can run the workflow.

**Step 4 — Dry run: publish `1.0.0` to TestPyPI (`workflow_dispatch`).**
GitHub → **Actions** → **publish** workflow → **Run workflow**. In "Use workflow from", select
the **tag `v1.0.0`** (not `main`), then Run. This triggers the `build` + `publish-testpypi`
jobs.
- Confirm the run is green and the `publish-testpypi` job uploaded successfully.
- If you set the `testpypi` environment to require approval, approve it when prompted.
- The uploaded version must be exactly `1.0.0` (proving `fetch-depth: 0` + the tag ref worked).
  If the Actions log shows a dev version like `0.9.1.devN+g...`, the run was from `main`, not
  the tag — re-run selecting the `v1.0.0` tag.

**Step 5 — Verify the TestPyPI install in a clean venv.**
TestPyPI does not host dependencies like `duckdb`, so allow real PyPI as an extra index:
```bash
python3 -m venv /tmp/cr_testpypi
/tmp/cr_testpypi/bin/pip install --upgrade pip
/tmp/cr_testpypi/bin/pip install \
  -i https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple \
  "crossroads-uk==1.0.0"
/tmp/cr_testpypi/bin/crossroads --version          # expect: crossroads 1.0.0
/tmp/cr_testpypi/bin/python -c "import crossroads; print(crossroads.__version__)"  # 1.0.0
rm -rf /tmp/cr_testpypi
```
Expected: both print `1.0.0`. This confirms the exact artifact that will go to PyPI installs
cleanly and reports the right version. If `crossroads --version` shows a different number, do
**not** proceed — the tag/version wiring is wrong.

**Step 6 — Publish the GitHub Release → uploads to PyPI.**
GitHub → **Releases** → **Draft a new release**:
- Choose the existing tag **`v1.0.0`** (do not create a new tag here).
- Title: `Crossroads-UK 1.0.0`. Description: paste the `## [1.0.0]` section from
  `CHANGELOG.md`.
- Click **Publish release**.
This fires the `release: published` event; the `build` + `publish-pypi` jobs run and upload
`1.0.0` to real PyPI via OIDC. Approve the `pypi` environment if you gated it. Watch the
Actions run to green.

**Step 7 — Verify the real PyPI install (the ship signal).**
```bash
python3 -m venv /tmp/cr_pypi
/tmp/cr_pypi/bin/pip install --upgrade pip
/tmp/cr_pypi/bin/pip install "crossroads-uk==1.0.0"     # plain PyPI, no extra index
/tmp/cr_pypi/bin/crossroads --version                    # expect: crossroads 1.0.0
rm -rf /tmp/cr_pypi
```
Also open `https://pypi.org/project/crossroads-uk/` and confirm the page renders the README,
shows version `1.0.0`, the MIT license, and the project URLs.

**Step 8 — Record the release in the runbook.**
Append to `docs/maintenance/pypi-release.md` a short "How to cut a release" section capturing
Steps 1–7 as the repeatable procedure for the next version (commit → CI green → tag → dispatch
to TestPyPI → verify → GitHub Release → verify PyPI), so future releases do not need this plan.

## Testing & Verification

The runbook steps are themselves the end-to-end test. The definitive ship checks:
- **Step 5:** clean-venv install from **TestPyPI** yields `crossroads 1.0.0`.
- **Step 7:** clean-venv `pip install crossroads-uk` from **PyPI** yields `crossroads 1.0.0`.
- The PyPI project page renders and shows the correct metadata.

**Stage ship-readiness checklist:**
- [ ] Release commit on `main`; `tests` CI green on it (3.11 + 3.12)
- [ ] `v1.0.0` tag pushed, pointing at that commit
- [ ] `publish` workflow dispatched **from the tag** → TestPyPI upload of `1.0.0` succeeded
- [ ] clean-venv TestPyPI install prints `crossroads 1.0.0`
- [ ] GitHub Release `v1.0.0` published → `publish-pypi` job green → PyPI has `1.0.0`
- [ ] clean-venv `pip install crossroads-uk` prints `crossroads 1.0.0`
- [ ] `https://pypi.org/project/crossroads-uk/` renders the README and correct metadata
- [ ] `docs/maintenance/pypi-release.md` updated with the repeatable release procedure

## End State / Handoff

`crossroads-uk` version `1.0.0` is live on PyPI and installable with `pip install
crossroads-uk`, its README renders on the project page, and the release is reproducible from
the `v1.0.0` tag. The Trusted-Publishing workflow and the maintenance runbook make every
future release a matter of: commit → tag → dispatch (TestPyPI) → GitHub Release (PyPI). The
PyPI publishing effort is complete.

## Failure Modes & Rollback

- **A PyPI upload with a filename/version already used fails.** PyPI never allows re-uploading
  a given version's files, even after deletion. If `1.0.0` was partially/incorrectly uploaded,
  you cannot reuse it — cut `1.0.1` (a fresh tag + release). This is exactly why Step 4–5
  validate on TestPyPI first.
- **`crossroads --version` shows a dev version after publishing.** The workflow ran from
  `main`, not the `v1.0.0` tag, or `fetch-depth: 0` is missing. Re-dispatch from the tag;
  never hand-edit a version.
- **OIDC rejected during upload.** The pending-publisher config (owner/repo/workflow/
  environment) does not match on that index — re-check `docs/maintenance/pypi-release.md` for
  PyPI vs TestPyPI.
- **TestPyPI install cannot find `duckdb`.** Expected — add `--extra-index-url
  https://pypi.org/simple` (Step 5). Do not publish `duckdb` anywhere.
- **Rollback:** you cannot un-publish a PyPI version for reuse; you can *yank* a broken
  release (marks it non-default for installers) and publish a fixed `1.0.1`. The git tag can
  be deleted/re-pushed only if no publish happened from it yet. Before any publish, rollback is
  simply `git checkout -- .` and deleting the local tag.
