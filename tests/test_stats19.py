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


def _empty_reference_stubs(con):
    """Empty codebook + column_manifest so a derivation's broad-clean loop is a no-op
    (leaving only the bespoke columns a test targets). Real builds load populated tables
    first; these bespoke-focused unit tests don't exercise the broad clean."""
    con.execute("CREATE TABLE IF NOT EXISTS codebook"
                "(variable VARCHAR, code VARCHAR, label VARCHAR, is_missing BOOLEAN)")
    con.execute("CREATE TABLE IF NOT EXISTS column_manifest"
                "(tbl VARCHAR, col VARCHAR, kind VARCHAR, dtype VARCHAR)")


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
    _empty_reference_stubs(con)
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
    _empty_reference_stubs(con)
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
    _empty_reference_stubs(con)
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
    _empty_reference_stubs(con)
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
    # longitude/latitude are numeric (DOUBLE), not geo — they carry real coordinate numbers
    # into silver (Stage 06 keep-in-place), so geo is 2 (OSGR easting/northing). speed_limit is
    # numeric (a literal mph quantity, not a labelled code list), so coded is 59 and numeric 17.
    assert by_kind == {"identity": 12, "geo": 2, "datetime": 2, "coded": 59, "numeric": 17, "text": 7}
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


# --- Stage 06: keep-in-place broad clean (every bronze column reaches silver) ---

def test_collision_broad_clean_keeps_codes_nulls_missing(con):
    """Coded columns keep their integer code (missing set -> NULL); numeric columns type
    correctly with sentinels -> NULL (incl. longitude as DOUBLE); text carried raw."""
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('road_type','6','Single carriageway',FALSE),('road_type','-1','Data missing or out of range',TRUE),"
        "  ('road_type','9','Unknown',TRUE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('collision','road_type','coded','INTEGER'),"
        "  ('collision','number_of_vehicles','numeric','INTEGER'),"
        "  ('collision','longitude','numeric','DOUBLE'),"        # geo->numeric reclassification
        "  ('collision','lsoa_of_accident_location','text','')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30','6','2','E01000001','-0.12'),"
        "  ('c2','2023','r2','531000','181000','06/01/2023','09:00','9','-1','E01000002','-1'),"
        "  ('c3','2023','r3','532000','182000','07/01/2023','10:00','x','x','E01000003','x')"
        ") AS t(accident_index,accident_year,accident_reference,location_easting_osgr,"
        "location_northing_osgr,date,time,road_type,number_of_vehicles,lsoa_of_accident_location,longitude)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT accident_index, road_type, number_of_vehicles, longitude, "
        "lsoa_of_accident_location FROM collisions").fetchall()}
    assert rows["c1"][1:] == (6, 2, -0.12, "E01000001")   # codes/number kept; DOUBLE lon; text raw
    assert rows["c2"][1] is None                        # '9=Unknown' -> NULL
    assert rows["c2"][2] is None                        # '-1' INTEGER numeric -> NULL
    assert rows["c2"][3] is None                        # '-1' DOUBLE longitude -> NULL
    assert rows["c3"][1] is None and rows["c3"][2] is None and rows["c3"][3] is None  # non-numeric -> NULL
    assert con.execute("SELECT count(*) FROM collisions").fetchone()[0] == 3   # keep-in-place
    assert con.execute("SELECT count(*) FROM collisions WHERE road_type = -1").fetchone()[0] == 0
    dt = {r[0]: r[1] for r in con.execute("DESCRIBE collisions").fetchall()}
    assert dt["road_type"] == "INTEGER" and dt["lsoa_of_accident_location"] == "VARCHAR"
    assert dt["longitude"] == "DOUBLE"                  # lon/lat carried as real numbers, not dropped/raw
    # Bespoke geom/datetime still there.
    assert "geom" in dt and dt["datetime_local"].startswith("TIMESTAMP")


