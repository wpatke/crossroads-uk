# Dataset Selection in the CLI Wizard
> Engineer: execute step by step, exactly as written.
> Part of the `005_console_architecture/` plan directory.

Add a self-discovering, multi-select **"Which datasets would you like?"** prompt to the console wizard, so the researcher chooses which queryable datasets to build (currently only `stats19`) using the same `1-3, 5` range grammar the years prompt uses — while spatial/boundary sources stay hidden and always active.

---

## Context & Objective

### What exists today

- **`src/crossroads/console.py`** — the wizard. `gather_parameters(reader, writer)` runs three prompts in order and returns `{"database_path", "years", "boundary_mode"}`. `run_wizard` composes gather → summary → confirm → `run_build`. `run_build` calls `engine_factory(database_path=...).build(years=..., boundary_mode=...)`. All I/O is injected via `reader`/`writer` for offline testing. The wizard does **not** currently touch the registry.
- **`src/crossroads/registry.py`** — `Registry` auto-discovers every concrete `BaseTransformer` subclass across modules in `crossroads.transformers`, sorted by `source_id` for determinism. `get_active(**kwargs)` returns the discovered transformers whose `is_active(**kwargs)` is `True`. `all()` returns every discovered instance.
- **`src/crossroads/transformers/base.py`** — the `BaseTransformer` ABC. `source_id` is an abstract property. `is_active(**kwargs)` defaults to `True` (always run).
- **`src/crossroads/transformers/stats19.py`** — `Stats19Transformer`, `source_id = "stats19"`. Its `is_active(**kwargs)` returns `bool(kwargs.get("years"))` — inactive without years. This is **explicitly tested** (`tests/test_stats19.py` asserts `is_active()` is `False` and `is_active(years=[2023])` is `True`) and relied on internally, so it must **not** change.
- **`src/crossroads/transformers/spatial.py`** — `_BoundaryTransformer` (abstract shared base) with two concrete subclasses `LADBoundaryTransformer` (`source_id = "ons_lad"`) and `CTYUABoundaryTransformer` (`source_id = "ons_ctyua"`). Both inherit the default `is_active` → always active. These are the spatial join tables other sources map against; they are **not** queryable datasets a researcher would pick.
- **`src/crossroads/client.py`** — `Client.build(**kwargs)` calls `self.registry.get_active(**kwargs)`, then for each active transformer calls `extract(cache_dir, **kwargs)` and `transform_and_load(con, cache_dir)`. Both `extract` implementations accept `**kwargs` (verified), so forwarding a new `datasets` kwarg through `build` is harmless — unknown kwargs are ignored by the transformers.
- **`tests/test_console.py`**, **`tests/test_registry.py`**, **`tests/test_spatial.py`**, **`tests/test_stats19.py`** — all green. The default suite runs offline; integration tests are `@pytest.mark.integration` and deselected by default.

### What changes

1. Mark spatial/boundary transformers as **infrastructure** (not user-selectable) so they stay out of the menu and always run.
2. Give the registry a way to (a) list the **selectable** datasets for the menu and (b) gate activation by an explicit user selection, **without** breaking the current no-selection (programmatic) build path.
3. Add a **datasets multi-select prompt** to the wizard (after the database-path prompt), pass the selection through to `build(datasets=[...])`, and show it in the summary.

### The goal

A researcher runs `crossroads`, sees a numbered list of datasets discovered automatically from the transformer modules, selects with `1-3, 5` grammar, and only the chosen datasets build. Adding a future dataset (e.g. a hospital-admissions or weather source) makes it appear in the menu with **no console/registry edits** — only its own transformer file is added. Proven by offline automated tests.

---

## Acceptance Criteria

Verifiable, all offline:

