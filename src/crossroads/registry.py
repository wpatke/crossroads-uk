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
