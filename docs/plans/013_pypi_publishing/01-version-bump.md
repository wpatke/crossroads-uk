# Stage 01 — Version Bump to 1.0.0
> Part of PyPI Publishing (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

No prior stage required. Verify the starting repo state:

```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
git tag --list                                   # expect: v0.9.0 (only)
grep -n "Unreleased\|## \[0.9.0\]" CHANGELOG.md   # expect an [Unreleased] section + a [0.9.0] section
grep -n 'version:' CITATION.cff                   # expect: version: "0.9.0"
grep -n '0.9.0' tests/test_release.py             # expect ONE hit, near line 57
grep -n 'Development Status' pyproject.toml        # expect: "Development Status :: 4 - Beta"
python -m pytest -q                                # expect: green (baseline)
```

You are editing metadata only; there is no behavioural code change in this stage. **Do not
commit** anything — Stage 04 (the maintainer runbook) owns the commit and tag.

## Objective

Describe the project as **1.0.0** in every place a human-owned version literal lives:
the changelog, the citation file, the one hardcoded test assertion, and the maturity
classifier. After this stage the working tree *reads as 1.0.0*, `pytest -q` is green, and
nothing is committed. The actual version number that ships is still produced by hatch-vcs
from the `v1.0.0` git tag created later in Stage 04 — these edits are the human-authored
metadata that must agree with that tag.

## Implementation Steps

**Step 1 — Promote the CHANGELOG `[Unreleased]` section to `[1.0.0]`.**
File: `CHANGELOG.md`. Currently it has an `## [Unreleased]` section (with `### Added` /
`### Changed` content for solar geometry + AADF) above `## [0.9.0] - 2026-07-10`.

1a. Rename the heading `## [Unreleased]` to `## [1.0.0] - <RELEASE_DATE>`, where
`<RELEASE_DATE>` is the actual date you cut the release, ISO format `YYYY-MM-DD` (e.g.
`2026-07-15`). Do not invent a different date; use the day the tag is created.

1b. Immediately under that new heading, add a one-line lead describing the milestone, before
the existing `### Added`/`### Changed` blocks. Suggested text:
```markdown
First stable release, and the first published to PyPI (`pip install crossroads-uk`). The
DuckDB schema and public API (`crossroads` CLI, `init_engine`, `build`) are now the stable
contract described above; subsequent changes follow the MAJOR/MINOR policy at the top of
this file.
```
Keep the existing `### Added` and `### Changed` bullet lists (solar geometry, AADF,
`schema_version` bumps) exactly as they are — they are the content of the 1.0.0 release.

1c. Add a link reference for the new version at the bottom of the file, next to the existing
`[0.9.0]:` line:
```markdown
[1.0.0]: https://github.com/wpatke/crossroads-uk/releases/tag/v1.0.0
```
Leave the existing `[0.9.0]: ...` line in place.

Expected result: `CHANGELOG.md` has `## [1.0.0] - <date>` as its top release section, no
`[Unreleased]` heading remains, and both `[1.0.0]:` and `[0.9.0]:` link references exist.

**Step 2 — Update `CITATION.cff` to 1.0.0.**
File: `CITATION.cff`. Change two fields:
```yaml
version: "1.0.0"
date-released: "<RELEASE_DATE>"    # same YYYY-MM-DD as the CHANGELOG heading in Step 1a
```
(Currently `version: "0.9.0"` and `date-released: "2026-07-10"`.) Leave everything else —
authors (`Will Patke`), title, abstract, license, repository-code — unchanged.

Expected result: `grep version: CITATION.cff` shows `1.0.0`, and `date-released` matches the
CHANGELOG release date.

**Step 3 — Update the one hardcoded version literal in the tests.**
File: `tests/test_release.py`. The `test_citation_cff_is_valid` test ends with an assertion
that pins CITATION.cff's version. Change it from `0.9.0` to `1.0.0`:
```python
        assert data["version"] == "1.0.0"
```
This is the **only** hardcoded version literal in the test suite — every other test
(`test_provenance.py`, `test_package.py`, `test_console.py`) reads `crossroads.__version__`
dynamically and needs no change. Do not add new literals; this assertion exists to keep the
citation file honest, so it tracks the citation file's value.

Expected result: `grep -n '0.9.0' tests/test_release.py` returns nothing; the assertion now
checks `"1.0.0"`.

**Step 4 — Bump the maturity classifier (reversible; see note).**
File: `pyproject.toml`, in `[project].classifiers`. Change:
```toml
  "Development Status :: 5 - Production/Stable",
```
(replacing `"Development Status :: 4 - Beta"`).

> **Why:** the CHANGELOG's versioning policy states the stable-contract guarantees take
> effect at 1.0.0, so 1.0.0 is `Production/Stable`. **If the maintainer prefers to remain
> Beta** for the first PyPI appearance, skip this one edit and leave `4 - Beta`; it is
> isolated, changes no behaviour, and no test asserts on it. Note the choice made.

Expected result: exactly one `Development Status` classifier line, reading
`5 - Production/Stable` (unless the Beta choice is taken).

## Testing & Verification

**Primary — the release-invariant suite passes with the new citation value:**
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
python -m pytest tests/test_release.py -q
```
Expected: green. If `test_citation_cff_is_valid` fails asserting `"0.9.0" == "1.0.0"` or the
reverse, Steps 2 and 3 are out of sync — the CITATION.cff `version:` and the test literal
must both say `1.0.0`.

**Full offline suite (nothing behavioural changed, so it must stay green):**
```bash
python -m pytest -q
```
Expected: green. If any *other* test fails on a version string, it was reading a literal it
should not — change it to read `crossroads.__version__` and note the deviation.

> Note on `crossroads --version` at this point: because hatch-vcs derives the version from
> git and the `v1.0.0` tag does **not exist yet**, an editable install here still reports a
> `0.9.x.devN+g...` value, not `1.0.0`. That is expected and correct — the clean `1.0.0`
> appears only after Stage 04 creates the tag and the package is rebuilt/reinstalled. Do not
> "fix" this by hardcoding a version anywhere.

**Stage ship-readiness checklist:**
- [ ] `CHANGELOG.md`: top release section is `## [1.0.0] - <date>`; no `[Unreleased]`
      heading remains; `[1.0.0]:` and `[0.9.0]:` link refs both present
- [ ] `CITATION.cff`: `version: "1.0.0"` and `date-released:` matches the CHANGELOG date
- [ ] `tests/test_release.py`: the citation assertion checks `"1.0.0"`; no `0.9.0` literal left
- [ ] `pyproject.toml`: `Development Status :: 5 - Production/Stable` (or a noted decision to
      keep `4 - Beta`)
- [ ] `python -m pytest -q` is green
- [ ] **Nothing committed** (verify with `git status` — changes are staged/unstaged, not committed)

## End State / Handoff

The working tree describes version 1.0.0 consistently across the changelog, citation file,
test literal, and classifier, and the full offline suite is green. Nothing is committed —
Stage 04 bundles these edits (plus the Stage 03 workflow) into the single commit that gets
tagged `v1.0.0`. Stage 02 may assume these edits are present when it builds and validates the
artifacts (the CHANGELOG/CITATION values are baked into the sdist/metadata).

## Failure Modes & Rollback

- **`test_citation_cff_is_valid` fails after the edit.** The `version:` in `CITATION.cff` and
  the literal in `tests/test_release.py` disagree — make both `1.0.0`.
- **Some other test suddenly fails on a version string.** It hardcoded a version it should
  read dynamically; switch it to `crossroads.__version__` and note it.
- **Rollback:** `git checkout -- CHANGELOG.md CITATION.cff tests/test_release.py pyproject.toml`
  restores the pre-stage state (nothing was committed).
