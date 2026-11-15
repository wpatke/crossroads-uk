# Part 04 — README, data-sources doc, CHANGELOG

## End state

Researchers can tell from the docs that every year from 1979 can be built, what the first
pre-2021 build will download, and how to reclaim the disk space.

## `README.md`

Add one paragraph to `## Usage`, straight after the weather paragraph and before
`## Real-world examples`. Nothing else in the README changes.

```markdown
**STATS19 years.** Any year from 1979 to DfT's newest published year can be built. DfT
publishes only its most recent years as single-year files (about 20 MB each). Older years
are only in DfT's combined 1979-onwards files, about 4.3 GB in total. The first build that
needs an older year downloads those files once into the cache folder
(`.crossroads_cache/` in the folder you run from), and later builds reuse them. The wizard
warns you about this before it starts. To free the space, or to pick up a DfT revision of
older years, delete the `*-1979-latest-published-year.csv` files from that folder.
```

- It has no year range such as "2021–2025", because DfT moves that window every year. The
  dated detail goes in `docs/data-sources.md`.
- It names the cache folder (`init_engine`'s default `cache_dir`), because the README does
  not mention the cache anywhere else.

## `docs/data-sources.md` — section 1 (DfT STATS19)

Add a "Downloads" sub-list:

- Single-year files: `dft-road-casualty-statistics-{collision,vehicle,casualty}-{year}.csv`.
  These are published for DfT's most recent years only (2021–2025 when checked in
  November 2026).
- Combined files: `dft-road-casualty-statistics-{type}-1979-latest-published-year.csv`,
  used for every other year. Rows are split into per-year cache files by the
  `collision_year` column, which is in all three files. The record text is copied
  unchanged.
- Link to the Road Safety Data page: https://www.gov.uk/government/statistics/road-safety-data.
- A short note on older-year data coverage, summarising part 03 (which fields DfT did
  not collect before a given year, and so are `-1`/NULL there).

## `CHANGELOG.md`

Add to the existing `## [Unreleased]` entry (opened by plan 016). Plans 016, 015 and 017
ship together as one release, 1.1.0:

```markdown
### Fixed
- **STATS19 builds for any year before 2021 failed** with an HTTP 404. DfT now publishes
  single-year files only for its most recent years (2021–2025 at the time of writing).
  Older years are only in its combined 1979-onwards files. Crossroads now falls back to
  those files: it downloads each one once into the cache (about 4.3 GB in total) and splits
  out the requested years, copying DfT's rows unchanged. Every year from 1979 can now be
  built. The tables and columns in the database are unchanged.
- The "not published yet" error now appears only for a year newer than DfT's newest
  published year, and names that year. A year before 1979 gets its own clear message.

### Changed
- The wizard's build summary now warns when the chosen years may need DfT's one-time
  4.3 GB download, before you confirm the build.
```

If part 03 leads to any reference-data change, it gets its own CHANGELOG line, because it
changes ingestion behaviour.

## Other mentions

- `tests/fixtures/stats19/README.md` says "Per-year files are only published individually
  for 2020 onward". Update this to say the single-year window moves, and that it was
  2021–2025 when checked in November 2026.
- The comment in `_derive_collision_silver` ("verified against live 2020-2024 DfT files")
  is a historical note about when it was checked, so it stays.