1. `Registry().selectable()` returns only user-selectable transformers: it **includes** `stats19` and **excludes** `ons_lad` and `ons_ctyua`.
2. `Registry().get_active(years=[2023], datasets=["stats19"])` includes `stats19`, `ons_lad`, `ons_ctyua` (spatial always runs). `get_active(years=[2023], datasets=[])` includes `ons_lad`, `ons_ctyua` but **not** `stats19` (deselected). `get_active(years=[2023])` — **no** `datasets` kwarg — includes `stats19`, `ons_lad`, `ons_ctyua` (backward-compatible pass-through).
3. `Stats19Transformer().user_selectable is True`; `LADBoundaryTransformer().user_selectable is False`; `CTYUABoundaryTransformer().user_selectable is False`.
4. `Stats19Transformer().display_name == "stats19"` (defaults to `source_id`).
5. `console.gather_parameters(reader, writer, available=[("stats19", "stats19")])` driven with a datasets answer returns a dict whose `datasets` key is the selected source-id list, plus the existing `database_path`, `years`, `boundary_mode` keys.
6. The datasets prompt accepts the `1-3, 5` grammar, dedupes/sorts, rejects out-of-range/backwards/non-numeric selections by re-asking, and **requires at least one selection**: an empty/whitespace answer is rejected and re-asked with a clear message (`select at least one dataset, e.g. 1 or 1-3`) — the wizard can never proceed with zero datasets.
7. The menu displays `display_name` (so a future `("era5_weather", "weather")` shows `weather`, not `era5_weather`).
8. `run_wizard` produces `build(datasets=[...], years=[...], boundary_mode=...)` with the selected ids; declining still never builds.
9. The offline integration test still builds a populated database when the sole dataset (`stats19`) is selected.
10. `Stats19Transformer().is_active()` is unchanged: `False` without years, `True` with years.
11. `pytest -q` (default suite) and `pytest -m integration -q tests/test_console.py` are both green.

---

## Scope

**In:**
- `user_selectable` flag + `display_name` on `BaseTransformer`; `user_selectable = False` on the spatial shared base.
- `Registry.selectable()` + the dataset-selection gate in `Registry.get_active`.
- New `available_datasets()` helper, `prompt_datasets`, selection-index parsers in `console.py`; wire `datasets` into `gather_parameters`, `format_summary`, `run_build`, `run_wizard`.
- Test updates + new tests.

**Out:**
- Making `years` / `boundary_mode` conditional on which datasets are selected (kept unconditional — deferred).
- Per-dataset parameter prompts, dataset descriptions/help text beyond the display name.
- Any change to `Stats19Transformer.is_active` or the spatial transformers' behavior beyond adding the flag.
- Anything in `client.py` (it already forwards `**kwargs`).

---

## Constraints

- **No new runtime dependency.** Stdlib only; `input`/`print` injected as today.
- **Self-discovery preserved (spec §4):** adding a dataset must require editing **only** the new transformer file. The registry/console must not name a specific source.
- **Backward compatibility:** a `build(...)` call with **no** `datasets` kwarg (the programmatic flow) must behave exactly as before — all selectable sources active.
- **Determinism (spec §2):** the menu order is the registry's existing `source_id` sort, so a given version lists datasets in a stable order.
- **Offline, deterministic tests.** No network in the default suite.
- **Keep it simple, comment in plain language (CLAUDE.md).** Match the existing docstring/comment density in `console.py` and the transformers.
- **Never stage or commit (CLAUDE.md).** Do not run `git add`/`git commit`.
- Do **not** copy or adapt the GPL-3 `../stats19/` reference package.

---

## Approach / Architecture

**Chosen design: a `user_selectable` class flag + a central selection gate in the registry.**

- `BaseTransformer.user_selectable = True` (class attribute, default). The spatial shared base sets it `False`, inherited by both boundary subclasses. A new dataset is selectable automatically (inherits `True`) — no edit anywhere else. This is the single source of truth for "does this appear in the wizard menu and obey the user's picks?".
- `BaseTransformer.display_name` — a property returning `self.source_id` by default; a source overrides it with a friendlier label by setting a class attribute (e.g. `display_name = "weather"`).
- `Registry.selectable()` lists the selectable transformers (menu source). `Registry.get_active` gains a gate: when the caller passes an explicit `datasets` list, a *selectable* source runs only if its `source_id` is in that list; *infrastructure* sources (`user_selectable=False`) always run; and when **no** `datasets` kwarg is passed the gate is a pass-through (backward compatibility). This composes with — does not replace — the existing `is_active` check, so `stats19`'s years-gating is untouched.
- The wizard discovers the menu via `available_datasets()` (wraps `Registry().selectable()`), presents it numbered, parses the `1-3, 5` selection into `source_id`s reusing the same range grammar as the years prompt, and threads the selection into `build(datasets=[...])`.

