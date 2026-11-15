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

## [1.1.0] - 2026-11-15

Every STATS19 year from 1979 can now be built, every database records a fingerprint of
each input file it was built from, and the STATS19 reference data follows DfT's 2025 Data
Guide. Databases built with this version have `schema_version` 5.

### Added
- **Data fingerprints.** Every database now records exactly which input files built it.
  A new `source_files` table holds a SHA-256 fingerprint, size, source URL and download date
  for every file each source read, and `crossroads_meta.data_fingerprint` gives one overall
  value to quote. Publishers such as DfT sometimes re-issue a file under the same name, so
  two builds with the same version and parameters can differ. Comparing data fingerprints
  now shows whether they used identical data. Crossroads writes a small `<file>.source.json`
  note beside each cache file to remember where it came from. Files cached by earlier
  versions are fingerprinted too, but their source URL and download date are unknown (NULL)
  until the cache is cleared and rebuilt.
- `casualties.escooter_flag`: 1 when the casualty was using an e-scooter, 0 when not. DfT
  added this column to its casualty files in its autumn 2026 re-issue. Earlier builds
  loaded it into the bronze table but dropped it from `casualties`. It is NULL for casualty
  files that do not have the column.
- `codebook.tbl`: the source file (`collision` / `vehicle` / `casualty`) each code belongs
  to. The codebook is now unique on `(tbl, variable, code)`, because `escooter_flag` is in
  both the vehicle and casualty files with different labels. **A query that joins
  `codebook` on `variable` alone should now also match `tbl`**, or it will get two rows for
  each `escooter_flag` code.

### Changed
- The codebook is now built from the 2025 edition of DfT's Road Safety Open Dataset Data
  Guide (previously the 2024 edition, whose download address now returns 404). New labels:
  `junction_detail` 19 "Other junction" (in use in every year, about 3,000–7,000 collisions
  a year), and `vehicle_type` 33 / `casualty_type` 33 "Personal powered transporter" (the
  code DfT now uses for e-scooters). These labels were blank before, and every build warned
  about undecoded codes in `junction_detail`, and also in `vehicle_type` and `casualty_type`
  for 2020 onwards. The guide prints the casualty code as a second `23`; its own conversion
  sheet gives `33`, and the codebook script corrects it (see
  [src/crossroads/reference/README.md](src/crossroads/reference/README.md)).
- Database `schema_version` 4 → 5 (additive: the new `source_files` table and the columns
  above).
- The wizard's build summary now warns when the chosen years may need DfT's one-time
  4.3 GB download, before you confirm the build.

### Fixed
- **STATS19 builds for any year before 2021 failed** with an HTTP 404. DfT now publishes
  single-year files only for its most recent years (2021–2025 at the time of writing).
  Older years are only in its combined 1979-onwards files. Crossroads now falls back to
  those files: it downloads each one once into the cache (about 4.3 GB in total) and splits
  out the requested years, copying DfT's rows unchanged. Every year from 1979 can now be
  built. The tables and columns this produces are unchanged.
- The "not published yet" error now appears only for a year newer than DfT's newest
  published year, and names that year. A year before 1979 gets its own clear message.

## [1.0.5] - 2026-11-01

Documentation only: the code and the databases it builds are the same as 1.0.4.

### Changed
- README: links to [AskCrossroads](https://github.com/wpatke/askcrossroads), a companion app
  for exploring a Crossroads-UK database by asking questions in plain English.
- README: links to other files in the repository are now full GitHub URLs, so they work on
  the PyPI project page as well as on GitHub.

## [1.0.4] - 2026-10-31

### Fixed
- **Precipitation was wrong in every earlier build that included weather.** ERA5-Land
  publishes precipitation as a running total since 00:00 UTC, not an hourly amount, and
  earlier builds stored that running total unchanged. `weather.precipitation_mm` and
  `collisions.precipitation_mm` therefore held the rain *so far that day*: a 23:00
  collision carried nearly the whole day's rain, a 01:00 collision almost none, and
  average hourly rainfall came out roughly 12x too high. Precipitation is now
  de-accumulated into true hourly amounts. Temperature was not affected.
- `weather.precipitation_mm` is now the rain in the hour *ending* at `valid_time` (the
  ERA5 convention). `collisions.precipitation_mm` is the rain during the clock hour the
  collision happened in (14:00-15:00 for a 14:30 collision). See
  [docs/methodology.md](docs/methodology.md#weather-value-handling).

**Action required:** rebuild any database that includes weather. To check an existing
database, run `SELECT crossroads_version FROM crossroads_meta`; anything below 1.0.4 with
weather is affected. Cached `.nc` files are still valid, so a rebuild downloads nothing new.

## [1.0.3] - 2026-07-16

### Changed
- Wizard: before the (masked) Copernicus CDS token prompt, note that input stays hidden
  while typing or pasting, so the blank-looking line no longer reads as a frozen terminal.
- README: added `pip install --upgrade crossroads-uk` instructions for updating an
  existing install.

## [1.0.2] - 2026-07-16

### Fixed
- Wizard weather setup: a rejected or malformed Copernicus CDS API key, and the `weather`
  extra not being installed, are now handled with clear, actionable guidance instead of a
  raw traceback.

## [1.0.1] - 2026-07-16

### Changed
- README: documented installation from PyPI (`pip install crossroads-uk`, plus the
  `[weather]` extra for the ERA5-Land source).

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

[1.0.5]: https://github.com/wpatke/crossroads-uk/releases/tag/v1.0.5
[1.0.4]: https://github.com/wpatke/crossroads-uk/releases/tag/v1.0.4
[1.0.3]: https://github.com/wpatke/crossroads-uk/releases/tag/v1.0.3
[1.0.2]: https://github.com/wpatke/crossroads-uk/releases/tag/v1.0.2
[1.0.1]: https://github.com/wpatke/crossroads-uk/releases/tag/v1.0.1
[1.0.0]: https://github.com/wpatke/crossroads-uk/releases/tag/v1.0.0
[0.9.0]: https://github.com/wpatke/crossroads-uk/releases/tag/v0.9.0
