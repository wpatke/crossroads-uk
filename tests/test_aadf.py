"""Tests for the AADF traffic-counts transformer (aadf.py).

Fast, offline, deterministic. Unit tests drive the silver derivation, the extract
cache/zip paths, and the boundary-mode stamp against hand-built inputs; the integration
test builds the real fixture end-to-end (opt-in via the `integration` marker)."""

import os
import shutil
import zipfile

import pytest

import crossroads
from crossroads.quality import ensure_quality_tables
from crossroads.transformers.aadf import (
    AadfTransformer, CSV_CACHE_FILE, ZIP_CACHE_FILE, COORD_RULE, COUNT_RULE,
)
# Reuse the console-test cache seeder (STATS19 + ONS + bank-holidays + AADF fixtures).
from tests.test_console import _seed_full_cache

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "aadf")
SAMPLE_CSV = os.path.join(FIXTURES, "dft_traffic_counts_aadf_sample.csv")
FIXTURE_ROWS = 14   # documented in tests/fixtures/aadf/README.md


# --- identity ---------------------------------------------------------------

def test_aadf_is_user_selectable_and_orders_after_boundaries():
    t = AadfTransformer()
    assert t.user_selectable is True
    assert t.display_name == "traffic counts (AADF)"
    # Must run after the boundary sources it stamps against.
    assert t.depends_on == ("ons_lad", "ons_ctyua")


def test_aadf_inactive_without_years():
    # A boundary-only build (no years) must not activate aadf, so it never downloads.
    t = AadfTransformer()
    assert t.is_active() is False
    assert t.is_active(years=[2023]) is True


# --- silver typing & flags (unit, synthetic bronze) -------------------------

# The columns the silver SELECT reads. A synthetic bronze needs exactly these (all text,
# as read_csv(all_varchar=true) would produce).
_BRONZE_COLS = [
    "count_point_id", "year", "region_name", "local_authority_name", "road_name",
    "road_type", "start_junction_road_name", "end_junction_road_name", "easting",
    "northing", "link_length_km", "estimation_method", "estimation_method_detailed",
    "all_motor_vehicles", "pedal_cycles", "two_wheeled_motor_vehicles", "cars_and_taxis",
    "buses_and_coaches", "lgvs", "all_hgvs",
]


def _make_bronze(con, rows):
    """Create a synthetic aadf_raw (all VARCHAR) from a list of dicts, so _derive_silver can
    run without a real CSV. Missing keys default to a harmless '0'/'x' placeholder."""
    cols_sql = ", ".join(f"{c} VARCHAR" for c in _BRONZE_COLS)
    con.execute(f"CREATE TABLE aadf_raw ({cols_sql})")
    placeholders = ", ".join("?" for _ in _BRONZE_COLS)
    values = [[r.get(c, "") for c in _BRONZE_COLS] for r in rows]
    con.executemany(f"INSERT INTO aadf_raw VALUES ({placeholders})", values)