**Alternatives rejected:**
- *Exclude spatial by module name in the registry* — hardcodes `transformers.spatial` in core code, brittle, breaks self-discovery.
- *Gate everything through `is_active` only* — there would be nothing that distinguishes "selectable dataset" from "always-on infrastructure", so the wizard couldn't build the menu; and it would force every future dataset to hand-write dataset-selection logic instead of getting it for free.
- *Replace `stats19.is_active(years)` with dataset-gating* — breaks the tested/relied-upon years behavior and the programmatic `build(years=[...])` flow.

**Data flow:**
`main` → `run_wizard(reader, writer)` → `gather_parameters` (`available = available_datasets()` → `Registry().selectable()`) → prompts: database_path, **datasets** (`prompt_datasets`), years, boundary_mode → `{"database_path","datasets","years","boundary_mode"}` → `format_summary` + confirm → `run_build` → `engine_factory(database_path=...).build(datasets=<ids>, years=..., boundary_mode=...)` → `Registry.get_active` applies `is_active` **and** the selection gate → only chosen datasets + always-on spatial run.

---

## Implementation Steps

### Step 1 — Add `user_selectable` and `display_name` to `BaseTransformer`

File: `src/crossroads/transformers/base.py`.

Inside `class BaseTransformer(ABC):`, **after** the abstract `source_id` property and **before** `quality_spec`, add:

```python
    # Whether this source appears in the interactive wizard's dataset menu and
    # obeys the user's dataset selection. True = a queryable dataset a researcher
    # picks. False = always-on infrastructure (e.g. spatial boundary tables that
    # other sources join against, never selected on their own). Default True so a
    # newly added transformer is selectable automatically — no console/registry edit.
    user_selectable = True

    @property
    def display_name(self) -> str:
        """Human-friendly label shown in the wizard's dataset menu.

        Defaults to ``source_id``. A source overrides it for a friendlier name by
        setting a plain class attribute, e.g. ``display_name = "weather"``.
        """
        return self.source_id
```

Expected result: `BaseTransformer` exposes `user_selectable` (default `True`) and a `display_name` defaulting to `source_id`. Subclasses that set `display_name = "..."` as a class attribute shadow the property (standard Python attribute override).

### Step 2 — Mark the spatial/boundary transformers as infrastructure

File: `src/crossroads/transformers/spatial.py`.

In `class _BoundaryTransformer(BaseTransformer):` (the shared abstract base — **not** the concrete subclasses), add near the top of the class body, alongside the other class-level identity declarations:

```python
    # Spatial boundaries are always-on infrastructure that other datasets join
    # against — never a dataset the researcher selects on its own. Keep them out of
    # the wizard menu and always active regardless of the user's dataset picks.
    user_selectable = False
```

Both `LADBoundaryTransformer` and `CTYUABoundaryTransformer` inherit this. Do **not** change their `is_active` (they remain always active).

Expected result: `LADBoundaryTransformer().user_selectable` and `CTYUABoundaryTransformer().user_selectable` are `False`; `Stats19Transformer().user_selectable` stays `True` (inherited default).

### Step 3 — Registry: `selectable()` and the selection gate

File: `src/crossroads/registry.py`.

**3a.** Add a `selectable()` method to `Registry` (place it right after `all()`):

```python
    def selectable(self):
        """Discovered transformers a researcher can pick in the wizard menu.

        Excludes always-on infrastructure (``user_selectable=False``, e.g. spatial
        boundary tables). Order follows the deterministic source_id sort from _discover.
        """
        return [t for t in self._transformers if getattr(t, "user_selectable", True)]
```

**3b.** Replace `get_active` with a version that applies the selection gate:

