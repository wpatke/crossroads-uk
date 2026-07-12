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
from crossroads.transformers.weather import (
    Era5WeatherTransformer, _looks_like_licence_error, _looks_like_too_large_error,
    _normalize_to_netcdf,
)
from crossroads.quality import ensure_quality_tables

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "weather")
SAMPLE_NC = os.path.join(FIXTURES, "era5_land_sample.nc")


def _write_stub_month_nc(path, year, month, n_hours=3):
    """Write a minimal ERA5-Land-shaped NetCDF for one month: a 2x2 grid, `n_hours`
    consecutive hours starting at midnight UTC on the 1st. Deterministic (values derived
    from indices), no network, no randomness."""
    import numpy as np
    import pandas as pd
    import xarray as xr
    times = pd.date_range(f"{year}-{month:02d}-01 00:00", periods=n_hours, freq="h")
    lat = [54.7, 54.8]
    lon = [-1.2, -1.1]
    shape = (n_hours, len(lat), len(lon))
    t2m = np.full(shape, 288.15)                 # 15 C, in Kelvin
    tp = np.full(shape, 0.001)                   # 1 mm, in metres
    ds = xr.Dataset(
        {"t2m": (("valid_time", "latitude", "longitude"), t2m),
         "tp":  (("valid_time", "latitude", "longitude"), tp)},
        coords={"valid_time": times, "latitude": lat, "longitude": lon},
    )
    ds["t2m"].attrs["units"] = "K"
    ds["tp"].attrs["units"] = "m"
    ds.to_netcdf(path)
    ds.close()


def _write_stub_month_zip(path, year, month, n_hours=3):
    """Write a stub month in the ZIP-wrapped form the real Copernicus CADS backend
    delivers: a .zip (named .nc) containing a single 'data_0.nc' member. This is what
    the live service returns for data_format='netcdf', so tests that use it exercise the
    zip-unwrap path (a plain .nc stub would not)."""
    import zipfile
    inner = path + ".inner.nc"
    _write_stub_month_nc(inner, year, month, n_hours=n_hours)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(inner, arcname="data_0.nc")
    os.remove(inner)


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


# --- A2. Per-month request shape (no network) ---
def test_build_request_shape():
    req = Era5WeatherTransformer._build_request(2022, 2)
    assert req["variable"] == ["2m_temperature", "total_precipitation"]
    assert req["year"] == "2022"
    assert req["month"] == ["02"]                        # single, zero-padded month
    assert len(req["day"]) == 31 and len(req["time"]) == 24
    assert req["area"] == [61.0, -8.5, 49.5, 2.0]
    assert req["data_format"] == "netcdf"


# --- A3. A full year downloads as 12 monthly requests, merged into one file ---
@pytest.mark.integration
def test_download_year_chunks_and_merges(tmp_path, monkeypatch):
    cache = str(tmp_path); calls = []

    class FakeClient:
        def retrieve(self, name, request, target):
            assert name == "reanalysis-era5-land"
            (month,) = request["month"]                 # one month per call
            calls.append(month)
            # Deliver the ZIP-wrapped form the real CADS backend returns, so this
            # end-to-end test exercises the zip-unwrap that _download performs.
            _write_stub_month_zip(target, 2022, int(month), n_hours=3)

    monkeypatch.setattr("cdsapi.Client", lambda *a, **k: FakeClient())

    dest = os.path.join(cache, "era5_land_2022.nc")
    Era5WeatherTransformer()._download(2022, dest)

    # 12 calls, one per month, in order
    assert calls == [f"{m:02d}" for m in range(1, 13)]
    # yearly merged file exists; monthly parts cleaned up; no leftover .part files
    assert os.path.exists(dest)
    assert not any(f.endswith(".part") for f in os.listdir(cache))
    assert not any(f.startswith("era5_land_2022_") for f in os.listdir(cache))

    import xarray as xr
    ds = xr.open_dataset(dest)
    try:
        tname = "valid_time" if "valid_time" in ds.coords else "time"
        times = ds[tname].values
        assert len(times) == 12 * 3                     # all months merged
        assert (times[:-1] <= times[1:]).all()          # sorted, no reordering
        assert len(set(times.tolist())) == len(times)   # no duplicate hours
    finally:
        ds.close()