def test_casualty_broad_clean(con):
    """Casualty keep-in-place: a coded column keeps its code, a -1 numeric -> NULL, and
    link_valid holds (orphan casualty flagged FALSE), driven against a collisions stub."""
    from crossroads.quality import ensure_quality_tables
    ensure_quality_tables(con)
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('sex_of_casualty','1','Male',FALSE),"
        "  ('sex_of_casualty','-1','Data missing or out of range',TRUE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('casualty','sex_of_casualty','coded','INTEGER'),"
        "  ('casualty','age_of_casualty','numeric','INTEGER')) AS t(tbl,col,kind,dtype)")
    # collisions stub: only 'x1' is a real collision, so 'x9' casualties are orphans.
    con.execute("CREATE TABLE collisions AS SELECT * FROM (VALUES ('x1')) AS t(accident_index)")
    con.execute("CREATE TABLE stats19_casualty_raw AS SELECT * FROM (VALUES "
        "  ('x1','1','1','1','30'),"     # linked, sex kept, age kept
        "  ('x1','1','2','-1','-1'),"    # linked, sex -1 -> NULL, age -1 -> NULL
        "  ('x9','1','1','1','40')"      # orphan -> link_valid FALSE
        ") AS t(accident_index,vehicle_reference,casualty_reference,sex_of_casualty,age_of_casualty)")
    t = Stats19Transformer(); t._derive_casualty_silver(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT source_row_key, sex_of_casualty, age_of_casualty, link_valid FROM casualties").fetchall()}
    assert rows["x1|1|1"][1:] == (1, 30, True)          # code + number kept; linked
    assert rows["x1|1|2"][1] is None and rows["x1|1|2"][2] is None   # -1 coded + -1 numeric -> NULL
    assert rows["x9|1|1"][3] is False                   # orphan flagged, not dropped
    assert con.execute("SELECT count(*) FROM casualties").fetchone()[0] == 3   # keep-in-place


def test_all_silver_tables_full_width(tmp_path):
    """Over the real sample: every bronze column reaches silver (minus the columns bespoke
    logic transforms in place), no -1 leaks into any cleaned column, lon/lat are DOUBLE."""
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)          # runs all invariants; raises on failure
    consumed = {"collision": {"location_easting_osgr", "location_northing_osgr", "date", "time"},
                "vehicle": set(), "casualty": set()}
    for table_kind, silver in (("collision", "collisions"), ("vehicle", "vehicles"), ("casualty", "casualties")):
        with open(os.path.join(FIXTURES, f"dft-road-casualty-statistics-{table_kind}-2023.csv")) as f:
            header = [h.strip().lower() for h in f.readline().strip().split(",")]
        cols = {r[0].lower() for r in client.con.execute(f"DESCRIBE {silver}").fetchall()}
        for c in header:
            if c in consumed[table_kind]:
                continue
            assert c in cols, f"{silver}: column '{c}' missing from silver (not keep-in-place)"
    # No raw '-1' missing marker leaks into ANY cleaned coded/numeric column — every such
    # column across all three tables, not a spot-check.
    for table_kind, silver in (("collision", "collisions"), ("vehicle", "vehicles"), ("casualty", "casualties")):
        cleaned = [r[0] for r in client.con.execute(
            "SELECT col FROM column_manifest WHERE tbl = ? AND kind IN ('coded','numeric')",
            [table_kind]).fetchall()]
        present = {r[0].lower() for r in client.con.execute(f"DESCRIBE {silver}").fetchall()}
        for c in cleaned:
            if c.lower() in present:
                leaked = client.con.execute(f"SELECT count(*) FROM {silver} WHERE {c} = -1").fetchone()[0]
                assert leaked == 0, f"{silver}.{c} still holds {leaked} raw -1 missing markers"
    # longitude/latitude are carried as real numbers (DOUBLE), not dropped or left raw text.
    ctypes = {r[0].lower(): r[1] for r in client.con.execute("DESCRIBE collisions").fetchall()}
    assert ctypes.get("longitude") == "DOUBLE" and ctypes.get("latitude") == "DOUBLE"
    # Geom/datetime dimensions still hold; gold views + spatial join unaffected.
    assert client.con.execute("SELECT count(*) FROM collisions WHERE geom_valid = FALSE").fetchone()[0] == 0
    assert client.con.execute("SELECT count(*) FROM collisions_spatial").fetchone()[0] > 0
    client.close()


