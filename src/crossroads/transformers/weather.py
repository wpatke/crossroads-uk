"""Copernicus ERA5-Land gridded reanalysis weather (spec §3B, §5 Phase 4).

A plain data source: it loads its own queryable weather grid table and stamps
nothing. STATS19 optionally consumes the ``weather`` table to annotate collisions
(see docs/plans/006_weather_integration/03), the same way it consumes the boundary
tables — so this module never needs to know about collisions.

ERA5-Land is a LAND reanalysis on a regular 0.1° latitude/longitude grid, hourly,
UTC-native. We ingest 2 m temperature (t2m, Kelvin) and total precipitation (tp,
metres). Cell centroids are reprojected ONCE to EPSG:27700 (spec §3A); the raw UTC
instant is preserved and a UK-local time derived (spec §3B). Sea cells (outside the
land model) carry NULL metrics BY DOMAIN — kept in place, not rejected.

Heavy dependencies (cdsapi, xarray, netCDF4) live in the optional ``[weather]`` extra
and are imported lazily, so this module imports cleanly for discovery without them.
Offline tests seed the cache with a committed synthetic .nc, so extract() downloads
nothing.

Note on ``tp``: ERA5-Land total precipitation is an accumulation in metres. This
transformer takes each published hourly value, converts to millimetres, and stores
it as-is (the spec §5 baseline is "precipitation" with no de-accumulation). This is
a documented simplification.
"""

import os
import zipfile

from crossroads.transformers.base import BaseTransformer
from crossroads.quality import (
    SourceQuality, Dimension, create_clean_view, record_source_rows, log_exclusion,
)

# ERA5-Land native grid step (degrees). The join key is round(coord / GRID_DEG).
GRID_DEG = 0.1

# British National Grid envelope, to verify centroids really are EPSG:27700 in tests.
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000

# UK bounding box for the real cdsapi request [North, West, South, East].
UK_AREA = [61.0, -8.5, 49.5, 2.0]

# Copernicus CDS pointers, surfaced in the friendly errors below when a download
# fails for a setup reason (no API key, or the dataset licence not yet accepted).
CDS_HOME_URL = "https://cds.climate.copernicus.eu"
ERA5_LAND_URL = "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land"


def _missing_key_message(exc):
    """Actionable setup steps for when cdsapi can't find a ~/.cdsapirc / API key.

    cdsapi raises a bare, cryptic exception in this case; we replace it with the
    exact file contents to create and where to get the token.
    """
    return (
        "Weather data needs a free Copernicus CDS API key, which was not found.\n\n"
        "Create the file ~/.cdsapirc containing these two lines:\n\n"
        f"    url: {CDS_HOME_URL}/api\n"
        "    key: <your-personal-access-token>\n\n"
        f"Get the token from your CDS profile ({CDS_HOME_URL} → log in → your\n"
        "profile → 'Personal Access Token'), then accept the ERA5-Land licence once at\n"
        f"    {ERA5_LAND_URL}  (Download tab → Terms of use → Accept)\n"
        "and re-run the build.\n\n"
        f"(underlying cdsapi error: {exc})"
    )


def _licence_message(exc):
    """Actionable steps for a download rejected because the licence isn't accepted."""
    return (
        "Copernicus rejected the ERA5-Land download. This almost always means the\n"
        "dataset licence has not been accepted for your account yet.\n\n"
        "Accept it once (free) at:\n"
        f"    {ERA5_LAND_URL}  (Download tab → Terms of use → Accept)\n"
        "then re-run the build.\n\n"
        f"(underlying cdsapi error: {exc})"
    )


def _too_large_message(exc):
    """Actionable steps when CDS rejects a request as too large (cost limit exceeded)."""
    return (
        "Copernicus rejected the request because it is too large (cost limit exceeded).\n\n"
        "Crossroads already downloads ERA5-Land one month at a time to stay under this\n"
        "limit, so this usually means the requested area or variable list was widened.\n"
        "Reduce the request size and re-run the build.\n\n"
        f"(underlying cdsapi error: {exc})"
    )


def _looks_like_too_large_error(exc):
    """Heuristic: is this CDS failure a cost/size rejection (a 403 that is NOT a licence
    problem)? CDS phrases it as 'cost limits exceeded' / 'request is too large' /
    'reduce your selection'."""
    text = str(exc).lower()
    return any(word in text for word in ("cost limit", "too large", "reduce your selection"))


