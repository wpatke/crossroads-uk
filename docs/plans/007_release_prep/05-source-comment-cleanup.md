# Stage 05 — Source-Comment Cleanup (remove references to invisible planning artifacts)
> Part of Release Preparation (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

No prior stage required. This stage is **behaviour-preserving** — it edits only comments and
docstrings, never code logic. Survey the scope first:
```bash
cd /Users/will/Documents/Code/Crossroads
grep -rniI "master-plan\|master plan" src/          # references to the never-committed master plan
grep -rnI "\bStep [0-9]" src/                        # bare "Step N" cross-references
grep -rnI "Stage [0-9]" src/                         # internal plan-stage shorthand
```
Expected (at authoring time): `console.py` references `master-plan Step 5` and `Stage 02`;
`quality.py`, `transformers/stats19.py`, `transformers/spatial.py` reference `Stage 02/03/06/07`
and `Step 3`. The exact hits will guide the edits — treat the grep output as the work list.

## Objective

A first-time public reader of the source should never encounter a reference to a document they
cannot see. Remove every mention of `master-plan`, bare `Step N`, and `Stage NN` from committed
source, rewriting each comment to say *what the code does* (or citing `docs/spec.md §N`, which **is**
committed) instead of *which private plan step produced it*. No behaviour changes.

## Implementation Steps

Work file-by-file from the grep list. For each hit, apply the appropriate rewrite:

- **`master-plan Step 5` / `master plan` →** drop it. Keep any real `spec §N` citation. Example,
  `src/crossroads/console.py:1`:
  ```python
  """Interactive data-compilation wizard (spec §6, master-plan Step 5)."""
  ```
  becomes
  ```python
  """Interactive data-compilation wizard (spec §6)."""
  ```
- **`see Stage 02's main` / cross-refs to a plan stage →** describe the thing directly, e.g.
  `console.py:7`:
  ```python
  # Production wires ``reader = lambda: input()`` and ``writer = print`` (see Stage 02's ``main``).
  ```
  becomes
  ```python
  # Production wires ``reader = lambda: input()`` and ``writer = print`` (see ``main`` below).
  ```
  and `console.py:12` `# used in Stage 02; harmless to import now` → `# used by run_build below`.
- **Section-header comments like `# --- Stage 02: confirmation, build wiring ... ---` →** rename to
  the behaviour: `# --- Confirmation, build wiring, and the entry point ---`.
- **`Stage 06`/`Stage 07`/`Step 3` inside `quality.py`, `stats19.py`, `spatial.py` →** replace with a
  plain description of the layer/operation. Examples:
  - `# The Stage 03 invariant compares this against bronze + quarantine counts.` →
    `# The build-end conservation invariant compares this against bronze + quarantine counts.`
  - `# broad keep-in-place clean (Stage 06): carry EVERY bronze column into silver` →
    `# broad keep-in-place clean: carry EVERY bronze column into silver`
  - `# CORE severity audit (Stage 07): raw twin + cleaned INTEGER + valid flag` →
    `# CORE severity audit: raw twin + cleaned INTEGER + valid flag`
  - `against the Step 3 boundary silver tables` → `against the boundary silver tables`
  - `UNDECIDED_QUALITY_SPEC_IS_FATAL = True    # enforced from Step 3 onward` →
    `# always enforced` (or delete the trailing note).

> Preserve every `spec §N` reference — `docs/spec.md` is committed and public, so those are good
> anchors. Only the private-plan references (`master-plan`, `Step N`, `Stage NN`) are removed.
> Do **not** touch string literals that are functionally meaningful (rule ids like
> `'stats19.coord.sentinel'`, table names, SQL) — those contain no plan references anyway.

Expected result after all edits: the three greps below return nothing under `src/`.

## Testing & Verification

**Integration test (PRIMARY) — behaviour is unchanged.** The whole offline suite must stay green,
proving comment edits changed no logic:
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
python -m pytest -q
```
Expected: identical pass count to before this stage.

**Drift-guard test.** Add `tests/test_no_internal_refs.py` so the cleanup cannot silently regress:
```python
"""Committed source must not reference invisible planning artifacts (master-plan / Step N /
Stage NN). docs/spec.md is committed, so 'spec §N' references are allowed and not matched here."""
import os
import re

SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
PATTERNS = [re.compile(r"master[- ]plan", re.I),
            re.compile(r"\bStep \d"),
            re.compile(r"\bStage \d")]


def test_src_has_no_internal_plan_references():
    offenders = []
    for dirpath, _dirs, files in os.walk(SRC):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if any(p.search(line) for p in PATTERNS):
                        offenders.append(f"{os.path.relpath(path, SRC)}:{lineno}: {line.strip()}")
    assert not offenders, "Internal plan references left in src/:\n" + "\n".join(offenders)
```
Run: `python -m pytest tests/test_no_internal_refs.py -q` → passes once all hits are cleaned.

**Manual grep confirmation:**
```bash
grep -rniI "master-plan\|master plan" src/ ; grep -rnI "\bStep [0-9]" src/ ; grep -rnI "Stage [0-9]" src/
```
Expected: all three print nothing.

**Stage ship-readiness checklist:**
- [ ] no `master-plan` / bare `Step N` / `Stage NN` references remain under `src/`
- [ ] all `spec §N` references preserved
- [ ] `tests/test_no_internal_refs.py` passes
- [ ] full `python -m pytest` green with an unchanged pass count (no logic touched)

## End State / Handoff

Committed source is free of references to private planning documents; a drift-guard test keeps it
that way. No functional change — every other stage's assumptions about behaviour still hold.

## Failure Modes & Rollback

- **A grep hit is inside a meaningful string, not a comment.** Unlikely (checked: the hits are in
  comments/docstrings), but if so, do not alter runtime strings — reword only surrounding comments and
  narrow the test if a legitimate literal trips it. Note the deviation.
- **Pass count changes after editing.** That means a docstring edit altered a doctest or a
  code line was touched by mistake — revert that file and redo comment-only.
- **Scope too broad (user wants to keep `Stage NN` shorthand).** Drop the third pattern from the test
  and clean only `master-plan` + `Step N` (see overview Open Questions).
- **Rollback:** `git checkout -- src/` restores all comments; delete `tests/test_no_internal_refs.py`.
