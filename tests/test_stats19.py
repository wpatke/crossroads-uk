"""Tests for the STATS19 transformer (stats19.py).

Offline: the cache is pre-seeded with committed sample CSVs so extract() finds the
files and performs no network download.
"""
import os
import shutil

import pytest
import crossroads
from crossroads.transformers.stats19 import Stats19Transformer
from crossroads.transformers.spatial import LADBoundaryTransformer, CTYUABoundaryTransformer

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
ONS_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ons")
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


def test_vehicles_and_casualties_link_to_collisions(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    # Every sample child row links to a collision (fixtures preserve integrity).
    for silver in ("vehicles", "casualties"):
        bad = client.con.execute(
            f"SELECT count(*) FROM {silver} WHERE link_valid = FALSE").fetchone()[0]
        assert bad == 0, f"{silver} has unexpected orphan rows"
    # Gold views exist and equal their silver (all-linked sample).
    for silver, view in (("vehicles", "vehicles_clean"), ("casualties", "casualties_clean")):
        s = client.con.execute(f"SELECT count(*) FROM {silver}").fetchone()[0]
        v = client.con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        assert s == v and s > 0
    client.close()


def test_orphan_vehicle_is_flagged_and_logged(con):
    # collisions has c1 only; a vehicle referencing c1 links, one referencing cX is an orphan.
    from crossroads.quality import ensure_quality_tables
    ensure_quality_tables(con)
    con.execute("CREATE TABLE collisions AS SELECT * FROM (VALUES ('c1')) AS t(accident_index)")
    con.execute(
        "CREATE TABLE stats19_vehicle_raw AS SELECT * FROM (VALUES "
        "  ('c1','1'), ('cX','1')"
        ") AS t(accident_index, vehicle_reference)")
    t = Stats19Transformer(); t._derive_vehicle_silver(con)

    rows = {r[0]: r[1] for r in con.execute(
        "SELECT accident_index, link_valid FROM vehicles").fetchall()}
    assert rows["c1"] is True and rows["cX"] is False
    assert con.execute("SELECT count(*) FROM vehicles").fetchone()[0] == 2   # retained
    ledger = con.execute(
        "SELECT source_row_key, rule_id FROM data_quality_log "
        "WHERE source_id = 'stats19_vehicle' AND severity = 'reject_dimension'").fetchall()
    assert ledger == [("cX|1", "stats19.link.orphan_vehicle")]


@pytest.mark.integration
def test_download_real_dft_sample(tmp_path):
    t = Stats19Transformer()
    cache = str(tmp_path / "cache")
    t.extract(cache, years=[2023])
    assert os.path.exists(os.path.join(
        cache, "dft-road-casualty-statistics-collision-2023.csv"))


# --- Stage 04: spatial join -------------------------------------------------

def _stub_boundaries(con):
    # Two boundary silver stubs with one polygon each, current vintage (valid_to NULL).
    for tbl, code in (("lad_boundaries", "E-LAD"), ("ctyua_boundaries", "E-CTY")):
        con.execute(
            f"CREATE TABLE {tbl} AS SELECT * FROM (VALUES "
            f"  ('{code}','Area', "
            f"   ST_GeomFromText('POLYGON((0 0,0 100,100 100,100 0,0 0))'), TRUE, "
            f"   DATE '2020-01-01', CAST(NULL AS DATE))"
            f") AS t(area_code, area_name, geom, geom_valid, valid_from, valid_to)")


def _stub_collisions(con, rows):
    # rows: list of (key, easting, northing, iso_datetime)
    values = ", ".join(
        f"('{k}', ST_Point({e},{n})::GEOMETRY, TRUE, TIMESTAMP '{dt}', "
        f" CAST(NULL AS VARCHAR), CAST(NULL AS VARCHAR))"
        for k, e, n, dt in rows)
    con.execute(
        f"CREATE TABLE collisions AS SELECT * FROM (VALUES {values}) "
        f"AS t(source_row_key, geom, geom_valid, datetime_local, lad_code, ctyua_code)")


def test_spatial_stamp_snapshot(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    _stub_boundaries(con)
    _stub_collisions(con, [("k_in", 50, 50, "2023-01-01 08:00"),
                           ("k_out", 500, 500, "2023-01-01 08:00")])   # inside / outside
    t = Stats19Transformer(); t._boundary_mode = "snapshot"; t._spatial_stamp(con)
    res = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT source_row_key, lad_code, ctyua_code FROM collisions").fetchall()}
    assert res["k_in"] == ("E-LAD", "E-CTY")      # point inside -> stamped
    assert res["k_out"] == (None, None)           # point outside -> unstamped


def test_spatial_stamp_temporal_picks_window(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    # Same polygon under two vintages with different codes and adjacent windows.
    con.execute(
        "CREATE TABLE lad_boundaries AS SELECT * FROM (VALUES "
        "  ('OLD','Old', ST_GeomFromText('POLYGON((0 0,0 100,100 100,100 0,0 0))'), TRUE, DATE '2010-01-01', DATE '2020-01-01'), "
        "  ('NEW','New', ST_GeomFromText('POLYGON((0 0,0 100,100 100,100 0,0 0))'), TRUE, DATE '2020-01-01', CAST(NULL AS DATE))"
        ") AS t(area_code, area_name, geom, geom_valid, valid_from, valid_to)")
    _stub_collisions(con, [("k_2015", 50, 50, "2015-06-01 08:00"),
                           ("k_2023", 50, 50, "2023-06-01 08:00")])
    t = Stats19Transformer(); t._boundary_mode = "temporal"; t._spatial_stamp(con)
    res = {r[0]: r[1] for r in con.execute(
        "SELECT source_row_key, lad_code FROM collisions").fetchall()}
    assert res["k_2015"] == "OLD" and res["k_2023"] == "NEW"


def test_spatial_stamp_tolerates_missing_boundary_table(con):
    # No boundary tables -> codes stay NULL and a warning is emitted (build still works).
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    _stub_collisions(con, [("k1", 50, 50, "2023-01-01 08:00")])
    t = Stats19Transformer(); t._boundary_mode = "snapshot"
    with pytest.warns(UserWarning, match="boundary table"):
        t._spatial_stamp(con)
    assert con.execute("SELECT lad_code FROM collisions").fetchone()[0] is None


def _seed_ons_cache(cache_dir):
    # Copy each committed ONS fixture to the name the newest vintage expects (mirrors
    # tests/test_spatial.py::_seed_cache).
    for prefix, cls in (("lad", LADBoundaryTransformer), ("ctyua", CTYUABoundaryTransformer)):
        newest = cls().vintages[-1]
        year = newest.valid_from[:4]
        src = os.path.join(ONS_FIXTURES, f"{prefix}_{year}", f"{prefix}_sample.geojson")
        shutil.copy(src, os.path.join(cache_dir, newest.source_file))


def _full_client(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_cache(cache)          # stats19 CSVs
    _seed_ons_cache(cache)      # ONS boundary geojson
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [
        CTYUABoundaryTransformer(), LADBoundaryTransformer(), Stats19Transformer()]
    return client


def test_end_to_end_build_stamps_collisions(tmp_path):
    client = _full_client(tmp_path)
    client.build(years=YEARS)          # snapshot default; runs all Step 2 invariants

    # collisions_spatial view == valid-geometry collisions.
    n_view = client.con.execute("SELECT count(*) FROM collisions_spatial").fetchone()[0]
    n_valid = client.con.execute(
        "SELECT count(*) FROM collisions WHERE geom_valid").fetchone()[0]
    assert n_view == n_valid and n_valid > 0

    # Every stamped code is a real LAD code (consistency).
    bad = client.con.execute(
        "SELECT count(*) FROM collisions WHERE lad_code IS NOT NULL "
        "AND lad_code NOT IN (SELECT area_code FROM lad_boundaries)").fetchone()[0]
    assert bad == 0

    # At least one collision stamped (requires the aligned fixture from step C).
    stamped = client.con.execute(
        "SELECT count(*) FROM collisions WHERE lad_code IS NOT NULL").fetchone()[0]
    assert stamped >= 1, ("No collisions stamped — re-trim the collision fixture to fall "
                          "inside the committed LAD sample (Stage 04 step C).")

    # R-Tree exists on collisions.geom.
    idx = {r[0] for r in client.con.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'collisions'").fetchall()}
    assert "collisions_geom_rtree" in idx
    client.close()


def test_rebuild_same_file_is_idempotent(tmp_path):
    # A second build against the SAME on-disk DB must not double rows or break invariants.
    db = str(tmp_path / "s.db")
    cache = str(tmp_path / "cache")
    _seed_cache(cache); _seed_ons_cache(cache)

    def run():
        cl = crossroads.init_engine(database_path=db, cache_dir=cache)
        cl.registry._transformers = [
            CTYUABoundaryTransformer(), LADBoundaryTransformer(), Stats19Transformer()]
        cl.build(years=YEARS)
        return cl

    first = run(); n1 = first.con.execute("SELECT count(*) FROM collisions").fetchone()[0]; first.close()
    second = run(); n2 = second.con.execute("SELECT count(*) FROM collisions").fetchone()[0]
    assert n1 == n2 and n2 > 0
    assert second.con.execute(
        "SELECT count(*) FROM duckdb_indexes() WHERE table_name='collisions'").fetchone()[0] == 1
    second.close()


# --- Stage 05: reference data (codebook + column manifest) -------------------

def test_reference_tables_load(con):
    t = Stats19Transformer()
    t._load_codebook(con); t._load_column_manifest(con)

    # Decodes a known code (casualty_severity 2 -> Serious).
    lab = con.execute(
        "SELECT label FROM codebook WHERE variable='casualty_severity' AND code='2'").fetchone()
    assert lab and lab[0].lower().startswith("serious")
    # A -1 sentinel is flagged missing (use a variable that HAS a -1 code — severity has none).
    assert con.execute(
        "SELECT is_missing FROM codebook WHERE variable='age_band_of_casualty' AND code='-1'"
    ).fetchone()[0] is True
    # The FULL missing set is flagged, not just -1 (2024 guide: ~6 non-(-1) missing rows).
    assert con.execute(
        "SELECT count(*) FROM codebook WHERE is_missing AND code <> '-1'").fetchone()[0] >= 1
    # Self-reported '9'/'99' unknowns are KEPT (is_missing = FALSE), matching stats19's behaviour.
    speed99 = con.execute(
        "SELECT is_missing FROM codebook WHERE variable='speed_limit' AND code='99'").fetchone()
    if speed99:                                      # present in the 2024 guide
        assert speed99[0] is False
    # Both audited severities are covered and exactly 1/2/3 (Fatal/Serious/Slight) — no sentinel.
    for v in ("collision_severity", "casualty_severity"):
        codes = {r[0] for r in con.execute(
            "SELECT code FROM codebook WHERE variable=?", [v]).fetchall()}
        assert {"1", "2", "3"} <= codes
        assert con.execute(
            "SELECT count(*) FROM codebook WHERE variable=? AND is_missing", [v]).fetchone()[0] == 0
    # Unique on (variable, code); is_missing is a real BOOLEAN.
    assert con.execute(
        "SELECT count(*)-count(DISTINCT (variable||'\x1f'||code)) FROM codebook").fetchone()[0] == 0
    assert {r[0]: r[1] for r in con.execute("DESCRIBE codebook").fetchall()}["is_missing"] == "BOOLEAN"


def test_column_manifest_covers_every_fixture_column(con):
    t = Stats19Transformer(); t._load_column_manifest(con)
    for table_kind in ("collision", "vehicle", "casualty"):
        with open(os.path.join(
                FIXTURES, f"dft-road-casualty-statistics-{table_kind}-2023.csv")) as f:
            header = {h.strip().lower() for h in f.readline().strip().split(",")}
        classified = {r[0].lower() for r in con.execute(
            "SELECT col FROM column_manifest WHERE tbl = ?", [table_kind]).fetchall()}
        missing = header - classified
        assert not missing, f"{table_kind}: unclassified columns {missing}"
    kinds = {r[0] for r in con.execute(
        "SELECT DISTINCT kind FROM column_manifest").fetchall()}
    assert kinds <= {"identity", "geo", "datetime", "coded", "numeric", "text"}, f"bad kinds {kinds}"
    # Verified size + breakdown for the committed (2024) manifest.
    assert con.execute("SELECT count(*) FROM column_manifest").fetchone()[0] == 99
    by_kind = {r[0]: r[1] for r in con.execute(
        "SELECT kind, count(*) FROM column_manifest GROUP BY kind").fetchall()}
    assert by_kind == {"identity": 12, "geo": 4, "datetime": 2, "coded": 60, "numeric": 14, "text": 7}
    # coded/numeric carry a dtype; identity/geo/datetime/text do not.
    assert con.execute("SELECT count(*) FROM column_manifest "
                       "WHERE kind IN ('coded','numeric') AND (dtype IS NULL OR dtype='')"
                       ).fetchone()[0] == 0
    # The four probabilistic severity-adjustment weights are DOUBLE numerics (note: column is `col`).
    assert con.execute("SELECT count(*) FROM column_manifest "
                       "WHERE col LIKE '%adjusted_severity%' AND kind='numeric' AND dtype='DOUBLE'"
                       ).fetchone()[0] == 4


def test_build_creates_reference_tables(tmp_path):
    client = _stats19_client(tmp_path)     # stats19-only registry helper
    client.build(years=YEARS)
    assert client.con.execute("SELECT count(*) FROM codebook").fetchone()[0] > 0
    assert client.con.execute("SELECT count(*) FROM column_manifest").fetchone()[0] > 0
    client.close()
