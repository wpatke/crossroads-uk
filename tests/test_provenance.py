"""crossroads_meta provenance stamp — fast, offline, real DuckDB (no fixtures needed)."""
import json

import duckdb

import crossroads
from crossroads import quality


def test_write_build_metadata_single_row():
    con = duckdb.connect(":memory:")
    params = {"datasets": ["stats19"], "years": [2023], "boundary_mode": "snapshot"}
    quality.write_build_metadata(con, parameters=params)

    rows = con.execute(
        "SELECT crossroads_version, schema_version, built_at_utc, parameters FROM crossroads_meta"
    ).fetchall()
    assert len(rows) == 1
    version, schema_version, built_at, params_json = rows[0]
    assert version == crossroads.__version__      # git-derived; not pinned to a literal
    assert schema_version == crossroads.SCHEMA_VERSION
    assert built_at is not None                          # UTC stamp present
    assert json.loads(params_json)["years"] == [2023]    # parameters captured faithfully


def test_write_build_metadata_is_idempotent():
    # A re-build must not accumulate rows — CREATE OR REPLACE keeps exactly one.
    con = duckdb.connect(":memory:")
    quality.write_build_metadata(con, parameters={"datasets": ["stats19"]})
    quality.write_build_metadata(con, parameters={"datasets": ["stats19", "era5_weather"]})
    n = con.execute("SELECT count(*) FROM crossroads_meta").fetchone()[0]
    assert n == 1
    latest = con.execute("SELECT parameters FROM crossroads_meta").fetchone()[0]
    assert "era5_weather" in latest                       # reflects the LATEST build
