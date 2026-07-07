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

    def selectable(self):
        """Discovered transformers a researcher can pick in the wizard menu.

        Excludes always-on infrastructure (``user_selectable=False``, e.g. spatial
        boundary tables). Order follows the deterministic source_id sort from _discover.
        """
        return [t for t in self._transformers if getattr(t, "user_selectable", True)]

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
