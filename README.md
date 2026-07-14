<p align="center">
  <img src="https://raw.githubusercontent.com/wpatke/crossroads-uk/main/docs/assets/logo.png" alt="Crossroads-UK" width="400">
</p>

# Crossroads-UK

[![tests](https://github.com/wpatke/crossroads-uk/actions/workflows/tests.yml/badge.svg)](https://github.com/wpatke/crossroads-uk/actions/workflows/tests.yml)

A reproducible Python pipeline that downloads, cleanses, and unifies UK road-safety
(DfT STATS19), meteorological (Copernicus ERA5-Land), and ONS boundary data into a
single local DuckDB database — built on the fly from version-controlled code.

Crossroads-UK does not ship a pre-baked database. You choose what to build; the pipeline
fetches the raw public sources and compiles a DuckDB file on your machine, so the result
is fresh, reproducible, and exactly scoped to your query.

## Install

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
Copernicus CDS API key — the build prints setup steps if it is missing.

## What you get

- A **keep-in-place** data model (bronze/silver/gold): raw rows are never deleted; records
  that fail validation are flagged with a reason in a queryable `data_quality_log`.
- Spatial standardisation to the British National Grid (EPSG:27700) with R-Tree indices.
- Snapshot or temporally-sliced boundary joins.
- Build-time conservation invariants that halt the build if any row goes unaccounted for.
- DfT AADF traffic volumes, LAD-stamped, enabling per-vehicle-km risk denominators (turn raw
  collision counts into an exposure-adjusted rate).

The full table/column data dictionary is in **[docs/schema.md](docs/schema.md)**.

See **[docs/methodology.md](docs/methodology.md)** for how the data is joined, converted, and
quality-flagged, and **[docs/spec.md](docs/spec.md)** for the full product definition.

## Example: real risk, not raw counts

Raw collision counts mislead: a busy motorway *looks* dangerous simply because more traffic
means more crashes. Dividing collisions by traffic volume gives **collisions per million
vehicle-km** — an exposure-adjusted rate — and because collisions and AADF count points already
share road names and ONS LAD codes in your database, the join is plain SQL, with no point-to-line
snapping.

```sql
WITH traffic AS (
    SELECT road_name, lad_code,
           SUM(all_motor_vehicles * link_length_km) AS daily_vehicle_km
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
SELECT t.road_name, t.lad_code, c.collisions, t.daily_vehicle_km,
       c.collisions / (t.daily_vehicle_km * 365 / 1e6)
           AS collisions_per_million_vehicle_km
FROM traffic t
JOIN crashes c USING (road_name, lad_code)
ORDER BY collisions_per_million_vehicle_km DESC
```

Representative M1 stretches from a real 2023 build (`datasets=["stats19", "aadf"]`), busiest
first (`count_points` is shown for context — it is not a column of the query above):

| road | lad_code  | collisions | daily_vehicle_km | count_points | collisions per M vehicle-km |
|------|-----------|-----------:|-----------------:|-------------:|----------------------------:|
| M1   | E06000056 |         80 |      3,965,864.3 |           17 |                      0.0553 |
| M1   | E07000240 |         55 |      3,047,956.0 |            6 |                      0.0494 |
| M1   | E06000062 |         43 |      4,380,953.0 |            6 |                      0.0269 |
| M1   | E08000018 |         21 |      2,169,706.9 |            5 |                      0.0265 |
| M1   | E09000003 |         18 |        680,304.0 |            4 |                      0.0725 |
| M1   | E08000036 |         17 |      1,357,071.6 |            3 |                      0.0343 |
| M1   | E07000033 |         16 |      2,739,534.6 |            4 |                      0.0160 |
| M1   | E08000035 |         15 |      2,524,444.8 |           11 |                      0.0163 |

Read the rate honestly: it is a road-level figure **within each local authority** (not
per-junction), covers major roads only, includes some DfT-estimated flows (see
`estimation_method`), and matches collisions to counts by recorded road identity — not by
point-to-line snapping — so a district with sparse count-point coverage yields a tiny denominator
and an inflated rate (rank by exposure, not the raw metric), and for cross-year work compare like
years so both sides share the same boundary vintage.

## Data & licences

Crossroads-UK downloads data directly from DfT, ONS, and Copernicus. You are responsible
for honouring each source's licence when you publish. See **[docs/data-sources.md](docs/data-sources.md)**
for each source, its licence, and the exact attribution to reproduce.

## Citing

If you use Crossroads-UK in research, please cite it — see **[CITATION.cff](CITATION.cff)**
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