# --- A3b. The CDS zip envelope is unwrapped to a plain, readable NetCDF ---
def test_normalize_unwraps_cds_zip(tmp_path):
    import xarray as xr
    zpath = os.path.join(str(tmp_path), "era5_land_2022_01.nc")
    _write_stub_month_zip(zpath, 2022, 1, n_hours=4)

    import zipfile
    assert zipfile.is_zipfile(zpath)                    # starts life as a CDS-style zip
    _normalize_to_netcdf(zpath)
    assert not zipfile.is_zipfile(zpath)                # now a real .nc, in place

    ds = xr.open_dataset(zpath)                          # xarray can open it (the bug case)
    try:
        assert "t2m" in ds.variables and "tp" in ds.variables
        tname = "valid_time" if "valid_time" in ds.coords else "time"
        assert len(ds[tname]) == 4
    finally:
        ds.close()

    # Idempotent + no-op on an already-plain .nc: a second call must not corrupt it.
    _normalize_to_netcdf(zpath)
    ds = xr.open_dataset(zpath); ds.close()             # still opens fine


# --- A4. Resume: completed months are not re-fetched ---
@pytest.mark.integration
def test_download_resumes_completed_months(tmp_path, monkeypatch):
    cache = str(tmp_path); calls = []
    # Pre-seed months 01..06 as if a previous run had completed them.
    for m in range(1, 7):
        _write_stub_month_nc(os.path.join(cache, f"era5_land_2022_{m:02d}.nc"), 2022, m)

    class FakeClient:
        def retrieve(self, name, request, target):
            (month,) = request["month"]; calls.append(month)
            _write_stub_month_nc(target, 2022, int(month), n_hours=3)

    monkeypatch.setattr("cdsapi.Client", lambda *a, **k: FakeClient())
    Era5WeatherTransformer()._download(2022, os.path.join(cache, "era5_land_2022.nc"))

    assert calls == [f"{m:02d}" for m in range(7, 13)]   # only the missing half fetched


# --- A5. Atomic write: a failed download leaves no partial file ---
def test_download_month_atomic_on_failure(tmp_path):
    dest = os.path.join(str(tmp_path), "era5_land_2022_02.nc")

    class FailingClient:
        def retrieve(self, name, request, target):
            with open(target, "w") as fh:               # simulate a partial write...
                fh.write("partial")
            raise RuntimeError("network died mid-download")

    with pytest.raises(RuntimeError, match="network died"):
        Era5WeatherTransformer()._download_month(FailingClient(), 2022, 2, dest)

    assert not os.path.exists(dest)                      # final name never created
    assert not os.path.exists(dest + ".part")           # partial temp cleaned up


# --- A6. Error classification: "too large" is not misreported as a licence error ---
def test_too_large_not_misclassified_as_licence():
    too_large = RuntimeError(
        "403 Client Error: Forbidden ... cost limits exceeded "
        "Your request is too large, please reduce your selection.")
    licence = RuntimeError(
        "403 Client Error: Forbidden ... required licences not accepted; "
        "accept the terms of use")
    assert _looks_like_too_large_error(too_large) is True
    assert _looks_like_licence_error(too_large) is False    # the bug being fixed
    assert _looks_like_licence_error(licence) is True        # genuine licence still caught


# --- A7. Live smoke test: real CDS month-boundary concat (opt-in, skipped by default) ---
_LIVE = pytest.mark.skipif(
    not (os.path.exists(os.path.expanduser("~/.cdsapirc"))
         and os.environ.get("CROSSROADS_RUN_LIVE")),
    reason="live CDS test: set CROSSROADS_RUN_LIVE=1 and provide ~/.cdsapirc to run")


@pytest.mark.live
@_LIVE
def test_live_two_month_download_and_merge(tmp_path):
    """The one thing the synthetic fixture cannot prove: that a REAL CDS monthly download
    parses, and that concatenating two adjacent real months yields no duplicate or missing
    hours at the month boundary. Run manually once:
        CROSSROADS_RUN_LIVE=1 pytest -m live -q tests/test_weather.py
    """
    import xarray as xr
    tx = Era5WeatherTransformer()
    import cdsapi
    client = cdsapi.Client()
    p1 = os.path.join(str(tmp_path), "era5_land_2022_01.nc")
    p2 = os.path.join(str(tmp_path), "era5_land_2022_02.nc")
    tx._download_month(client, 2022, 1, p1)
    tx._download_month(client, 2022, 2, p2)
    dest = os.path.join(str(tmp_path), "era5_land_2022.nc")
    tx._merge_months([p1, p2], dest)

    ds = xr.open_dataset(dest)
    try:
        tname = "valid_time" if "valid_time" in ds.coords else "time"
        times = ds[tname].values
        assert (times[:-1] < times[1:]).all()           # strictly increasing: no dup, sorted
        assert len(times) == (31 + 28) * 24             # Jan + Feb 2022 hourly, complete
        assert "t2m" in ds.variables and "tp" in ds.variables
    finally:
        ds.close()


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
