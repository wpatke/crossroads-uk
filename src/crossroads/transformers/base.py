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

    # Whether this source appears in the interactive wizard's dataset menu and
    # obeys the user's dataset selection. True = a queryable dataset a researcher
    # picks. False = always-on infrastructure (e.g. spatial boundary tables that
    # other sources join against, never selected on their own). Default True so a
    # newly added transformer is selectable automatically — no console/registry edit.
    user_selectable = True

    # Optional ordering dependencies: source_ids this transformer should run AFTER
    # when they are also active in the same build. "Optional" means an edge to a
    # source that is not active (not selected, or is_active() False) is simply
    # dropped — the dependent still runs, and guards at ETL time (e.g. by checking
    # whether the table it wants exists). This is ordering only; it never forces an
    # unselected source to run. A source declares it with a plain class attribute,
    # e.g. ``depends_on = ("era5_weather",)``. The registry topologically sorts the
    # active set by these edges (see registry.resolve_order).
    depends_on = ()

    @property
    def display_name(self) -> str:
        """Human-friendly label shown in the wizard's dataset menu.

        Defaults to ``source_id``. A source overrides it for a friendlier name by
        setting a plain class attribute, e.g. ``display_name = "weather"``.
        """
        return self.source_id

    def quality_spec(self) -> "SourceQuality | QualityExemption | None":
        """Declare this source's audit surface for the quality engine
        (conservation, flag/ledger agreement, reject-rate invariants).

        Override to return one of:
          • SourceQuality(...)         -> this source is audited.
          • QualityExemption(reason=)  -> deliberately NOT audited, with a written
                                          reason (e.g. it aggregates rows so the
                                          conservation invariant does not apply).

        The inherited default below (None) means 'undecided'. For an ACTIVE
        transformer that is enforced at build time: a real source must make a
        conscious choice (see crossroads.quality.resolve_quality_specs). Keeping
        this concrete (not @abstractmethod) means a subclass that forgets to
        override it stays discoverable by the registry and fails loudly at the
        coverage gate, rather than being silently dropped from discovery.
        """
        return None

    def is_active(self, **kwargs) -> bool:
        """Whether this source should run for a given ``build(**kwargs)`` call.

        Defaults to ``True`` (always run). A source gated behind a build parameter
        overrides this (e.g. ``return bool(kwargs.get("years"))``).
        """
        return True

    @abstractmethod
    def extract(self, cache_dir: str, **kwargs) -> None:
        """Stream/download raw files directly to the local hardware cache."""
        raise NotImplementedError

    @abstractmethod
    def transform_and_load(self, con: duckdb.DuckDBPyConnection, cache_dir: str) -> None:
        """Execute zero-loss transformations and load into target DuckDB tables.

        Must be idempotent across re-builds: recreate this source's own bronze and
        silver tables with ``CREATE OR REPLACE TABLE`` (or ``DROP``+``CREATE``),
        never a bare ``CREATE TABLE`` (errors on the second build) nor
        ``CREATE TABLE IF NOT EXISTS`` + ``INSERT`` (would double the rows). The
        engine clears the SHARED audit tables for this source before this runs
        (see ``crossroads.quality.reset_source_audit``); recreating bronze/silver
        is the transformer's half of keeping a re-build idempotent.
        """
        raise NotImplementedError
