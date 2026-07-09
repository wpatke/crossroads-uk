"""Offline tests for the ERA5-Land weather source.

These require the [weather] extra (xarray + netCDF4). The module-level importorskip
means the default `pip install -e '.[dev]'` suite SKIPS this file rather than failing,
so weather stays fully optional. Run the weather tests with:

    pip install -e '.[weather]'
    pytest -q tests/test_weather.py
    pytest -m integration -q tests/test_weather.py
"""

import os
import shutil

import pytest

pytest.importorskip("xarray")   # skip the whole module without the [weather] extra

import crossroads
from crossroads.transformers.weather import Era5WeatherTransformer
from crossroads.quality import ensure_quality_tables

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "weather")
SAMPLE_NC = os.path.join(FIXTURES, "era5_land_sample.nc")


def _weather_bronze(con):
    """Seed a synthetic bronze (three grid-cell/hours) so silver can be derived
    without a real .nc file: two land cells and one 'sea' cell (NaN metrics)."""
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    con.execute("INSTALL icu"); con.execute("LOAD icu")
    ensure_quality_tables(con)              # so the geom ledger write path is exercised
    con.execute(
        "CREATE TABLE era5_weather_raw AS SELECT * FROM (VALUES "
        "  (TIMESTAMP '2023-06-15 13:00:00', 54.7, -1.2, 288.15, 0.0010), "   # land (BST)
        "  (TIMESTAMP '2023-06-15 13:00:00', 54.8, -1.2, 289.15, 0.0000), "   # land
        "  (TIMESTAMP '2023-06-15 13:00:00', 55.9, -3.0, CAST('NaN' AS DOUBLE), CAST('NaN' AS DOUBLE)) "  # sea
        ") AS t(valid_time, latitude, longitude, t2m, tp)")


# --- A. Silver derivation on a synthetic bronze (no .nc) — core unit test ---
def test_weather_silver_epsg27700_and_conversions(con):
    _weather_bronze(con)
    Era5WeatherTransformer()._derive_silver_and_ledger(con)

    assert con.execute("SELECT count(*) FROM weather").fetchone()[0] == 3   # keep-in-place

    e = con.execute("SELECT min(ST_X(geom)), max(ST_X(geom)), min(ST_Y(geom)), max(ST_Y(geom)) "
                    "FROM weather WHERE geom IS NOT NULL").fetchone()
    assert 0 <= e[0] and e[1] <= 700_000 and 0 <= e[2] and e[3] <= 1_300_000   # EPSG:27700

    row = con.execute(
        "SELECT temperature_c, precipitation_mm FROM weather "
        "WHERE grid_i = CAST(round(54.7*10) AS INTEGER) AND grid_j = CAST(round(-1.2*10) AS INTEGER)").fetchone()
    assert abs(row[0] - 15.0) < 1e-6 and abs(row[1] - 1.0) < 1e-6           # K->C, m->mm

    local = con.execute("SELECT CAST(valid_time_local AS VARCHAR) FROM weather LIMIT 1").fetchone()[0]
    assert local.startswith("2023-06-15 14:00:00")                          # 13:00 UTC -> 14:00 BST

    sea = con.execute("SELECT temperature_c, precipitation_mm, geom_valid FROM weather "
                      "WHERE grid_i = CAST(round(55.9*10) AS INTEGER)").fetchone()
    assert sea[0] is None and sea[1] is None and sea[2] is True             # NULL by domain, geom valid

    # No geom rejection for these all-coordinate rows.
    assert con.execute("SELECT count(*) FROM data_quality_log "
                       "WHERE source_id='era5_weather'").fetchone()[0] == 0


# --- A2. The real download builds the correct CDS request (no network) ---
def test_download_builds_correct_cds_request(monkeypatch):
    """_download() is the only method that can't run offline (real Copernicus call),
    but its risky part is the request dict, not the network. Monkeypatch the cdsapi
    client to capture the retrieve() arguments and pin the request shape — a typo in
    the dataset name, variables, or format would otherwise fail silently in production."""
    captured = {}

    class FakeClient:
        def retrieve(self, name, request, dest):
            captured.update(name=name, request=request, dest=dest)

    monkeypatch.setattr("cdsapi.Client", lambda *a, **k: FakeClient())

    Era5WeatherTransformer()._download(2023, "/tmp/era5_land_2023.nc")

    assert captured["name"] == "reanalysis-era5-land"
    assert captured["dest"] == "/tmp/era5_land_2023.nc"
    req = captured["request"]
    assert req["variable"] == ["2m_temperature", "total_precipitation"]
    assert req["year"] == "2023"                    # stringified year
    assert req["data_format"] == "netcdf"
    assert req["area"] == [61.0, -8.5, 49.5, 2.0]   # UK bbox [N, W, S, E]
    # Full hourly / all-months / all-days coverage requested.
    assert len(req["month"]) == 12 and len(req["time"]) == 24


# --- B. Fixture has real ERA5-Land structure ---
def test_sample_nc_has_era5land_structure():
    import xarray as xr
    ds = xr.open_dataset(SAMPLE_NC)
    try:
        assert "t2m" in ds.variables and "tp" in ds.variables
        assert ds["t2m"].attrs.get("units") == "K" and ds["tp"].attrs.get("units") == "m"
        assert "latitude" in ds.coords and "longitude" in ds.coords
        assert ("valid_time" in ds.coords) or ("time" in ds.coords)
    finally:
        ds.close()


# --- C. Weather-only offline build — real parse path + invariants (integration) ---
@pytest.mark.integration
def test_weather_only_build_offline(tmp_path):
    cache = str(tmp_path / "cache"); os.makedirs(cache, exist_ok=True)
    shutil.copy(SAMPLE_NC, os.path.join(cache, "era5_land_2023.nc"))   # seed the year filename

    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [Era5WeatherTransformer()]        # weather only this test
    client.build(datasets=["era5_weather"], years=[2023])             # runs §9 invariants
    try:
        nb = client.con.execute("SELECT count(*) FROM era5_weather_raw").fetchone()[0]
        ns = client.con.execute("SELECT count(*) FROM weather").fetchone()[0]
        assert nb > 0 and nb == ns                                    # conservation
        assert client.con.execute("SELECT count(*) FROM weather_clean").fetchone()[0] == \
               client.con.execute("SELECT count(*) FROM weather WHERE geom_valid").fetchone()[0]
        b = client.con.execute("SELECT min(ST_X(geom)), max(ST_X(geom)) FROM weather "
                               "WHERE geom IS NOT NULL").fetchone()
        assert 0 <= b[0] and b[1] <= 700_000
    finally:
        client.close()
