"""Tests for the ONS boundary transformer (spatial.py).

Offline tests: the cache is pre-seeded with committed fixture GeoJSON files
so extract() finds the source files and performs no network download.
"""

import json
import os
import shutil

import duckdb
import pytest
import crossroads
from crossroads.quality import ensure_quality_tables
from crossroads.transformers.spatial import (
    LADBoundaryTransformer,
    CTYUABoundaryTransformer,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ons")


def _seed_cache(cache_dir):
    """Copy each committed GeoJSON fixture into the build cache under the name the
    newest vintage expects, so the snapshot build runs fully offline regardless of
    which edition is currently newest in the manifest.

    Each fixture directory is named lad_<year> / ctyua_<year> matching the newest
    vintage's year; the file has the correct column names for that year.
    """
    os.makedirs(cache_dir, exist_ok=True)
    seeds = (
        ("lad", LADBoundaryTransformer),
        ("ctyua", CTYUABoundaryTransformer),
    )
    for prefix, cls in seeds:
        newest = cls().vintages[-1]
        year = newest.valid_from[:4]  # e.g. "2025" from "2025-12-01"
        src = os.path.join(FIXTURES, f"{prefix}_{year}", f"{prefix}_sample.geojson")
        shutil.copy(src, os.path.join(cache_dir, newest.source_file))


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
    label = vintages[-1].label  # newest vintage label, e.g. "2025-12"

    # Create a synthetic bronze: one valid polygon, one NULL geometry.
    vf_case, vt_case = t._validity_case_sql(vintages)
    con.execute(
        f"CREATE TABLE {t.bronze_table} ("
        f"  vintage VARCHAR, area_code VARCHAR, area_name VARCHAR, geom GEOMETRY"
        f")"
    )
    con.execute(
        f"INSERT INTO {t.bronze_table} VALUES "
        f"(?, 'E99000001', 'Valid Area', "
        f"  ST_GeomFromText('POLYGON((0 0,100 0,100 100,0 100,0 0))')), "
        f"(?, 'E99000002', 'Null Geom Area', NULL)",
        [label, label],
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
    assert key == "E99000002|" + label
    assert rule == LADBoundaryTransformer.GEOM_RULE
    assert sev == "reject_dimension"


# ---------------------------------------------------------------------------
# Existing Stage 01 smoke tests (preserved)
# ---------------------------------------------------------------------------

def test_vintages_loaded_from_manifest():
    # The registry now comes from ons_boundaries.json rather than hard-coded constants.
    vintages = LADBoundaryTransformer().vintages
    assert len(vintages) >= 1
    latest = vintages[-1]
    assert latest.url.endswith("f=geojson")
    assert "outSR=27700" in latest.url
    assert latest.code_col in latest.url
    assert latest.valid_to is None


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


def test_full_registry_loaded():
    lad = LADBoundaryTransformer().vintages
    ctyua = CTYUABoundaryTransformer().vintages
    assert len(lad) == 15
    assert len(ctyua) == 11
    # Newest edition is December 2025 for both types.
    assert lad[-1].label == "2025-12" and lad[-1].valid_to is None
    assert ctyua[-1].label == "2025-12" and ctyua[-1].valid_to is None


def test_validity_windows_chain_by_date():
    lad = LADBoundaryTransformer().vintages
    # Sorted ascending by valid_from; each valid_to equals the next valid_from.
    for earlier, later in zip(lad, lad[1:]):
        assert earlier.valid_from < later.valid_from
        assert earlier.valid_to == later.valid_from
    # Spot-check: Dec 2016 valid_to should be Dec 2018 (2017 gap absorbed).
    by_label = {v.label: v for v in lad}
    assert by_label["2016-12"].valid_to == "2018-12-01"


def test_field_name_casing_preserved():
    by_label = {v.label: v for v in LADBoundaryTransformer().vintages}
    assert by_label["2019-12"].code_col == "lad19cd"   # older editions lowercase
    assert by_label["2024-12"].code_col == "LAD24CD"   # newer editions uppercase


def test_existing_empty_build_still_succeeds():
    # Zero-transformer build: loading spatial must not break it.
    client = crossroads.init_engine()
    client.registry._transformers = []  # bypass auto-discovery of spatial transformers
    client.build()
    assert client.con.execute(
        "SELECT count(*) FROM data_quality_log"
    ).fetchone()[0] == 0
    client.close()


# ---------------------------------------------------------------------------
# Stage 03 — Temporal boundary slicing
# ---------------------------------------------------------------------------

def _two_vintage_lad():
    """A LAD transformer restricted to the two editions that have committed
    fixtures, so temporal mode can be tested fully offline. Setting .vintages on
    the instance shadows the manifest-loaded class attribute; _vintages_for reads
    self.vintages, so temporal mode then resolves exactly these two."""
    t = LADBoundaryTransformer()
    t.vintages = tuple(v for v in t.vintages if v.label in ("2024-12", "2025-12"))
    return t


def _seed_cache_temporal_lad(cache_dir, vintages):
    """Seed each given vintage's source_file from its matching committed fixture."""
    os.makedirs(cache_dir, exist_ok=True)
    fixture_for = {
        "2024-12": ("lad_2024", "lad_sample"),
        "2025-12": ("lad_2025", "lad_sample"),
    }
    for v in vintages:
        sub, stem = fixture_for[v.label]
        src = os.path.join(FIXTURES, sub, stem + ".geojson")
        shutil.copy(src, os.path.join(cache_dir, v.source_file))


def test_snapshot_mode_loads_latest_vintage_only(tmp_path):
    # Default mode: only the newest LAD vintage (2025-12) is loaded.
    client = _boundary_client(tmp_path)          # snapshot seed (newest only)
    client.build()                               # no boundary_mode -> snapshot
    vintages = [r[0] for r in client.con.execute(
        "SELECT DISTINCT vintage FROM lad_boundaries ORDER BY vintage"
    ).fetchall()]
    assert vintages == ["2025-12"]
    # The latest vintage is current (valid_to IS NULL).
    assert client.con.execute(
        "SELECT count(*) FROM lad_boundaries WHERE valid_to IS NOT NULL"
    ).fetchone()[0] == 0
    client.close()


def test_temporal_mode_loads_all_vintages_with_windows(tmp_path):
    t = _two_vintage_lad()
    cache = str(tmp_path / "cache")
    _seed_cache_temporal_lad(cache, t.vintages)
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [t]
    client.build(boundary_mode="temporal")

    vintages = [r[0] for r in client.con.execute(
        "SELECT DISTINCT vintage FROM lad_boundaries ORDER BY vintage"
    ).fetchall()]
    assert vintages == ["2024-12", "2025-12"]

    # The newest vintage is current (open window); the earlier one is closed.
    assert client.con.execute(
        "SELECT valid_to FROM lad_boundaries WHERE vintage = '2025-12' LIMIT 1"
    ).fetchone()[0] is None
    assert client.con.execute(
        "SELECT valid_to FROM lad_boundaries WHERE vintage = '2024-12' LIMIT 1"
    ).fetchone()[0] is not None

    # Composite key keeps the same area code unique across vintages.
    dupe_keys = client.con.execute(
        "SELECT count(*) - count(DISTINCT source_row_key) FROM lad_boundaries"
    ).fetchone()[0]
    assert dupe_keys == 0
    # The same area_code appears under both vintages (fixtures share codes).
    shared = client.con.execute(
        "SELECT count(*) FROM ("
        "  SELECT area_code FROM lad_boundaries GROUP BY area_code HAVING count(*) = 2"
        ")"
    ).fetchone()[0]
    assert shared == 3
    client.close()


def test_temporal_mode_passes_invariants(tmp_path):
    # Conservation/agreement/reject-rate must hold across multiple vintages.
    t = _two_vintage_lad()
    cache = str(tmp_path / "cache")
    _seed_cache_temporal_lad(cache, t.vintages)
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [t]
    client.build(boundary_mode="temporal")   # raises if any invariant fails
    # bronze == silver (keep-in-place) across both vintages.
    b = client.con.execute("SELECT count(*) FROM ons_lad_raw").fetchone()[0]
    s = client.con.execute("SELECT count(*) FROM lad_boundaries").fetchone()[0]
    assert b == s and s == 6                  # 3 (2024-12) + 3 (2025-12)
    client.close()


# --- year-scoped selection (pure logic over the real registry; no build/network) ---

def test_temporal_year_scoping_selects_overlapping_editions():
    # Request 2020-2021: only the editions whose windows overlap that span load.
    picked = {v.label for v in LADBoundaryTransformer()._vintages_for(
        boundary_mode="temporal", years=[2020, 2021])}
    assert picked == {"2019-12", "2020-12", "2021-05", "2021-12"}


def test_temporal_years_before_coverage_use_earliest_and_warn():
    # 2014-2015 precede the earliest LAD edition (2016-12): stand in + warn.
    t = LADBoundaryTransformer()
    with pytest.warns(UserWarning, match="earliest ONS boundary edition"):
        picked = [v.label for v in t._vintages_for(
            boundary_mode="temporal", years=[2014, 2015])]
    assert picked == ["2016-12"]


def test_temporal_unscoped_loads_every_edition():
    t = LADBoundaryTransformer()
    assert len(t._vintages_for(boundary_mode="temporal")) == len(t.vintages) == 15


def test_snapshot_ignores_years():
    # Default (snapshot) mode loads the latest edition regardless of years.
    picked = [v.label for v in LADBoundaryTransformer()._vintages_for(years=[2014])]
    assert picked == ["2025-12"]


# ---------------------------------------------------------------------------
# R-Tree spatial index tests
# ---------------------------------------------------------------------------

def test_rtree_index_exists_on_boundary_tables(tmp_path):
    client = _boundary_client(tmp_path)
    client.build()
    # duckdb_indexes() lists user indexes; assert an index exists on each silver table.
    idx = client.con.execute(
        "SELECT table_name, index_name FROM duckdb_indexes() "
        "WHERE table_name IN ('lad_boundaries', 'ctyua_boundaries')"
    ).fetchall()
    tables_with_index = {r[0] for r in idx}
    assert "lad_boundaries" in tables_with_index
    assert "ctyua_boundaries" in tables_with_index
    # The index name follows the deterministic convention.
    names = {r[1] for r in idx}
    assert "lad_boundaries_geom_rtree" in names
    client.close()


def test_rebuild_against_same_file_keeps_one_index(tmp_path):
    # A second build against the SAME on-disk database must not error on a
    # duplicate index and must leave exactly one index per silver table.
    db_path = str(tmp_path / "b.db")
    cache = str(tmp_path / "cache")
    _seed_cache(cache)

    def run_once():
        client = crossroads.init_engine(database_path=db_path, cache_dir=cache)
        client.registry._transformers = [
            CTYUABoundaryTransformer(), LADBoundaryTransformer(),
        ]
        client.build()
        return client

    first = run_once(); first.close()
    second = run_once()           # must not raise on the duplicate-index path
    count = second.con.execute(
        "SELECT count(*) FROM duckdb_indexes() WHERE table_name = 'lad_boundaries'"
    ).fetchone()[0]
    assert count == 1
    # Invariants still pass (index creation changes no row counts).
    assert second.con.execute("SELECT count(*) FROM lad_boundaries").fetchone()[0] == 3
    second.close()


def test_spatial_predicate_works(tmp_path):
    # Functional proof a spatial predicate runs correctly against the indexed
    # table (index present and not breaking queries; not a proof the planner
    # uses it — real perf validation lands in Step 4's point-in-polygon joins).
    client = _boundary_client(tmp_path)
    client.build()
    # A point known to fall inside one of the sample polygons should match exactly
    # that polygon via ST_Contains. (Pick a point from inside a fixture polygon's
    # extent; ST_Centroid of a row is guaranteed inside a convex-ish polygon, and is
    # a safe choice for the sample.)
    hit = client.con.execute(
        "SELECT count(*) FROM lad_boundaries "
        "WHERE ST_Contains(geom, (SELECT ST_Centroid(geom) FROM lad_boundaries LIMIT 1))"
    ).fetchone()[0]
    assert hit >= 1
    client.close()


def test_rtree_index_tolerates_null_geometry():
    # A flagged-invalid boundary carries geom = NULL (spec §9). The R-Tree build
    # must not error on it, and the NULL row must simply be absent from spatial
    # results rather than breaking the query. Guards against a DuckDB version bump
    # changing NULL handling.
    con = duckdb.connect()
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    con.execute("CREATE TABLE t (id INT, geom GEOMETRY)")
    con.execute("INSERT INTO t VALUES "
                "(1, ST_GeomFromText('POLYGON((0 0,0 10,10 10,10 0,0 0))')), "
                "(2, NULL)")                     # the flagged-invalid case
    con.execute("CREATE INDEX t_geom_rtree ON t USING RTREE (geom)")  # must not raise
    hit = con.execute(
        "SELECT id FROM t WHERE ST_Contains(geom, ST_Point(5,5))").fetchall()
    assert hit == [(1,)]                          # valid polygon matched, NULL ignored


def test_silver_geom_column_is_bare_geometry(tmp_path):
    # GUARD for the load-bearing `geom::GEOMETRY` cast in the silver projection.
    # ST_Read on the ONS GeoJSON (which declares crs EPSG:27700) yields a
    # CRS-qualified GEOMETRY('EPSG:27700') column, and DuckDB's RTREE index
    # rejects that type ("RTree indexes can only be created over GEOMETRY
    # columns"). The cast strips the CRS label to a bare GEOMETRY. If someone
    # removes the cast, the silver geom type changes and this test fails with a
    # clear message (rather than only an opaque index-build error at build time).
    client = _boundary_client(tmp_path)
    client.build()

    # DESCRIBE reports each column's declared type as a string. The bare type is
    # exactly "GEOMETRY"; a CRS-qualified column reports "GEOMETRY('EPSG:27700')".
    for table in ("lad_boundaries", "ctyua_boundaries"):
        rows = client.con.execute(f"DESCRIBE {table}").fetchall()
        geom_type = {r[0]: r[1] for r in rows}["geom"]
        assert geom_type == "GEOMETRY", (
            f"{table}.geom must be a bare GEOMETRY for the RTREE index; got "
            f"{geom_type!r}. Did the `geom::GEOMETRY` cast get removed from "
            f"_derive_silver_and_ledger in spatial.py?"
        )

    client.close()


# ---------------------------------------------------------------------------
# Integration test (opt-in) — exercises the real download against live ONS
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize(
    "transformer_cls",
    [LADBoundaryTransformer, CTYUABoundaryTransformer],
    ids=["lad", "ctyua"],
)
def test_download_latest_vintage(transformer_cls, tmp_path):
    """Download the newest vintage of each boundary source from the live ONS
    endpoint and verify the file is valid, projected GeoJSON.

    Deselected by default (see the `integration` marker in pyproject.toml).
    Run it deliberately with:  pytest -m integration

    Uses extract() with an empty cache dir so the real download path executes.
    tmp_path is cleaned up by pytest automatically after the test.
    """
    t = transformer_cls()
    cache = str(tmp_path / "cache")

    # extract() in snapshot mode downloads only the newest vintage.
    t.extract(cache)

    newest = t.vintages[-1]
    dest = os.path.join(cache, newest.source_file)
    assert os.path.exists(dest), "Downloaded file not found in cache"

    with open(dest, encoding="utf-8") as f:
        data = json.load(f)

    # Must be a GeoJSON FeatureCollection with at least one feature.
    assert data.get("type") == "FeatureCollection"
    features = data.get("features", [])
    assert len(features) > 0, "Downloaded GeoJSON has no features"

    # Confirm outSR=27700 was honoured: coordinates must be projected eastings/
    # northings (metres, in the hundreds of thousands), NOT lon/lat degrees
    # (which would be |lon|<180, |lat|<90). We test coordinate *magnitude*
    # rather than a strict Great Britain box because the UK dataset includes
    # Northern Ireland, whose EPSG:27700 eastings fall outside (west of) the GB
    # envelope. Navigate down through Polygon/MultiPolygon nesting to a point.
    coords = features[0]["geometry"]["coordinates"]
    while isinstance(coords[0][0], list):
        coords = coords[0]
    easting, northing = coords[0]
    assert 1_000 < abs(easting) < 2_000_000, (
        f"Easting {easting} does not look like EPSG:27700 metres")
    assert 1_000 < abs(northing) < 2_000_000, (
        f"Northing {northing} does not look like EPSG:27700 metres")
