# Stage 04 — Reference Fixes & Repository Hygiene
> Part of Release Preparation (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Assumes Stage 03 has run (it performs the `docs/AI_DISCLOSURE.md` → `docs/ai-disclosure.md` rename
and updates the `spec.md` links, so the README/spec references are consistent). The repo-wide
link-integrity test added here also covers the files created in Stages 02–03 and 08, so it is best
run last. Verify:
```bash
cd /Users/will/Documents/Code/Crossroads
ls docs/ai-disclosure.md 2>/dev/null || echo "run Stage 03 first (it does the rename)"
grep -n "AI_DISCLOSURE" docs/spec.md             # remaining occurrence: the §7 blueprint tree only
grep -n "weather_test\|^\*\.db\|^\*\.duckdb" .gitignore
git status --porcelain | grep -i "weather_test" || echo "no weather_test in tree now"
```
> This stage applies the documentation naming scheme from the overview's Cross-Cutting
> Constraints: only `README.md`/`LICENSE`/`CHANGELOG.md`/`CITATION.cff` stay UPPER-CASE; the content
> docs are lower-case under `docs/`. The `ai-disclosure.md` rename itself is done in Stage 03; this
> stage only corrects the stale §7 blueprint tree that still names it at the old location.

## Objective

Remove the dangling and stale internal references before release, add the safety-net test
that catches this class of drift, ensure a non-affiliation disclaimer is present, and stop
extensionless build artifacts from being committed.

## Implementation Steps

> **Note:** the `docs/AI_DISCLOSURE.md` → `docs/ai-disclosure.md` rename and the two `spec.md`
> link updates are performed in **Stage 03** (so the README and spec links are consistent the moment
> the README is written). This stage assumes that rename is already done and only fixes the §7 tree.

**Step 1 — Fix the stale location in `docs/spec.md` §7 "Repository Blueprint".**
The §7 tree lists `AI_DISCLOSURE.md` at the repo root, but it lives under `docs/`. Delete the
root-level `├── AI_DISCLOSURE.md` line and show the real `docs/` contents:
```text
├── docs/
│   ├── ai-disclosure.md
│   ├── data-sources.md
│   ├── methodology.md
│   ├── schema.md
│   ├── spec.md
│   └── plans/                              # Implementation plans (numbered subdirectories)
```
> The §7 plan-subdirectory *names* (e.g. `001_spatial_infrastructure`) are explicitly labelled
> "examples" in the spec, so they need not match the real `docs/plans/` names. Fix the
> `ai-disclosure.md` location and, while here, list the other new `docs/` files for accuracy.

**Step 2 — Fix the spec's headline API example (`docs/spec.md` §8, and §1).**
§8 currently shows an example that does not match the real `build()` signature:
```python
client.build(
    years=[2022, 2023, 2024],
    include_weather=True,
    spatial_grain="local_authority"
)
```
The real API (verified in `src/crossroads/client.py` and `src/crossroads/console.py`) selects
datasets by `source_id` and controls boundaries with `boundary_mode`. Replace the block with:
```python
client = cr.init_engine(database_path="local_analytics.db")
client.build(
    datasets=["stats19", "era5_weather"],   # weather is a selectable dataset, not a flag
    years=[2022, 2023, 2024],
    boundary_mode="snapshot",               # or "temporal"
)
client.close()
```
> Verify the exact `source_id`s against the code before writing: `stats19` (`transformers/stats19.py`)
> and `era5_weather` (`transformers/weather.py`, `source_id = "era5_weather"`). Boundaries run via
> `boundary_mode`, not as a menu dataset.

Also in §1, the illustrative `regions: list[str]` argument is **not implemented**. Either remove it
or mark it explicitly as a *future/illustrative* parameter, so a reader does not assume it exists.
(The `include_weather` mention in §4's `BaseTransformer` docstring example is a clearly-hypothetical
"e.g. a weather source" and may be left, or aligned to the real `is_active` gate — low priority.)

The same phantom kwargs appear in **committed source**: `src/crossroads/client.py`'s `build` docstring
(lines ~26-28) says `` ``years=[...]``, ``include_weather=True``, ``spatial_grain="local_authority"`` ``.
Fix it to the real forwarded kwargs — `datasets`, `years`, `boundary_mode`, and the optional
`reject_ceiling` — so the code's own documentation matches its behaviour. (This is API-accuracy, not a
planning-artifact reference, so it belongs here rather than in Stage 05's scrub.)

**Step 2b — Confirm every reference to the renamed file is updated.** After Stage 03's rename/link
updates and this stage's §7 fix, grep the whole repo (excluding `docs/plans/**`, which are these plan
files) for any surviving upper-case reference:
```bash
grep -rn "AI_DISCLOSURE" --include=*.md . | grep -v "docs/plans/"
```
Expected: nothing. The README (Stage 03) links `docs/ai-disclosure.md`; `spec.md` links
`ai-disclosure.md`; the §7 blueprint (this stage) shows `docs/ai-disclosure.md`. Step 4's strengthened
test then proves all of this mechanically.

