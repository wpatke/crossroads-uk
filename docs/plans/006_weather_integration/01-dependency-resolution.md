# Stage 01 — Optional dependency resolution
> Part of *Meteorological Grid Integration (ERA5-Land)*. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Verify before starting (clean checkout):

```bash
pip install -e '.[dev]'
pytest -q          # full default suite green, offline
```

- `src/crossroads/transformers/base.py` defines `BaseTransformer(ABC)` with `source_id` (abstract), `user_selectable = True`, `display_name`, `quality_spec()`, `is_active()`, and abstract `extract`/`transform_and_load`.
- `src/crossroads/registry.py` `Registry._discover` sorts discovered instances by `source_id`; `get_active(**kwargs)` filters by `is_active` and the `datasets` selection gate and returns them **in discovery order**.
- `src/crossroads/transformers/stats19.py` `Stats19Transformer` has `source_id = "stats19"` and already reads `lad_boundaries`/`ctyua_boundaries` in `_spatial_stamp`. The boundary sources are `ons_lad` and `ons_ctyua` (`spatial.py`).

## Objective

Give the transformer contract an **optional dependency** declaration (`depends_on`) and teach the registry to return the active transformers in a **dependency-respecting, deterministic order**. A declared cycle **raises a clear exception** (`DependencyCycleError`) — we fail loud rather than ship a silently under-enriched build. Properly *handling* a cycle (re-running the idempotent optional step to a fixpoint) is a real feature we may want one day; it is **deferred**, and the exception message says so. Make STATS19's existing (currently implicit, alphabetical) ordering explicit by declaring its boundary dependencies. **No behaviour change** — the resolved order must equal today's order — suite stays green. This is the seam the weather feature plugs into.

## Implementation Steps

### Step 1 — Add `depends_on` to the transformer contract

File: `src/crossroads/transformers/base.py`.

Inside `class BaseTransformer(ABC):`, after the `user_selectable = True` block (and before the `display_name` property), add:

```python
    # Optional ordering dependencies: source_ids this transformer should run AFTER
    # when they are also active in the same build. "Optional" means an edge to a
    # source that is not active (not selected, or is_active() False) is simply
    # dropped — the dependent still runs, and guards at ETL time (e.g. by checking
    # whether the table it wants exists). This is ordering only; it never forces an
    # unselected source to run. A source declares it with a plain class attribute,
    # e.g. ``depends_on = ("era5_weather",)``. The registry topologically sorts the
    # active set by these edges (see registry.resolve_order).
    depends_on = ()
```

Expected result: `BaseTransformer.depends_on == ()`; every existing transformer inherits the empty default (no edges), so ordering is unchanged until a source overrides it.

### Step 2 — Topological resolver + cycle error in the registry

File: `src/crossroads/registry.py`.

**2a.** Add a cycle exception near the top (after the imports). The message frames a cycle as *not yet supported*, not a permanent prohibition — the intended future handling is a re-run of the idempotent optional step to a fixpoint:

```python
class DependencyCycleError(Exception):
    """Active transformers declare a circular ``depends_on`` relationship, so no valid
    single-pass import order exists. Failing loud is deliberate: a best-effort order
    would silently under-enrich one direction of the cycle. Cyclic optional dependencies
    are NOT SUPPORTED YET — the intended future handling is to re-run the (idempotent)
    optional enrichment step to a fixpoint. Until then, remove the cycle."""
```

**2b.** Add a module-level `resolve_order` function (place it above the `Registry` class or as a `@staticmethod` — a free function keeps it unit-testable in isolation):

