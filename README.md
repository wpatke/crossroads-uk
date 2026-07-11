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
client.build(datasets=["stats19"], years=[2022, 2023, 2024], boundary_mode="snapshot")
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

The full table/column data dictionary is in **[docs/schema.md](docs/schema.md)**.

See **[docs/methodology.md](docs/methodology.md)** for how the data is joined, converted, and
quality-flagged, and **[docs/spec.md](docs/spec.md)** for the full product definition.

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