def _looks_like_licence_error(exc):
    """Heuristic: does this cdsapi failure look like an unaccepted-licence rejection?

    Match on licence wording ONLY. The previous version also matched bare '403' /
    'forbidden', but a 'request too large' rejection is also a 403 — that made this
    function swallow cost/size errors and report them as a licence problem. A cost/size
    error is explicitly excluded here.
    """
    if _looks_like_too_large_error(exc):
        return False
    text = str(exc).lower()
    return any(word in text for word in
               ("licence", "license", "not accepted", "terms of use"))


def _normalize_to_netcdf(path):
    """Ensure `path` is a plain NetCDF file, unwrapping the CDS zip envelope if present.

    The new Copernicus CADS backend delivers ERA5-Land as a ZIP that wraps a single
    NetCDF member (e.g. 'data_0.nc'), even when data_format='netcdf' was requested. Our
    xarray parse path expects a raw .nc, so if `path` is such a zip we replace it in
    place with its inner NetCDF. A genuine .nc (offline test stubs, or a future CADS that
    returns raw netcdf) is not a zip, so this is a no-op for it — and idempotent, so it is
    safe to run again on an already-unwrapped file.

    The replacement is atomic (write a temp, then os.replace), so a crash mid-unwrap
    leaves the original file intact rather than a half-written one."""
    if not zipfile.is_zipfile(path):
        return                                        # already a plain .nc — nothing to do
    with zipfile.ZipFile(path) as zf:
        members = [n for n in zf.namelist() if n.endswith(".nc")]
        if len(members) != 1:
            raise RuntimeError(
                f"expected exactly one .nc inside the CDS zip {path}, found {members}")
        data = zf.read(members[0])
    tmp = path + ".ncpart"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)                             # atomic overwrite: path is now real .nc