**Step 3 — Ensure a non-affiliation disclaimer exists.**
Stage 03's README already contains: *"This project is not affiliated with or endorsed by
the Department for Transport, the Office for National Statistics, Ordnance Survey, or
Copernicus/ECMWF."* Verify it is present in `README.md`:
```bash
grep -n "not affiliated" README.md
```
If Stage 03 has not run yet, add that sentence to the README's "Licence & AI disclosure"
section (or create a short `## Disclaimer` section). Do not duplicate it across many files
— one canonical statement in the README is sufficient.

**Step 3b — Align the `LICENSE` copyright line with the author's name.**
`LICENSE` reads `Copyright (c) 2026 wpatke`. Change it to match `CITATION.cff` (Stage 03):
```text
Copyright (c) 2026 Will Patke
```

**Step 4 — Add a strengthened repo-wide reference-integrity test.**
This is the safety net that catches the whole class of drift (and would have caught the
`AI_DISCLOSURE` rename). It checks three things: (a) relative **file** links resolve; (b)
`path#anchor` **fragments** match a real heading in the target markdown file; (c) `§N`
**spec-section citations** correspond to a real section number in `docs/spec.md`. Create
`tests/test_docs_links.py`:
```python
"""Fail the build if a committed doc points at something that does not exist.

Scans first-party markdown (README, docs/, but NOT docs/plans/** or vendored trees) and checks:
  (a) relative file links resolve to a real file;
  (b) `file.md#anchor` fragments match a GitHub-style heading slug in that file;
  (c) `§N` spec-section citations name a real section number in docs/spec.md.
External (http/https/mailto) links and bare #anchors are not file-checked. Offline, deterministic.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
SPEC = os.path.join(REPO_ROOT, "docs", "spec.md")

# Never scan: implementation plans (may reference future files), venv, coverage, VCS internals.
SKIP_DIRS = {".venv", ".git", "htmlcov", "node_modules", "__pycache__", ".pytest_cache"}
SKIP_PATH_PARTS = (os.path.join("docs", "plans"),)

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.M)
# Section citations like "§9", "§3A", "§5 Phase 4" -> capture the leading number (+ optional letter).
SECTION_CITE_RE = re.compile(r"§\s*(\d+)([A-Z]?)")


def _iter_markdown_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if any(part in dirpath for part in SKIP_PATH_PARTS):
            continue
        for name in filenames:
            if name.endswith(".md"):
                yield os.path.join(dirpath, name)