# --- Stage 07: core severity audit (collision_severity + casualty_severity) -----

def test_collision_severity_core_audit(con):
    """collision_severity is promoted to the formal audit: a raw twin, a cleaned INTEGER
    (missing set -> NULL), a valid flag, and one ledger row per FALSE. The stub codebook
    injects -1/9 missing codes to exercise the FALSE branch (a mechanism test — the real
    guide lists no sentinel for severity)."""
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('collision_severity','1','Fatal',FALSE),('collision_severity','2','Serious',FALSE),"
        "  ('collision_severity','3','Slight',FALSE),('collision_severity','-1','Data missing or out of range',TRUE),"
        "  ('collision_severity','9','Unknown',TRUE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('collision','collision_severity','coded','INTEGER')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30','2'),"
        "  ('c2','2023','r2','531000','181000','06/01/2023','09:00','-1'),"
        "  ('c3','2023','r3','532000','182000','07/01/2023','10:00','9'),"
        "  ('c4','2023','r4','533000','183000','08/01/2023','11:00','x')"
        ") AS t(accident_index,accident_year,accident_reference,location_easting_osgr,"
        "location_northing_osgr,date,time,collision_severity)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT accident_index, collision_severity, collision_severity_valid, collision_severity_raw "
        "FROM collisions").fetchall()}
    assert rows["c1"][1:] == (2, True, "2")
    assert rows["c2"][1:] == (None, False, "-1")
    assert rows["c3"][1:] == (None, False, "9")
    assert rows["c4"][1:] == (None, False, "x")
    assert con.execute("SELECT count(*) FROM collisions").fetchone()[0] == 4   # keep-in-place
    ledger = con.execute("SELECT source_row_key, rule_id FROM data_quality_log "
        "WHERE rule_id='stats19.collision_severity.missing' ORDER BY source_row_key").fetchall()
    assert ledger == [("c2", "stats19.collision_severity.missing"),
                      ("c3", "stats19.collision_severity.missing"),
                      ("c4", "stats19.collision_severity.missing")]
    dt = {r[0]: r[1] for r in con.execute("DESCRIBE collisions").fetchall()}
    assert dt["collision_severity"] == "INTEGER" and dt["collision_severity_raw"] == "VARCHAR"
    assert dt["collision_severity_valid"] == "BOOLEAN"


def test_collision_severity_guards_bare_minus_one_without_codebook(con):
    """Regression pin: the codebook lists ONLY the real severity codes (1/2/3, no -1 row,
    matching the real 2024 guide). A raw -1 must still clean to NULL + valid = FALSE + a
    ledger row via the bare -1 guard, NOT via the codebook. Fails iff the guard is removed."""
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('collision_severity','1','Fatal',FALSE),('collision_severity','2','Serious',FALSE),"
        "  ('collision_severity','3','Slight',FALSE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('collision','collision_severity','coded','INTEGER')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30','2'),"
        "  ('c2','2023','r2','531000','181000','06/01/2023','09:00','-1')"
        ") AS t(accident_index,accident_year,accident_reference,location_easting_osgr,"
        "location_northing_osgr,date,time,collision_severity)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT accident_index, collision_severity, collision_severity_valid, collision_severity_raw "
        "FROM collisions").fetchall()}
    assert rows["c1"][1:] == (2, True, "2")
    assert rows["c2"][1:] == (None, False, "-1")   # nulled by the -1 guard, NOT the codebook
    ledger = con.execute("SELECT source_row_key FROM data_quality_log "
        "WHERE rule_id='stats19.collision_severity.missing'").fetchall()
    assert ledger == [("c2",)]


