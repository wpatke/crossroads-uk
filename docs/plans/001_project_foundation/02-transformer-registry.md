# Stage 02 — Transformer contract & registry

## Objective

Define the data-source contract every future transformer implements (`BaseTransformer` ABC with `source_id` / `extract` / `transform_and_load` plus an `is_active` activation hook), and a `Registry` that discovers concrete transformer classes dynamically (`pkgutil` to enumerate modules, `inspect` to select subclasses) and filters them by activation.

## Implementation Steps

### 1. Create the `transformers` package

```bash
mkdir -p src/crossroads/transformers
```

Create `src/crossroads/transformers/__init__.py` as an **empty file** (it only marks the directory as a package so `pkgutil` can walk it):

```python
```
(zero bytes / empty is fine; a single blank line is acceptable.)

### 2. Create `src/crossroads/transformers/base.py`

The contract mirrors `docs/spec.md` §4, with the `is_active` hook added per the overview. Exact contents:

```python
"""The transformer contract every Crossroads data source implements."""

from abc import ABC, abstractmethod

import duckdb


class BaseTransformer(ABC):
    """Abstract base for a single data source's extract/transform/load pipeline.

    Concrete subclasses are discovered automatically by ``crossroads.registry.Registry``
    when their module is placed in ``crossroads.transformers``. No core engine code is
    edited to add a source.
    """

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Unique identifier for the datasource (e.g. 'stats19', 'era5_weather')."""
        raise NotImplementedError

    def is_active(self, **kwargs) -> bool:
        """Whether this source should run for a given ``build(**kwargs)`` call.

        Defaults to ``True`` (always run). A source gated behind a build flag overrides
        this, e.g. a weather source returns ``kwargs.get("include_weather", False)``.
        """
        return True

    @abstractmethod
    def extract(self, cache_dir: str, **kwargs) -> None:
        """Stream/download raw files directly to the local hardware cache."""
        raise NotImplementedError

    @abstractmethod
    def transform_and_load(self, con: duckdb.DuckDBPyConnection, cache_dir: str) -> None:
        """Execute zero-loss transformations and load into target DuckDB tables."""
        raise NotImplementedError
```

### 3. Create `src/crossroads/registry.py`

Exact contents:

```python
"""Dynamic discovery and activation of data-source transformers.

Discovery mechanism (spec §4, "module inspection"):
  1. ``pkgutil.iter_modules`` enumerates every module in the target package.
  2. Each module is imported.
  3. ``inspect`` selects classes that are concrete (non-abstract) ``BaseTransformer``
     subclasses defined in that module.
Adding a data source is therefore "drop a module into ``crossroads.transformers``" —
no edit to this file or the orchestrator.
"""

import importlib
import inspect
import pkgutil

from crossroads.transformers.base import BaseTransformer


class Registry:
    """Discovers transformer instances and filters them by activation."""

    def __init__(self, package=None):
        if package is None:
            import crossroads.transformers as package
        self._package = package
        self._transformers = self._discover(package)

    @staticmethod
    def _discover(package):
        discovered = []
        prefix = package.__name__ + "."
        for module_info in pkgutil.iter_modules(package.__path__, prefix):
            module = importlib.import_module(module_info.name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseTransformer)
                    and not inspect.isabstract(obj)
                    and obj.__module__ == module_info.name
                ):
                    discovered.append(obj())
        # Deterministic order for reproducibility (spec §2).
        discovered.sort(key=lambda t: t.source_id)
        return discovered

    def all(self):
        """Every discovered transformer instance."""
        return list(self._transformers)

    def get_active(self, **kwargs):
        """Discovered transformers whose ``is_active(**kwargs)`` returns True."""
        return [t for t in self._transformers if t.is_active(**kwargs)]
```

Key correctness points:
- `not inspect.isabstract(obj)` excludes `BaseTransformer` itself (it has abstract methods), so the default real package — whose only transformer module is `base.py` — yields **zero** concrete transformers.
- `obj.__module__ == module_info.name` ensures a class is counted only in the module that *defines* it, not in modules that merely `import` it.
- `discovered.sort(key=...)` guarantees stable ordering across runs.

### 4. Create the discovery tests

Create `tests/test_registry.py` with **exactly**:

