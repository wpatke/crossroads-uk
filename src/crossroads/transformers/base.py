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