def test_collision_severity_reads_legacy_accident_severity(con):
    """A pre-2024 tranche names the column accident_severity; the CORE clean coalesces the
    aliases so it still populates the canonical collision_severity."""
    from crossroads.quality import ensure_quality_tables
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    ensure_quality_tables(con)
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('collision_severity','1','Fatal',FALSE),('collision_severity','2','Serious',FALSE),"
        "  ('collision_severity','3','Slight',FALSE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('collision','collision_severity','coded','INTEGER')) AS t(tbl,col,kind,dtype)")
    # Bronze carries accident_severity (legacy name), NOT collision_severity.
    con.execute("CREATE TABLE stats19_collision_raw AS SELECT * FROM (VALUES "
        "  ('c1','2023','r1','530000','180000','05/01/2023','08:30','2')"
        ") AS t(accident_index,accident_year,accident_reference,location_easting_osgr,"
        "location_northing_osgr,date,time,accident_severity)")
    t = Stats19Transformer(); t._derive_collision_silver(con)
    row = con.execute(
        "SELECT collision_severity, collision_severity_valid, collision_severity_raw "
        "FROM collisions").fetchone()
    assert row == (2, True, "2")   # legacy accident_severity coalesced into collision_severity


def test_casualty_severity_core_audit(con):
    """Mirror of the collision audit for casualty_severity, driven against a collisions
    stub. The stub codebook injects a -1 missing code to exercise the FALSE branch."""
    from crossroads.quality import ensure_quality_tables
    ensure_quality_tables(con)
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('casualty_severity','1','Fatal',FALSE),('casualty_severity','2','Serious',FALSE),"
        "  ('casualty_severity','3','Slight',FALSE),('casualty_severity','-1','Data missing or out of range',TRUE)"
        ") AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('casualty','casualty_severity','coded','INTEGER')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE collisions AS SELECT * FROM (VALUES ('x1')) AS t(accident_index)")
    con.execute("CREATE TABLE stats19_casualty_raw AS SELECT * FROM (VALUES "
        "  ('x1','1','1','2'),"      # valid severity, linked
        "  ('x1','1','2','-1'),"     # -1 -> NULL, valid FALSE
        "  ('x1','2','1','x')"       # non-numeric -> NULL, valid FALSE
        ") AS t(accident_index,vehicle_reference,casualty_reference,casualty_severity)")
    t = Stats19Transformer(); t._derive_casualty_silver(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT source_row_key, casualty_severity, casualty_severity_valid, casualty_severity_raw "
        "FROM casualties").fetchall()}
    assert rows["x1|1|1"][1:] == (2, True, "2")
    assert rows["x1|1|2"][1:] == (None, False, "-1")
    assert rows["x1|2|1"][1:] == (None, False, "x")
    assert con.execute("SELECT count(*) FROM casualties").fetchone()[0] == 3   # keep-in-place
    ledger = con.execute("SELECT source_row_key FROM data_quality_log "
        "WHERE rule_id='stats19.casualty_severity.missing' ORDER BY source_row_key").fetchall()
    assert ledger == [("x1|1|2",), ("x1|2|1",)]
    dt = {r[0]: r[1] for r in con.execute("DESCRIBE casualties").fetchall()}
    assert dt["casualty_severity"] == "INTEGER" and dt["casualty_severity_raw"] == "VARCHAR"
    assert dt["casualty_severity_valid"] == "BOOLEAN"


def test_severities_audited_end_to_end(tmp_path):
    """Over the real sample: both severities carry raw/clean/valid columns of the right
    types, every row is valid (severity is mandatory), and no raw -1 survives as a code."""
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)          # runs all invariants; raises on failure
    for tbl, col in (("collisions", "collision_severity"), ("casualties", "casualty_severity")):
        dt = {r[0]: r[1] for r in client.con.execute(f"DESCRIBE {tbl}").fetchall()}
        assert dt[col] == "INTEGER" and dt[f"{col}_raw"] == "VARCHAR" and dt[f"{col}_valid"] == "BOOLEAN"
        assert client.con.execute(
            f"SELECT count(*) FROM {tbl} WHERE {col}_valid = FALSE").fetchone()[0] == 0
        assert client.con.execute(
            f"SELECT count(*) FROM {tbl} WHERE {col} = -1").fetchone()[0] == 0
    client.close()


