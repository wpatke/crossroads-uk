import os
import sys

import duckdb
import pytest

# tests/ has no __init__.py, so pytest only puts tests/ itself on sys.path. Add the
# repo root too, so cross-module helper imports like `from tests.test_console import ...`
# resolve (tests/ then acts as an implicit namespace package).
_REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def con():
    """A fresh in-memory DuckDB connection, closed after the test."""
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()
