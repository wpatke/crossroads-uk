# Part 01 — Download fallback and streaming split

Files changed: `src/crossroads/transformers/stats19.py`, `src/crossroads/net.py`, and
`src/crossroads/console.py` (the shared `EARLIEST_YEAR` import and a download warning in
the build summary).

## End state

- `extract(cache_dir, years=[...])` produces the same per-year cache files as today,
  `dft-road-casualty-statistics-{type}-{year}.csv`, for **every** requested year from 1979
  to DfT's newest published year.
- A year with a single-year file on DfT uses it, exactly as now.
- Any other year is copied, byte for byte, out of DfT's combined file
  `dft-road-casualty-statistics-{type}-1979-latest-published-year.csv`. That file is
  downloaded at most once into the same cache directory.
- Each per-year file made by the split holds DfT's header line plus exactly that year's
  records, in the order they appear in the combined file. Every character is unchanged.
- `transform_and_load()` and everything after it is **unchanged**.
- Error messages match the cases in "Error messages" below.
- No `.part` file is ever left behind, and an already-cached file is never re-downloaded or
  re-split.

## New constants (top of `stats19.py`)

Replace the out-of-date comment above `DFT_BASE_URL` ("2020-2024 are published
individually…") with an accurate one, and add:

```python
# DfT's first year of STATS19 data. The wizard's year prompt uses this too.
EARLIEST_YEAR = 1979

# DfT publishes only its most recent years (2021-2025 when checked in November 2026) as
# single-year files. Every year, including those, is also in one large combined file per
# type. The single-year window moves forward each year, so the code never hard-codes which
# years have single-year files: it tries the single-year file and falls back on a 404.
_COMBINED_TEMPLATE = "dft-road-casualty-statistics-{ftype}-1979-latest-published-year.csv"

# The three file types, in the order they are fetched. Collision comes first so that a
# problem with a year is found before the two larger vehicle/casualty downloads start.
_FILE_TYPES = ("collision", "vehicle", "casualty")

# Column that says which year a combined-file row belongs to. DfT's current files call it
# collision_year; older DfT files called it accident_year. Both are accepted.
_YEAR_COLUMNS = ("collision_year", "accident_year")

# How many years back from today to look for DfT's newest single-year file.
_NEWEST_YEAR_SEARCH = 3
```

`console.py` then imports `EARLIEST_YEAR` instead of defining its own `1979`, so the two
can never drift apart. `_EARLIEST_STATS19_YEAR = EARLIEST_YEAR` keeps the existing name
working inside `console.py` and its tests.

## `net.py` — new `url_exists(url)` helper

DfT's server rejects `HEAD` requests (405), so existence is checked with an ordinary `GET`
that is closed before any of the body is read:

```python
def url_exists(url, *, timeout=HTTP_TIMEOUT_SECONDS):
    """True if `url` answers 200, False if it answers 404. Any other error is raised.

    Uses a normal GET because some servers (DfT's included) refuse HEAD requests. The
    response is closed straight away without reading the body, so even a large file
    costs almost nothing to check.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
```

It is used only for the "too recent?" check below.

## `extract()` — new flow

```text
1. Any requested year < EARLIEST_YEAR  -> ValueError "STATS19 starts in 1979" (no network).
2. For each type in _FILE_TYPES, for each year:
       per-year file already cached?          -> skip
       single-year URL downloaded OK?         -> done (exactly today's _fetch)
       single-year URL answered 404?          -> remember (type, year) as "needs combined"
3. If anything needs the combined file:
   a. Work out DfT's newest published year (_newest_published_year, below). Any
      requested-and-missing year newer than that -> ValueError "not published yet".
      This happens BEFORE any combined download.
   b. For each type that needs it (collision first):
        combined file not cached -> download it (same _fetch: .part, validate, atomic move)
        split it into the missing years for that type (_split_combined)
```

`_fetch(url, dest)` changes in one way. On a 404 it now **returns `False`** instead of
raising, so `extract()` can decide what a missing file means. It returns `True` on success.
Every other failure raises the same `ValueError` as today: other HTTP errors, or a body
that isn't a STATS19 CSV.

### `_newest_published_year()`

```python
def _newest_published_year(self):
    """DfT's newest year that has a single-year collision file, or None if not found.

    DfT always publishes its newest years as single-year files, so counting down from
    the current calendar year finds the newest published year in one or two checks.
    Returns None if none of the last few years exist (for example, if DfT renamed its
    files). Then the combined file's own contents decide instead (see _split_combined).
    """
```

It counts down `date.today().year`, `-1`, `-2` and calls `url_exists` on the single-year
collision URL. It runs only when at least one file answered 404, and costs 1–3 small
requests.

### `_split_combined(combined_path, ftype, years, cache_dir)`

This is a streaming, all-or-nothing split:

```text
open the combined file as UTF-8 with errors="surrogateescape" and newline=""
    (newline="" keeps "\r\n" vs "\n" exactly; surrogateescape round-trips any
     byte that is not valid UTF-8, so even a bad byte is copied unchanged)
wrap the file in a small generator that remembers each line it hands to csv.reader
read the header record -> keep its original text; find the year column
    (_YEAR_COLUMNS; the header is BOM-stripped and lower-cased only for this lookup)
    no year column -> ValueError
for each record csv.reader returns:
    raw = the original text of the lines it consumed (one record may span several
          lines if a quoted field contains a line break)
    empty record (a blank line)          -> skip; it holds no data
    record too short to have a year      -> ValueError (fail loud; never drop a row silently)
    year not one we want                 -> skip
    otherwise: open "<per-year file>.part" on first use and write the header text;
               write raw (adding the file's own line ending if the very last record
               of the combined file had none, so two records can never run together)
close every .part file
any wanted year got no rows -> delete every .part, raise ValueError (see messages)
otherwise os.replace each .part onto its per-year filename
on ANY exception: close and delete every .part, then re-raise
```

Notes:

- **Why the "remember each line" trick is safe.** `csv.reader` asks for one line at a time
  and stops as soon as a record is complete. It never reads ahead. So the lines it asked
  for since the last record are exactly that record's original text. This was checked
  with records containing quoted commas, `""`, a quoted line break, CRLF endings, a blank
  line, and a final record with no newline.
- **Memory.** Only one record is held at a time, plus one open file per wanted year
  (at most 42 for 1979–2020, well under the operating system's open-file limit).
- **One pass per type.** All missing years for a type come from a single read of its
  combined file. A 1979–2020 build reads each combined file once.
- **All-or-nothing.** The per-year files are moved into place only after the whole combined
  file has been read. A crash or error midway leaves only `.part` files, and those are
  deleted. Because every per-year file is moved into place only when complete, the existing
  "already cached → skip" rule remains safe.
- **A malformed row is not hidden.** A row with the wrong number of columns still has its
  year in the second field, so it is copied into the right per-year file. The existing
  bronze quarantine and conservation check then catch it, exactly as they would for a
  single-year file. Only a row too short to contain a year at all is fatal at split time.

### Why the `csv` module and not DuckDB

DuckDB could do the split as `COPY (SELECT * FROM read_csv(…) WHERE collision_year = …)`.
We do not use it, for four reasons:

1. **It would rewrite values.** DuckDB parses then re-writes every value. Quoting,
   `""` versus a blank cell, number formatting, and line endings could change. Our split
   copies the original text, so a split file is byte-identical to the records DfT wrote.
2. **It would weaken the independent row count.** `_count_source_rows` deliberately counts
   rows with Python's `csv` module, *not* DuckDB, so it can catch DuckDB dropping a row. If
   DuckDB also produced the per-year files, a DuckDB parsing problem could drop the same
   rows at both steps and go unnoticed.
3. **It would hide bad lines.** DuckDB either fails or skips a malformed line during the
   split. Our split passes it through, so the existing quarantine records it.
4. **No real speed need.** This is a one-off step per new year selection, limited by the
   multi-gigabyte download rather than the split.

## Choice of year column (decision 4 in the overview)

- **Used:** `collision_year`, or `accident_year` if a file uses the older naming. It is the
  only year field present in **all three** files. Routing vehicles and casualties by the
  same field as collisions keeps each year's three files consistent with each other, so the
  vehicle/casualty → collision link check works per build.
- **Not used:** `date`, which is in the collision file only, and the first four characters of
  the index, which is a naming convention and not a published field.
- **Checked against the data:** the scan in part 03 confirms that `collision_year` matches
  the year in `date` for every collision row (see the findings there).

## Error messages

| Situation | Message (wording may be polished, facts must stay) |
|---|---|
| Year < 1979 | `STATS19 data starts in 1979, so {year} cannot be built. Choose a year from 1979 onwards.` |
| Year > DfT's newest published year | `STATS19 data for {year} has not been published yet: DfT's newest published year is {newest}. DfT usually publishes a year's data partway through the following year.` |
| Combined file 404 | `DfT's combined STATS19 file was not found at {url} (HTTP 404). DfT may have renamed or moved it; see https://www.gov.uk/government/statistics/road-safety-data.` |
| A requested year has no rows in the combined file | `DfT's combined {type} file has no rows for {year}, so it cannot be built. The cached file may be out of date: delete {path} to download a fresh copy.` |
| Combined header has no year column | `{path} has no collision_year or accident_year column, so it cannot be split by year. DfT may have changed the file's format.` |
| Other HTTP error / HTML error page | Unchanged from today. |

The old "try an earlier year" message is removed. A 404 on a single-year file is now
normal for an older year, not an error.

## Wizard warning about the large download (`console.py`)

The README note only reaches people who read it first. So the wizard also warns, in the
build summary it shows **before** asking "build now?", so the user can still say no.

- **When:** `stats19` is among the chosen datasets, and at least one chosen year is older
  than `date.today().year - 5`.
- **Why that cutoff:** DfT keeps about its five most recent years as single-year files. In
  November 2026 that gives 2021, which matches what DfT publishes. The cutoff moves forward
  by itself each year, so no year is hard-coded. For a few months each year (after New Year,
  before DfT publishes the new year) it may warn about one year that still has a
  single-year file. That only over-warns, which is harmless, so the wording says "may".
- **Text** (one extra line in `format_summary`):

  ```
    Note          : years before {cutoff} may need a one-time download of about 4.3 GB
                    from DfT (later builds reuse it from the cache).
  ```

- The 4.3 GB figure is a module constant with a comment saying when it was measured
  (November 2026). DfT's combined files grow by roughly 0.1 GB a year.
- The wizard does not check the cache. A repeat user sees the note again, but the note
  already says the download happens only once.

## Known limits (documented, not fixed)

- **The cached combined file is never refreshed.** This matches today's per-year cache,
  which is also never refreshed. DfT sometimes revises earlier years. To pick up a revised
  combined file, delete it from the cache. This goes in the README.
- **If a year moves out of DfT's single-year window**, a fresh cache gets it from the
  combined file. An existing cache keeps the single-year copy it already has. Both are
  DfT's published data for that year.
