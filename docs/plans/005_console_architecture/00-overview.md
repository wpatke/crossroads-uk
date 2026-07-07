# Interactive Console Engine — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Build `src/crossroads/console.py`: a terminal-native, multi-stage **data-compilation wizard** that
prompts the researcher for build parameters (database path, years, boundary mode), validates each
answer, shows a summary, asks for confirmation, then drives `client.build()` and reports the resulting
DuckDB file. Expose it as a launchable `crossroads` console command. This is the researcher-facing
front door to the pipeline built in Steps 1–4.

## Context & Objective

**What exists today (Steps 1–4, already merged and green).**
- `src/crossroads/client.py` — `init_engine(database_path=":memory:", cache_dir=".crossroads_cache")`
  returns a `Client`. `Client.build(**kwargs)` opens the DuckDB connection (`self.con`), loads the
  Spatial extension, creates the quality audit tables, runs each active transformer, then runs the
  build-end invariants (fatal on violation). `build()` returns `self`. `Client.close()` closes the
  connection. The build parameters actually consumed by transformers today are:
  - `years: list[int]` — activates the STATS19 transformer (`is_active` returns `bool(kwargs.get("years"))`)
    and windows boundary vintages in temporal mode.
  - `boundary_mode: "snapshot" | "temporal"` — consumed by `spatial.py` and `stats19.py`; default
    `"snapshot"` (latest ONS vintage only) vs `"temporal"` (all vintages with validity windows).
  - `reject_ceiling: float` (optional) — overrides the default reject-rate ceiling. **Not exposed by the
    wizard** (advanced tuning; stays at its default). Documented here only so you know it exists.
- `src/crossroads/transformers/` — `spatial.py` (ONS LAD/CTYUA boundaries), `stats19.py`
  (Collision/Vehicle/Casualty). Both download to `cache_dir` unless the expected file already exists
  there (offline tests pre-seed the cache with committed fixtures — see Global Testing below).
- `src/crossroads/registry.py` — auto-discovers concrete `BaseTransformer` subclasses. `client.build()`
  runs whichever are active for the given kwargs.
- `tests/` — all green. Integration tests are marked `@pytest.mark.integration` and **deselected by
  default** (`addopts = "-m 'not integration'"` in `pyproject.toml`); run deliberately with
  `pytest -m integration`. `tests/fixtures/stats19/` holds sample Collision/Vehicle/Casualty CSVs for
  2023; `tests/fixtures/ons/` holds sample LAD/CTYUA GeoJSON. `tests/test_stats19.py` already contains
  the seeding helpers `_seed_cache` (STATS19 CSVs) and `_seed_ons_cache` (ONS GeoJSON) and a working
  offline end-to-end build via `_full_client(tmp_path)` — **study these; the console integration test
  reuses the same seeding approach.**
- `pyproject.toml` — package `crossroads-uk`, import `crossroads`, Hatchling build backend,
  `requires-python>=3.11`. **No `[project.scripts]` entry point exists yet.**

There is **no** `src/crossroads/console.py` and **no** `crossroads` command yet.

**What changes.** Add `console.py` (the wizard) and a `[project.scripts]` entry point so
`crossroads` launches the wizard. Nothing in `client.py`, `registry.py`, or the transformers changes —
the console is a pure consumer of the existing `init_engine(...).build(...)` surface.

**The goal.** A researcher runs `crossroads`, answers a few prompts, confirms, and gets a populated
DuckDB file — with **no** manual code required. Proven by automated tests that drive the wizard with
scripted input (no human interaction, no network).

## Approach / Architecture — shared by all stages

**Locked decisions (do not revisit):**

1. **Stdlib only.** The wizard uses plain `input()`/`print` — **no** new runtime dependency (honors the
   spec §2 minimal-dependency constraint). I/O is injected, not hard-coded, so tests drive it offline.

2. **Dependency-injected I/O.** The wizard never calls `input()`/`print` directly in its logic. It takes:
   - `reader: Callable[[], str]` — returns the next input line (production default: `input`; the builtin
     `input` already strips the trailing newline and echoes the prompt, but the wizard passes prompts to
     `writer`, so `reader` is called with no argument — wrap as `lambda: input()`).
   - `writer: Callable[[str], None]` — emits a line (production default: `print`).
   A test supplies a list-backed `reader` (scripted answers) and a list-collecting `writer` (captured
   output). This is the seam that makes the whole wizard testable with zero mocking of stdin/stdout.

3. **Dependency-injected engine.** The build step takes an `engine_factory` callable (default:
   `crossroads.init_engine`) and an optional `cache_dir`. This is the seam that lets:
   - the fast functional test inject a **fake** factory that records the exact `init_engine`/`build`
     arguments without doing any work, and
   - the offline integration test inject the **real** factory but point it at a fixture-seeded
     `cache_dir`, so a real build runs with no network.

4. **Parameters gathered (active only).** `database_path`, `years`, `boundary_mode`. `spatial_grain` and
   `include_weather` from the spec §8 flow are **intentionally omitted** — nothing consumes them today;
   they are added to the wizard in Step 6 (weather) when a transformer reads them. `reject_ceiling` and
   `cache_dir` are **not** prompted (advanced; left at defaults).

5. **Flow:** gather → summary → confirm → build → report. On "no" at the confirmation, abort cleanly
   without building. The launch command is `crossroads` → `crossroads.console:main`.

**Module shape (`src/crossroads/console.py`) — the target public surface both stages build toward:**