def test_silver_typing_flags_and_ledger(con):
    con.execute("INSTALL spatial; LOAD spatial")
    ensure_quality_tables(con)
    # Three rows: one fully valid, one with a blank easting, one with a bad count.
    _make_bronze(con, [
        {"count_point_id": "100", "year": "2023", "road_name": "A179", "road_type": "Major",
         "easting": "447619", "northing": "534978", "link_length_km": "4.2",
         "estimation_method": "Counted", "all_motor_vehicles": "15202", "lgvs": "2264",
         "all_hgvs": "548"},
        {"count_point_id": "200", "year": "2023", "road_name": "A689",
         "easting": "", "northing": "533000", "all_motor_vehicles": "500"},
        {"count_point_id": "300", "year": "2023", "road_name": "A689",
         "easting": "450000", "northing": "533000", "all_motor_vehicles": "x"},
    ])
    t = AadfTransformer()
    t._derive_silver(con)
    t._log_rejections(con)

    # Keep-in-place: silver is 1:1 with bronze.
    assert con.execute("SELECT count(*) FROM aadf").fetchone()[0] == 3

    # The good row: typed values, geometry present, both flags TRUE.
    good = con.execute(
        "SELECT count_point_id, year, easting, all_motor_vehicles, geom_valid, count_valid, "
        "       geom IS NOT NULL, lad_code FROM aadf WHERE source_row_key = '100|2023'"
    ).fetchone()
    assert good == (100, 2023, 447619, 15202, True, True, True, None)

    # Blank easting -> geom NULL, geom_valid FALSE, raw twin preserved; count still valid.
    bad_geom = con.execute(
        "SELECT easting_raw, easting, geom IS NULL, geom_valid, count_valid "
        "FROM aadf WHERE source_row_key = '200|2023'"
    ).fetchone()
    assert bad_geom == ("", None, True, False, True)

    # Non-numeric count -> all_motor_vehicles NULL, count_valid FALSE, raw twin preserved.
    bad_count = con.execute(
        "SELECT all_motor_vehicles_raw, all_motor_vehicles, count_valid, geom_valid "
        "FROM aadf WHERE source_row_key = '300|2023'"
    ).fetchone()
    assert bad_count == ("x", None, False, True)

    # Ledger: exactly one COORD_RULE row for the blank-easting key and one COUNT_RULE row
    # for the bad-count key (flag/ledger agreement on the failure paths).
    ledger = con.execute(
        "SELECT source_row_key, rule_id FROM data_quality_log "
        "WHERE source_id = 'aadf' ORDER BY rule_id"
    ).fetchall()
    assert ledger == [("200|2023", COORD_RULE), ("300|2023", COUNT_RULE)]


# --- extract paths (unit, no network) ---------------------------------------

def test_extract_skips_when_csv_cached(tmp_path):
    # A seeded CSV means extract() returns without touching the network or the file.
    cache = str(tmp_path / "cache")
    os.makedirs(cache)
    dest = os.path.join(cache, CSV_CACHE_FILE)
    shutil.copy(SAMPLE_CSV, dest)
    before = os.path.getmtime(dest), os.path.getsize(dest)
    AadfTransformer().extract(cache)                 # must not raise, must not re-download
    after = os.path.getmtime(dest), os.path.getsize(dest)
    assert before == after                           # file untouched


def test_extract_unzips_single_csv_member(tmp_path):
    # A cached zip with one CSV member (arbitrary internal name) is unzipped to the
    # canonical cache filename, offline.
    cache = str(tmp_path / "cache")
    os.makedirs(cache)
    zip_path = os.path.join(cache, ZIP_CACHE_FILE)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("some_internal_name.csv", "count_point_id,year\n1,2023\n")
    AadfTransformer().extract(cache)
    out = os.path.join(cache, CSV_CACHE_FILE)
    assert os.path.exists(out)
    with open(out) as fh:
        assert fh.read() == "count_point_id,year\n1,2023\n"


def test_extract_rejects_multi_csv_zip(tmp_path):
    # A zip with more than one CSV member is ambiguous -> fail loudly.
    cache = str(tmp_path / "cache")
    os.makedirs(cache)
    zip_path = os.path.join(cache, ZIP_CACHE_FILE)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("a.csv", "x\n")
        zf.writestr("b.csv", "y\n")
    with pytest.raises(ValueError, match="exactly one .csv"):
        AadfTransformer().extract(cache)


# --- boundary-mode stamping (unit, synthetic two-vintage boundary table) -----

def _two_vintage_lad(con):
    """A lad_boundaries table with two vintages covering the same point but carrying
    different codes and non-overlapping [valid_from, valid_to) windows."""
    con.execute(
        "CREATE TABLE lad_boundaries "
        "(area_code VARCHAR, geom GEOMETRY, geom_valid BOOLEAN, "
        " valid_from DATE, valid_to DATE)")
    poly = "ST_GeomFromText('POLYGON((0 0, 0 2000, 2000 2000, 2000 0, 0 0))')"
    con.execute(
        f"INSERT INTO lad_boundaries VALUES "
        f"('OLD01', {poly}, TRUE, DATE '2010-04-01', DATE '2020-04-01'), "
        f"('NEW01', {poly}, TRUE, DATE '2020-04-01', NULL)")


