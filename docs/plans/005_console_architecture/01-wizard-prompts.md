# Stage 01 — Wizard state machine & prompts
> Part of the Interactive Console Engine. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Steps 1–4 are merged and green. Verify before starting:

- `python -c "import crossroads; print(crossroads.init_engine)"` prints a function — the engine surface
  exists.
- `src/crossroads/console.py` does **not** exist yet: `ls src/crossroads/console.py` errors.
- `pytest` (default suite) is green: `pytest -q` passes.

If any of these is false, stop and reconcile before proceeding.

## Objective

Create `src/crossroads/console.py` with I/O-injected prompt primitives and three validated parameter
prompts (`database_path`, `years`, `boundary_mode`), composed by `gather_parameters`. **No build wiring
in this stage** — this stage delivers only the parameter-gathering front half of the wizard, fully unit
tested offline.

## Implementation Steps

### Step 1 — Create `src/crossroads/console.py` with the prompt primitive

Create the file with a module docstring and the `_prompt` helper. Design notes:

- The wizard never calls `input()`/`print` in its logic. Every function takes `reader` (a zero-arg
  callable returning the next input line) and `writer` (a one-arg callable that emits a line).
- `_prompt(reader, writer, message, *, parse, default=None)` is the ask/validate/re-ask loop:
  1. Emit `message` via `writer`. If `default` is not `None`, include it in the message, e.g.
     `"Boundary mode [snapshot]: "`.
  2. Call `reader()` to get the raw line; strip surrounding whitespace.
  3. If the stripped line is empty **and** `default is not None`, use the default as the raw value.
  4. Call `parse(raw)` — a callable that returns the cleaned value or raises `ValueError` with a
     human-readable message.
  5. On `ValueError`, emit the error via `writer` (e.g. `"  ✗ {message}. Please try again."`) and loop
     back to step 1.
  6. On success, return the parsed value.
- Keep it small and readable; add a plain-language comment explaining the re-ask loop.

```python
"""Interactive data-compilation wizard (spec §6, master-plan Step 5).

A terminal wizard that gathers build parameters, confirms them, and drives
``crossroads.init_engine(...).build(...)``. All input/output is injected
(``reader``/``writer``) so the wizard is driven by scripted input in tests with
no real stdin/stdout and no network. Production wires ``reader = lambda: input()``
and ``writer = print`` (see Stage 02's ``main``).
"""

from datetime import date

from crossroads import init_engine  # used in Stage 02; harmless to import now


def _prompt(reader, writer, message, *, parse, default=None):
    """Ask, validate, and re-ask until ``parse`` accepts the input.

    ``parse(raw)`` returns the cleaned value or raises ``ValueError`` with a
    human-readable reason. An empty line falls back to ``default`` when one is set.
    """
    label = f"{message} [{default}]: " if default is not None else f"{message}: "
    while True:
        writer(label)
        raw = reader().strip()
        if not raw and default is not None:
            raw = str(default)
        try:
            return parse(raw)
        except ValueError as exc:
            writer(f"  Invalid input: {exc}. Please try again.")
```

### Step 2 — `prompt_database_path`

Add the database-path prompt. Rules:

- Default: `"crossroads.db"`.
- Accept any non-empty path string, including `":memory:"`. Reject an empty value **only** when there is
  no default in play — but since a default is always supplied here, an empty line resolves to the
  default, so `parse` just needs to reject a value that is empty after the default fallback (defensive).
- Do **not** touch the filesystem here (no existence/overwrite check) — keep it a pure string prompt. The
  build step later creates/opens the file.

```python
def _parse_database_path(raw):
    if not raw:
        raise ValueError("database path cannot be empty")
    return raw


def prompt_database_path(reader, writer):
    """Where to write the DuckDB file. ':memory:' is allowed for a throwaway build."""
    return _prompt(reader, writer,
                   "Database file path", parse=_parse_database_path,
                   default="crossroads.db")
```

### Step 3 — `prompt_years`

Add the years prompt. This is the most validation-heavy field.

- Accept a comma- **or** space-separated list of individual years **and/or closed ranges**, e.g.
  `"1990-2000, 2010, 2022-2024"` means 1990–2000 inclusive, 2010, and 2022–2024 inclusive.
- A range token is `START-END` with a **tight hyphen** (no spaces around it). Both endpoints are
  validated with the same rules as a single year, and the range expands to every year from `START` to
  `END` inclusive. Reject a backwards range (`END < START`) with a clear message.
