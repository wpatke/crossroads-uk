# Project Foundation & Core Architecture — Plan Overview

Stand up the empty-but-correct `crossroads` Python package and the architectural contracts every later data source plugs into (transformer interface, dynamic registry, orchestrator, test harness) — with **no data-specific logic yet**.

## Context & Objective

**What exists today.** The repository at `/Users/will/Documents/Code/Crossroads` currently contains only:
- `LICENSE` (MIT, "Copyright (c) 2026 wpatke"),
- `CLAUDE.md`, `AI_DISCLOSURE.md`,
- `docs/spec.md` (the authoritative product definition) and `docs/plans/` (this plan lives here),
- `.gitignore` (ignores `/CLAUDE.md` and `.claude/`), `.gitattributes` (LF normalization).

There is **no** `pyproject.toml`, **no** `src/`, **no** `tests/`. Python 3.12 is installed via Homebrew at `/opt/homebrew/bin/python3.12` (system Python is 3.9.6 — do **not** use it for this project). `duckdb` is not yet installed.

**What changes.** This effort creates the installable package skeleton and the core engine contracts described in `docs/spec.md` §4 ("Modular Data Architecture") and §7 ("Repository Blueprint"):
- A `pyproject.toml` so `pip install -e .` works and `import crossroads` succeeds.
- `src/crossroads/transformers/base.py` — the `BaseTransformer` ABC (the `source_id` / `extract` / `transform_and_load` contract, plus an `is_active` activation hook).
- `src/crossroads/registry.py` — dynamic transformer discovery (enumerate modules with `pkgutil`, select concrete `BaseTransformer` subclasses with `inspect`).
- `src/crossroads/client.py` — `init_engine(...)` and a `build(...)` orchestrator that opens a DuckDB connection and runs the registry's `extract → transform_and_load` loop, with **zero transformers wired in**.
- `tests/conftest.py` shared DuckDB fixture and passing smoke tests.

**The goal.** A researcher can run the `docs/spec.md` §8 target flow shape —
`import crossroads as cr; client = cr.init_engine(database_path="local.db"); client.build(...)` —
and get a clean **no-op** build (it does nothing because no data sources are registered yet) without errors. Real data sources are authored in separate, later plans and simply drop a module into `transformers/`.

## Approach / Architecture

**Layout (`docs/spec.md` §7, src-layout):**
```
crossroads/
├── pyproject.toml
├── README.md                     # minimal stub now; expanded in a later release plan
├── src/
│   └── crossroads/
│       ├── __init__.py           # exposes init_engine, Client, __version__
│       ├── client.py             # init_engine(...) + Client.build(...) orchestrator
│       ├── registry.py           # transformer discovery & activation
│       └── transformers/
│           ├── __init__.py       # empty marker (makes it a discoverable package)
│           └── base.py           # BaseTransformer ABC
└── tests/
    ├── conftest.py               # shared DuckDB fixture
    ├── test_package.py           # import + version smoke test
    ├── test_registry.py          # discovery + activation filtering
    └── test_client.py            # no-op build smoke test
```
`console.py`, `quality.py`, and the `spatial.py`/`stats19.py`/`weather.py` transformers from the §7 blueprint are **out of scope here** — they belong to later plans. Do not create them.

**The provider-plugin contract (`docs/spec.md` §4).** Every future data source subclasses `BaseTransformer` and implements three deterministic phases — `source_id` (property), `extract(cache_dir, **kwargs)`, `transform_and_load(con, cache_dir)`. To these the foundation adds one activation hook:

- `is_active(self, **kwargs) -> bool` — defaults to `True`. The orchestrator passes the user's `build(...)` keyword arguments to this method; a source returns `True` when the run wants its data. Example a later weather source would use: `return kwargs.get("include_weather", False)`. This is how `build(include_weather=True, ...)`-style flags gate which sources run, without the core engine knowing any source by name.

