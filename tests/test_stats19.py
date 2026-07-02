"""Tests for the STATS19 transformer (stats19.py).

Offline: the cache is pre-seeded with committed sample CSVs so extract() finds the
files and performs no network download.
"""
import os
import shutil

import pytest
import crossroads
from crossroads.transformers.stats19 import Stats19Transformer

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
YEARS = [2023]


def _seed_cache(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    for ftype in ("collision", "vehicle", "casualty"):
        name = f"dft-road-casualty-statistics-{ftype}-2023.csv"
        shutil.copy(os.path.join(FIXTURES, name), os.path.join(cache_dir, name))


def _stats19_client(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_cache(cache)
    client = crossroads.init_engine(cache_dir=cache)     # in-memory DB, seeded cache
    client.registry._transformers = [Stats19Transformer()]   # stats19 only this stage
    return client


def test_bronze_and_minimal_silver_end_to_end(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)     # runs invariants; raises if conservation fails

    for bronze, silver in (
        ("stats19_collision_raw", "collisions"),
        ("stats19_vehicle_raw", "vehicles"),
        ("stats19_casualty_raw", "casualties"),
    ):
        b = client.con.execute(f"SELECT count(*) FROM {bronze}").fetchone()[0]
        s = client.con.execute(f"SELECT count(*) FROM {silver}").fetchone()[0]
        assert b > 0 and b == s, f"{bronze}={b} must equal {silver}={s} (keep-in-place)"

    # Three audit units recorded a source-row count each.
    sids = {r[0] for r in client.con.execute(
        "SELECT DISTINCT source_id FROM source_ingest_log").fetchall()}
    assert {"stats19_collision", "stats19_vehicle", "stats19_casualty"} <= sids
    client.close()


def test_source_row_keys_are_unique(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    for silver in ("collisions", "vehicles", "casualties"):
        dupes = client.con.execute(
            f"SELECT count(*) - count(DISTINCT source_row_key) FROM {silver}"
        ).fetchone()[0]
        assert dupes == 0, f"{silver} has duplicate source_row_key values"
    client.close()


def test_identity_normalized_to_accident_index(tmp_path):
    # Canonical silver identity is accident_index, never NULL for the sample.
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    nulls = client.con.execute(
        "SELECT count(*) FROM collisions WHERE accident_index IS NULL"
    ).fetchone()[0]
    assert nulls == 0
    client.close()


def test_stats19_inactive_without_years():
    # No years -> nothing to ingest -> transformer is skipped.
    assert Stats19Transformer().is_active() is False
    assert Stats19Transformer().is_active(years=[2023]) is True


def test_quality_spec_declares_three_units():
    specs = Stats19Transformer().quality_spec()
    assert len(specs) == 3
    ids = {s.source_id for s in specs}
    assert ids == {"stats19_collision", "stats19_vehicle", "stats19_casualty"}


def test_collision_reference_alias_branch(con):
    # Prove the collision_* -> accident_* normalization on a synthetic modern-schema
    # bronze (no committed collision_* fixture needed). Independent reimplementation of
    # the reference package's index-normalization behaviour (inspiration only, no copy).
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute(
        "CREATE TABLE stats19_collision_raw AS "
        "SELECT * FROM (VALUES "
        "  ('2024A1','2024','ref1','530000','180000','05/01/2024','08:30')) "
        "AS t(collision_index, collision_year, collision_reference, "
        "     location_easting_osgr, location_northing_osgr, date, time)")
    t = Stats19Transformer()
    t._derive_collision_silver(con)
    row = con.execute(
        "SELECT accident_index, accident_year, source_row_key FROM collisions").fetchone()
    assert row[0] == "2024A1" and row[1] == "2024" and row[2] == "2024A1"


def test_collision_geometry_is_epsg_27700(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    # Every non-null collision point sits inside the British National Grid envelope.
    row = client.con.execute(
        "SELECT min(ST_X(geom)), max(ST_X(geom)), min(ST_Y(geom)), max(ST_Y(geom)) "
        "FROM collisions WHERE geom IS NOT NULL").fetchone()
    assert 0 <= row[0] and row[1] <= 700_000       # easting band
    assert 0 <= row[2] and row[3] <= 1_300_000     # northing band
    # The clean sample has all-valid geometry.
    assert client.con.execute(
        "SELECT count(*) FROM collisions WHERE geom_valid = FALSE").fetchone()[0] == 0
    client.close()


def test_collision_datetime_local_built(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    # datetime_local is a real TIMESTAMP and all sample rows parsed.
    bad = client.con.execute(
        "SELECT count(*) FROM collisions WHERE datetime_valid = FALSE").fetchone()[0]
    assert bad == 0
    dtype = {r[0]: r[1] for r in client.con.execute("DESCRIBE collisions").fetchall()}
    assert dtype["datetime_local"].startswith("TIMESTAMP")
    assert dtype["geom"] == "GEOMETRY"     # bare geometry (RTREE-ready in Stage 04)
    client.close()


def test_sentinel_and_bad_date_flagged_and_logged(con):
    # Drive the collision derivation against a hand-built bronze containing a valid
    # row, a sentinel-coordinate row, and a bad-date row. Proves geom/datetime flags
    # + matching ledger entries without needing a dirty fixture (keeps the sample clean
    # so the e2e reject rate stays 0). Mirrors spatial.py's invalid-geometry test.
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute(
        "CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30'), "   # valid
        "  ('c2','2023','r2','-1','-1','06/01/2023','09:00'), "           # sentinel coords
        "  ('c3','2023','r3','531000','181000','not-a-date','10:00')  "   # bad date
        ") AS t(accident_index, accident_year, accident_reference, "
        "       location_easting_osgr, location_northing_osgr, date, time)")
    t = Stats19Transformer()
    t._derive_collision_silver(con)

    rows = {r[0]: r for r in con.execute(
        "SELECT source_row_key, geom_valid, datetime_valid, geom IS NULL "
        "FROM collisions").fetchall()}
    assert rows["c1"][1] is True and rows["c1"][2] is True        # valid
    assert rows["c2"][1] is False and rows["c2"][3] is True       # geom NULL, flagged
    assert rows["c3"][2] is False                                 # bad date flagged
    # Row is retained (keep-in-place): 3 silver rows, none deleted.
    assert con.execute("SELECT count(*) FROM collisions").fetchone()[0] == 3

    # Ledger has exactly the two rejections with the right rules.
    ledger = con.execute(
        "SELECT source_row_key, rule_id FROM data_quality_log "
        "WHERE source_id = 'stats19_collision' AND severity = 'reject_dimension' "
        "ORDER BY source_row_key").fetchall()
    assert ledger == [("c2", "stats19.coord.sentinel"),
                      ("c3", "stats19.datetime.invalid")]


def test_missing_time_falls_back_to_midnight(con):
    # A blank time is NOT a rejection: datetime_local is that date at 00:00 and valid.
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute(
        "CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c9','2023','r9','530000','180000','07/01/2023','')"
        ") AS t(accident_index, accident_year, accident_reference, "
        "       location_easting_osgr, location_northing_osgr, date, time)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    row = con.execute(
        "SELECT datetime_valid, CAST(datetime_local AS VARCHAR) FROM collisions").fetchone()
    assert row[0] is True and row[1].startswith("2023-01-07 00:00:00")


@pytest.mark.integration
def test_download_real_dft_sample(tmp_path):
    t = Stats19Transformer()
    cache = str(tmp_path / "cache")
    t.extract(cache, years=[2023])
    assert os.path.exists(os.path.join(
        cache, "dft-road-casualty-statistics-collision-2023.csv"))