```python
import importlib
import sys
import textwrap

import pytest

from crossroads.registry import Registry
from crossroads.transformers.base import BaseTransformer


@pytest.fixture
def make_transformer_package(tmp_path):
    """Build a throwaway importable package containing a transformer module.

    Each call MUST use a unique top-level package name: Python caches imports in
    sys.modules by name, so reusing a name would return a stale module. The fixture
    cleans up sys.modules and sys.path afterward.
    """
    created = []

    def _make(name, body):
        pkg_dir = tmp_path / name
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "source.py").write_text(textwrap.dedent(body))
        sys.path.insert(0, str(tmp_path))
        return importlib.import_module(name)

    def _track(name):
        created.append(name)

    _make.track = _track

    yield _make

    # Cleanup: remove imported modules and the temp path.
    for name in created:
        for mod_name in list(sys.modules):
            if mod_name == name or mod_name.startswith(name + "."):
                del sys.modules[mod_name]
    while str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))


def test_default_registry_discovers_no_concrete_transformers():
    # The real crossroads.transformers package contains only the abstract base,
    # so a default Registry finds zero concrete transformers (a clean no-op engine).
    registry = Registry()
    assert registry.all() == []
    assert registry.get_active() == []
    assert registry.get_active(include_weather=True) == []


def test_discovers_a_concrete_transformer(make_transformer_package):
    pkg = make_transformer_package(
        "mockpkg_discovery",
        """
        from crossroads.transformers.base import BaseTransformer

        class MockSource(BaseTransformer):
            @property
            def source_id(self):
                return "mock"

            def extract(self, cache_dir, **kwargs):
                pass

            def transform_and_load(self, con, cache_dir):
                pass
        """,
    )
    make_transformer_package.track("mockpkg_discovery")

    registry = Registry(package=pkg)
    ids = [t.source_id for t in registry.all()]
    assert ids == ["mock"]
    assert all(isinstance(t, BaseTransformer) for t in registry.all())


def test_abstract_subclass_is_not_discovered(make_transformer_package):
    # A subclass that leaves an abstract method unimplemented stays abstract and
    # must NOT be instantiated/discovered.
    pkg = make_transformer_package(
        "mockpkg_abstract",
        """
        from crossroads.transformers.base import BaseTransformer

        class StillAbstract(BaseTransformer):
            # extract/transform_and_load left unimplemented -> still abstract
            @property
            def source_id(self):
                return "nope"
        """,
    )
    make_transformer_package.track("mockpkg_abstract")

    registry = Registry(package=pkg)
    assert registry.all() == []


def test_get_active_filters_on_is_active(make_transformer_package):
    pkg = make_transformer_package(
        "mockpkg_activation",
        """
        from crossroads.transformers.base import BaseTransformer

        class AlwaysOn(BaseTransformer):
            @property
            def source_id(self):
                return "always"

            def extract(self, cache_dir, **kwargs):
                pass

            def transform_and_load(self, con, cache_dir):
                pass

        class WeatherLike(BaseTransformer):
            @property
            def source_id(self):
                return "weatherish"

            def is_active(self, **kwargs):
                return kwargs.get("include_weather", False)

            def extract(self, cache_dir, **kwargs):
                pass

            def transform_and_load(self, con, cache_dir):
                pass
        """,
    )
    make_transformer_package.track("mockpkg_activation")

    registry = Registry(package=pkg)

    off = {t.source_id for t in registry.get_active()}
    on = {t.source_id for t in registry.get_active(include_weather=True)}

    assert off == {"always"}
    assert on == {"always", "weatherish"}
```

> Note on the fixture: `make_transformer_package.track(name)` registers a package name for cleanup. Call it once per package you create so `sys.modules` is cleaned between tests. (The `_make`/`track` split keeps the helper simple and the cleanup reliable.)

## Testing & Verification

**Integration test (PRIMARY) — discovery and activation work end-to-end** against real, dynamically-built packages (not mocks of `pkgutil`/`inspect`). With the venv active, from the repo root:

```bash
python -m pytest -q tests/test_registry.py
```
Expected: `4 passed` — default no-op discovery, concrete discovery, abstract exclusion, and activation filtering.

**Full suite (nothing regressed):**
```bash
python -m pytest -q
```
Expected: `6 passed` (the 2 from Stage 01 plus these 4).

**Manual sanity (optional):**
```bash
python -c "from crossroads.registry import Registry; print(Registry().all())"
```
Expected: `[]`.

## Known Pitfalls

- **`test_default_registry_discovers_no_concrete_transformers` fails (finds something).** A concrete transformer leaked into `crossroads.transformers`, or the `inspect.isabstract` / `__module__` guard was dropped. Ensure only `base.py` (+ empty `__init__.py`) is in the package and the guards are intact.
- **Second/third discovery test sees the wrong module.** Two tests reused the same package name and hit the `sys.modules` cache. Ensure each `make_transformer_package(...)` call uses a unique name and is registered via `.track(...)`.
- **`pkgutil.iter_modules` finds nothing for a temp package.** The temp package is missing its `__init__.py`, or `tmp_path` was not added to `sys.path`. The fixture handles both — verify it wasn't altered.
- **`ImportError: crossroads.transformers` in `Registry()`.** The `transformers/__init__.py` marker file is missing. Create it.
