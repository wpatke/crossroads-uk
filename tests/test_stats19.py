"""Tests for the STATS19 transformer (stats19.py).

Offline: the cache is pre-seeded with committed sample CSVs so extract() finds the
files and performs no network download.
"""
import os
import shutil
import urllib.request
import urllib.error

import pytest
import crossroads
from crossroads.transformers.stats19 import Stats19Transformer
from crossroads.transformers.spatial import LADBoundaryTransformer, CTYUABoundaryTransformer

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
ONS_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ons")
YEARS = [2023]


def test_stats19_is_user_selectable_with_default_display_name():
    # STATS19 is a queryable dataset the researcher picks; label defaults to source_id.
    t = Stats19Transformer()
    assert t.user_selectable is True
    assert t.display_name == "stats19"


def test_stats19_declares_optional_dependencies():
    deps = Stats19Transformer().depends_on
    assert "era5_weather" in deps and "ons_lad" in deps and "ons_ctyua" in deps

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


# --- fetch hardening (offline: the downloader is monkeypatched, no network) ---

def test_looks_like_stats19_csv_recognises_both_headers(tmp_path):
    """Unit: the header sniff accepts historical + modern headers, rejects HTML."""
    t = Stats19Transformer()
    historical = tmp_path / "hist.csv"
    historical.write_text("accident_index,accident_year\n123,2019\n")
    modern = tmp_path / "modern.csv"
    modern.write_text("collision_index,collision_year\n123,2023\n")
    html = tmp_path / "bad.html"
    html.write_text("<!DOCTYPE html><html><body>Not found</body></html>")
    assert t._looks_like_stats19_csv(str(historical)) is True
    assert t._looks_like_stats19_csv(str(modern)) is True
    assert t._looks_like_stats19_csv(str(html)) is False


def test_fetch_missing_year_raises_friendly_error(tmp_path, monkeypatch):
    """A 404 (unpublished year) -> ValueError, and nothing is cached."""
    cache = str(tmp_path / "cache")

    def fake_urlretrieve(url, filename):
        # Simulate DfT returning 404 for a not-yet-published year.
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlretrieve", fake_urlretrieve)
    t = Stats19Transformer()
    with pytest.raises(ValueError) as exc:
        t.extract(cache, years=[2999])
    msg = str(exc.value)
    assert "404" in msg and "earlier year" in msg
    # Clean cache: no partial (.part) and no final (.csv) files left behind.
    assert os.listdir(cache) == []


def test_fetch_html_error_page_is_rejected(tmp_path, monkeypatch):
    """A 200-OK response whose body is an HTML error page -> ValueError, nothing cached."""
    cache = str(tmp_path / "cache")

    def fake_urlretrieve(url, filename):
        # Simulate a server that answers a missing file with 200 OK + an HTML page.
        with open(filename, "w") as f:
            f.write("<!DOCTYPE html><html><body>File not found</body></html>")

    monkeypatch.setattr(urllib.request, "urlretrieve", fake_urlretrieve)
    t = Stats19Transformer()
    with pytest.raises(ValueError) as exc:
        t.extract(cache, years=[2999])
    assert "not a STATS19 CSV" in str(exc.value)
    assert os.listdir(cache) == []   # .part removed, nothing promoted to the cache


def test_fetch_valid_csv_is_cached_atomically(tmp_path, monkeypatch):
    """A valid STATS19 CSV lands at its final path with no leftover .part file."""
    cache = str(tmp_path / "cache")

    def fake_urlretrieve(url, filename):
        # A minimal but valid-looking STATS19 CSV (header names the index column).
        with open(filename, "w") as f:
            f.write("collision_index,collision_year,collision_severity\n"
                    "2999010000001,2999,3\n")

    monkeypatch.setattr(urllib.request, "urlretrieve", fake_urlretrieve)
    t = Stats19Transformer()
    t.extract(cache, years=[2999])   # loops collision/vehicle/casualty -> three files
    files = sorted(os.listdir(cache))
    assert files == [
        "dft-road-casualty-statistics-casualty-2999.csv",
        "dft-road-casualty-statistics-collision-2999.csv",
        "dft-road-casualty-statistics-vehicle-2999.csv",
    ]
    assert not any(f.endswith(".part") for f in files), "no temporary .part file should remain"


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


# --- Weather stamping (Stage 03) ---------------------------------------------

def _stub_weather(con):
    # One weather cell (54.7,-1.2) at 14:00 LOCAL (13:00 UTC in BST).
    con.execute("INSTALL icu"); con.execute("LOAD icu")
    con.execute(
        "CREATE TABLE weather AS SELECT "
        "  CAST(round(54.7*10) AS INTEGER) AS grid_i, "
        "  CAST(round(-1.2*10) AS INTEGER) AS grid_j, "
        "  ((TIMESTAMP '2023-06-15 13:00:00') AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London' AS valid_time_local, "
        "  15.0 AS temperature_c, 1.0 AS precipitation_mm")


