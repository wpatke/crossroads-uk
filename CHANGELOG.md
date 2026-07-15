# Changelog

All notable changes to Crossroads-UK are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), read for this project as:

- **MAJOR** — a breaking change to the stable contract (a removed/renamed database column or
  table, or a CLI/`init_engine`/`build` API break). Stays `1` while schema changes are additive.
  Bumped by hand, by cutting a new GitHub Release (a git tag such as `v2.0.0`).
- **MINOR** — additive schema/feature changes: a new datasource, column, or table. Bumped by hand,
  by cutting a GitHub Release such as `v1.1.0`.
- **Build identity** (automatic, dev builds only) — set by `hatch-vcs` from git. A release (git tag)
  is a clean number such as `1.1.0`; any commit after it reads e.g. `1.1.1.dev3+g1a2b3c4` — the
  commit distance plus the exact git hash, so a dev build always pins its exact source. Never
  hand-edited.

The physical database shape also carries its own monotonic `schema_version` integer in the
`crossroads_meta` table. Because reproducibility depends on the runtime stack, **any change to
declared dependencies or ingestion behaviour is a release** and is recorded here; the versions each
release was tested against are listed in [docs/methodology.md](docs/methodology.md).

## [1.0.0] - 2026-07-15

First stable release, and the first published to PyPI (`pip install crossroads-uk`). The
DuckDB schema and public API (`crossroads` CLI, `init_engine`, `build`) are now the stable
contract described above; subsequent changes follow the MAJOR/MINOR policy at the top of
this file.

### Added
- `solar_elevation_deg` and `solar_azimuth_deg` on the `collisions` table — the sun's apparent
  elevation and azimuth at each collision's place and time, computed mathematically (NOAA solar
  position algorithm, in SQL) from `geom` and `datetime_local`, with no external data or new
  dependency. Enables isolating low-angle solar glare as a casualty factor.
- DfT AADF traffic-count source (`aadf` dataset): national count-point volumes 2000-onward,
  LAD/CTYUA-stamped honouring `boundary_mode`, with an `aadf_clean` gold view and an R-Tree
  index on the count-point geometry. Adds a README risk showcase (collisions per million
  vehicle-km) and a wizard temporal-mode warning when traffic counts are built in temporal mode.

### Changed
- Database `schema_version` 1 → 2 (additive: the two `collisions` columns above).
- Database `schema_version` 2 → 3 (additive: the `aadf`/`aadf_clean`/`aadf_raw` tables above).

## [0.9.0] - 2026-07-10

First public release. **Pre-1.0 (Beta):** the pipeline is usable and reproducible, but the
DuckDB schema and public API are not yet frozen — they may still change before `1.0.0`. The
stable-contract guarantees described above take effect at `1.0.0`.

### Added
- Reproducible build pipeline unifying DfT STATS19 road-safety data, Copernicus
  ERA5-Land weather, and ONS boundaries into a single local DuckDB database.
- Keep-in-place data-quality model (bronze/silver/gold) with a queryable
  `data_quality_log` exclusion ledger and build-time conservation invariants.
- Interactive data-compilation wizard (`crossroads`).
- Spatial standardisation to EPSG:27700 with R-Tree indices; snapshot and temporal
  boundary modes.

[1.0.0]: https://github.com/wpatke/crossroads-uk/releases/tag/v1.0.0
[0.9.0]: https://github.com/wpatke/crossroads-uk/releases/tag/v0.9.0
