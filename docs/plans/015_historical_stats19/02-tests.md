# Part 02 — Offline tests

Files: `tests/test_stats19.py`, `tests/test_net.py`, new fixtures under
`tests/fixtures/stats19/combined/`, and `tests/fixtures/stats19/README.md`.

All tests are offline. Downloads are faked with `monkeypatch.setattr(urllib.request,
"urlopen", fake)`, the pattern the existing fetch tests already use. Each fake either
serves bytes for a URL or raises `HTTPError(404)`, and records which URLs were asked for, so
a test can assert that something was **not** downloaded.

## End state

- `pytest` passes with no network access.
- Every behaviour in part 01 has a test: the split, the fallback, cache reuse, and each
  error message.
- One end-to-end `client.build()` runs from split files only, proving the rest of the
  pipeline (bronze, conservation, silver, link checks) works unchanged.

## Fixture: a small hand-made "combined" sample

`tests/fixtures/stats19/combined/` holds three files named exactly like DfT's:

```
dft-road-casualty-statistics-collision-1979-latest-published-year.csv
dft-road-casualty-statistics-vehicle-1979-latest-published-year.csv
dft-road-casualty-statistics-casualty-1979-latest-published-year.csv
```

- **Header:** DfT's real current header, with the modern `collision_*` names, copied from
  the live combined file.
- **Rows:** 2–3 collisions each for **2005, 2019 and 2020**, plus linked vehicles and
  casualties. They are hand-made by copying rows from the 2023 fixture (as refreshed by plan
  016, so the casualty file includes `escooter_flag`) and changing the index, year,
  reference and date. The coordinates are left alone, so they still fall
  inside the committed LAD sample and the spatial stamp still has something to stamp.
  The 2005 rows use `-1` for fields DfT did not collect then (for example
  `enhanced_severity_collision`, the LSOA columns), matching the real old data
  (see part 03).
- **Years are interleaved** (for example 2019, 2005, 2020, 2005, …), as in the real file,
  so the test proves a split does not assume rows are grouped by year.
- `tests/fixtures/stats19/README.md` gains a short section saying these files are
  hand-made, how they were made, and that they are not DfT data verbatim.

The older `accident_*` naming and the awkward-format cases are written inline in
`tmp_path` by the tests that need them. That keeps the committed fixture realistic.

## Tests (`tests/test_stats19.py`)

Split (`_split_combined`):

1. **`test_split_combined_writes_exact_per_year_files`**: split the fixture collision
   file for `[2005, 2020]`. Each output equals the header line plus exactly that
   year's records, compared **as bytes** against the original lines. 2019 is not written
   because it was not requested. No `.part` files remain.
2. **`test_split_combined_accepts_accident_year_header`**: a tiny inline file with an
   `accident_index,accident_year,…` header splits correctly.
3. **`test_split_combined_keeps_awkward_records_exact`**: an inline file with CRLF line
   endings, a quoted field containing a comma and a line break, a `""` cell, a blank line,
   and a last record with no final newline. Every output record is byte-identical to the
   input record. The record without a newline gets the file's line ending, so no records
   are merged. `_count_source_rows` on the outputs equals the number of records written.
4. **`test_split_combined_missing_year_raises_and_caches_nothing`**: asking for a year
   with no rows raises the "no rows for {year}" `ValueError`. **No** per-year file is
   created, not even for the years that did have rows (all-or-nothing). No `.part`
   files remain.
5. **`test_split_combined_without_year_column_raises`**: the header has neither year
   column → clear `ValueError`, nothing written.

`extract()` fallback and caching:

6. **`test_extract_falls_back_to_combined_when_single_year_missing`**: the fake returns 404
   for every single-year URL of 2005/2019/2020, 200 for the newest-year probe, and serves
   the fixture combined files. After `extract(years=[2005, 2020])`, all six per-year files
   exist and match the expected bytes. Each combined URL was requested **once**.
7. **`test_extract_prefers_single_year_file`**: with a mix of years, where 2023 is served
   as a single-year file and 2005 only through the combined file, 2023 comes from the
   single-year URL and is byte-identical to what was served. Only 2005 is split.
8. **`test_extract_reuses_cached_combined_file`**: the combined files are pre-copied into
   the cache, and the fake **fails the test** if a combined URL is requested. Then
   `extract` still produces the per-year files.
9. **`test_extract_skips_already_split_years`**: run `extract` twice for the same years.
   On the second run, `_split_combined` is monkeypatched to fail if called, and nothing is
   downloaded.

Error messages:

10. **Replace** `test_fetch_missing_year_raises_friendly_error` with
    **`test_extract_too_recent_year_message`**: the fake returns 404 for the requested year
    2999 and 200 for the probe of the current calendar year. The message says 2999 is not
    published yet and names the newest year. **No combined URL is requested**, and the
    cache is empty.
    (The probe counts down from `date.today().year`. The fake answers 200 for any single-year
    collision URL other than 2999's, so the test does not depend on today's date.)
11. **`test_extract_year_before_1979_message`**: `years=[1978]` → "starts in 1979". The fake
    fails the test if *any* URL is requested.
12. **`test_extract_combined_404_message`**: single-year and combined both 404 →
    "renamed or moved" message, nothing cached.
13. The existing `test_fetch_html_error_page_is_rejected` and
    `test_fetch_valid_csv_is_cached_atomically` keep passing unchanged, because their
    fakes answer 200.

End-to-end:

14. **`test_build_from_combined_file_end_to_end`**: seed the cache with only the combined
    fixture files. The fake answers 404 for single-year URLs and 200 for the probe. Run
    `client.build(years=[2005, 2019, 2020])`. The invariants pass. The row counts in
    bronze and silver equal the fixture's per-year row counts, and every vehicle and
    casualty has `link_valid = TRUE`.

## Tests (`tests/test_net.py`)

15. **`url_exists`** returns `True` for a 200, returns `False` for a 404, and raises for
    a 500. The body is never read (the fake response raises if `.read()` is called).

## Console

16. If `console.py` now imports `EARLIEST_YEAR`, the existing year-range tests in
    `tests/test_console.py` still pass. Run them to confirm.
17. **`test_summary_warns_about_large_download_for_old_years`**: `format_summary` with
    `stats19` and year 2000 includes the "about 4.3 GB" note.
18. **`test_summary_no_download_note_for_recent_years_or_without_stats19`**: no note when
    every year is `date.today().year - 1`, and no note when 2000 is chosen without
    `stats19`. Both tests compute their years from `date.today()`, so they don't go stale.
