# Data fingerprints — Plan Overview
> Multi-part plan. Read this overview first, then the part you are implementing.
> Each part's End State is the contract. Adapt the steps to reality and note any deviations.

Record, inside every database, **exactly which input files built it**: a fingerprint (a
SHA-256 hash) of each file, where it came from, and when it was downloaded. Also record one
overall "data fingerprint" a researcher can quote in a paper.

## Why

The spec promises that the same Crossroads version with the same parameters builds a
structurally identical database (`docs/spec.md` §2). That promise silently assumes the input
files are the same. They are not always:

- **DfT re-issues files under the same names.** The 2023 STATS19 files downloaded on
  15 July 2026 and on 31 October 2026 have the same row counts, but the October copy has an
  extra casualty column (`escooter_flag`), e-scooters moved from `-1` to code `33`, and
  revised severity-adjustment weights.
- The cache is never refreshed. So two researchers with the same version and parameters can
  build different databases, depending on when each first downloaded the files.
- Today the database cannot tell them apart. `crossroads_meta` records the version, schema
  and parameters, but nothing about the data.

A **fingerprint** is a short code computed from every byte of a file. Two files have the
same fingerprint only if they are byte-for-byte identical, so comparing fingerprints answers
"was this built from exactly the same data?" without sending anyone the files.

## What exists today

- `crossroads_meta` (`src/crossroads/quality.py`, `write_build_metadata`) is a single-row
  stamp: `crossroads_version`, `schema_version`, `built_at_utc`, `parameters`.
- Every source except weather downloads through one helper,
  `crossroads.net.download_to_file`. It streams to `<dest>.part`, validates, then
  `os.replace`s the file into place.
- Three sources build the file they read from the file they downloaded:
  - STATS19 older years: per-year files **split** from DfT's combined file (plan 015).
  - AADF: the CSV is **extracted** from a downloaded zip.
  - Weather: a year file is **merged** from 12 monthly CDS downloads, which are then deleted.
- Files already in someone's cache were downloaded before this feature existed, so we do not
  know where or when they came from.

## Decisions

1. **Hash the file actually read, at build time.** Each source records a fingerprint of
   every file its `transform_and_load` reads. Computing it at build time (not trusting a
   value saved earlier) means the fingerprint always describes the file that really went
   into the database, even if the cache was edited by hand.

2. **A small "where did this come from" note next to each cache file.** When Crossroads
   downloads or creates a cache file, it writes a sidecar file `<file>.source.json` with:
   - `origin_url`: the URL it was downloaded from (for a derived file, the URL of the file
     it was made from);
   - `origin_sha256`: the fingerprint of the downloaded file, computed **while
     downloading**, so a 1.5 GB file costs no extra read;
   - `downloaded_at_utc`: when the download finished;
   - `derived_by`: `null` for a direct download, otherwise a short plain-English note such
     as `"split by collision_year from the combined 1979-onwards file"`.
   The sidecar is written atomically (`.part` then `os.replace`), like every other cache
   file. JSON keeps it readable by a person opening the cache folder.

3. **One new table, `source_files`, one row per input file per source:**

   | Column | Meaning |
   |---|---|
   | `source_id` | Audited source, e.g. `stats19_collision`, `ons_lad`, `aadf` |
   | `file_name` | Cache file name that was read |
   | `sha256` | Fingerprint of that file, computed at build time |
   | `size_bytes` | File size |
   | `origin_url` | From the sidecar (NULL if unknown) |
   | `origin_sha256` | From the sidecar (NULL if unknown) |
   | `downloaded_at_utc` | From the sidecar (NULL if unknown) |
   | `derived_by` | From the sidecar (NULL for a direct download or if unknown) |

4. **One overall `data_fingerprint` column in `crossroads_meta`.** It is a SHA-256 of the
   sorted list of `source_id | file_name | sha256` lines. One value to quote: "built with
   Crossroads 1.1.0, data fingerprint `3f9a…`". Same files → same value, on any machine.

5. **Unknown means NULL, never a guess.** A cache file with no sidecar (downloaded before
   this feature, or seeded by a test) still gets its real `sha256` and `size_bytes`. Its
   origin columns are NULL. We do not use the file's modified time as a stand-in for the
   download date, because copying a file changes it. The README says how to get full
   provenance: delete the cache and rebuild.

6. **Warn if a downloaded file changed since download.** If a sidecar says the file is a
   direct download (`derived_by` is null) and its `origin_sha256` differs from the hash
   computed now, the build issues a `warnings.warn` saying the file was modified after
   download. The build does not stop: the recorded `sha256` is still the truth about what
   was used.

7. **Weather is recorded honestly, not perfectly.** CDS generates a new NetCDF file for
   every request, and the monthly files are deleted after merging. So weather's
   `origin_url` is the CDS dataset page, `origin_sha256` is NULL, and `derived_by` says
   "merged from 12 monthly CDS requests". The merged year file's own `sha256` is still
   recorded. That is what identifies the data used.

8. **Reproducibility rules.** `sha256`, `size_bytes`, `origin_url`, `origin_sha256`,
   `derived_by` and `data_fingerprint` depend only on the files, so they are part of the
   "structurally identical" guarantee. `downloaded_at_utc` is a machine-stamped timestamp
   and is excluded, like `built_at_utc` and `ingested_at`.

## Rejected alternatives

- **Comparing with the server's `Last-Modified` / `ETag` headers.** DfT's server sent no
  `Last-Modified` header in our checks, and a header describes the server's copy, not the
  one in your cache. A fingerprint of the bytes is the only reliable record.
- **Refusing to build when a file is re-issued.** We cannot know a re-issue happened
  without re-downloading. Detecting re-issues (for example an opt-in `refresh` check) is a
  possible later feature. This plan only *records* what was used.
- **Hashing only at download time.** That is cheaper, but it would be wrong after a hand
  edit, and it would leave every existing cache with no fingerprints at all.

## Parts

| Part | File | What |
|---|---|---|
| 01 | [01-implementation.md](01-implementation.md) | `provenance.py`, hashing downloads, sidecars, `source_files`, `data_fingerprint`, wiring into each source |
| 02 | [02-tests-docs.md](02-tests-docs.md) | Offline tests, `docs/schema.md`, README, methodology, CHANGELOG |

## Cross-cutting constraints

- **Standard library only** (`hashlib`, `json`, `os`).
- **Streaming hashes.** Files are read in 1 MB chunks and never loaded whole into memory.
- **Order:** implement **after** plan 015 (historical STATS19). The split it adds writes
  derived per-year files, and those need sidecars too.
- **Schema change (additive):** a new table and a new `crossroads_meta` column. Plan 016
  already moved `SCHEMA_VERSION` from 4 to 5 for the unreleased 1.1.0. Plans 016, 015 and
  017 ship together, so this plan **keeps it at 5** and adds its changes to the same
  version. That is a **MINOR** release under this project's rules.
- **Cost:** every input file is read once more at build time to hash it. A full 1979–2025
  STATS19 build reads about 4.3 GB of per-year files, roughly 10–20 seconds on a laptop SSD.
  That is small next to the rest of the build.
- **Simple, commented code.** The maintainer reads every line before committing.
