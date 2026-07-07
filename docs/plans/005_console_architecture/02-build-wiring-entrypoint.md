# Stage 02 — Build wiring & entry point
> Part of the Interactive Console Engine. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Stage 01 is complete and merged. Verify before starting:

- `python -c "from crossroads.console import gather_parameters; print(gather_parameters)"` prints a
  function.
- `tests/test_console.py` exists and `pytest -q tests/test_console.py` is green.
- `pyproject.toml` has **no** `[project.scripts]` table yet: `grep -n "project.scripts" pyproject.toml`
  returns nothing.

If any is false, stop and reconcile (implement Stage 01 first).

## Objective

Add the back half of the wizard to `src/crossroads/console.py`: the summary + confirmation step, the
build translator `run_build`, the orchestrator `run_wizard`, and the `main` entry point. Register the
`crossroads` console command in `pyproject.toml`. Prove the whole path with a fast functional test
(mocked engine) and an offline integration test (real build against fixture-seeded cache).

## Implementation Steps

### Step 1 — Confirmation prompt and summary

Add to `console.py`, reusing the Stage 01 primitives:

```python
def _parse_yes_no(raw):
    value = raw.strip().lower()
    if value in ("y", "yes"):
        return True
    if value in ("n", "no"):
        return False
    raise ValueError("answer 'y' or 'n'")


def prompt_confirm(reader, writer, *, default=True):
    """Ask the user to confirm before the (possibly long) build runs."""
    shown = "Y/n" if default else "y/N"
    return _prompt(reader, writer, f"Proceed with the build? ({shown})",
                   parse=_parse_yes_no, default=("y" if default else "n"))


def format_summary(params):
    """Human-readable recap of the gathered parameters, shown before confirmation."""
    years = ", ".join(str(y) for y in params["years"])
    return (
        "\nBuild summary:\n"
        f"  Database file : {params['database_path']}\n"
        f"  Years         : {years}\n"
        f"  Boundary mode : {params['boundary_mode']}\n"
    )
```

Note: `prompt_confirm` passes `default` as a string (`"y"`/`"n"`) into `_prompt`, so an empty line
resolves to the default before `_parse_yes_no` runs.

### Step 2 — `run_build` (the translator)

Translate the parameter dict into the existing engine surface. This is the **only** function that touches
`init_engine`/`build`, isolated behind the `engine_factory` seam so tests can inject a fake.

```python
def run_build(params, *, engine_factory=init_engine, cache_dir=None, writer=None):
    """Open the engine and run the build for the gathered parameters.

    ``engine_factory`` defaults to ``crossroads.init_engine`` and is injectable so
    tests can (a) record the exact call without doing work, or (b) point a real
    build at a fixture-seeded ``cache_dir`` and run offline. Returns the Client.
    """
    say = writer or (lambda _line: None)
    # cache_dir is threaded through only when the caller overrides it (tests);
    # production leaves it None so init_engine uses its default cache location.
    engine_kwargs = {"database_path": params["database_path"]}
    if cache_dir is not None:
        engine_kwargs["cache_dir"] = cache_dir

    client = engine_factory(**engine_kwargs)
    say("\nBuilding database — this may take a while for large year ranges...")
    client.build(years=params["years"], boundary_mode=params["boundary_mode"])
    say(f"Done. Database written to {params['database_path']}")
    return client
```

### Step 3 — `run_wizard` (the orchestrator)

Compose gather → summary → confirm → build.

```python
def run_wizard(reader, writer, *, engine_factory=init_engine, cache_dir=None):
    """Drive the full wizard. Returns the built Client, or None if the user declined.

    All I/O is injected so this is fully testable with scripted input.
    """
    params = gather_parameters(reader, writer)
    writer(format_summary(params))
    if not prompt_confirm(reader, writer, default=True):
        writer("Aborted — no database was built.")
        return None
    return run_build(params, engine_factory=engine_factory,
                     cache_dir=cache_dir, writer=writer)
```

### Step 4 — `main` (the entry point)

Wire the production I/O and handle interruption gracefully.

