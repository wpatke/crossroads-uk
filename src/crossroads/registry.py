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


class DependencyCycleError(Exception):
    """Active transformers declare a circular ``depends_on`` relationship, so no valid
    single-pass import order exists. Failing loud is deliberate: a best-effort order
    would silently under-enrich one direction of the cycle. Cyclic optional dependencies
    are NOT SUPPORTED YET — the intended future handling is to re-run the (idempotent)
    optional enrichment step to a fixpoint. Until then, remove the cycle."""


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
        return resolve_order(active)