def _two_year_aadf(con):
    """A hand-built aadf silver with two rows at the same point, years 2015 and 2023."""
    con.execute(
        "CREATE OR REPLACE TABLE aadf "
        "(source_row_key VARCHAR, year INTEGER, geom GEOMETRY, "
        " lad_code VARCHAR, ctyua_code VARCHAR)")
    con.execute(
        "INSERT INTO aadf VALUES "
        "('P|2015', 2015, ST_Point(1000, 1000), NULL, NULL), "
        "('P|2023', 2023, ST_Point(1000, 1000), NULL, NULL)")


def test_boundary_mode_stamping_temporal_and_snapshot(con):
    con.execute("INSTALL spatial; LOAD spatial")
    _two_vintage_lad(con)
    _two_year_aadf(con)
    t = AadfTransformer()

    # Temporal: each row resolves to the vintage in force on 1 July of its year.
    t._boundary_mode = "temporal"
    t._stamp_area_codes(con)
    got = dict(con.execute(
        "SELECT source_row_key, lad_code FROM aadf ORDER BY source_row_key").fetchall())
    assert got == {"P|2015": "OLD01",   # 1 Jul 2015 in [2010-04-01, 2020-04-01)
                   "P|2023": "NEW01"}   # 1 Jul 2023 >= 2020-04-01 (open window)

    # Snapshot: both rows take the latest vintage (valid_to IS NULL), regardless of year.
    con.execute("UPDATE aadf SET lad_code = NULL")
    t._boundary_mode = "snapshot"
    t._stamp_area_codes(con)
    got = dict(con.execute(
        "SELECT source_row_key, lad_code FROM aadf ORDER BY source_row_key").fetchall())
    assert got == {"P|2015": "NEW01", "P|2023": "NEW01"}


def test_stamp_warns_when_boundary_table_absent(con):
    # No boundary tables built: the stamp warns and leaves codes NULL (pipeline survives).
    con.execute("INSTALL spatial; LOAD spatial")
    _two_year_aadf(con)
    t = AadfTransformer()
    with pytest.warns(UserWarning, match="boundary table lad_boundaries not found"):
        t._stamp_area_codes(con)
    assert con.execute(
        "SELECT count(*) FROM aadf WHERE lad_code IS NOT NULL").fetchone()[0] == 0


# --- full offline build (integration) ---------------------------------------

@pytest.mark.integration
def test_full_offline_build(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_full_cache(cache)
    db = str(tmp_path / "aadf.duckdb")
    client = crossroads.init_engine(database_path=db, cache_dir=cache)
    # Real registry: aadf is selected; the always-on boundary sources run too (aadf stamps
    # against them). Boundaries load from the seeded ONS fixtures; no network.
    client.build(datasets=["aadf"], years=[2023])   # raises if any §9 invariant fails
    try:
        con = client.con
        # Full history landed (the fixture spans 2022-2023), keep-in-place 1:1 with bronze.
        silver = con.execute("SELECT count(*) FROM aadf").fetchone()[0]
        bronze = con.execute("SELECT count(*) FROM aadf_raw").fetchone()[0]
        assert silver == bronze == FIXTURE_ROWS
        # Every fixture point has valid geometry and stamps to Hartlepool.
        assert con.execute("SELECT bool_and(geom_valid) FROM aadf").fetchone()[0] is True
        assert con.execute(
            "SELECT count(*) FROM aadf WHERE lad_code = 'E06000001'").fetchone()[0] == silver
        # Gold view exists and matches the silver count (all fixture rows are clean).
        assert con.execute("SELECT count(*) FROM aadf_clean").fetchone()[0] == silver
        # The R-Tree index exists on the silver geometry.
        names = {r[0] for r in con.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'aadf'").fetchall()}
        assert "aadf_geom_rtree" in names
    finally:
        client.close()