```python
def main(argv=None):
    """Console entry point. Wires stdin/stdout, returns a process exit code.

    ``argv`` is accepted for signature stability but currently unused (the wizard
    takes all parameters interactively). Ctrl-C / EOF abort cleanly without a
    traceback.
    """
    reader = lambda: input()   # prompt text is emitted via writer, so input() gets no prompt
    writer = print
    try:
        client = run_wizard(reader, writer)
    except (KeyboardInterrupt, EOFError):
        writer("\nCancelled.")
        return 130   # conventional exit code for SIGINT
    if client is not None:
        client.close()
    return 0
```

Note: `main` closes the client after a successful build so the DuckDB file handle is released (the file
is already written by `build`). On the abort/decline path `client` is `None`, so nothing to close.

### Step 5 — Register the `crossroads` entry point

Edit `pyproject.toml`. Add a `[project.scripts]` table (place it directly after the
`[project.optional-dependencies]` block, before `[tool.hatch.build.targets.wheel]`):

```toml
[project.scripts]
crossroads = "crossroads.console:main"
```

Then reinstall so the script registers:

```
pip install -e .
```

Expected: install succeeds; `which crossroads` resolves to a launcher in the environment's bin dir.

## Testing & Verification

Extend `tests/test_console.py` with the `scripted` helper from Stage 01. Add both fast functional tests
and the offline integration test.

### A. Functional tests (fast, offline — the correct-invocation proof)

Use a **recording fake** engine so no real work runs:

```python
class _FakeClient:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.build_kwargs = None
        self.closed = False
    def build(self, **kwargs):
        self.build_kwargs = kwargs
        return self
    def close(self):
        self.closed = True


def test_wizard_produces_correct_build_invocation():
    reader, writer, _ = scripted(["mydb.duckdb", "2022 2023", "temporal", "y"])
    captured = {}
    def factory(**kwargs):
        client = _FakeClient(**kwargs)
        captured["client"] = client
        return client
    result = console.run_wizard(reader, writer, engine_factory=factory)
    assert result is captured["client"]
    assert captured["client"].init_kwargs == {"database_path": "mydb.duckdb"}
    assert captured["client"].build_kwargs == {"years": [2022, 2023],
                                               "boundary_mode": "temporal"}


def test_decline_does_not_build():
    reader, writer, output = scripted([":memory:", "2023", "snapshot", "n"])
    calls = []
    def factory(**kwargs):
        calls.append(kwargs); return _FakeClient(**kwargs)
    result = console.run_wizard(reader, writer, engine_factory=factory)
    assert result is None
    assert calls == []            # build path never entered
    assert any("Aborted" in line for line in output)
```

Add a `main` abort-path test that exercises the real entry point offline (declining means no build, so
no network):

```python
def test_main_abort_path_returns_zero(monkeypatch, capsys):
    answers = iter([":memory:", "2023", "snapshot", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert console.main() == 0
    assert "Aborted" in capsys.readouterr().out
```

### B. Integration test (offline real build — the populated-database proof) — PRIMARY

Mark with `@pytest.mark.integration`. Reuse the committed fixtures and the seeding approach already in
`tests/test_stats19.py` (`_seed_cache` for STATS19 CSVs, `_seed_ons_cache` for ONS GeoJSON). Drive the
**real** `init_engine` via `run_wizard`, injecting the seeded `cache_dir` so the build runs offline.

```python
import os, shutil
import pytest
from crossroads.transformers.spatial import LADBoundaryTransformer, CTYUABoundaryTransformer

STATS19_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
ONS_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ons")


def _seed_full_cache(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    # STATS19 CSVs (2023 sample).
    for ftype in ("collision", "vehicle", "casualty"):
        name = f"dft-road-casualty-statistics-{ftype}-2023.csv"
        shutil.copy(os.path.join(STATS19_FIXTURES, name), os.path.join(cache_dir, name))
    # ONS boundary GeoJSON, copied to the filename each newest vintage expects.
    for prefix, cls in (("lad", LADBoundaryTransformer), ("ctyua", CTYUABoundaryTransformer)):
        newest = cls().vintages[-1]
        year = newest.valid_from[:4]
        src = os.path.join(ONS_FIXTURES, f"{prefix}_{year}", f"{prefix}_sample.geojson")
        shutil.copy(src, os.path.join(cache_dir, newest.source_file))


@pytest.mark.integration
def test_wizard_builds_populated_database_offline(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_full_cache(cache)
    db_path = str(tmp_path / "wizard.duckdb")
    # Scripted answers: db path, one year (matches the fixture), snapshot, confirm.
    reader, writer, _ = scripted([db_path, "2023", "snapshot", "y"])
    client = console.run_wizard(reader, writer, cache_dir=cache)  # real init_engine
    try:
        assert client is not None
        assert os.path.exists(db_path)                            # file written
        n = client.con.execute("SELECT count(*) FROM collisions").fetchone()[0]
        assert n > 0                                              # silver populated
        # gold view exists and is queryable
        client.con.execute("SELECT count(*) FROM collisions_spatial").fetchone()
    finally:
        client.close()
```