def test_completeness_counts(con):
    """Counts are correct on synthetic silver: present = count(col), missing = the rest,
    text columns are not reported, and missing_rate is the missing fraction."""
    t = Stats19Transformer()
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('casualty','casualty_severity','coded','INTEGER'),"
        "  ('casualty','age_of_casualty','numeric','INTEGER'),"
        "  ('casualty','lsoa_of_casualty','text','')) AS t(tbl,col,kind,dtype)")   # text -> NOT reported
    con.execute("CREATE TABLE casualties AS SELECT * FROM (VALUES "
        "  (2,    34,   'E01'),(3, NULL, 'E02'),(NULL, NULL, 'E03')"
        ") AS t(casualty_severity, age_of_casualty, lsoa_of_casualty)")
    t._ensure_completeness_table(con)
    t._write_completeness(con, "casualties", "stats19_casualty", "casualty")
    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT column_name, kind, n_total, n_present, n_missing, missing_rate "
        "FROM stats19_completeness WHERE source_id='stats19_casualty' ORDER BY column_name").fetchall()}
    assert rows["casualty_severity"] == ("coded", 3, 2, 1, 1/3)
    assert rows["age_of_casualty"]   == ("numeric", 3, 1, 2, 2/3)
    assert "lsoa_of_casualty" not in rows        # text columns are not reported


def test_completeness_idempotent(con):
    """Re-running the writer for one source clears its prior rows first (no doubling)."""
    t = Stats19Transformer()
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('casualty','casualty_severity','coded','INTEGER')) AS t(tbl,col,kind,dtype)")
    con.execute("CREATE TABLE casualties AS SELECT * FROM (VALUES (2)) AS t(casualty_severity)")
    t._ensure_completeness_table(con)
    t._write_completeness(con, "casualties", "stats19_casualty", "casualty")
    t._write_completeness(con, "casualties", "stats19_casualty", "casualty")
    assert con.execute("SELECT count(*) FROM stats19_completeness "
                       "WHERE source_id='stats19_casualty'").fetchone()[0] == 1


def test_completeness_report_end_to_end(tmp_path):
    """Over the real sample: one row per cleaned coded/numeric column per source (count
    derived from the manifest so it self-corrects), every rate in [0,1]."""
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    for sid, kind in (("stats19_collision","collision"),("stats19_vehicle","vehicle"),
                      ("stats19_casualty","casualty")):
        expected = client.con.execute(
            "SELECT count(*) FROM column_manifest WHERE tbl=? AND kind IN ('coded','numeric')",
            [kind]).fetchone()[0]
        got = client.con.execute(
            "SELECT count(*) FROM stats19_completeness WHERE source_id=?", [sid]).fetchone()[0]
        assert got == expected and got > 0, f"{sid}: {got} rows, expected {expected}"
    assert client.con.execute(
        "SELECT count(*) FROM stats19_completeness WHERE missing_rate < 0 OR missing_rate > 1").fetchone()[0] == 0
    # Fixture-coupled: this holds only because the committed casualty sample has no missing
    # casualty_severity. If that fixture is ever edited to include a missing severity (e.g. to
    # exercise the Stage 07 ledger), update this expectation — the rate is not a bug then.
    sev = client.con.execute(
        "SELECT missing_rate FROM stats19_completeness "
        "WHERE source_id='stats19_casualty' AND column_name='casualty_severity'").fetchone()[0]
    assert sev == 0.0        # mandatory field, clean sample
    client.close()


