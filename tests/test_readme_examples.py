"""Integration tests for the README "Why Crossroads-UK?" worked examples.

These verify the *logic and shape* of the two showcase SQL queries against the
committed Hartlepool fixtures — NOT the national numbers in the README (CI is
offline). The SQL strings below are byte-for-byte copies of the README code
fences; if you edit one copy, edit the other (nothing parses the README, so the
two can silently drift). Style mirrors tests/test_aadf.py: build from fixtures,
compute the expectation from the build, then compare.
"""
import os
import shutil

import pytest

import crossroads
from tests.test_console import _seed_full_cache

# The committed synthetic ERA5-Land NetCDF, seeded into the build cache for the
# weather test. Its cache filename is year-specific (era5_land_{year}.nc); for a
# years=[2023] build that is era5_land_2023.nc — the name weather._filename()
# produces and the name the other weather tests seed under.
WEATHER_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "weather", "era5_land_sample.nc")
WEATHER_CACHE_NAME = "era5_land_2023.nc"

# --- Example G (sun glare): byte-for-byte copy of the README's ```sql``` block ---
README_GLARE = """
-- Low sun close to the driver's line of travel: classic sun-glare geometry.
WITH v AS (
    SELECT accident_index,
           CASE vehicle_direction_to        -- DfT 8-point code -> compass bearing (deg)
               WHEN 1 THEN 0 WHEN 2 THEN 45 WHEN 3 THEN 90 WHEN 4 THEN 135
               WHEN 5 THEN 180 WHEN 6 THEN 225 WHEN 7 THEN 270 WHEN 8 THEN 315
           END AS bearing_deg
    FROM vehicles
    WHERE vehicle_direction_to BETWEEN 1 AND 8    -- 0 = parked, -1/9 = unknown
)
SELECT c.accident_index,
       c.collision_severity,                                  -- 1=Fatal 2=Serious 3=Slight
       round(c.solar_elevation_deg, 1) AS sun_elevation_deg,  -- low = near the horizon
       round(180 - abs(abs(c.solar_azimuth_deg - v.bearing_deg) - 180), 1) AS sun_offset_deg
FROM collisions c
JOIN v USING (accident_index)
WHERE c.solar_elevation_deg BETWEEN 0 AND 10                  -- sun above the horizon, but low
  AND 180 - abs(abs(c.solar_azimuth_deg - v.bearing_deg) - 180) <= 30   -- within 30 deg ahead
ORDER BY sun_offset_deg
LIMIT 1
"""

# The README query keeps a display LIMIT 1 (show the single most striking row). For the
# logic check we need every glare row, so run the same query without that display limit.
GLARE_FULL = README_GLARE.replace("\nLIMIT 1", "")

# --- Example A (unified columns): byte-for-byte copy of the README's ```sql``` block ---
README_UNIFIED = """
SELECT accident_index,
       collision_severity,     -- 1=Fatal 2=Serious 3=Slight
       temperature_c,          -- ERA5-Land 2 m air temp at the collision hour
       precipitation_mm,       -- ERA5-Land hourly precipitation at that cell
       solar_elevation_deg,    -- sun's elevation (NOAA); negative = below horizon
       lad_code,               -- ONS local authority (point-in-polygon)
       is_bank_holiday         -- gov.uk bank-holiday calendar for that nation
FROM collisions
WHERE datetime_valid AND geom_valid
ORDER BY datetime_local
LIMIT 1
"""


