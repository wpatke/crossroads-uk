# Historical STATS19 years — Plan Overview
> Multi-part plan. Read this overview first, then the part you are implementing.
> Each part's End State is the contract. Adapt the steps to reality and note any deviations.

Fix: building STATS19 for any year before 2021 fails with an HTTP 404. After this fix, every
year from 1979 to DfT's newest published year builds. Years that DfT publishes as a single-year
file still use that file. Every other year is taken from DfT's combined 1979-onwards file.

## The bug

`Stats19Transformer.extract()` (`src/crossroads/transformers/stats19.py`) only downloads
single-year files:

```
https://data.dft.gov.uk/road-accidents-safety-data/dft-road-casualty-statistics-{type}-{year}.csv
```

for `type` in `collision`, `vehicle`, `casualty`. The wizard (`src/crossroads/console.py`)
accepts any year from 1979 (`_EARLIEST_STATS19_YEAR`). So a build such as `years=2000-2024`
fails on its first year with:

```
ValueError: No STATS19 data found at .../dft-road-casualty-statistics-collision-2000.csv (HTTP 404).
DfT usually publishes a year's data partway through the following year, so this year may not
be available yet -- try an earlier year.
```

The message is also wrong: 2000 is not "too recent". It is too old for DfT's single-year files.

## What DfT publishes today (checked 2026-11-14)

Every URL below was requested directly from `data.dft.gov.uk`.

| File | Status | Size |
|---|---|---|
| `…-collision-2019.csv`, `…-collision-2020.csv` (and `vehicle-2020`, `casualty-2020`) | **404** | — |
| `…-collision-2021.csv` … `…-collision-2025.csv` | 200 | ~19 MB each |
| `…-collision-2026.csv` | 404 (not yet published) | — |
| `…-collision-1979-latest-published-year.csv` | 200 | 1.53 GB |
| `…-vehicle-1979-latest-published-year.csv` | 200 | 1.78 GB |
| `…-casualty-1979-latest-published-year.csv` | 200 | 0.99 GB |
| `…-collision-last-5-years.csv` | 200 | 98 MB |

So:

- Single-year files now cover **2021–2025** only. **2020 is no longer published on its own**,
  so 2020 also needs the fix. The window appears to move forward one year at a time, so
  2021 may disappear next year. The code therefore must not hard-code which years have
  single-year files. It tries the single-year file first and falls back on a 404.
- The three combined files total **about 4.3 GB**.
- The server does not support `HEAD` requests (it answers 405) and ignores byte-range
  requests. A cheap "does this file exist?" check therefore has to open a normal `GET` and
  close it before reading the body.
- The combined files use **exactly the same header** as the single-year files. They use the
  modern names (`collision_index`, `collision_year`, `collision_ref_no`, …), even for 1979 rows.
- Rows in the combined files are **not grouped by year**. The first two casualty rows are
  from 2013 and then 2019. So a split must be able to route any row to any year's file at
  any point in the file.
- The `last-5-years` files overlap with the single-year files, so we do not use them.

## Decisions

1. **Single-year file first, combined file as the fallback.** For each requested
   `(type, year)` that is not already cached, try the single-year URL exactly as today. If
   DfT answers 404, add the year to a "needs the combined file" list. No list of
   "single-year years" is hard-coded.

2. **Download each combined file once, then split it into the per-year filenames the
   pipeline already expects.** The combined file is streamed to disk with the existing
   `download_to_file` helper. That helper already streams in chunks, writes to `.part`,
   validates, and renames atomically. A streaming split then writes
   `dft-road-casualty-statistics-{type}-{year}.csv` for each needed year. Nothing after
   `extract()` changes: `_cached_files`, `_count_source_rows`, `_load_bronze`, and the
   conservation invariants see ordinary per-year files.

3. **Split with Python's `csv` module, copying each record's original text.** Python's
   `csv.reader` parses each row, which tells us its year and where the record ends, even
   when a quoted field contains a comma or a line break. We then write the record's
   **original characters** to the per-year file. We do not re-create the record with
   `csv.writer`. This keeps every byte of every value exactly as DfT wrote it, including
   quoting, `""` versus blank, and line endings. Full reasoning, and why not DuckDB, is in
   [01-download-and-split.md](01-download-and-split.md).

4. **The year comes from the `collision_year` column (or `accident_year` in an older-style
   header).** This column is in all three files, so a vehicle or casualty row always goes
   to the same year's file as its collision. That matters because the link check joins
   vehicles and casualties to collisions in the same build. The `date` column exists only
   in the collision file, so it cannot be used for all three. The index prefix is a
   convention, not a field. Details are in part 01.

5. **Only the requested years are split, and the combined file stays in the cache.** A
   later build with different years reuses the cached combined file. It never
   re-downloads it, and it never re-splits a year that is already cached. Cost: about
   4.3 GB of cache disk after the first pre-2021 build. This is documented in the README
   with how to reclaim the space.

6. **Accurate error messages, checked before any large download.**
   - Year before 1979 → "STATS19 starts in 1979". No network is used.
   - Year newer than DfT's newest published year → the existing "not published yet"
     message, now naming the newest published year. This is checked with a small
     existence probe **before** the 4.3 GB download starts, so a typo such as `2015-2026`
     fails within seconds.
   - Combined file itself missing (404) → "DfT may have renamed or moved it", with a link
     to the Road Safety Data page.
   - A year inside DfT's range that the combined file has no rows for → a clear "no rows
     for this year" message. This should never happen with real data.

7. **No reference-data changes without review.** Findings on older years' codes and
   columns are in [03-older-years-findings.md](03-older-years-findings.md). Any change to
   `stats19_codebook.csv` / `stats19_columns.csv` is a separate decision for the maintainer.

## Parts

| Part | File | What |
|---|---|---|
| 01 | [01-download-and-split.md](01-download-and-split.md) | `extract()` fallback, combined download, streaming split, error messages, `net.url_exists`, wizard download warning |
| 02 | [02-tests.md](02-tests.md) | Offline tests and the hand-made combined fixture |
| 03 | [03-older-years-findings.md](03-older-years-findings.md) | How 1979–2020 data meets the codebook, manifest, quality checks, and completeness report |
| 04 | [04-docs-changelog.md](04-docs-changelog.md) | README, `docs/data-sources.md`, CHANGELOG |

## Follow-on plan

[017_data_fingerprints](../017_data_fingerprints/00-overview.md) records a fingerprint of
every input file in the database. It is implemented after this plan, and it adds a
"where did this come from" sidecar to the per-year files that `_split_combined` writes.
This plan does not write sidecars itself.

## Cross-cutting constraints

- **Standard library only** for downloading and splitting (`urllib`, `csv`, `os`). No new
  dependencies.
- **Never load a whole combined file into memory.** Both the download and the split stream.
- **Never leave a half-written file in the cache.** Every output is written to `<name>.part`
  and moved into place with `os.replace` only after it is complete. On any error, every
  `.part` file is deleted.
- **Skip anything already cached.** This covers single-year files, split per-year files, and
  combined files.
- **No network access in tests.** Downloads are faked with `monkeypatch` on
  `urllib.request.urlopen`, the same way `tests/test_stats19.py` already does.
- **Simple, commented code.** The maintainer reads every line before committing.
- **No schema change.** The database tables and columns are unchanged. Only which years can
  be built changes.
