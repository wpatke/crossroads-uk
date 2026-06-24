# Crossroads-UK

A reproducible Python pipeline that downloads, cleanses, and unifies UK road-safety
(DfT Stats19), meteorological (ERA5-Land), and ONS boundary data into a single local
DuckDB database — built on the fly from version-controlled code.

See [`docs/spec.md`](docs/spec.md) for the full product definition.

> **Status:** early foundation. Installation and usage documentation will be expanded
> as the pipeline's data sources come online.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```
