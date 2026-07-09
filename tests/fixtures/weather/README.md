# Weather test fixture — `era5_land_sample.nc`

**This file is synthetic.** It is *not* a real Copernicus download. It is a tiny
NetCDF hand-built to imitate the structure of a genuine ERA5-Land hourly download
so the offline weather tests can exercise the real xarray/netCDF parse path with no
network access and no Copernicus credentials.

## Provenance / alignment

The grid cells and hours are derived from the committed STATS19 collision fixture
`tests/fixtures/stats19/dft-road-casualty-statistics-collision-2023.csv`:

- each collision's `longitude`/`latitude` is snapped to its 0.1° ERA5-Land node
  (`round(coord, 1)`), and the node axes are padded by ±one 0.1° cell of margin;
- each collision's **local** timestamp is floored to the hour and converted to the
  **UTC** instant (via `zoneinfo.ZoneInfo("Europe/London")`), so the weather silver's
  derived *local* hour lines up with the collision's local hour.

This guarantees, by construction, that at least one collision falls on a weather
cell + hour — Stage 03's stamping always has something to match.

The cube also contains **exactly one all-NaN "sea" cell** (valid coordinates,
`t2m`/`tp` = NaN) in a margin latitude that no collision maps to. It proves the
transformer keeps sea cells in place with NULL metrics (ERA5-Land is a land model)
without tripping the quality engine's reject ceiling.

Values are deterministic functions of the array indices only — no randomness, no
wall-clock — so regeneration is reproducible.

## Structure verification

The dimension/coordinate names (`valid_time`, `latitude`, `longitude`), the variable
names (`t2m`, `tp`), and their units (`K`, `m`) mirror a real ERA5-Land download. The
Stage-02 test `test_sample_nc_has_era5land_structure` asserts this schema, so the
fixture is pinned to genuine ERA5-Land shape rather than a hand-guess. Verify the
imitated header once against a real Copernicus sample if the CDS output format ever
changes.

## Regenerate / verify

```bash
pip install -e '.[weather]'
python scripts/build_weather_fixture.py            # rewrite this fixture
python scripts/build_weather_fixture.py --check    # exits 0 iff committed == fresh
```

`--check` compares the **decoded** dataset (coords, variable names, units, data
arrays), not the raw bytes — NetCDF embeds library/version metadata, so a byte
compare would be meaningless.

## Imitated `ncdump -h` header

The committed fixture currently expands to the following shape (a small patch over
County Durham, six hours in 2023). The axis lengths grow if the collision fixture
changes; the *structure* below is the contract:

```
netcdf era5_land_sample {
dimensions:
        valid_time = 6 ;
        latitude = 3 ;
        longitude = 3 ;
variables:
        int64 valid_time(valid_time) ;
                valid_time:units = "hours since 2023-09-30 13:00:00" ;
                valid_time:calendar = "proleptic_gregorian" ;
        double latitude(latitude) ;
        double longitude(longitude) ;
        double t2m(valid_time, latitude, longitude) ;
                t2m:units = "K" ;
        double tp(valid_time, latitude, longitude) ;
                tp:units = "m" ;
}
```