def _stub_collisions_geo(con, rows):
    # rows: (key, lon, lat, iso_local_dt). Build 27700 geom via reprojection so the
    # stamp's inverse reprojection lands back on the same grid cell.
    vals = ", ".join(
        f"('{k}', ST_Transform(ST_Point({lon},{lat}),'EPSG:4326','EPSG:27700',always_xy:=true)::GEOMETRY, "
        f" TRUE, TIMESTAMP '{dt}', NULL, NULL, CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE))"
        for k, lon, lat, dt in rows)
    con.execute(
        f"CREATE TABLE collisions AS SELECT * FROM (VALUES {vals}) AS "
        f"t(source_row_key, geom, geom_valid, datetime_local, lad_code, ctyua_code, "
        f"  temperature_c, precipitation_mm)")


def test_weather_stamp_matches_cell_and_hour(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    _stub_weather(con)
    _stub_collisions_geo(con, [
        ("k_in",  -1.2, 54.7, "2023-06-15 14:30:00"),   # same cell + hour -> stamped
        ("k_hour", -1.2, 54.7, "2023-06-15 16:30:00"),  # same cell, wrong hour -> NULL
        ("k_cell", 0.0, 52.0, "2023-06-15 14:30:00"),   # wrong cell -> NULL
    ])
    Stats19Transformer()._weather_stamp(con)
    res = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT source_row_key, temperature_c, precipitation_mm FROM collisions").fetchall()}
    assert res["k_in"] == (15.0, 1.0)
    assert res["k_hour"] == (None, None)
    assert res["k_cell"] == (None, None)


def test_weather_stamp_tolerates_missing_weather_table(con):
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    _stub_collisions_geo(con, [("k1", -1.2, 54.7, "2023-06-15 14:30:00")])
    with pytest.warns(UserWarning, match="weather table"):
        Stats19Transformer()._weather_stamp(con)          # no weather table -> warn, skip
    assert con.execute("SELECT temperature_c FROM collisions").fetchone()[0] is None


def test_collisions_has_weather_columns_even_without_weather(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)          # no weather in this build
    dt = {r[0].lower(): r[1] for r in client.con.execute("DESCRIBE collisions").fetchall()}
    assert dt["temperature_c"] == "DOUBLE" and dt["precipitation_mm"] == "DOUBLE"
    # All NULL (no weather table existed).
    assert client.con.execute(
        "SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0] == 0
    client.close()


@pytest.mark.integration
def test_stats19_plus_weather_stamps_collisions_offline(tmp_path):
    pytest.importorskip("xarray")
    from crossroads.transformers.weather import Era5WeatherTransformer
    cache = str(tmp_path / "cache")
    _seed_cache(cache); _seed_ons_cache(cache)             # existing stats19 + ONS seeders
    weather_nc = os.path.join(os.path.dirname(__file__), "fixtures", "weather", "era5_land_sample.nc")
    shutil.copy(weather_nc, os.path.join(cache, "era5_land_2023.nc"))

    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [
        CTYUABoundaryTransformer(), LADBoundaryTransformer(),
        Stats19Transformer(), Era5WeatherTransformer()]    # get_active resolves weather-first
    client.build(datasets=["stats19", "era5_weather"], years=YEARS)   # runs §9 invariants
    try:
        # Weather grid built.
        assert client.con.execute("SELECT count(*) FROM weather").fetchone()[0] > 0
        # At least one collision stamped (fixtures are aligned by construction — Stage 02).
        stamped = client.con.execute(
            "SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0]
        assert stamped >= 1, ("No collisions stamped — regenerate the weather fixture with "
                              "scripts/build_weather_fixture.py so its cells/hours cover the "
                              "committed collision fixture.")
        # Row count unchanged (stamp is an UPDATE, not a join that fans out).
        assert client.con.execute("SELECT count(*) FROM collisions").fetchone()[0] > 0
    finally:
        client.close()


@pytest.mark.integration
def test_weather_build_is_idempotent(tmp_path):
    pytest.importorskip("xarray")
    from crossroads.transformers.weather import Era5WeatherTransformer
    db = str(tmp_path / "w.db"); cache = str(tmp_path / "cache")
    _seed_cache(cache); _seed_ons_cache(cache)
    shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "weather", "era5_land_sample.nc"),
                os.path.join(cache, "era5_land_2023.nc"))

    def run():
        cl = crossroads.init_engine(database_path=db, cache_dir=cache)
        cl.registry._transformers = [CTYUABoundaryTransformer(), LADBoundaryTransformer(),
                                     Stats19Transformer(), Era5WeatherTransformer()]
        cl.build(datasets=["stats19", "era5_weather"], years=YEARS); return cl
    a = run(); n1 = a.con.execute("SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0]; a.close()
    b = run(); n2 = b.con.execute("SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0]
    assert n1 == n2 and n2 >= 1
    b.close()


def test_solar_angles_present_and_ranged(tmp_path):
    """Every valid collision is stamped with in-range solar angles; invalid rows stay NULL."""
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    con = client.con
    # Columns exist.
    cols = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='collisions'").fetchall()]
    assert "solar_elevation_deg" in cols and "solar_azimuth_deg" in cols
    # Non-NULL iff geom AND datetime present.
    mism = con.execute(
        "SELECT count(*) FROM collisions "
        "WHERE (solar_elevation_deg IS NULL) <> "
        "      (geom IS NULL OR datetime_local IS NULL)").fetchone()[0]
    assert mism == 0, "angles must be non-NULL exactly when geom AND datetime_local exist"
    # Ranges hold for the stamped rows.
    bad = con.execute(
        "SELECT count(*) FROM collisions WHERE solar_elevation_deg IS NOT NULL AND ("
        " solar_elevation_deg < -90 OR solar_elevation_deg > 90 "
        " OR solar_azimuth_deg < 0 OR solar_azimuth_deg >= 360)").fetchone()[0]
    assert bad == 0, "elevation must be in [-90,90] and azimuth in [0,360)"
    client.close()


def test_solar_stamp_matches_known_noaa_values(tmp_path):
    """NOAA anchor: London at winter-solstice GMT noon -> elevation ~15.1 deg, azimuth ~180 (due
    south); midnight -> sun below the horizon. Uses GMT so local time == UTC (no BST offset)."""
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    # Minimal table with the columns _solar_stamp reads + writes. geom is EPSG:27700, built
    # from London lon/lat (0.13 W, 51.50 N) so the stamp's reprojection round-trips it back.
    con.execute(
        "CREATE TABLE collisions AS "
        "SELECT * FROM (VALUES "
        "  ('noon', TIMESTAMP '2023-12-22 12:00:00'), "
        "  ('midnight', TIMESTAMP '2023-12-22 00:00:00') "
        ") AS t(source_row_key, datetime_local)")
    con.execute("ALTER TABLE collisions ADD COLUMN geom GEOMETRY")
    con.execute(
        "UPDATE collisions SET geom = ST_Transform("
        "  ST_Point(-0.13, 51.50), 'EPSG:4326', 'EPSG:27700', always_xy := true)")
    con.execute("ALTER TABLE collisions ADD COLUMN solar_elevation_deg DOUBLE")
    con.execute("ALTER TABLE collisions ADD COLUMN solar_azimuth_deg DOUBLE")
    Stats19Transformer()._solar_stamp(con)
    noon = con.execute("SELECT solar_elevation_deg, solar_azimuth_deg "
                       "FROM collisions WHERE source_row_key='noon'").fetchone()
    midnight = con.execute("SELECT solar_elevation_deg "
                           "FROM collisions WHERE source_row_key='midnight'").fetchone()
    assert abs(noon[0] - 15.1) < 1.5, f"winter-noon elevation was {noon[0]}"
    assert abs(noon[1] - 180) < 5,   f"winter-noon azimuth was {noon[1]}"
    assert midnight[0] < 0,          f"midnight elevation should be < 0, was {midnight[0]}"
    con.close()


def test_solar_stamp_applies_bst_offset_in_summer(tmp_path):
    """BST anchor (complements the winter/GMT anchor): London at the summer solstice, 13:00
    BST -> elevation ~61.9 deg, azimuth ~180 (due south). This ONLY holds if the Europe/London
    offset is applied: 13:00 BST == 12:00 UTC ~ solar noon. If the code wrongly treated local
    time as UTC, the sun would read ~1 hour past noon (azimuth well west of 180), so this
    catches a daylight-saving handling bug the winter anchor cannot."""
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    con.execute(
        "CREATE TABLE collisions AS "
        "SELECT * FROM (VALUES "
        "  ('summer_noon_bst', TIMESTAMP '2023-06-21 13:00:00') "
        ") AS t(source_row_key, datetime_local)")
    con.execute("ALTER TABLE collisions ADD COLUMN geom GEOMETRY")
    con.execute(
        "UPDATE collisions SET geom = ST_Transform("
        "  ST_Point(-0.13, 51.50), 'EPSG:4326', 'EPSG:27700', always_xy := true)")
    con.execute("ALTER TABLE collisions ADD COLUMN solar_elevation_deg DOUBLE")
    con.execute("ALTER TABLE collisions ADD COLUMN solar_azimuth_deg DOUBLE")
    Stats19Transformer()._solar_stamp(con)
    elev, az = con.execute(
        "SELECT solar_elevation_deg, solar_azimuth_deg "
        "FROM collisions WHERE source_row_key='summer_noon_bst'").fetchone()
    assert abs(elev - 61.9) < 1.5, f"summer-noon (BST) elevation was {elev}"
    # Tight azimuth band: a BST-ignoring bug lands ~1h past noon (~197 deg) and fails here.
    assert abs(az - 180) < 6, f"summer-noon (BST) azimuth was {az} — BST offset not applied?"
    con.close()