If the newest-vintage seeding detail drifts from `test_stats19.py`, mirror whatever
`test_stats19.py::_seed_ons_cache` does at that time — it is the source of truth for fixture filenames.

### C. Entry-point verification

Add a metadata test that tolerates a not-yet-reinstalled package (skip rather than fail):

```python
import importlib.metadata as md


def test_console_script_registered():
    scripts = md.entry_points(group="console_scripts")
    match = [e for e in scripts if e.name == "crossroads"]
    if not match:
        pytest.skip("run `pip install -e .` to register the console script")
    assert match[0].value == "crossroads.console:main"
```

Then do the manual/CLI verification (proves the real command launches and aborts cleanly, offline):

```
pip install -e .
printf ':memory:\n2023\nsnapshot\nn\n' | crossroads ; echo "exit=$?"
```

Expected: the wizard prints its prompts and the summary, then `Aborted — no database was built.`, and
`exit=0`.

### Run commands

```
pytest -q tests/test_console.py                 # fast functional + metadata (integration deselected)
pytest -m integration -q tests/test_console.py  # the offline real-build integration test
pytest -q                                       # full default suite still green
```

**Stage ship-readiness checklist:**
- [ ] `run_wizard` calls the engine factory once with `database_path=<answer>` and `build(years=...,
      boundary_mode=...)` exactly as answered (functional test).
- [ ] Declining at confirmation returns `None` and never builds (functional test).
- [ ] The offline integration test writes a real DuckDB file with a populated `collisions` table and a
      queryable `collisions_spatial` view, with no network access.
- [ ] `pyproject.toml` has `[project.scripts] crossroads = "crossroads.console:main"`; after
      `pip install -e .`, `crossroads` launches the wizard and the piped abort run exits `0`.
- [ ] `pytest -q` and `pytest -m integration -q` both green.

## End State / Handoff

`crossroads` is a launchable command that runs the full data-compilation wizard: gather → summary →
confirm → build → report, driving the existing `init_engine(...).build(...)` surface. The path is proven
end-to-end by an offline integration test (real build to a populated DuckDB file) and by fast functional
tests (correct invocation; clean decline; clean abort). This completes master-plan **Step 5** — an
automated test drives the wizard with scripted input and confirms it produces the correct `build()`
invocation and a populated database, with no manual interaction. **Step 6 (weather)** will extend the
wizard by adding `include_weather` (and any weather-specific prompts) to `gather_parameters` and passing
it through `run_build` — no structural change to the seams built here.

## Failure Modes & Rollback

- **`main` dumps a traceback on Ctrl-C/EOF** → poor UX. Guard: `main` catches `KeyboardInterrupt`/
  `EOFError`, prints `Cancelled.`, returns `130`. (Not unit-tested by default; the abort-path test covers
  the normal decline route.)
- **Entry point not found after edit** → `crossroads: command not found`. Cause: `pip install -e .` not
  re-run after adding `[project.scripts]`. Fix: rerun the editable install. The metadata test `skip`s
  (not fails) in this state so the suite stays green.
- **Integration test reaches the network** → a fixture filename drifted from what the transformer expects,
  so `extract()` fell through to download. Fix: re-mirror `test_stats19.py`'s seeding helpers (the source
  of truth for fixture filenames); the build must find every file already in `cache`.
- **`run_build` closes the client too early** → it must **not** close inside `run_build`/`run_wizard`
  (tests and `main` need the open connection to inspect/close it). Only `main` closes. Keep it that way.
- **Rollback:** revert the `console.py` additions from this stage, remove the `[project.scripts]` table
  from `pyproject.toml`, rerun `pip install -e .`, and delete the Stage 02 tests from
  `tests/test_console.py`. Stage 01 remains intact and green.