- Because the hyphen is the range separator, spaces around it are **not** supported: `"2020 - 2024"`
  splits into the junk token `"-"` and is rejected. This is a deliberate simplification — document it in
  the error message ("use e.g. 2015-2018").
- Parse each single-year token to `int`. Reject non-numeric tokens with a clear message.
- Valid range for any year (single or range endpoint): `1979` (earliest STATS19 data) to the current
  year (`date.today().year`). Reject values outside this range. Using `date.today().year` keeps the upper
  bound from rotting; this is input validation, not data, so it does not affect reproducibility.
- **Dedupe and sort ascending** (overlapping ranges collapse naturally via the set).
- Require **at least one** year (the wizard's purpose is to compile collision data, and the STATS19
  transformer is inactive without years). Reject an empty result.
- No default (years is required — pass `default=None`).

```python
_EARLIEST_STATS19_YEAR = 1979


def _parse_one_year(token, latest):
    """Parse a single year token to an int and range-check it. Shared by singles and range endpoints."""
    try:
        y = int(token)
    except ValueError:
        raise ValueError(f"'{token}' is not a whole number")
    if not (_EARLIEST_STATS19_YEAR <= y <= latest):
        raise ValueError(f"{y} is outside {_EARLIEST_STATS19_YEAR}–{latest}")
    return y


def _parse_years(raw):
    # Split on commas and/or whitespace; ignore empty tokens from double separators.
    tokens = [t for t in raw.replace(",", " ").split() if t]
    if not tokens:
        raise ValueError("enter at least one year or range, e.g. 2015-2018 2021")
    latest = date.today().year
    years = set()
    for t in tokens:
        if "-" in t:
            # A closed range like "1990-2000" (tight hyphen, no surrounding spaces).
            start, _, end = t.partition("-")
            lo = _parse_one_year(start, latest)
            hi = _parse_one_year(end, latest)
            if lo > hi:
                raise ValueError(f"range '{t}' is backwards (start after end)")
            years.update(range(lo, hi + 1))
        else:
            years.add(_parse_one_year(t, latest))
    return sorted(years)


def prompt_years(reader, writer):
    """One or more collision years/ranges to ingest (STATS19 is inactive without years)."""
    return _prompt(reader, writer,
                   "Years to ingest (e.g. 2015-2018 2021, space or comma separated)",
                   parse=_parse_years, default=None)
```

### Step 4 — `prompt_boundary_mode`

Add the boundary-mode prompt. Rules:

- Two valid values: `"snapshot"` (default) and `"temporal"`.
- Accept case-insensitively; normalize to lowercase. Also accept the numeric shortcuts `"1"` → snapshot,
  `"2"` → temporal, and show them in the message so the choice is discoverable.
- Reject anything else with a message listing the valid options.
- Default: `"snapshot"`.

```python
_BOUNDARY_MODES = {"snapshot", "temporal"}
_BOUNDARY_ALIASES = {"1": "snapshot", "2": "temporal"}


def _parse_boundary_mode(raw):
    value = _BOUNDARY_ALIASES.get(raw, raw.lower())
    if value not in _BOUNDARY_MODES:
        raise ValueError("choose 'snapshot' (1) or 'temporal' (2)")
    return value


def prompt_boundary_mode(reader, writer):
    """Retrospective snapshot (latest ONS vintage) vs temporally-sliced boundaries."""
    writer("Boundary mode: 1) snapshot = latest ONS boundaries (default); "
           "2) temporal = boundaries as they were on each collision date.")
    return _prompt(reader, writer,
                   "Boundary mode", parse=_parse_boundary_mode, default="snapshot")
```

### Step 5 — `gather_parameters`

Compose the three prompts into the parameter dict the build step consumes.

```python
def gather_parameters(reader, writer):
    """Run the parameter prompts and return the build-parameter dict.

    Keys map 1:1 onto the build surface: ``database_path`` feeds
    ``init_engine(database_path=...)``; ``years`` and ``boundary_mode`` feed
    ``client.build(...)``.
    """
    writer("Crossroads-UK — data compilation wizard")
    writer("")  # blank spacer line
    return {
        "database_path": prompt_database_path(reader, writer),
        "years": prompt_years(reader, writer),
        "boundary_mode": prompt_boundary_mode(reader, writer),
    }
```

## Testing & Verification

Create `tests/test_console.py`. All tests this stage are fast, offline, deterministic. Use a small
scripted-I/O harness:

