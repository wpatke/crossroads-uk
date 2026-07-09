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

    def extract(self, cache_dir, **kwargs):
        os.makedirs(cache_dir, exist_ok=True)
        self._years = [int(y) for y in (kwargs.get("years") or [])]
        for year in self._years:
            path = os.path.join(cache_dir, self._filename(year))
            if not os.path.exists(path):          # offline-friendly: skip if cached/seeded
                self._download(year, path)

    def _download(self, year, dest):
        """Download one year of ERA5-Land hourly t2m + tp over the UK as NetCDF.

        Real path only (needs a Copernicus ~/.cdsapirc). cdsapi is imported lazily so
        the module and the offline tests do not require the [weather] extra. The file
        is large; it is cached, so a re-run does not re-download."""
        import cdsapi                           # lazy: real download only
        client = cdsapi.Client()
        client.retrieve(
            "reanalysis-era5-land",
            {
                "variable": ["2m_temperature", "total_precipitation"],
                "year": str(year),
                "month": [f"{m:02d}" for m in range(1, 13)],
                "day": [f"{d:02d}" for d in range(1, 32)],
                "time": [f"{h:02d}:00" for h in range(24)],
                "area": UK_AREA,
                "data_format": "netcdf",
            },
            dest,
        )

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
        n = con.execute(f"SELECT count(*) FROM {self.BRONZE}").fetchone()[0]
        record_source_rows(con, self.source_id, n)                    # conservation
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
        for p in nc_paths:
            ds = xr.open_dataset(p)
            tname = "valid_time" if ("valid_time" in ds.variables or "valid_time" in ds.coords) else "time"
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