def _slug(heading):
    """GitHub-style anchor slug: lower-case, drop punctuation, spaces -> hyphens."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s+", "-", s)


def _anchors_of(path):
    if not path.endswith(".md") or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {_slug(h) for h in HEADING_RE.findall(fh.read())}


def _spec_section_numbers():
    """Section numbers present in spec.md, e.g. {'1','2','3','3A','3B','5','9'}.
    Spec headings look like '## 3. Ingestion' and '### A. Spatial ...' nested under a number."""
    numbers, current = set(), None
    with open(SPEC, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^##\s+(\d+)\.", line)      # top-level "## N. Title"
            if m:
                current = m.group(1); numbers.add(current); continue
            m = re.match(r"^###\s+([A-Z])\.", line)   # sub "### A. Title" -> "NA"
            if m and current:
                numbers.add(current + m.group(1))
    return numbers


def test_relative_markdown_links_resolve():
    broken = []
    for md_path in _iter_markdown_files():
        with open(md_path, encoding="utf-8") as fh:
            text = fh.read()
        for target in LINK_RE.findall(text):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            raw = target.strip("<>").strip('"').strip("'")
            local, _, frag = raw.partition("#")
            local = local.strip()
            if not local:
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(md_path), local))
            rel = os.path.relpath(md_path, REPO_ROOT)
            if not os.path.exists(resolved):
                broken.append(f"{rel} -> {target} (missing file)")
            elif frag and _slug(frag) not in _anchors_of(resolved):
                broken.append(f"{rel} -> {target} (no such anchor #{frag})")
    assert not broken, "Broken markdown references:\n" + "\n".join(broken)


def test_spec_section_citations_exist():
    valid = _spec_section_numbers()
    assert valid, "could not parse any section numbers from docs/spec.md"
    bad = []
    for md_path in _iter_markdown_files():
        if os.path.abspath(md_path) == os.path.abspath(SPEC):
            continue  # the spec citing itself internally is fine to skip
        with open(md_path, encoding="utf-8") as fh:
            text = fh.read()
        for num, letter in SECTION_CITE_RE.findall(text):
            cite = num + letter
            if cite not in valid and num not in valid:
                bad.append(f"{os.path.relpath(md_path, REPO_ROOT)} cites spec §{cite} (no such section)")
    assert not bad, "Stale spec-section citations:\n" + "\n".join(bad)
```
> `docs/plans/**` is skipped by the skip rule — plan files legitimately reference future files.
> If the spec's heading style differs from `## N.` / `### A.`, adjust `_spec_section_numbers()`
> to match reality and note it; the goal is "every cited §N is real", not a specific regex.

**Step 5 — Stop extensionless build artifacts from being committed.**
`.gitignore` covers `*.db` and `*.duckdb`, but a stray `weather_test` (a database copy with
*no extension*, seen in the working tree this session) matches neither. Append to
`.gitignore` under the "Crossroads build artifacts" section:
```gitignore
# Extensionless database copies produced during ad-hoc testing
weather_test
```
Then confirm no such artifact is staged:
```bash
git status --porcelain | grep -iE "weather_test|\.db$|\.duckdb$" || echo "clean"
```
> Do **not** delete `crossroads.db`, `test.duckdb`, or `weather_test` from disk as part of
> this stage — they are already git-ignored (or now will be) and deleting user files is out
> of scope. If the user asks, that is a separate, explicit cleanup.

## Testing & Verification

**Integration test (PRIMARY) — repo-wide reference integrity.**
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
python -m pytest tests/test_docs_links.py -q
```
Expected: 2 passed (`test_relative_markdown_links_resolve`, `test_spec_section_citations_exist`).
On failure each assertion lists the exact `file -> target (reason)` — a missing file, a bad
`#anchor`, or a stale `§N` citation — fix each. This is precisely the drift this stage prevents,
and it is the mechanical guarantee that the doc renames left no stale reference.

**Gitignore check.**
```bash
git check-ignore weather_test && echo "ignored OK"
```
Expected: prints `weather_test` and `ignored OK`.

**Full suite.**
```bash
python -m pytest -q
```
Expected: entire offline suite green, including `test_docs_links.py`, `test_release.py`,
and the console notice test from Stage 02.

**Stage ship-readiness checklist:**
- [ ] `docs/spec.md` §7 blueprint shows the `docs/` files lower-case, no root-level `AI_DISCLOSURE.md` line
- [ ] `grep -rn "AI_DISCLOSURE" --include=*.md . | grep -v docs/plans/` is empty (rename done in Stage 03; §7 fixed here)
- [ ] `docs/spec.md` §8 example uses `datasets=[...]` + `boundary_mode=...` (real API); §1 `regions` removed/marked illustrative
- [ ] `src/crossroads/client.py` `build` docstring lists the real kwargs (`datasets`, `years`, `boundary_mode`, `reject_ceiling`) — no `include_weather`/`spatial_grain`
- [ ] `LICENSE` copyright reads `Copyright (c) 2026 Will Patke` (matches `CITATION.cff`)
- [ ] `README.md` contains the non-affiliation disclaimer
- [ ] `.gitignore` ignores `weather_test`; `git check-ignore weather_test` confirms
- [ ] `tests/test_docs_links.py` passes (both link-resolution and `§N`-citation tests); full `python -m pytest` green

## End State / Handoff

Release is documentation-clean: no dangling internal links, a test that keeps it that way,
a non-affiliation disclaimer, and no stray artifacts committable. Combined with Stages
01–03, the repo is ready for the human to perform the release commit + `git tag v1.0.0`
(command documented in Stage 01 — the executor must not run it).

## Failure Modes & Rollback

- **Link test flags a plan file.** It should not — `docs/plans/**` is skipped. If it does,
  confirm `SKIP_PATH_PARTS` uses the OS path separator (`os.path.join`), already handled.
- **Link test flags a legitimately external or templated link** (e.g. a `[year]`
  placeholder inside parentheses). Placeholders like `[year]` are not markdown links
  (no `](`), so they are ignored; genuine external links are skipped by scheme. If a false
  positive appears, narrow the target, don't weaken the test.
- **`§N` citation test is too strict / spec uses a different heading style.** Adjust
  `_spec_section_numbers()` to match the real spec headings (the intent is "every cited §N exists",
  not a fixed regex); note the change.
- **Rollback:** revert the `spec.md` §7 blueprint edit and the §8 API-example edit, remove the
  `weather_test` line from `.gitignore`, and delete `tests/test_docs_links.py`. (The
  `ai-disclosure.md` rename and its `spec.md` link updates are Stage 03's to roll back.) State
  returns to pre-stage.
