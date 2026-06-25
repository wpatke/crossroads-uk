"""The pipeline orchestrator and database controller."""

import duckdb

from crossroads.registry import Registry


class Client:
    """Owns the DuckDB connection and drives the transformer registry.

    With no transformers registered, ``build`` is a clean no-op: it opens a usable
    connection, runs an empty loop, and returns. Real data sources are added by
    dropping modules into ``crossroads.transformers`` (no change to this class).
    """

    def __init__(self, database_path: str = ":memory:", cache_dir: str = ".crossroads_cache"):
        self.database_path = database_path
        self.cache_dir = cache_dir
        self.registry = Registry()
        self.con = None

    def build(self, **kwargs) -> "Client":
        """Open the database and run the extract/transform_and_load loop.

        ``**kwargs`` (e.g. ``years=[...]``, ``include_weather=True``,
        ``spatial_grain="local_authority"``) are forwarded to each transformer's
        ``is_active`` and ``extract`` methods. Returns ``self`` for chaining.
        """
        self.con = duckdb.connect(self.database_path)
        for transformer in self.registry.get_active(**kwargs):
            transformer.extract(self.cache_dir, **kwargs)
            transformer.transform_and_load(self.con, self.cache_dir)
        return self

    def close(self) -> None:
        """Close the DuckDB connection if open."""
        if self.con is not None:
            self.con.close()
            self.con = None


def init_engine(database_path: str = ":memory:", cache_dir: str = ".crossroads_cache") -> Client:
    """Initialize a local Crossroads engine instance.

    Mirrors the spec §8 target flow: ``client = cr.init_engine(database_path="local.db")``.
    """
    return Client(database_path=database_path, cache_dir=cache_dir)
