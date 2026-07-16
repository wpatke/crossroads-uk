# Crossroads-UK <img src="https://raw.githubusercontent.com/wpatke/crossroads-uk/main/docs/assets/logo.png" alt="Crossroads-UK" align="right" width="260">

[![tests](https://github.com/wpatke/crossroads-uk/actions/workflows/tests.yml/badge.svg)](https://github.com/wpatke/crossroads-uk/actions/workflows/tests.yml)

A reproducible Python pipeline that downloads, cleanses, and unifies UK road-safety
(DfT STATS19), meteorological (Copernicus ERA5-Land), and ONS boundary data into a
single local DuckDB database, built on the fly from version-controlled code.

Crossroads-UK does not ship a pre-baked database. You choose what to build; the pipeline
fetches the raw public sources and compiles a DuckDB file on your machine, so the result
is fresh, reproducible, and exactly scoped to your query.

## Why Crossroads-UK? What can it do?

**More than road safety.**
Crossroads-UK is a reproducible engine for unifying UK public datasets
into one local, analysis-ready DuckDB database. Road safety is where it starts, not where it stops:
the engine is dataset-agnostic by design, so any UK public source can be added as a new transformer
without touching the core (see [Modular Data Architecture](docs/spec.md#4-modular-data-architecture) in
the spec). The rest of this section shows what that
unification buys a researcher today.

**Join anything to anything.**
Every source lands in one local DuckDB database on standardised keys (ONS
local-authority codes, road numbers, dates, British National Grid coordinates), so you can combine them
in ways no one built a feature for, using ordinary SQL. Does heavy rain shift the severity mix on the
M1? Do collisions spike on bank holidays in one local authority? Which roads see the most crashes per
vehicle-km? Each is just a join you write, and DuckDB runs it locally. See the
[real-world examples](#real-world-examples) below.

**A schema built for fast reads.**
A single collision row carries its weather, its
local authority, its bank-holiday status, and the position of the sun overhead. Those are datasets that
normally take a lot of work to align: gridded weather matched to each crash point, coordinates
geocoded to ONS boundaries. Crossroads does that alignment once, at build time, so what would be a
multi-source join is a single `SELECT`:

```sql
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
```

**Measure risk, not raw counts.**
Traffic-exposure denominators turn collision counts into an
exposure-adjusted rate. See [Example 1: real risk, not raw counts](#example-1-real-risk-not-raw-counts)
below.

### Crossroads-UK is built to be trusted

It doesn't just download data and hand it over. It builds the database on your machine, records
anything it couldn't use instead of silently dropping it, and stamps each build so a colleague can
reproduce it exactly:

- A **keep-in-place** data model (bronze/silver/gold): raw rows are never deleted; records
  that fail validation are flagged with a reason in a queryable `data_quality_log`.
- Spatial standardisation to the British National Grid (EPSG:27700) with R-Tree indices.
- Snapshot or temporally-sliced boundary joins.
- Build-time conservation invariants that halt the build if any row goes unaccounted for.
- DfT AADF traffic volumes, LAD-stamped, enabling per-vehicle-km risk denominators (turn raw
  collision counts into an exposure-adjusted rate).
- A **provenance stamp** on every database (version, schema, build parameters, and build time)
  so a re-run reproduces the result and you can cite it: `SELECT * FROM crossroads_meta;`

The full table/column data dictionary is in **[docs/schema.md](docs/schema.md)**.

See **[docs/methodology.md](docs/methodology.md)** for how the data is joined, converted, and
quality-flagged, and **[docs/spec.md](docs/spec.md)** for the full product definition.

## Install from PyPI

Crossroads-UK is published on PyPI as [`crossroads-uk`](https://pypi.org/project/crossroads-uk/) (requires Python 3.11+):

```bash
pip install crossroads-uk
```

Already installed? Upgrade to the latest release with:

```bash
pip install --upgrade crossroads-uk
```

If you would also like the ERA5-Land weather source (it needs a free Copernicus CDS API key -
the build prints setup steps), run this command instead - it installs everything above, plus the
weather libraries:

```bash
pip install "crossroads-uk[weather]"
```

Then launch the interactive wizard - note the command is `crossroads`, without the `-uk`:

```bash
crossroads
```

## Install from source

```bash
python3 -m venv .venv         # Python 3.11 or newer
source .venv/bin/activate
pip install -e .              # add ".[weather]" for the ERA5-Land weather source
```

## Usage

Run the interactive wizard:

```bash
crossroads
```

It asks for an output database path, which datasets to build, which years to ingest, and
the boundary mode, then compiles the database. Or drive it from Python:

```python
import crossroads as cr

client = cr.init_engine(database_path="local_analytics.db")
client.build(
    datasets=["stats19", "aadf"],   # aadf = DfT traffic counts
    years=[2022, 2023, 2024], boundary_mode="snapshot",
)
client.close()
```

The **weather** source additionally needs the `weather` extra installed and a free
Copernicus CDS API key. The build prints setup steps if it is missing.

## Real-world examples

### Example 1: real risk, not raw counts

Raw collision counts mislead: a busy motorway *looks* dangerous simply because more traffic
means more crashes. Dividing collisions by traffic volume gives **collisions per million
vehicle-km** (an exposure-adjusted rate), and because collisions and AADF count points already
share road names and ONS LAD codes in your database, the join is plain SQL, with no point-to-line
snapping.

```sql
WITH traffic AS (
    SELECT road_name, lad_code,
           SUM(all_motor_vehicles * link_length_km) AS daily_vehicle_km,
           COUNT(DISTINCT count_point_id) AS count_points   -- how many AADF counters back this road
    FROM aadf_clean
    WHERE year = 2023 AND lad_code IS NOT NULL
    GROUP BY road_name, lad_code
),
crashes AS (
    SELECT 'M' || first_road_number AS road_name, lad_code,
           COUNT(*) AS collisions
    FROM collisions_spatial
    WHERE first_road_class = 1 AND first_road_number > 0
          AND lad_code IS NOT NULL
    GROUP BY 1, 2
)
SELECT t.road_name, t.lad_code, c.collisions, t.daily_vehicle_km, t.count_points,
       c.collisions / (t.daily_vehicle_km * 365 / 1e6)    -- * 365: annualise the daily flow to match a full year of collisions
           AS collisions_per_million_vehicle_km
FROM traffic t
JOIN crashes c USING (road_name, lad_code)
ORDER BY collisions_per_million_vehicle_km DESC
```

M1 by local authority from a real 2023 build (`datasets=["stats19", "aadf"]`).

| road | lad_code  | collisions | daily_vehicle_km | count_points | collisions per M vehicle-km |
|------|-----------|-----------:|-----------------:|-------------:|----------------------------:|
| M1 † | E07000096 |         15 |          5,895.2 |            1 |                      6.9711 |
| M1   | E06000056 |         80 |      3,965,864.3 |           17 |                      0.0553 |
| M1   | E07000240 |         55 |      3,047,956.0 |            6 |                      0.0494 |
| M1   | E06000062 |         43 |      4,380,953.0 |            6 |                      0.0269 |
| M1   | E08000018 |         21 |      2,169,706.9 |            5 |                      0.0265 |
| M1   | E09000003 |         18 |        680,304.0 |            4 |                      0.0725 |
| M1   | E08000036 |         17 |      1,357,071.6 |            3 |                      0.0343 |
| M1   | E07000033 |         16 |      2,739,534.6 |            4 |                      0.0160 |
| M1   | E08000035 |         15 |      2,524,444.8 |           11 |                      0.0163 |

† **Why does E07000096 top the list? Is it really dangerous?** What looks like a deadly road is just
a counting mismatch: we counted all 15 crashes along this stretch of the M1, but measured traffic on
only a tiny section of it (AADF has just one count point there). We do not have data for the true
road traffic. A single count point can't tell you a road is dangerous *or* safe.

### Example 2: questions the raw data can't answer

Crossroads can calculate facts beyond what is in the data. For example, the raw STATS19 record tells you
which way each vehicle was pointing but not where the sun was. Crossroads computes the sun's exact
elevation and azimuth for every collision from the crash's own place and time, in-database and with
nothing downloaded (the standard NOAA solar-position math). Put the two together, and you can isolate
collisions where a low sun sat directly ahead of the driver.

```sql
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
```

`sun_offset_deg` is the angle between the sun and the direction of travel. `0` means the sun was
dead ahead. The single most head-on case in a real 2023 national build: a low morning sun 6° above
the horizon, dead in the driver's line of travel.

| accident_index | collision_severity | sun_elevation_deg | sun_offset_deg |
|----------------|-------------------:|------------------:|---------------:|
| 2023311351084  |                  3 |               6.0 |            0.0 |

## Data & licences

Crossroads-UK downloads data directly from DfT, ONS, Copernicus, and gov.uk. You are responsible
for honouring each source's licence when you publish. See **[docs/data-sources.md](docs/data-sources.md)**
for each source, its licence, and the exact attribution to reproduce.

## Citing

If you use Crossroads-UK in research, please cite it. See **[CITATION.cff](CITATION.cff)**
(GitHub's "Cite this repository" button) or the latest release entry in
**[CHANGELOG.md](CHANGELOG.md)**.

## Development

```bash
pip install -e ".[dev]"
python -m pytest                 # fast, offline suite
python -m pytest -m integration  # slow / networked tests (run deliberately)
```

## Licence & AI disclosure

Crossroads-UK is released under the [MIT Licence](LICENSE). This project is **not
affiliated with or endorsed by** the Department for Transport, the Office for National
Statistics, Ordnance Survey, or Copernicus/ECMWF. AI usage in development is documented in
**[docs/ai-disclosure.md](docs/ai-disclosure.md)**.