```python
def resolve_order(transformers):
    """Order an ALREADY-FILTERED list of active transformers so each runs after its
    active ``depends_on`` sources. Deterministic (spec §2): among transformers that
    are ready (all dependencies already emitted), the smallest source_id goes next,
    so with no edges the result is exactly the source_id sort we used before.

    Optional edges: a depends_on entry that is not in this active set is ignored
    (the dependency was not selected / not active). A declared cycle among the
    active set raises DependencyCycleError (see the tail of the function).

    Kahn's algorithm over the active set:
      • edge (dep -> t) means dep must come before t;
      • start from nodes with no *active* incoming edges;
      • repeatedly emit the ready node with the smallest source_id.
    """
    by_id = {t.source_id: t for t in transformers}
    active_ids = set(by_id)
    # incoming[t] = the set of active source_ids t must wait for.
    incoming = {
        t.source_id: {d for d in getattr(t, "depends_on", ()) if d in active_ids}
        for t in transformers
    }
    ready = sorted(sid for sid, deps in incoming.items() if not deps)
    ordered = []
    remaining = dict(incoming)
    while ready:
        sid = ready.pop(0)               # smallest source_id first (ready is kept sorted)
        ordered.append(by_id[sid])
        del remaining[sid]
        # Drop this node from everyone still waiting on it; collect newly-ready ones.
        newly_ready = []
        for other, deps in remaining.items():
            if sid in deps:
                deps.discard(sid)
                if not deps:
                    newly_ready.append(other)
        for r in newly_ready:
            # Insert keeping `ready` sorted so selection stays deterministic.
            ready.append(r)
        ready.sort()
    if remaining:
        # Fail loud on a declared cycle (deliberate — see the overview). A best-effort
        # order would silently under-enrich one direction of the cycle, and a warning is
        # too easy to ignore. Cyclic optional dependencies are not supported yet; the
        # intended future handling is a re-run of the idempotent optional step to a
        # fixpoint. This branch is inert today (no source declares a cycle).
        raise DependencyCycleError(
            "circular optional depends_on among active transformers "
            f"[{', '.join(sorted(remaining))}]. Cyclic dependencies are not supported "
            "yet — remove the cycle (or make one edge non-circular)."
        )
    return ordered
```

**2c.** Apply the resolver at the end of `Registry.get_active`. The current method builds `active` (a list filtered by `is_active` and the `datasets` gate) and returns it; change the final `return active` to:

```python
        return resolve_order(active)
```

Leave the filtering logic above it untouched. (If `resolve_order` is a `@staticmethod`/method, call `self.resolve_order(active)`; a free function is simplest.)

Expected result: `get_active` returns the active transformers in dependency order. With all current `depends_on` empty, the order is the source_id sort — identical to today.

### Step 3 — Make STATS19's ordering explicit

File: `src/crossroads/transformers/stats19.py`.

On `Stats19Transformer`, add the class attribute (near `source_id`):

```python
    # STATS19 stamps codes from the boundary tables (and, once weather is built,
    # weather metrics) onto its own collisions table, so those sources must import
    # first. Declared explicitly rather than relying on source_id alphabetical order.
    # Optional: any of these not selected this build is skipped (guarded at ETL time).
    # "era5_weather" is included now so the ordering is correct the moment the weather
    # source exists; until then the edge is inert (era5_weather simply isn't active).
    depends_on = ("era5_weather", "ons_lad", "ons_ctyua")
```

Expected result: with boundaries active (and weather not yet existing), the resolver still emits `ons_ctyua`, `ons_lad`, `stats19` in that order (weather absent → edge dropped) — identical to today. Once the weather source exists (Stage 02), it sorts before `stats19` automatically.

## Testing & Verification

Offline. Commands:

```bash
pytest -q                          # full default suite still green
pytest -q tests/test_registry.py tests/test_stats19.py
```

### A. New registry unit tests — the resolver

Add to `tests/test_registry.py`. These drive `resolve_order` directly on lightweight fakes so no real transformer is needed:

```python
def test_resolve_order_orders_dependencies_before_dependents():
    from crossroads.registry import resolve_order

    class Fake:
        def __init__(self, sid, deps=()):
            self.source_id = sid
            self.depends_on = deps

    a = Fake("a")
    b = Fake("b", deps=("a",))          # b after a
    c = Fake("c", deps=("b",))          # c after b
    order = [t.source_id for t in resolve_order([c, b, a])]
    assert order == ["a", "b", "c"]


def test_resolve_order_is_deterministic_with_no_edges():
    from crossroads.registry import resolve_order

    class Fake:
        def __init__(self, sid):
            self.source_id = sid
            self.depends_on = ()

    order = [t.source_id for t in resolve_order([Fake("stats19"), Fake("ons_lad"),
                                                 Fake("ons_ctyua"), Fake("era5_weather")])]
    # No edges -> pure source_id sort (the previous behaviour).
    assert order == ["era5_weather", "ons_ctyua", "ons_lad", "stats19"]


def test_resolve_order_drops_inactive_dependency_edges():
    from crossroads.registry import resolve_order

    class Fake:
        def __init__(self, sid, deps=()):
            self.source_id = sid
            self.depends_on = deps

    # stats19 depends on era5_weather, but weather is NOT in the active set -> edge dropped.
    only = resolve_order([Fake("stats19", deps=("era5_weather", "ons_lad")), Fake("ons_lad")])
    order = [t.source_id for t in only]
    assert order == ["ons_lad", "stats19"]   # weather absent, no error, correct order


def test_resolve_order_raises_on_cycle():
    import pytest
    from crossroads.registry import resolve_order, DependencyCycleError

    class Fake:
        def __init__(self, sid, deps=()):
            self.source_id = sid
            self.depends_on = deps

    a = Fake("a", deps=("b",))
    b = Fake("b", deps=("a",))
    with pytest.raises(DependencyCycleError):
        resolve_order([b, a])
```