```
# Prompt primitives (Stage 5a)
_prompt(reader, writer, message, *, parse, default=None)   # ask/validate/re-ask loop
prompt_database_path(reader, writer) -> str
prompt_years(reader, writer) -> list[int]
prompt_boundary_mode(reader, writer) -> str
gather_parameters(reader, writer) -> dict                  # {"database_path","years","boundary_mode"}

# Build wiring + orchestration (Stage 5b)
prompt_confirm(reader, writer, *, default=True) -> bool
format_summary(params) -> str
run_build(params, *, engine_factory=init_engine, cache_dir=None)   # -> Client
run_wizard(reader, writer, *, engine_factory=init_engine, cache_dir=None)  # -> Client | None
main(argv=None) -> int                                     # entry point: wires input/print, returns exit code
```

**Data flow:** `main` builds the default `reader`/`writer` → `run_wizard` → `gather_parameters`
(three prompt functions, each validating and re-asking on bad input) → `format_summary` + `prompt_confirm`
→ on confirm, `run_build` translates the params dict into
`engine_factory(database_path=params["database_path"], [cache_dir=...]).build(years=params["years"],
boundary_mode=params["boundary_mode"])` → report the DB path. On decline, print an abort line and return
without building.

## Cross-Cutting Constraints (every stage obeys these)

- **No new runtime dependency.** Stdlib only. (Spec §2.) `pytest` remains the only dev dep.
- **No edits to `client.py`, `registry.py`, `quality.py`, or any transformer.** The console is a pure
  consumer of the existing surface. (Provider-plugin purity, spec §4.)
- **Offline & deterministic tests.** No test may reach the network in the default suite. Real-build tests
  are `@pytest.mark.integration`, seed the cache with committed fixtures, and still run offline.
- **Keep it simple, comment in plain language.** (CLAUDE.md.) Prefer small, readable functions the user
  can read before committing.
- **Never stage or commit.** Do not run `git add`/`git commit`. (CLAUDE.md.)
- **Match existing style.** Follow the docstring/comment density and naming already in `client.py` and
  the transformers. Tests go in `tests/test_console.py`, mirroring the structure of `tests/test_client.py`
  and `tests/test_stats19.py`.

## Stage Map

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Wizard state machine & prompts | The prompt primitives and the three validated parameter prompts, plus `gather_parameters`. Pure I/O-injected logic; no build yet. | `console.py` exposes `gather_parameters(reader, writer)` returning `{"database_path","years","boundary_mode"}`; validation re-asks on bad input; unit tests green. | Steps 1–4 | `01-wizard-prompts.md` |
| 02 | Build wiring & entry point | Summary + confirmation, `run_build`, `run_wizard`, `main`; the `crossroads` console-script entry point; the functional (mocked) and offline integration (real build) tests. | `crossroads` command launches the wizard; scripted input drives a real offline build to a populated DB; functional + integration tests green. | 01 | `02-build-wiring-entrypoint.md` |

## Global Testing & Ship

All tests live in `tests/test_console.py`. Run the fast suite with `pytest tests/test_console.py`
(offline, deterministic) and the integration test with `pytest -m integration tests/test_console.py`.

- **Stage 01 proves** (fast, offline): each prompt validates and re-asks on bad input; `gather_parameters`
  returns the exact expected dict for a scripted set of answers; years parsing handles comma- and
  space-separated lists, dedupes, sorts, and rejects out-of-range/non-numeric tokens; boundary-mode
  selection honors the default and rejects unknown values.
- **Stage 02 proves:**
  - *(fast, offline)* A scripted full run calls the injected `engine_factory` exactly once with
    `database_path=<answer>` and calls `.build(years=<answer>, boundary_mode=<answer>)` — the wizard
    produces the **correct build invocation**. Declining at the confirmation returns `None` and **never**
    calls build. `main` returns `0` on the abort path with no network.
  - *(integration, offline)* Seeding the cache with the committed STATS19 + ONS fixtures (reusing the
    `_seed_cache`/`_seed_ons_cache` pattern) and driving `run_wizard` with scripted input
    (`years=[2023]`, `boundary_mode="snapshot"`, confirm=yes) writes a real DuckDB file whose
    `collisions` table is populated and whose `collisions_spatial` view exists — the wizard produces a
    **populated database** with no manual interaction and no network.
  - *(entry point)* The `crossroads` console script resolves to `crossroads.console:main` (verified via
    `importlib.metadata` after an editable reinstall), and running it on the abort path exits `0`.

**Ship-readiness for the whole step:** `pytest` (full default suite) green, `pytest -m integration`
green, `pip install -e .` succeeds, and `crossroads` launches the wizard. This satisfies master-plan
Step 5 "Done when": an automated test drives the wizard with scripted input and confirms it produces
the correct `build()` invocation and a populated database, with no manual interaction required.

## Open Questions / Risks

- **`reader` semantics with builtin `input`.** `input()` prints its prompt argument and reads a line. The
  wizard sends prompts through `writer` and calls `reader()` with no prompt, so the production `reader`
  is `lambda: input()` and the production `writer` is `print`. Keep prompt text in `writer` calls so the
  scripted-`reader` tests see the same prompts the user does. (Resolved by design; noted so the executor
  doesn't pass the prompt into `input()` and bypass the `writer` seam.)
- **EOF / Ctrl-C handling.** `main` must not dump a traceback when the user hits Ctrl-C or sends EOF.
  Catch `KeyboardInterrupt`/`EOFError` in `main`, print a short abort line, and return a non-zero exit
  code. Specified in Stage 02.
- **Entry-point registration requires reinstall.** Adding `[project.scripts]` only takes effect after
  `pip install -e .` re-runs. Stage 02's verification includes the reinstall step; a metadata test that
  can't find the script should be `skip`ped (not failed) when the package was not reinstalled, to keep
  the default suite robust. Specified in Stage 02.
