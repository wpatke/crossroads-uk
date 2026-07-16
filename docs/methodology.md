# Methodology

How Crossroads-UK turns raw public datasets into a single analysable database. This is a
summary for researchers; the authoritative detail lives in [spec.md](spec.md).

See [schema.md](schema.md) for the table-and-column data dictionary.

## Sources

| Source | Publisher | Native format | Native CRS / time |
|--------|-----------|---------------|-------------------|
| STATS19 collisions/vehicles/casualties | DfT | CSV | EPSG:27700 / UK local time |
| LAD & CTYUA boundaries | ONS | Shapefile (BGC) | EPSG:27700 |
| ERA5-Land weather | Copernicus | NetCDF | EPSG:4326 / UTC, hourly |

## Spatial join

All geometries are reprojected **once at ingestion** to the British National Grid
(EPSG:27700) and never at query time; R-Tree indices are built on disk. Collision points
are matched to boundary polygons by point-in-polygon. Coordinate sentinels (`0`/`-1`,
DfT's "data missing or out of range") set `geom = NULL` and `geom_valid = FALSE` and are
logged — never deleted. Detail: [spec.md §3A](spec.md), [spec.md §5](spec.md).

## Temporal alignment

Every record carries a `*_local` column in UK civil time (`Europe/London`). Sources that
natively record a true instant (ERA5-Land, UTC) also carry `*_utc`; a `*_utc` is never
reconstructed from local time. Weather is matched to collisions at the hourly grain.
Detail: [spec.md §3B](spec.md).

## Solar geometry (the glare vector)

Every collision is stamped with the sun's position at its exact place and time:
`solar_elevation_deg` (apparent elevation above the horizon, refraction-corrected; negative
means the sun is below the horizon) and `solar_azimuth_deg` (clockwise from true north:
0 = N, 90 = E, 180 = S, 270 = W). Both are computed **mathematically** with the standard NOAA
solar-position algorithm implemented in SQL — no download and no new dependency. The inputs are
the collision's `geom` (reprojected to lon/lat) and the instant derived from `datetime_local`
(the `Europe/London` civil time interpreted via ICU). Consistent with [spec.md §2](spec.md), only
the resulting angles are stored — no `*_utc` column is reconstructed for this local-native source;
the single autumn DST fall-back hour is resolved deterministically by ICU (and the sun is below
the horizon during it anyway). Angles are `NULL` exactly when `geom` or `datetime_local` is
missing, inheriting the existing `geom_valid` / `datetime_valid` audit flags.

This surfaces a lethal lighting condition DfT's subjective "Light Conditions" field misses —
direct low-angle solar glare. Example: blinding low morning sun on (roughly) east-facing travel,
in clear weather:

```sql
SELECT count(*) FROM collisions_spatial
WHERE solar_elevation_deg BETWEEN 0 AND 15      -- low sun, above the horizon
  AND solar_azimuth_deg   BETWEEN 45 AND 135     -- sun in the eastern sky (morning)
  AND weather_conditions = 1;                    -- DfT "Fine no high winds" (clear)
```

## Weather value handling

ERA5-Land 2 m temperature (Kelvin) and total precipitation (metres, an hourly
accumulation) are ingested; precipitation is converted to millimetres and stored as
published (no de-accumulation — a documented simplification). Sea cells outside the land
model carry `NULL` metrics by domain, kept in place. Detail:
[spec.md §5 Phase 4](spec.md).

## Boundary drift

Two modes: **snapshot** evaluates every event against the latest ONS boundaries;
**temporal** appends `valid_from`/`valid_to` so an event maps to the boundaries that
existed on its date. Detail: [spec.md §3C](spec.md).

## Data quality (keep-in-place)

No source row is ever deleted. Bad values are nulled in the typed "silver" columns, the
raw value is preserved, a boolean flag records the failure, and a `data_quality_log` row
explains it. A CSV line so broken it cannot be structured at all (wrong column count) is
written verbatim to `quarantine_raw` and the build continues — it is recorded, not dropped,
and never crashes the build. "Gold" views filter to valid-only. Four invariants are
asserted on every build — conservation (`source == clean + quarantined`, with `source`
counted independently of the loaded rows), flag/ledger agreement, a reject-rate ceiling,
and a quarantine-rate ceiling — and the build halts if any row is unaccounted for. Full
model: [spec.md §9](spec.md).

## Reproducibility

A given Crossroads-UK version, with the same parameters and the same pinned source
vintages, produces a structurally identical database. Reference tables (STATS19 codebook,
column manifest, ONS boundary manifest) are version-pinned and regenerable by committed
scripts. See [../src/crossroads/reference/README.md](../src/crossroads/reference/README.md).

### Tested with (v1.0.0)

Reproducibility depends on the runtime stack, and `duckdb>=1.5` is a floating floor. To
reproduce the exact `1.0.0` behaviour (notably coordinate reprojection, which rides on
DuckDB Spatial + PROJ), pin these versions:

| Component | Tested version             |
|-----------|----------------------------|
| Python | 3.11 (authored on 3.12.13) |
| DuckDB | 1.5.4                      |
| xarray (weather extra) | 2026.4.0                   |
| cdsapi (weather extra) | 0.7.7                      |
| netCDF4 (weather extra) | 1.7.4                      |

Any change to these — or to ingestion behaviour — is a new release (see
[../CHANGELOG.md](../CHANGELOG.md)).