class Era5WeatherTransformer(BaseTransformer):
    """Ingests ERA5-Land NetCDF into a weather grid (bronze/silver/gold), audited
    under source_id 'era5_weather'. A plain source — it stamps nothing."""

    source_id = "era5_weather"
    display_name = "weather"                 # friendly wizard-menu label

    BRONZE = "era5_weather_raw"
    SILVER = "weather"
    CLEAN_VIEW = "weather_clean"
    GEOM_RULE = "era5.geom.invalid"

    def is_active(self, **kwargs):
        # Nothing to ingest without a time range; a no-years build skips weather.
        return bool(kwargs.get("years"))

    # --- extract (real download; offline tests pre-seed the cache) ---------
    def _filename(self, year):
        return f"era5_land_{year}.nc"

    def _month_filename(self, year, month):
        return f"era5_land_{year}_{month:02d}.nc"

    @staticmethod
    def _build_request(year, month):
        """The cdsapi request dict for ONE month of ERA5-Land over the UK. Days 1-31 and
        all 24 hours are always requested; CDS returns only the timestamps that exist for
        the month (e.g. February yields 28/29 days), so month length needs no special case."""
        return {
            "variable": ["2m_temperature", "total_precipitation"],
            "year": str(year),
            "month": [f"{month:02d}"],
            "day": [f"{d:02d}" for d in range(1, 32)],
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": UK_AREA,
            "data_format": "netcdf",
        }

    def extract(self, cache_dir, **kwargs):
        os.makedirs(cache_dir, exist_ok=True)
        self._years = [int(y) for y in (kwargs.get("years") or [])]
        for year in self._years:
            path = os.path.join(cache_dir, self._filename(year))
            if not os.path.exists(path):          # offline-friendly: skip if cached/seeded
                self._download(year, path)

    def _download(self, year, dest):
        """Download one year of ERA5-Land as twelve per-month NetCDF files, then merge
        them into `dest` (era5_land_{year}.nc).

        A whole-year request is rejected by CDS as too large (cost limit), so we request
        one month at a time. Each month is cached and written atomically, so an
        interrupted build resumes without re-fetching completed months. cdsapi and xarray
        are imported lazily so the module and the offline tests do not require the
        [weather] extra."""
        import cdsapi                                   # lazy: real download only
        cache_dir = os.path.dirname(dest)

        # cdsapi raises a bare, cryptic exception when it can't find the API key
        # (no ~/.cdsapirc and no CDSAPI_* env vars). Translate it into setup steps.
        try:
            client = cdsapi.Client()
        except Exception as exc:
            raise RuntimeError(_missing_key_message(exc)) from exc

        month_paths = []
        for month in range(1, 13):
            mpath = os.path.join(cache_dir, self._month_filename(year, month))
            if not os.path.exists(mpath):               # RESUME: skip completed months
                self._download_month(client, year, month, mpath)
            month_paths.append(mpath)

        self._merge_months(month_paths, dest)           # writes dest atomically
        for mpath in month_paths:                        # cleanup: yearly file is the cache key now
            os.remove(mpath)

    def _download_month(self, client, year, month, dest):
        """Retrieve ONE month to `dest`, written atomically: download to a '.part' temp
        file and rename to the final name only on success. A killed download therefore
        never leaves a partial .nc that the resume check (os.path.exists) would mistake
        for a complete month."""
        tmp = dest + ".part"
        try:
            client.retrieve("reanalysis-era5-land", self._build_request(year, month), tmp)
        except Exception as exc:
            if os.path.exists(tmp):
                os.remove(tmp)                       # never leave a partial temp behind
            # Give targeted help for the two common post-auth failures; otherwise let the
            # real error through unchanged (network, disk, etc. are self-explanatory).
            if _looks_like_licence_error(exc):
                raise RuntimeError(_licence_message(exc)) from exc
            if _looks_like_too_large_error(exc):
                raise RuntimeError(_too_large_message(exc)) from exc
            raise
        _normalize_to_netcdf(tmp)                     # unwrap the CDS zip envelope if present
        os.rename(tmp, dest)                          # atomic promote on success

    def _merge_months(self, month_paths, dest):
        """Concatenate the monthly NetCDF files into a single yearly file at `dest`,
        sorted on the time axis, written atomically. Eager xarray only (no dask, so no
        open_mfdataset). Tolerates the 'valid_time' or 'time' coordinate name across CDS
        vintages, the same way _load_bronze does."""
        import xarray as xr                            # lazy: real download/merge only
        datasets = [xr.open_dataset(p) for p in sorted(month_paths)]
        tmp = dest + ".part"
        try:
            first = datasets[0]
            tname = "valid_time" if ("valid_time" in first.variables
                                     or "valid_time" in first.coords) else "time"
            combined = xr.concat(datasets, dim=tname).sortby(tname)
            try:
                combined.to_netcdf(tmp)
            finally:
                combined.close()
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)                          # do not leave a partial merge
            raise
        finally:
            for ds in datasets:
                ds.close()
        os.rename(tmp, dest)                            # atomic promote on success

    # --- transform_and_load ------------------------------------------------
    def transform_and_load(self, con, cache_dir):
        years = getattr(self, "_years", None) or []
        if not years:
            return                              # defensive: is_active gates on years

        con.execute("INSTALL icu"); con.execute("LOAD icu")   # UTC -> Europe/London below

        paths = [os.path.join(cache_dir, self._filename(y)) for y in years]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            raise FileNotFoundError(
                f"[era5_weather] no cached NetCDF for years {years}; extract() must run "
                f"first (or seed the cache in tests).")

        self._load_bronze(con, paths)
        self._derive_silver_and_ledger(con)
        # CONSERVATION: source_rows is the grid-cell total counted INDEPENDENTLY from the
        # NetCDF dimensions (see _load_bronze), not count(bronze), so a dropped cell in the
        # xarray->dataframe materialisation would fail the invariant. NetCDF is read
        # all-or-nothing (no per-row reject concept), so there is no quarantine path here.
        record_source_rows(con, self.source_id, self._source_cell_count)
        create_clean_view(con, self.CLEAN_VIEW, self.SILVER, ["geom_valid"])   # gold

    def _load_bronze(self, con, nc_paths):
        """Faithful bronze: one row per (grid cell, hour). Parse each .nc with xarray
        (lazy) into a long DataFrame (valid_time, latitude, longitude, t2m, tp),
        register it, and CREATE OR REPLACE the bronze table. Values are not edited here
        (that is silver's job). Tolerates the 'valid_time' or 'time' coordinate name
        across CDS vintages."""
        import xarray as xr                      # lazy
        import pandas as pd                      # lazy (xarray dependency)
        frames = []
        self._source_cell_count = 0              # independent conservation count (grid cells)
        for p in nc_paths:
            ds = xr.open_dataset(p)
            tname = "valid_time" if ("valid_time" in ds.variables or "valid_time" in ds.coords) else "time"
            # One bronze row per grid cell = the product of t2m's dimensions. Counted from
            # the dataset dims (not len(df)) so it is an independent check of the load.
            cells = 1
            for d in ds["t2m"].dims:
                cells *= ds.sizes[d]
            self._source_cell_count += cells
            df = ds[["t2m", "tp"]].to_dataframe().reset_index()
            df = df.rename(columns={tname: "valid_time"})
            frames.append(df[["valid_time", "latitude", "longitude", "t2m", "tp"]])
            ds.close()
        bronze_df = pd.concat(frames, ignore_index=True)
        con.register("era5_bronze_df", bronze_df)
        try:
            con.execute(
                f"CREATE OR REPLACE TABLE {self.BRONZE} AS SELECT * FROM era5_bronze_df")
        finally:
            con.unregister("era5_bronze_df")

    def _derive_silver_and_ledger(self, con):
        """Keep-in-place silver, 1:1 with bronze. Factored out so tests can drive it
        against a synthetic bronze without a .nc file.

          • source_row_key  = grid_i|grid_j|YYYYMMDDHH (deterministic, unique per cell-hour)
          • geom            = EPSG:27700 centroid via ST_Transform(..., always_xy := true)
          • geom_valid      = coordinates present (a centroid always reprojects)
          • valid_time_utc  = raw naive UTC instant
          • valid_time_local= (utc AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London' (naive local)
          • temperature_c   = t2m - 273.15   (Kelvin -> Celsius; NULL/NaN -> NULL)
          • precipitation_mm= tp * 1000       (metres -> millimetres; NULL/NaN -> NULL)
          • grid_i/grid_j   = round(lat/lon * 10) integer grid index (STATS19's join key)

        Sea/NaN cells (ERA5-Land is a LAND model) keep NULL metrics BY DOMAIN — not a
        reject dimension. The only audited dimension is geom (like spatial.py).

        Missing metrics arrive two ways: as SQL NULL (DuckDB coerces a pandas NaN from
        the real .nc parse to NULL) or as a genuine floating-point NaN (e.g. a
        'NaN'::DOUBLE bronze in a unit test). Both must land as NULL, so the CASE guards
        IS NULL OR isnan(...) — a NaN temperature/precipitation is not a real value."""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.SILVER} AS "
            f"WITH typed AS ("
            f"  SELECT "
            f"    CAST(latitude  AS DOUBLE) AS latitude, "
            f"    CAST(longitude AS DOUBLE) AS longitude, "
            f"    CAST(valid_time AS TIMESTAMP) AS valid_time_utc, "
            f"    TRY_CAST(t2m AS DOUBLE) AS t2m_k, "
            f"    TRY_CAST(tp  AS DOUBLE) AS tp_m "
            f"  FROM {self.BRONZE}"
            f") "
            f"SELECT "
            f"  CAST(round(latitude * 10)  AS INTEGER) || '|' || "
            f"  CAST(round(longitude * 10) AS INTEGER) || '|' || "
            f"  strftime(valid_time_utc, '%Y%m%d%H') AS source_row_key, "
            f"  latitude, longitude, "
            f"  CAST(round(latitude * 10)  AS INTEGER) AS grid_i, "
            f"  CAST(round(longitude * 10) AS INTEGER) AS grid_j, "
            f"  ST_Transform(ST_Point(longitude, latitude), 'EPSG:4326', 'EPSG:27700', "
            f"               always_xy := true)::GEOMETRY AS geom, "
            f"  (latitude IS NOT NULL AND longitude IS NOT NULL) AS geom_valid, "
            f"  valid_time_utc, "
            f"  (valid_time_utc AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London' AS valid_time_local, "
            f"  CASE WHEN t2m_k IS NULL OR isnan(t2m_k) THEN NULL ELSE t2m_k - 273.15 END AS temperature_c, "
            f"  CASE WHEN tp_m  IS NULL OR isnan(tp_m)  THEN NULL ELSE tp_m  * 1000.0 END AS precipitation_mm "
            f"FROM typed"
        )
        # LEDGER: one reject_dimension row per geom_valid = FALSE, so flag/ledger
        # agreement holds. A missing coordinate is the only geom rejection.
        bad = con.execute(
            f"SELECT source_row_key FROM {self.SILVER} WHERE geom_valid = FALSE").fetchall()
        for (key,) in bad:
            log_exclusion(
                con, source_id=self.source_id, source_row_key=key,
                column_name="geom", rule_id=self.GEOM_RULE,
                rule_desc="grid cell latitude/longitude missing",
                severity="reject_dimension", raw_value=None)

    def quality_spec(self):
        # Audited like a boundary layer: geometry validity is the single dimension.
        # Missing land-model metrics over sea are NULL by domain, NOT a reject.
        return SourceQuality(
            source_id=self.source_id,
            bronze_table=self.BRONZE,
            silver_table=self.SILVER,
            dimensions=(Dimension("geom", "geom_valid", (self.GEOM_RULE,)),),
            key_column="source_row_key",
        )