**Dynamic discovery (`docs/spec.md` §4 "module inspection").** The registry discovers sources at construction time:
1. `pkgutil.iter_modules(package.__path__, package.__name__ + ".")` enumerates every module file inside the `crossroads.transformers` package.
2. Each module is imported with `importlib.import_module`.
3. `inspect.getmembers(module, inspect.isclass)` lists its classes; the registry keeps each class that **is a subclass of `BaseTransformer`**, **is not itself abstract** (`inspect.isabstract` is `False` — this excludes `BaseTransformer`), and **is defined in that module** (`obj.__module__ == module_info.name`, so imported-but-not-defined classes aren't double-counted).
4. Each surviving class is instantiated with no arguments and stored.

Because the only module in `transformers/` at the end of this effort is `base.py` (whose `BaseTransformer` is abstract and therefore skipped), `Registry()` discovers **zero** concrete transformers — which is exactly why the foundation build is a clean no-op.

To make discovery testable without polluting the real package, `Registry.__init__` accepts an optional `package` argument; when `None` it defaults to importing `crossroads.transformers`. Tests point it at throwaway packages built in a `tmp_path`.

**Orchestration loop (`docs/spec.md` §4).** `Client.build(**kwargs)` opens the DuckDB connection, then:
```python
for transformer in self.registry.get_active(**kwargs):
    transformer.extract(self.cache_dir, **kwargs)
    transformer.transform_and_load(self.con, self.cache_dir)
```
With zero transformers this loop body never executes; `build` still opens a usable, queryable connection and returns the client.

**Alternatives rejected.**
- *Decorator-based registration* — requires the transformer module to be imported before it self-registers; discovery would need an import trigger anyway, so it adds ceremony over the `pkgutil`+`inspect` scan. Rejected.
- *Entry-points (`importlib.metadata`)* — most decoupled but heaviest, and awkward for in-repo/test transformers that aren't separately installed. Rejected for a single-package, drop-a-file-in workflow.
- *"All discovered transformers always run"* — simplest, but it defers the activation-flag decision and would force a later plan to rewrite the build loop. The `is_active` hook costs ~3 lines now and avoids that. Chosen the hook.
- *setuptools backend / Python 3.9 floor* — Hatchling is the chosen modern PEP 621 backend; `>=3.11` is the chosen floor (clean `X | Y` typing matching the spec's signature style, best tracebacks for debugging ingestion, no dependency downside since DuckDB supports it). Rejected the alternatives deliberately.

## Project-Wide Constraints

- **Python / tooling:** package import name `crossroads`, distribution name `crossroads-uk`. Build backend **Hatchling**. `requires-python = ">=3.11"`. Develop against `/opt/homebrew/bin/python3.12`. src-layout (`src/crossroads/`).
- **Dependencies are deliberate and minimal** (`docs/spec.md` §2 "Upstream Trust Boundary"): runtime dependency is **`duckdb`** only; dev dependency is **`pytest`** only. Do not add geospatial / NetCDF / HTTP libraries here — later plans extend `pyproject.toml` as they need them.
- **Provider-plugin purity** (`docs/spec.md` §4): the orchestrator and registry must never name a concrete data source. Adding a source is "drop a module into `transformers/`", never an edit to `client.py` or `registry.py`.
- **Determinism / reproducibility** (`docs/spec.md` §2): no wall-clock or randomness in engine logic; discovery order may be sorted for stability if needed.

## Stage Map (sequential — do in order)

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Package skeleton & install | `pyproject.toml`, src-layout, README stub, `.gitignore` updates; editable install into a venv. | `pip install -e ".[dev]"` succeeds in a fresh `.venv`; `import crossroads` works; `crossroads.__version__` is a string; `pytest` green. | — | `01-package-skeleton.md` |
| 02 | Transformer contract & registry | `transformers/base.py` (`BaseTransformer` ABC + `is_active`) and `registry.py` (`pkgutil`+`inspect` discovery, `get_active`). Proven with throwaway mock transformer packages. | `Registry()` default-discovers zero concrete transformers; a mock concrete subclass in a temp package is discovered; `get_active(**kwargs)` filters on `is_active`. `pytest` green. | 01 | `02-transformer-registry.md` |
| 03 | Orchestrator & test harness | `client.py` (`init_engine` + `Client.build` loop), `__init__.py` exports, `conftest.py` DuckDB fixture, no-op build smoke test. | `cr.init_engine(...).build(...)` runs a clean no-op build, leaves a queryable connection, returns the client. Full `pytest` green. | 01, 02 | `03-orchestrator-harness.md` |

## Global Testing & Ship

All tests are real and runnable (manual testing is not relied upon). From the repo root with the venv active, `python -m pytest` must be green at the end of **every** stage. The end-to-end proof of this effort attaches to Stage 03: constructing the engine and running an empty `build()` against both an on-disk and in-memory DuckDB database, asserting the connection is open and queryable afterward. There is no real source data in scope, so there are no data-fidelity assertions yet — those arrive with the first real transformer in a later plan.

**Per-stage gate (run from repo root, venv active):**
```bash
python -m pytest -q
```
Expected: all tests pass, zero failures, zero errors.

## Open Questions / Risks

- **README placement.** `pyproject.toml` sets `readme = "README.md"`, so a `README.md` must exist for the build to succeed. Stage 01 creates a **minimal stub**; a later release-focused plan expands it. (Resolved: stub now.)
- **`duckdb` version floor.** Pinned `duckdb>=1.0` (current major line). If install resolves a surprising version, note it but do not pin tighter without reason.
- **sys.modules caching in registry tests.** Discovery tests import dynamically-created packages; each test must use a **unique** top-level package name and clean up `sys.modules`/`sys.path` (Stage 02 specifies the fixture that does this). Risk only materializes if names collide.