```python
    def get_active(self, **kwargs):
        """Discovered transformers that should run for this build.

        A transformer runs when its ``is_active(**kwargs)`` is True AND it passes the
        dataset-selection gate:
          • When the caller supplies an explicit ``datasets`` list (the wizard does),
            a user-selectable source runs only if its ``source_id`` is in that list.
          • Infrastructure sources (``user_selectable=False``) always run.
          • With no ``datasets`` kwarg (the programmatic build flow), the gate is a
            pass-through, so behavior is unchanged.
        """
        datasets = kwargs.get("datasets")
        active = []
        for t in self._transformers:
            if not t.is_active(**kwargs):
                continue
            if (
                datasets is not None
                and getattr(t, "user_selectable", True)
                and t.source_id not in datasets
            ):
                continue  # selectable but not chosen by the user
            active.append(t)
        return active
```

Expected result: no-`datasets` calls behave exactly as before; an explicit `datasets` list restricts which selectable sources run while spatial always runs.

### Step 4 — Console: discovery helper + selection parsers

File: `src/crossroads/console.py`.

**4a.** Add a discovery helper (place it near the top, after the imports). Use a **function-level import** of `Registry` to avoid any import-time coupling:

```python
def available_datasets():
    """Discover the user-selectable datasets for the wizard menu.

    Returns a list of ``(source_id, display_name)`` in the registry's stable order.
    Imported lazily so importing the console module stays cheap.
    """
    from crossroads.registry import Registry
    return [(t.source_id, t.display_name) for t in Registry().selectable()]
```

**4b.** Add the selection-index parsers (place them just before `gather_parameters`). These mirror the years grammar but validate 1-based menu indices:

```python
def _parse_one_index(token, count):
    """Parse a single menu index token to an int and range-check it (1..count)."""
    try:
        i = int(token)
    except ValueError:
        raise ValueError(f"'{token}' is not a whole number")
    if not (1 <= i <= count):
        raise ValueError(f"{i} is not between 1 and {count}")
    return i


def _parse_selection(raw, count):
    """Parse a '1-3, 5' style menu selection into a sorted list of 1-based indices.

    Same comma/space/range grammar as the years prompt: singles and tight-hyphen
    ranges, deduped and sorted. Requires at least one index.
    """
    tokens = [t for t in raw.replace(",", " ").split() if t]
    if not tokens:
        raise ValueError("select at least one dataset, e.g. 1 or 1-3")
    picked = set()
    for t in tokens:
        if "-" in t:
            start, _, end = t.partition("-")
            lo = _parse_one_index(start, count)
            hi = _parse_one_index(end, count)
            if lo > hi:
                raise ValueError(f"range '{t}' is backwards (start after end)")
            picked.update(range(lo, hi + 1))
        else:
            picked.add(_parse_one_index(t, count))
    return sorted(picked)
```

**4c.** Add `prompt_datasets` (place it after `prompt_database_path`, i.e. in the order the wizard asks):

```python
def prompt_datasets(reader, writer, available):
    """Multi-select which datasets to build.

    ``available`` is a list of ``(source_id, display_name)`` in stable order (from
    ``available_datasets()``). Presents them numbered and returns the selected
    ``source_id`` list, using the same range grammar as the years prompt.
    """
    if not available:
        # Defensive: there is always at least one selectable dataset in practice.
        raise ValueError("no selectable datasets are available")
    writer("Which datasets would you like?")
    for i, (_source_id, label) in enumerate(available, start=1):
        writer(f"  {i}. {label}")

    def parse(raw):
        indices = _parse_selection(raw, len(available))
        # Map 1-based menu indices back to source_ids.
        return [available[i - 1][0] for i in indices]

    return _prompt(reader, writer,
                   "Datasets (e.g. 1 or 1-3, 5)", parse=parse, default=None)
```

Expected result: the menu prints `display_name`s; an out-of-range/backwards/non-numeric/empty answer re-asks; a valid answer returns the selected `source_id`s.

### Step 5 — Console: thread `datasets` through gather → summary → build

File: `src/crossroads/console.py`.

**5a.** Replace `gather_parameters` with a version that adds the datasets prompt **after** the database-path prompt and accepts an injectable `available` menu:

```python
def gather_parameters(reader, writer, *, available=None):
    """Run the parameter prompts and return the build-parameter dict.

    Keys map onto the build surface: ``database_path`` feeds
    ``init_engine(database_path=...)``; ``datasets``, ``years`` and
    ``boundary_mode`` feed ``client.build(...)``. ``available`` is the dataset menu
    (list of ``(source_id, display_name)``); defaults to live discovery and is
    injectable so tests supply a fixed menu.
    """
    if available is None:
        available = available_datasets()
    writer("Crossroads-UK — data compilation wizard")
    writer("")  # blank spacer line
    return {
        "database_path": prompt_database_path(reader, writer),
        "datasets": prompt_datasets(reader, writer, available),
        "years": prompt_years(reader, writer),
        "boundary_mode": prompt_boundary_mode(reader, writer),
    }
```

**5b.** Update `format_summary` to show the datasets (add the line between Database file and Years):

```python
def format_summary(params):
    """Human-readable recap of the gathered parameters, shown before confirmation."""
    years = ", ".join(str(y) for y in params["years"])
    datasets = ", ".join(params["datasets"])
    return (
        "\nBuild summary:\n"
        f"  Database file : {params['database_path']}\n"
        f"  Datasets      : {datasets}\n"
        f"  Years         : {years}\n"
        f"  Boundary mode : {params['boundary_mode']}\n"
    )
```

**5c.** Update the `client.build(...)` call inside `run_build` to pass the selection:

```python
    client.build(datasets=params["datasets"],
                 years=params["years"],
                 boundary_mode=params["boundary_mode"])
```

**5d.** Update `run_wizard` to accept and forward the injectable menu:

```python
def run_wizard(reader, writer, *, engine_factory=init_engine, cache_dir=None, available=None):
    """Drive the full wizard. Returns the built Client, or None if the user declined.

    All I/O is injected so this is fully testable with scripted input. ``available``
    overrides the dataset menu for deterministic tests.
    """
    params = gather_parameters(reader, writer, available=available)
    writer(format_summary(params))
    if not prompt_confirm(reader, writer, default=True):
        writer("Aborted — no database was built.")
        return None
    return run_build(params, engine_factory=engine_factory,
                     cache_dir=cache_dir, writer=writer)
```

`main` needs **no** change: it calls `run_wizard(reader, writer)` with `available=None`, so production uses live discovery.

Expected result: the wizard asks database path → datasets → years → boundary mode; the summary lists the datasets; the build receives `datasets=<selected ids>`.

---

## Testing & Verification

All tests are offline. Commands to run at the end:

```
pytest -q                                       # full default suite (integration deselected)
pytest -m integration -q tests/test_console.py  # offline real-build integration test
```

### A. Update existing full-flow console tests (insert a datasets answer)

File: `tests/test_console.py`. The datasets prompt is now the **second** prompt (after database path). Every test that drives the *full* `gather_parameters`/`run_wizard`/`main` flow must insert a datasets answer and (where injected) pass a fixed `available` menu. Edits:

1. `test_gather_parameters_happy_path` — call `console.gather_parameters(reader, writer, available=[("stats19", "stats19")])`; scripted answers `["mydb.duckdb", "1", "2021 2022", "temporal"]`; assert the returned dict equals `{"database_path": "mydb.duckdb", "datasets": ["stats19"], "years": [2021, 2022], "boundary_mode": "temporal"}`.
2. `test_defaults_via_empty_lines` — call with `available=[("stats19", "stats19")]`; scripted `["", "1", "2023", ""]` (datasets has no default, so answer `"1"`); assert `database_path == "crossroads.db"`, `datasets == ["stats19"]`, `years == [2023]`, `boundary_mode == "snapshot"`.
3. `test_wizard_produces_correct_build_invocation` — call `console.run_wizard(reader, writer, engine_factory=factory, available=[("stats19", "stats19")])`; scripted `["mydb.duckdb", "1", "2022 2023", "temporal", "y"]`; assert `build_kwargs == {"datasets": ["stats19"], "years": [2022, 2023], "boundary_mode": "temporal"}` and `init_kwargs == {"database_path": "mydb.duckdb"}`.
4. `test_decline_does_not_build` — call `run_wizard(..., engine_factory=factory, available=[("stats19", "stats19")])`; scripted `[":memory:", "1", "2023", "snapshot", "n"]`; assert result is `None`, factory never called, output contains "Aborted".
5. `test_main_abort_path_returns_zero` — `main` uses live discovery (real registry lists `stats19` at index 1), so just insert `"1"`: answers `iter([":memory:", "1", "2023", "snapshot", "n"])`; assert `console.main() == 0` and "Aborted" in captured output.
6. `test_wizard_builds_populated_database_offline` (integration) — uses the real menu (do **not** inject `available`; real discovery lists `stats19` at index 1). Insert `"1"`: scripted `[db_path, "1", "2023", "snapshot", "y"]`; keep the existing `run_wizard(reader, writer, cache_dir=cache)` call and populated-DB assertions.