def test_labelled_view_decodes_and_never_stores(con):
    t = Stats19Transformer()
    con.execute("CREATE TABLE codebook AS SELECT * FROM (VALUES "
        "  ('casualty_severity','1','Fatal',FALSE),('casualty_severity','2','Serious',FALSE),"
        "  ('casualty_severity','-1','Data missing or out of range',TRUE),"
        "  ('sex_of_casualty','1','Male',FALSE)) AS t(variable,code,label,is_missing)")
    con.execute("CREATE TABLE column_manifest AS SELECT * FROM (VALUES "
        "  ('casualty','casualty_severity','coded','INTEGER'),"
        "  ('casualty','sex_of_casualty','coded','INTEGER'),"
        "  ('casualty','age_of_casualty','numeric','INTEGER')) AS t(tbl,col,kind,dtype)")   # numeric -> not labelled
    con.execute("CREATE TABLE casualties AS SELECT * FROM (VALUES "
        "  ('k1', 2,    1, 34),('k2', NULL, 1, NULL)"
        ") AS t(source_row_key, casualty_severity, sex_of_casualty, age_of_casualty)")
    t._create_labelled_views(con)
    rows = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT source_row_key, casualty_severity_label, sex_of_casualty_label "
        "FROM casualties_labelled").fetchall()}
    assert rows["k1"] == ("Serious", "Male")
    assert rows["k2"][0] is None                 # cleaned-NULL severity -> NULL label
    cols = {r[0] for r in con.execute("DESCRIBE casualties").fetchall()}
    assert not any(c.endswith("_label") for c in cols)       # labels NOT stored
    view_cols = {r[0] for r in con.execute("DESCRIBE casualties_labelled").fetchall()}
    assert "age_of_casualty_label" not in view_cols          # numeric not labelled
    assert con.execute("SELECT count(*) FROM casualties_labelled").fetchone()[0] == 2   # no fan-out


def test_labelled_views_end_to_end(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    for silver, view, kind in (("collisions","collisions_labelled","collision"),
                               ("vehicles","vehicles_labelled","vehicle"),
                               ("casualties","casualties_labelled","casualty")):
        silver_cols = {r[0].lower() for r in client.con.execute(f"DESCRIBE {silver}").fetchall()}
        coded = [c for (c,) in client.con.execute(
            "SELECT col FROM column_manifest WHERE tbl=? AND kind='coded'", [kind]).fetchall()
            if c.lower() in silver_cols]
        view_cols = {r[0].lower() for r in client.con.execute(f"DESCRIBE {view}").fetchall()}
        for c in coded:
            assert f"{c}_label" in view_cols, f"{view} missing {c}_label"
        s = client.con.execute(f"SELECT count(*) FROM {silver}").fetchone()[0]
        v = client.con.execute(f"SELECT count(*) FROM {view}").fetchone()[0]
        assert v == s and s > 0                                         # no fan-out/loss
        assert not any(c.endswith("_label") for c in silver_cols), f"{silver} must not store labels"
    lab = client.con.execute(
        "SELECT DISTINCT casualty_severity_label FROM casualties_labelled "
        "WHERE casualty_severity = 2").fetchone()
    assert lab and lab[0].lower().startswith("serious")
    # Every PRESENT code in a codebook-COVERED column must decode -- across ALL coded columns
    # of ALL three views, not just casualty_severity. This is the regression tripwire for a
    # systematic decode break (e.g. a future zero-padded code '07' that the INTEGER->VARCHAR
    # join would silently miss): if any covered column loses its labels, the suite fails loudly
    # and names the column, instead of quietly serving blanks. Scope to covered columns only
    # (>=1 codebook row) so a legitimately-uncovered column like enhanced_severity_collision
    # (all-NULL labels by design) is NOT treated as a failure.
    for silver, view, kind in (("collisions","collisions_labelled","collision"),
                               ("vehicles","vehicles_labelled","vehicle"),
                               ("casualties","casualties_labelled","casualty")):
        silver_cols = {r[0].lower() for r in client.con.execute(f"DESCRIBE {silver}").fetchall()}
        coded = [c for (c,) in client.con.execute(
            "SELECT col FROM column_manifest WHERE tbl=? AND kind='coded'", [kind]).fetchall()
            if c.lower() in silver_cols]
        for c in coded:
            covered = client.con.execute(
                "SELECT count(*) FROM codebook WHERE variable = ?", [c]).fetchone()[0] > 0
            if not covered:
                continue                 # dictionary has nothing for this column yet -> blanks expected
            undecoded = client.con.execute(
                f"SELECT count(*) FROM {view} "
                f"WHERE {c} IS NOT NULL AND {c}_label IS NULL").fetchone()[0]
            assert undecoded == 0, f"{view}.{c} has {undecoded} undecoded codes"
    client.close()
