# Stage 03 — Orchestrator & test harness

## Objective

Create the orchestrator: `init_engine(...)` returns a `Client`, and `client.build(**kwargs)` opens a DuckDB connection and runs the registry's `extract → transform_and_load` loop over active transformers. With zero transformers this is a clean no-op build that still leaves a queryable connection. Wire the public API into the package and add the shared DuckDB test fixture.

## Implementation Steps

### 1. Create `src/crossroads/client.py`

Exact contents (the loop body mirrors `docs/spec.md` §4):

```python
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
```

Design notes:
- `build` leaves the connection **open** after returning so a researcher can query the result immediately; `close()` is provided for explicit teardown and is used by tests.
- `database_path` defaults to `":memory:"` so `build()` works with no arguments.
- The loop forwards `**kwargs` exactly as the spec's orchestration snippet does.

### 2. Update `src/crossroads/__init__.py` to export the public API

Replace its contents with **exactly**:

```python
"""Crossroads-UK: reproducible UK road-safety / weather / boundary data pipeline."""

from crossroads.client import Client, init_engine

__all__ = ["Client", "init_engine"]

__version__ = "0.0.1"
```

### 3. Create the shared DuckDB fixture `tests/conftest.py`

Exact contents:

```python
import duckdb
import pytest


@pytest.fixture
def con():
    """A fresh in-memory DuckDB connection, closed after the test."""
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()
```

### 4. Create the orchestrator smoke test `tests/test_client.py`

Exact contents:

```python
import crossroads
from crossroads.client import Client


def test_init_engine_returns_client():
    client = crossroads.init_engine()
    assert isinstance(client, Client)
    assert client.database_path == ":memory:"
    assert client.con is None  # not opened until build()


def test_empty_build_is_noop_in_memory():
    client = crossroads.init_engine()
    result = client.build(years=[2023], include_weather=True, spatial_grain="local_authority")
    # build returns self and leaves an open, queryable connection
    assert result is client
    assert client.con is not None
    assert client.con.execute("SELECT 42").fetchone()[0] == 42
    client.close()
    assert client.con is None


def test_build_against_on_disk_database(tmp_path):
    db_path = tmp_path / "local_analytics.db"
    client = crossroads.init_engine(database_path=str(db_path))
    client.build()
    assert client.con.execute("SELECT 1").fetchone()[0] == 1
    client.close()
    # the database file was created on disk
    assert db_path.exists()


def test_shared_con_fixture_works(con):
    assert con.execute("SELECT 7 * 6").fetchone()[0] == 42
```

## Testing & Verification

**Integration test (PRIMARY) — the §8 target-flow shape runs end-to-end.** This is the cumulative proof of the whole foundation: construct the engine and run an empty `build()` against both in-memory and on-disk DuckDB, asserting the connection is open and queryable and (for on-disk) the file is created. With the venv active, from the repo root:

```bash
python -m pytest -q tests/test_client.py
```
Expected: `4 passed`.

**Full suite (the foundation is green end-to-end):**
```bash
python -m pytest -q
```
Expected: `10 passed` (2 from Stage 01, 4 from Stage 02, 4 here).

**Spec §8 flow, run for real from a script (optional but recommended):**
```bash
python - <<'PY'
import crossroads as cr
client = cr.init_engine(database_path=":memory:")
client.build(years=[2022, 2023, 2024], include_weather=True, spatial_grain="local_authority")
print("rows queryable:", client.con.execute("SELECT 1").fetchone()[0])
client.close()
print("OK: empty no-op build completed")
PY
```
Expected: prints `rows queryable: 1` then `OK: empty no-op build completed`, with no exceptions.

## Known Pitfalls

- **`import crossroads` raises `ImportError` after the `__init__.py` edit.** `client.py` is missing or has a syntax error, or the import path is wrong. Confirm `src/crossroads/client.py` exists and imports `Registry` from `crossroads.registry`.
- **`build()` raises because `get_active` got unexpected kwargs.** `Registry.get_active(**kwargs)` and `BaseTransformer.is_active(**kwargs)` must both accept arbitrary `**kwargs`. Verify Stage 02's signatures weren't narrowed.
- **On-disk DB test fails: file not created.** DuckDB creates the file on `connect`; ensure `database_path` is passed through `init_engine → Client → duckdb.connect`. A `:memory:` value here means the path didn't propagate.
- **`con` fixture test errors with "fixture 'con' not found".** `tests/conftest.py` is missing or in the wrong directory — it must sit directly in `tests/`.
