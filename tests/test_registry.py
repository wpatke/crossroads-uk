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


def test_default_registry_discovers_at_least_one_transformer():
    # The default registry should find at least one concrete transformer.
    # (Exact contents vary as new transformers are added to the package.)
    registry = Registry()
    assert len(registry.all()) >= 1
    assert len(registry.get_active()) >= 1


def test_selectable_excludes_spatial_infrastructure():
    ids = {t.source_id for t in Registry().selectable()}
    assert "stats19" in ids
    assert "ons_lad" not in ids and "ons_ctyua" not in ids


def test_get_active_dataset_selection_gate():
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
