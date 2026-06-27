"""Tests for the ONS boundary transformer (spatial.py).

Offline tests: the cache is pre-seeded with committed fixture GeoJSON files
so extract() finds the source files and performs no network download.
"""

import os
import shutil

import pytest
import crossroads
from crossroads.quality import ensure_quality_tables
from crossroads.transformers.spatial import (
    LADBoundaryTransformer,
    CTYUABoundaryTransformer,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ons")


def _seed_cache(cache_dir):
    """Copy committed GeoJSON fixture files into the build cache.

    extract() checks for the source_file in the cache and skips the download
    when it is present, so seeding here makes all boundary tests fully offline.
    """
    os.makedirs(cache_dir, exist_ok=True)
    for sub, stem in (("lad_2024", "lad_sample"), ("ctyua_2024", "ctyua_sample")):
        src = os.path.join(FIXTURES, sub)
        shutil.copy(os.path.join(src, stem + ".geojson"), cache_dir)


def _boundary_client(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_cache(cache)
    client = crossroads.init_engine(cache_dir=cache)  # in-memory DB, seeded cache
    client.registry._transformers = [
        CTYUABoundaryTransformer(),
        LADBoundaryTransformer(),
    ]
    return client


# ---------------------------------------------------------------------------
# End-to-end build tests
# ---------------------------------------------------------------------------

def test_build_ingests_boundaries_end_to_end(tmp_path):
    client = _boundary_client(tmp_path)
    client.build()  # real extract (offline) + transform_and_load + invariants

    # Silver tables populated (keep-in-place 1:1 with bronze).
    lad = client.con.execute("SELECT count(*) FROM lad_boundaries").fetchone()[0]
    ctyua = client.con.execute("SELECT count(*) FROM ctyua_boundaries").fetchone()[0]
    assert lad == 3 and ctyua == 2   # matches the committed sample sizes

    # Bronze matches silver 1:1.
    assert client.con.execute("SELECT count(*) FROM ons_lad_raw").fetchone()[0] == lad

    # Gold views exist and (clean sample) equal silver.
    assert client.con.execute(
        "SELECT count(*) FROM lad_boundaries_clean"
    ).fetchone()[0] == lad

    client.close()


def test_boundary_geometry_is_epsg_27700(tmp_path):
    client = _boundary_client(tmp_path)
    client.build()

    # Every geometry must sit inside the British National Grid envelope.
    row = client.con.execute(
        "SELECT min(ST_XMin(geom)), max(ST_XMax(geom)), "
        "       min(ST_YMin(geom)), max(ST_YMax(geom)) FROM lad_boundaries"
    ).fetchone()
    assert 0 <= row[0] and row[1] <= 700_000    # easting band
    assert 0 <= row[2] and row[3] <= 1_300_000  # northing band

    client.close()


def test_boundary_build_passes_step2_invariants(tmp_path):
    # The real quality engine runs at build end; a clean sample must pass all
    # three invariants (conservation, flag/ledger agreement, reject-rate).
    client = _boundary_client(tmp_path)
    client.build()  # raises QualityInvariantError if any invariant failed

    # Sanity: source row count recorded for each source.
    n = client.con.execute(
        "SELECT count(*) FROM source_ingest_log WHERE source_id = 'ons_lad'"
    ).fetchone()[0]
    assert n == 1

    client.close()


# ---------------------------------------------------------------------------
# Quality spec shape test
# ---------------------------------------------------------------------------

def test_quality_spec_shape():
    spec = LADBoundaryTransformer().quality_spec()
    assert spec.source_id == "ons_lad"
    assert spec.silver_table == "lad_boundaries"
    assert spec.dimensions[0].flag_column == "geom_valid"


# ---------------------------------------------------------------------------
# FALSE-branch: invalid geometry is flagged and logged
# ---------------------------------------------------------------------------

def test_invalid_geometry_is_flagged_and_logged(con):
    # Exercise the geom_valid=FALSE path by driving the silver/ledger derivation
    # against a hand-built bronze that contains one NULL-geometry feature.
    # This proves flag/ledger agreement on the failure path without needing an
    # invalid fixture file.
    con.execute("LOAD spatial")
    ensure_quality_tables(con)

    t = LADBoundaryTransformer()
    vintages = t._vintages_for()

    # Create a synthetic bronze: one valid polygon, one NULL geometry.
    vf_case, vt_case = t._validity_case_sql(vintages)
    con.execute(
        f"CREATE TABLE {t.bronze_table} ("
        f"  vintage VARCHAR, area_code VARCHAR, area_name VARCHAR, geom GEOMETRY"
        f")"
    )
    con.execute(
        f"INSERT INTO {t.bronze_table} VALUES "
        f"('2024', 'E99000001', 'Valid Area', "
        f"  ST_GeomFromText('POLYGON((0 0,100 0,100 100,0 100,0 0))')), "
        f"('2024', 'E99000002', 'Null Geom Area', NULL)"
    )

    # Drive silver/ledger derivation directly (no bronze→file→extract roundtrip).
    t._derive_silver_and_ledger(con, vintages)

    # Silver must have 1 valid and 1 invalid row.
    valid_n = con.execute(
        f"SELECT count(*) FROM {t.silver_table} WHERE geom_valid = TRUE"
    ).fetchone()[0]
    invalid_n = con.execute(
        f"SELECT count(*) FROM {t.silver_table} WHERE geom_valid = FALSE"
    ).fetchone()[0]
    assert valid_n == 1 and invalid_n == 1

    # The ledger must have exactly one reject_dimension entry for the bad row.
    ledger = con.execute(
        "SELECT source_row_key, rule_id, severity "
        "FROM data_quality_log WHERE source_id = ?",
        ["ons_lad"],
    ).fetchall()
    assert len(ledger) == 1
    key, rule, sev = ledger[0]
    assert key == "E99000002|2024"
    assert rule == LADBoundaryTransformer.GEOM_RULE
    assert sev == "reject_dimension"


# ---------------------------------------------------------------------------
# Existing Stage 01 smoke tests (preserved)
# ---------------------------------------------------------------------------

def test_build_loads_spatial_extension():
    client = crossroads.init_engine()
    client.registry._transformers = []  # test spatial extension only, not boundary ingest
    client.build()
    assert client.con.execute(
        "SELECT ST_AsText(ST_Point(1, 2))"
    ).fetchone()[0] == "POINT (1 2)"
    lon_lat = client.con.execute(
        "SELECT ST_X(g), ST_Y(g) FROM ("
        "  SELECT ST_Transform(ST_Point(530000, 180000), 'EPSG:27700', 'EPSG:4326') AS g"
        ")"
    ).fetchone()
    assert 50.0 < lon_lat[0] < 53.0
    client.close()


def test_existing_empty_build_still_succeeds():
    # Zero-transformer build: loading spatial must not break it.
    client = crossroads.init_engine()
    client.registry._transformers = []  # bypass auto-discovery of spatial transformers
    client.build()
    assert client.con.execute(
        "SELECT count(*) FROM data_quality_log"
    ).fetchone()[0] == 0
    client.close()