@pytest.mark.integration
def test_glare_query_logic(tmp_path):
    """Example G's angular logic is correct: the SQL glare rule matches an
    independent Python recomputation from the pipeline's own solar output. Passes
    with or without the [weather] extra (glare needs only solar + heading)."""
    cache = str(tmp_path / "cache")
    _seed_full_cache(cache)
    db = str(tmp_path / "glare.duckdb")
    client = crossroads.init_engine(database_path=db, cache_dir=cache)
    client.build(datasets=["stats19"], years=[2023], boundary_mode="snapshot")
    try:
        con = client.con
        # The full glare query (README logic minus its display LIMIT) returns well-formed rows.
        full_rows = con.execute(GLARE_FULL).fetchall()  # cols: index, severity, elev, offset
        for _, _, elev, offset in full_rows:
            assert 0 <= elev <= 10            # low-sun filter honoured
            assert 0 <= offset <= 30          # within-30-deg filter honoured

        # Independent recomputation of the SAME glare rule from the pipeline's solar output.
        _BEARING = {1: 0, 2: 45, 3: 90, 4: 135, 5: 180, 6: 225, 7: 270, 8: 315}
        pairs = con.execute("""
            SELECT c.accident_index, c.solar_elevation_deg, c.solar_azimuth_deg,
                   v.vehicle_direction_to
            FROM collisions c JOIN vehicles v USING (accident_index)
            WHERE v.vehicle_direction_to BETWEEN 1 AND 8
              AND c.solar_elevation_deg IS NOT NULL AND c.solar_azimuth_deg IS NOT NULL
        """).fetchall()
        expected = set()
        for idx, elev, az, code in pairs:
            bearing = _BEARING[code]
            offset = 180 - abs(abs(az - bearing) - 180)      # same circular distance as the SQL
            if 0 <= elev <= 10 and offset <= 30:
                expected.add((idx, round(offset, 1)))
        got = {(r[0], r[3]) for r in full_rows}               # (accident_index, sun_offset_deg)
        assert got == expected     # SQL glare logic matches the documented rule exactly

        # The README query itself (with LIMIT 1) shows the single smallest-offset glare row.
        top = con.execute(README_GLARE).fetchall()
        assert len(top) <= 1
        if expected:
            assert len(top) == 1
            assert top[0][3] == min(offset for _, offset in expected)   # closest to dead ahead
    finally:
        client.close()


@pytest.mark.integration
def test_unified_columns_with_weather(tmp_path):
    """Example A's weather columns populate when the [weather] extra is installed.
    Skipped without xarray (guard is inside the test, so Test G / Test A-free still run)."""
    pytest.importorskip("xarray")
    cache = str(tmp_path / "cache")
    _seed_full_cache(cache)
    shutil.copy(WEATHER_FIXTURE, os.path.join(cache, WEATHER_CACHE_NAME))
    db = str(tmp_path / "unified.duckdb")
    client = crossroads.init_engine(database_path=db, cache_dir=cache)
    client.build(datasets=["stats19", "era5_weather"], years=[2023], boundary_mode="snapshot")
    try:
        con = client.con
        assert len(con.execute(README_UNIFIED).fetchall()) >= 1
        assert con.execute("SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL"
                           ).fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM collisions WHERE precipitation_mm IS NOT NULL"
                           ).fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM collisions WHERE lad_code = 'E06000001'"
                           ).fetchone()[0] >= 1
    finally:
        client.close()


@pytest.mark.integration
def test_unified_columns_free_without_weather(tmp_path):
    """Example A's free columns (solar / boundary / bank-holiday) populate from a
    plain stats19 build; temperature_c is entirely NULL because weather wasn't built.
    Passes with or without the [weather] extra."""
    cache = str(tmp_path / "cache")
    _seed_full_cache(cache)
    db = str(tmp_path / "unified_free.duckdb")
    client = crossroads.init_engine(database_path=db, cache_dir=cache)
    client.build(datasets=["stats19"], years=[2023], boundary_mode="snapshot")
    try:
        con = client.con
        assert con.execute("SELECT count(*) FROM collisions WHERE solar_elevation_deg IS NOT NULL"
                           ).fetchone()[0] >= 1
        assert con.execute("SELECT count(*) FROM collisions WHERE lad_code = 'E06000001'"
                           ).fetchone()[0] >= 1
        con.execute("SELECT is_bank_holiday FROM collisions LIMIT 1").fetchone()  # column exists
        assert con.execute("SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL"
                           ).fetchone()[0] == 0    # weather not built -> NULL by design
    finally:
        client.close()
