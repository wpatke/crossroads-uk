"""Build the synthetic ERA5-Land NetCDF test fixture.

This is a developer tool (committed, but NOT shipped in the wheel and NOT run at
build/test time — mirrors scripts/build_stats19_codebook.py). It writes a tiny
NetCDF file shaped exactly like a real Copernicus ERA5-Land download, so the
offline weather tests can parse a genuine .nc without any network or credentials.

The fixture is deliberately aligned to the committed STATS19 collision fixture:
the grid cells and hours are chosen so that at least one collision falls on a
weather cell + hour, guaranteeing Stage 03's stamping has something to match.

Usage:
    python scripts/build_weather_fixture.py            # (re)writes the fixture
    python scripts/build_weather_fixture.py --check    # verify committed == fresh

Requires the [weather] extra (xarray, netCDF4). Run: pip install -e '.[weather]'
"""

import csv
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import xarray as xr

# Repo-relative paths (this file lives in scripts/).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
COLLISION_CSV = os.path.join(
    _REPO, "tests", "fixtures", "stats19",
    "dft-road-casualty-statistics-collision-2023.csv")
FIXTURE_DIR = os.path.join(_REPO, "tests", "fixtures", "weather")
FIXTURE_NC = os.path.join(FIXTURE_DIR, "era5_land_sample.nc")

# How many collision rows to align to. A handful is plenty to seed the grid.
N_COLLISIONS = 6

UTC = ZoneInfo("UTC")
LONDON = ZoneInfo("Europe/London")


def _collision_cells():
    """Read the collision fixture and return the (lat0, lon0, utc_hour) triples the
    weather grid must cover.

      • lat0/lon0 = the 0.1° ERA5-Land node the collision sits in (round to 1 decimal).
      • utc_hour  = the collision's LOCAL time floored to the hour, converted to the
                    UTC instant. Storing this as the weather row's UTC time makes the
                    weather silver's derived LOCAL hour match the collision's local hour.
    """
    triples = []
    with open(COLLISION_CSV, newline="") as f:
        for row in csv.DictReader(f):
            if len(triples) >= N_COLLISIONS:
                break
            try:
                lon = float(row["longitude"])
                lat = float(row["latitude"])
                # STATS19 dates are DD/MM/YYYY with HH:MM times.
                local = datetime.strptime(
                    f"{row['date']} {row['time']}", "%d/%m/%Y %H:%M")
            except (ValueError, KeyError):
                continue  # skip rows with missing/unparseable coordinates or timestamps
            local_hour = local.replace(minute=0, second=0, microsecond=0, tzinfo=LONDON)
            utc_hour = local_hour.astimezone(UTC).replace(tzinfo=None)  # naive UTC
            triples.append((round(lat, 1), round(lon, 1), utc_hour))
    return triples


def _build_dataset():
    """Assemble the synthetic ERA5-Land dataset: a dense (time x lat x lon) cube whose
    axes cover every collision triple, plus one all-NaN 'sea' cell."""
    triples = _collision_cells()

    # Axes: the distinct collision nodes, each padded by +-one 0.1deg cell of margin so
    # the grid looks like a real regular patch rather than a single point.
    lat_nodes = sorted({t[0] for t in triples})
    lon_nodes = sorted({t[1] for t in triples})
    lats = sorted({round(v, 1) for n in lat_nodes for v in (n - 0.1, n, n + 0.1)})
    lons = sorted({round(v, 1) for n in lon_nodes for v in (n - 0.1, n, n + 0.1)})
    times = sorted({t[2] for t in triples})

    lats = np.array(lats, dtype="float64")
    lons = np.array(lons, dtype="float64")
    time_arr = np.array(times, dtype="datetime64[ns]")

    # Deterministic values from indices only (no randomness, no wall-clock).
    nt, ny, nx = len(time_arr), len(lats), len(lons)
    t2m = np.empty((nt, ny, nx), dtype="float64")
    tp = np.empty((nt, ny, nx), dtype="float64")
    for h in range(nt):
        for i in range(ny):
            for j in range(nx):
                t2m[h, i, j] = 285.0 + 0.1 * i - 0.05 * j   # Kelvin
                tp[h, i, j] = 0.0005 * (h + 1)              # metres

    # Exactly one all-NaN 'sea' cell (valid coordinates, missing metrics). It lives in
    # the margin latitude (never a collision node), so it never steals a real match.
    sea_i = 0                     # lats[0] is a margin row (below every collision node)
    t2m[0, sea_i, 0] = np.nan
    tp[0, sea_i, 0] = np.nan

    ds = xr.Dataset(
        data_vars={
            "t2m": (("valid_time", "latitude", "longitude"), t2m, {"units": "K"}),
            "tp": (("valid_time", "latitude", "longitude"), tp, {"units": "m"}),
        },
        coords={
            "valid_time": ("valid_time", time_arr),
            "latitude": ("latitude", lats),
            "longitude": ("longitude", lons),
        },
    )
    return ds


def _write(path):
    ds = _build_dataset()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds.to_netcdf(path)
    ds.close()


def _decoded_equal(path_a, path_b):
    """Compare two NetCDF files by their DECODED contents (coords, variable names,
    units, data arrays) — not their bytes (NetCDF embeds library/version metadata,
    so a byte compare is meaningless). Returns (ok, message)."""
    a = xr.open_dataset(path_a)
    b = xr.open_dataset(path_b)
    try:
        if set(a.data_vars) != set(b.data_vars):
            return False, f"variables differ: {set(a.data_vars)} != {set(b.data_vars)}"
        if set(a.coords) != set(b.coords):
            return False, f"coords differ: {set(a.coords)} != {set(b.coords)}"
        for name in a.data_vars:
            if a[name].attrs.get("units") != b[name].attrs.get("units"):
                return False, f"{name} units differ"
            # equal_nan so the sea cell (NaN in the same place) counts as equal.
            if not np.array_equal(a[name].values, b[name].values, equal_nan=True):
                return False, f"{name} data differ"
        for name in a.coords:
            if not np.array_equal(a[name].values, b[name].values):
                return False, f"coord {name} differs"
        return True, "decoded datasets match"
    finally:
        a.close()
        b.close()


def main(argv):
    if "--check" in argv:
        if not os.path.exists(FIXTURE_NC):
            print(f"[FAIL] committed fixture missing: {FIXTURE_NC}")
            return 1
        with tempfile.TemporaryDirectory() as tmp:
            fresh = os.path.join(tmp, "fresh.nc")
            _write(fresh)
            ok, msg = _decoded_equal(FIXTURE_NC, fresh)
        print(("[OK] " if ok else "[FAIL] ") + msg)
        return 0 if ok else 1

    _write(FIXTURE_NC)
    print(f"[OK] wrote {FIXTURE_NC}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
