"""The pipeline orchestrator and database controller."""

import duckdb

from crossroads import quality
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

        ``**kwargs`` (e.g. ``datasets=["stats19", "era5_weather"]``, ``years=[...]``,
        ``boundary_mode="snapshot"``) are forwarded to each transformer's
        ``is_active`` and ``extract`` methods. An optional ``reject_ceiling``
        kwarg overrides the global default reject-rate ceiling. Returns ``self``.

        At build end the shared data-quality invariants run (spec §9); any
        violation raises and halts the build.
        """
        self.con = duckdb.connect(self.database_path)
        # Load the DuckDB Spatial Extension once, as foundational infrastructure
        # (spec §5 Phase 1). It is generic (names no data source, so provider-plugin
        # purity holds), idempotent, and cheap. INSTALL needs the network only on the
        # first run on a machine; thereafter the extension is cached locally. Every
        # spatial source (boundaries now, weather later) relies on this being loaded.
        self.con.execute("INSTALL spatial")
        self.con.execute("LOAD spatial")
        # Create the shared audit tables up-front so transformers can write to them.
        quality.ensure_quality_tables(self.con)

        active = self.registry.get_active(**kwargs)
        for transformer in active:
            # Clear this source's rows from the shared audit tables before it is
            # (re)built, so a re-build against an existing on-disk database stays
            # idempotent (log_exclusion / quarantine_row are plain appends). The
            # transformer is responsible for recreating its own bronze/silver.
            # A transformer may write audit rows under several source_ids (e.g.
            # STATS19's collision/vehicle/casualty) — reset each one.
            for source_id in quality.declared_source_ids(transformer):
                quality.reset_source_audit(self.con, source_id)
            transformer.extract(self.cache_dir, **kwargs)
            transformer.transform_and_load(self.con, self.cache_dir)

        # Coverage gate: resolve each active source's quality_spec() decision
        # (audit / explicit exemption / undecided), then run the build-end
        # invariants (conservation, flag/ledger agreement, reject-rate tripwire).
        # Both the gate and the invariants are fatal on violation.
        specs = quality.resolve_quality_specs(self.con, active)
        default_ceiling = kwargs.get("reject_ceiling") or quality.DEFAULT_REJECT_CEILING
        quality.run_invariants(self.con, specs, default_ceiling=default_ceiling)
        # Stamp build provenance LAST, so only a database that passed the invariants is recorded.
        quality.write_build_metadata(self.con, parameters=kwargs)
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
