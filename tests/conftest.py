import duckdb
import pytest


@pytest.fixture
def con():
    """A fresh in-memory DuckDB connection, closed after the test."""
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()