### B. New console tests (fast, offline)

Add to `tests/test_console.py`:

```python
def test_prompt_datasets_single_selection():
    reader, writer, output = scripted(["1"])
    result = console.prompt_datasets(reader, writer, [("stats19", "stats19")])
    assert result == ["stats19"]
    assert any("1. stats19" in line for line in output)   # menu was shown


def test_prompt_datasets_uses_display_name():
    reader, writer, output = scripted(["1"])
    console.prompt_datasets(reader, writer, [("era5_weather", "weather")])
    assert any("1. weather" in line for line in output)   # friendly label, not source_id


def test_prompt_datasets_range_and_list_dedup_sort():
    menu = [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]
    reader, writer, _ = scripted(["1-3, 4"])
    assert console.prompt_datasets(reader, writer, menu) == ["a", "b", "c", "d"]
    # overlapping selections collapse
    reader, writer, _ = scripted(["2-3 3-4"])
    assert console.prompt_datasets(reader, writer, menu) == ["b", "c", "d"]


def test_prompt_datasets_rejects_out_of_range_then_accepts():
    reader, writer, output = scripted(["9", "1"])   # 9 > count(1), re-ask, then valid
    result = console.prompt_datasets(reader, writer, [("stats19", "stats19")])
    assert result == ["stats19"]
    assert sum("Invalid input" in line for line in output) == 1


def test_prompt_datasets_rejects_backwards_range():
    menu = [("a", "A"), ("b", "B"), ("c", "C")]
    reader, writer, output = scripted(["3-1", "1-2"])
    assert console.prompt_datasets(reader, writer, menu) == ["a", "b"]
    assert any("backwards" in line for line in output)


def test_prompt_datasets_requires_at_least_one():
    reader, writer, output = scripted(["", "1"])   # empty rejected, then valid
    result = console.prompt_datasets(reader, writer, [("stats19", "stats19")])
    assert result == ["stats19"]
    assert sum("Invalid input" in line for line in output) == 1


def test_format_summary_includes_datasets():
    summary = console.format_summary({
        "database_path": "x.db", "datasets": ["stats19"],
        "years": [2023], "boundary_mode": "snapshot",
    })
    assert "Datasets" in summary and "stats19" in summary
```

### C. New registry / transformer tests (fast, offline)

Add to `tests/test_registry.py`:

```python
def test_selectable_excludes_spatial_infrastructure():
    from crossroads.registry import Registry
    ids = {t.source_id for t in Registry().selectable()}
    assert "stats19" in ids
    assert "ons_lad" not in ids and "ons_ctyua" not in ids


def test_get_active_dataset_selection_gate():
    from crossroads.registry import Registry
    reg = Registry()
    # Explicit selection: stats19 chosen -> runs; spatial always runs.
    chosen = {t.source_id for t in reg.get_active(years=[2023], datasets=["stats19"])}
    assert {"stats19", "ons_lad", "ons_ctyua"} <= chosen
    # Empty selection: stats19 dropped; spatial still runs.
    none_chosen = {t.source_id for t in reg.get_active(years=[2023], datasets=[])}
    assert "stats19" not in none_chosen
    assert {"ons_lad", "ons_ctyua"} <= none_chosen
    # No datasets kwarg: backward-compatible pass-through (all active as before).
    legacy = {t.source_id for t in reg.get_active(years=[2023])}
    assert {"stats19", "ons_lad", "ons_ctyua"} <= legacy
```

Add to `tests/test_spatial.py` (near the other boundary-transformer assertions):