### B. New stats19 unit test — the declaration

Add to `tests/test_stats19.py`:

```python
def test_stats19_declares_optional_dependencies():
    from crossroads.transformers.stats19 import Stats19Transformer
    deps = Stats19Transformer().depends_on
    assert "era5_weather" in deps and "ons_lad" in deps and "ons_ctyua" in deps
```

### C. Registry integration — real active order unchanged

Add to `tests/test_registry.py`:

```python
def test_get_active_real_order_unchanged_by_declaration():
    from crossroads.registry import Registry
    reg = Registry()
    # Boundaries + stats19 active; weather source does not exist yet, so stats19's
    # era5_weather edge is inert. Order must match the historical source_id order.
    order = [t.source_id for t in reg.get_active(years=[2023])]
    assert order == ["ons_ctyua", "ons_lad", "stats19"]
```

### D. Regression — nothing else moved

- `tests/test_registry.py` existing selection/gate tests still pass (`resolve_order` preserves the source_id order when there are no active edges; `get_active`'s filtering is untouched).
- `tests/test_stats19.py::test_end_to_end_build_stamps_collisions` still passes: its manual `_transformers = [CTYUA, LAD, stats19]` resolves to `ctyua, lad, stats19` (stats19's boundary edges honoured; weather absent), so the boundary tables exist before the spatial stamp exactly as before.
- `tests/test_console.py`, `tests/test_client.py`, `tests/test_quality.py`, `tests/test_spatial.py` unchanged and green.

### Stage ship-readiness checklist

- [ ] `python -c "from crossroads.transformers.base import BaseTransformer; print(BaseTransformer.depends_on)"` prints `()`.
- [ ] `python -c "from crossroads.registry import Registry; print([t.source_id for t in Registry().get_active(years=[2023])])"` prints `['ons_ctyua', 'ons_lad', 'stats19']` (unchanged).
- [ ] `python -c "from crossroads.transformers.stats19 import Stats19Transformer as S; print(S().depends_on)"` prints the three-source tuple.
- [ ] `pytest -q` green.

## End State / Handoff (the contract)

- `BaseTransformer.depends_on` exists (default `()`), importable behaviour available on every transformer.
- `crossroads.registry` exposes `resolve_order(transformers)` and `DependencyCycleError`; `Registry.get_active` returns the active set in dependency-respecting, deterministic order and **raises `DependencyCycleError`** on a declared cycle (message: cyclic deps not supported yet). Actually handling a cycle (re-run to fixpoint) is the deferred future feature.
- `Stats19Transformer.depends_on == ("era5_weather", "ons_lad", "ons_ctyua")`; today's build order is byte-for-byte unchanged (weather inactive).
- Stage 02 may assume: creating a transformer with `source_id = "era5_weather"` and no `depends_on` will automatically sort before `stats19` in any build where both are active, because `stats19` already declares the edge.

## Failure Modes & Rollback

- **Resolver changes order subtly.** Guardrail: test A's no-edge case pins the exact historical order; test C pins the real `get_active` order.
- **A typo in a `depends_on` id** silently becomes an inert (dropped) edge, since unknown ids are treated as inactive. This is intentional (optional edges) but could hide a real ordering need; Stage 03's end-to-end stamp test is the backstop that the weather edge actually orders weather first.
- **Cycle raises loudly.** A genuine mutual `depends_on` raises `DependencyCycleError` (fail fast, no silent partial enrichment); no current source declares one, so the branch is inert. If a real cyclic-enrichment need arises later, the deferred feature is a re-run of the idempotent optional step to a fixpoint (not built now — speculative).
- **Rollback:** revert `base.py`, `registry.py`, `stats19.py`, and the added tests. `get_active` returns to plain discovery order; no schema/entry-point/dependency change was made. `git checkout` the three source files + the two test files restores the prior green state.