```python
from crossroads import console


def scripted(answers):
    """Return (reader, writer, output) where reader replays `answers` in order."""
    answers = list(answers)
    output = []
    def reader():
        return answers.pop(0)
    def writer(line):
        output.append(line)
    return reader, writer, output
```

Write these tests (integration tests are PRIMARY per the plan, but this stage has no build to integrate;
the real end-to-end integration test lands in Stage 02. Here we prove the state machine exhaustively):

1. **`test_gather_parameters_happy_path`** — feed `["mydb.duckdb", "2021 2022", "temporal"]`; assert the
   returned dict equals `{"database_path": "mydb.duckdb", "years": [2021, 2022], "boundary_mode":
   "temporal"}`.
2. **`test_defaults_via_empty_lines`** — feed `["", "2023", ""]`; assert `database_path == "crossroads.db"`,
   `years == [2023]`, `boundary_mode == "snapshot"`.
3. **`test_years_comma_and_space_dedup_sort`** — call `prompt_years` with a scripted reader returning
   `"2023, 2021 2021,2022"`; assert result `[2021, 2022, 2023]`.
4. **`test_years_ranges_and_singles`** — call `prompt_years` with a scripted reader returning
   `"1990-1992, 2010, 2022-2024"`; assert result `[1990, 1991, 1992, 2010, 2022, 2023, 2024]`. Also
   confirm overlapping ranges collapse: `"2010-2012 2011-2013"` returns `[2010, 2011, 2012, 2013]`.
5. **`test_years_rejects_bad_range`** — scripted reader returns `["2024-2020", "2020-2024"]` in turn (a
   backwards range, then a valid one); assert `prompt_years` re-asks once and returns
   `[2020, 2021, 2022, 2023, 2024]`; assert the captured `output` contains one "Invalid input" line
   mentioning "backwards". Optionally also assert `"2020 - 2024"` (spaced hyphen) is rejected before a
   valid retry, proving the tight-hyphen rule.
6. **`test_years_rejects_then_accepts`** — scripted reader returns `["abc", "1500", "2023"]` in turn;
   assert `prompt_years` re-asks twice and finally returns `[2023]`; assert the captured `output`
   contains two "Invalid input" lines.
7. **`test_years_requires_at_least_one`** — scripted reader returns `["", "2023"]` for `prompt_years`
   (which has **no** default, so the empty line is rejected, then `2023` accepted); assert result
   `[2023]` and one "Invalid input" line.
8. **`test_boundary_mode_numeric_and_case`** — `prompt_boundary_mode` with `"2"` → `"temporal"`; with
   `"SNAPSHOT"` → `"snapshot"`; with `["bogus", "1"]` → re-asks then `"snapshot"`.
9. **`test_database_path_allows_memory`** — `prompt_database_path` with `":memory:"` returns `":memory:"`.

Run and confirm green:

```
pytest -q tests/test_console.py
```

**Stage ship-readiness checklist:**
- [ ] `src/crossroads/console.py` exists and imports cleanly (`python -c "import crossroads.console"`).
- [ ] `gather_parameters` returns the exact 3-key dict for scripted input.
- [ ] Every prompt re-asks on invalid input rather than raising.
- [ ] `pytest -q tests/test_console.py` green; full `pytest -q` still green (nothing else touched).

## End State / Handoff

`src/crossroads/console.py` exposes `_prompt`, `prompt_database_path`, `prompt_years`,
`prompt_boundary_mode`, and `gather_parameters(reader, writer) -> {"database_path", "years",
"boundary_mode"}`. All parameter-gathering is validated and unit-tested offline. **Stage 02 may assume**
`gather_parameters` exists with that exact signature and return shape, and will add the confirmation
step, `run_build`, `run_wizard`, `main`, and the `crossroads` entry point on top of it. No build is
triggered yet.

## Failure Modes & Rollback

- **A prompt raises instead of re-asking** → the wizard would crash on a typo. Guard: `_prompt` catches
  only `ValueError` from `parse` and loops; `parse` functions raise `ValueError` (never other types) with
  a human message. Tests 4–6 prove the re-ask path.
- **`reader()` exhausts its scripted answers in a test** → `IndexError` from `answers.pop(0)`, which
  surfaces as a test failure pointing at an unexpected extra prompt (useful signal, not a product bug).
- **Rollback:** delete `src/crossroads/console.py` and `tests/test_console.py`. Nothing else was modified,
  so the repo returns to its pre-stage state.