```python
def test_boundary_transformers_are_not_user_selectable():
    from crossroads.transformers.spatial import (
        LADBoundaryTransformer, CTYUABoundaryTransformer,
    )
    assert LADBoundaryTransformer().user_selectable is False
    assert CTYUABoundaryTransformer().user_selectable is False
```

Add to `tests/test_stats19.py` (near the existing `is_active` assertions):

```python
def test_stats19_is_user_selectable_with_default_display_name():
    from crossroads.transformers.stats19 import Stats19Transformer
    t = Stats19Transformer()
    assert t.user_selectable is True
    assert t.display_name == "stats19"
```

### D. Regression checks (must remain green, unchanged)

- `tests/test_stats19.py` existing `is_active()` / `is_active(years=[2023])` assertions still pass (we did not touch `Stats19Transformer.is_active`).
- `tests/test_registry.py::test_get_active_filters_on_is_active` still passes (its mock package passes no `datasets`, so the gate is a pass-through; its mock transformers inherit `user_selectable=True`).
- `tests/test_client.py` and `tests/test_quality.py` builds (no `datasets` kwarg) still pass unchanged.

### Ship-readiness checklist

- [ ] `python -c "from crossroads.registry import Registry; print([t.source_id for t in Registry().selectable()])"` prints `['stats19']` (spatial excluded).
- [ ] `python -c "from crossroads.transformers.spatial import LADBoundaryTransformer as L; print(L().user_selectable)"` prints `False`.
- [ ] `pytest -q` green (full default suite).
- [ ] `pytest -m integration -q tests/test_console.py` green.
- [ ] Manual sanity (optional, offline): `printf 'x.db\n1\n2023\nsnapshot\nn\n' | crossroads` shows the "Which datasets would you like?" menu with `1. stats19`, then `Aborted — no database was built.` and exits `0`.

---

## Performance

Negligible. `selectable()`/`get_active` iterate the small in-memory transformer list once (`O(#transformers)`, currently 3). `available_datasets()` runs discovery once per wizard invocation (module import + inspection, already done at build time). The selection parser is `O(#tokens)`. No hot paths, no I/O added.

---

## Failure Modes

- **Empty / bad selection (interactive)** → `_parse_selection` raises `ValueError`; `_prompt` re-asks. The wizard therefore cannot submit zero datasets. Covered by tests B (`requires_at_least_one`, `rejects_out_of_range`, `rejects_backwards_range`).
- **Empty selection (programmatic `build(datasets=[])`)** → **deliberately not a hard error.** The gate runs only always-on infrastructure (spatial), producing a boundaries-only database. This path is unreachable from the wizard (the re-ask above blocks it), so adding a raise would introduce a special case and risk colliding with legitimate infrastructure-only builds. Left as documented behavior; revisit only if a real programmatic caller trips on it.
- **Spaced hyphen in a range** (`"1 - 3"`) → tokenizes to junk `"-"` → rejected, same deliberate simplification as the years prompt. (Range needs a tight hyphen `1-3`.)
- **No selectable datasets discovered** → `prompt_datasets` raises `ValueError("no selectable datasets are available")`. Cannot occur while `stats19` exists; defensive only.
- **A future transformer forgets `display_name`** → falls back to `source_id` via the base property. No breakage.
- **A future transformer that should be infrastructure forgets `user_selectable=False`** → it appears in the menu (safe default). Guardrail: the spatial test in C documents the pattern; reviewers see the flag on the shared base.
- **Backward-compat regression** → the no-`datasets` pass-through branch and test C `legacy` assertion guard the programmatic flow.

## Rollback

Revert the edits to `base.py`, `spatial.py`, `registry.py`, `console.py`, and the test changes/additions in `tests/test_console.py`, `tests/test_registry.py`, `tests/test_spatial.py`, `tests/test_stats19.py`. No schema, packaging, or entry-point changes were made, so reverting restores the prior green state. (Or `git checkout` the five source/test files.)

## Open Questions

- **Menu order stability across versions.** Indices follow the `source_id` sort, so adding a dataset can shift existing numbers between releases. Acceptable for an interactive prompt; revisit only if stable numbering becomes a requirement.
- **Per-dataset parameters** (e.g. a non-year dataset that shouldn't be asked for years) are intentionally deferred; `years`/`boundary_mode` stay unconditional for this change.
