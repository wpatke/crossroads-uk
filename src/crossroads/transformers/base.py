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
