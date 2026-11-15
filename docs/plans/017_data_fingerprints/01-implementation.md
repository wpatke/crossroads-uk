# Part 01 — Implementation

Files: new `src/crossroads/provenance.py`. Changed: `src/crossroads/net.py`,
`src/crossroads/quality.py`, and each
transformer in `src/crossroads/transformers/`.

## End state

- After any build, `source_files` has one row per input file per audited source, and
  `crossroads_meta.data_fingerprint` holds one overall fingerprint.
- Every file Crossroads downloads or creates in the cache has a `<file>.source.json`
  sidecar next to it.
- A build from an old cache (no sidecars) still succeeds, and still records `sha256` and
  `size_bytes`. Its origin columns are NULL.
- Building twice from the same cache gives identical `source_files` rows and an identical
  `data_fingerprint`. Even `downloaded_at_utc` matches, because it is read from the
  sidecar rather than stamped at build time.

## New module: `src/crossroads/provenance.py`

Small, plain functions. No classes.

```python
SIDECAR_SUFFIX = ".source.json"
_CHUNK = 1024 * 1024        # hash files 1 MB at a time, never the whole file in memory

def sha256_file(path):
    """Fingerprint of a file: the SHA-256 of every byte, as 64 hex characters."""

def write_sidecar(path, *, origin_url, origin_sha256, derived_by=None):
    """Write <path>.source.json saying where `path` came from and when.
    Written to a .part file first and moved into place, like every cache file."""
    # JSON keys: origin_url, origin_sha256, downloaded_at_utc (ISO 8601, UTC, set now),
    #            derived_by

def read_sidecar(path):
    """The sidecar's contents as a dict, or None if there is no sidecar
    (a file cached before fingerprints existed, or seeded by a test)."""

def copy_origin(derived_path, from_path, derived_by):
    """For a file made from another file (split, unzip, merge): write a sidecar for
    `derived_path` that carries over from_path's origin_url, origin_sha256 and
    downloaded_at_utc, plus a note saying how it was made. If from_path has no
    sidecar, the derived file's origin fields are written as null."""

def record_source_files(con, source_id, paths):
    """Replace this source's rows in source_files with one row per path:
    the fingerprint computed NOW, the size, and whatever the sidecar says.
    Warns if a direct download no longer matches the fingerprint taken while
    downloading it (the file was changed after download)."""

def data_fingerprint(con):
    """One SHA-256 over every source_files row, as sorted
    'source_id|file_name|sha256' lines. NULL if source_files is empty."""
```

`copy_origin` keeps the original download time. A per-year file split from a combined file
downloaded on 14 November 2026 records 14 November 2026, the day the data left DfT.

## `net.py` — hash while downloading, then write the sidecar

`download_to_file` currently uses `shutil.copyfileobj`. Replace it with an explicit chunk
loop that also updates a `hashlib.sha256()`. Same streaming, same timeout behaviour:

```python
digest = hashlib.sha256()
while True:
    chunk = resp.read(_CHUNK)
    if not chunk:
        break
    fh.write(chunk)
    digest.update(chunk)
```

After the existing `os.replace(tmp, dest)`, call
`provenance.write_sidecar(dest, origin_url=url, origin_sha256=digest.hexdigest())`, and
return the hex digest. Because this lives in the one shared download helper, STATS19, ONS,
AADF and bank holidays all get sidecars with no changes to their download code.

If the process dies between the `os.replace` and the sidecar write, the file is complete but
has no sidecar. It is then treated as "origin unknown", which is honest and harmless.

## `quality.py`

- `ensure_quality_tables` also creates:

  ```sql
  CREATE TABLE IF NOT EXISTS source_files (
      source_id VARCHAR, file_name VARCHAR, sha256 VARCHAR, size_bytes BIGINT,
      origin_url VARCHAR, origin_sha256 VARCHAR, downloaded_at_utc TIMESTAMP,
      derived_by VARCHAR)
  ```

- Add `"source_files"` to `_SOURCE_AUDIT_TABLES`, so a rebuild clears a source's old rows
  first, exactly like `source_ingest_log`.
- `write_build_metadata` adds a `data_fingerprint VARCHAR` column to `crossroads_meta`,
  filled from `provenance.data_fingerprint(con)`. It already runs last, after every source,
  so all rows are present.

## Each source: record the files it read

Each `transform_and_load` already has the list of paths it reads. Add one call next to the
existing `record_source_rows` (or next to the bronze load for exempt sources):

| Source | `source_id` | Files recorded | Sidecar comes from |
|---|---|---|---|
| STATS19 | `stats19_collision` / `_vehicle` / `_casualty` | the per-year files from `_cached_files(cache_dir, ftype)` | single-year: `download_to_file`; split: `copy_origin(..., "split by collision_year from the combined 1979-onwards file")` in `_split_combined` |
| ONS boundaries | `ons_lad`, `ons_ctyua` | each vintage's GeoJSON | `download_to_file` |
| AADF | `aadf` | the extracted CSV | `copy_origin(csv, zip, "extracted from the downloaded zip")` in `_unzip_single_csv` |
| Bank holidays | `bank_holidays` | the JSON feed | `download_to_file` |
| Weather | `era5_weather` | each `era5_land_{year}.nc` | `write_sidecar(year_file, origin_url=<CDS dataset page>, origin_sha256=None, derived_by="merged from 12 monthly CDS requests")` in `_merge_months` |

Bank holidays is quality-exempt, but it is still recorded: being exempt from the
row-conservation check has nothing to do with knowing which file was used.

**Coverage rule.** Every active source must record at least one file. This is enforced by a
test (part 02), not by a new build-time gate, to keep the engine unchanged.

## `SCHEMA_VERSION`

`src/crossroads/__init__.py` is already `SCHEMA_VERSION = 5` (from plan 016, unreleased),
so it is not changed. `docs/schema.md`'s version 5 notes list this plan's additions too.

## What is deliberately not done

- **No refresh or re-issue detection.** We record what was used. We do not re-download to
  check for a newer copy.
- **No back-filling of old caches.** Origin is unknown for files downloaded before this
  feature, and is recorded as NULL.
- **The committed reference files** (`codebook`, `column_manifest`, `ons_boundaries.json`)
  are not fingerprinted. They ship inside the package, so `crossroads_version` already
  identifies them exactly.
