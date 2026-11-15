# Part 02 — Tests and documentation

## End state

- Offline tests cover hashing, sidecars, the `source_files` table, `data_fingerprint`, the
  "changed after download" warning, and every source's wiring.
- `docs/schema.md`, the README, `docs/methodology.md` and `CHANGELOG.md` describe the
  feature, and the schema-doc drift guard covers the new table and column.

## Tests

New `tests/test_fingerprints.py` (unit tests, no network):

1. `sha256_file` matches `hashlib.sha256(data).hexdigest()` for a small file, and for a
   file bigger than one chunk (so the chunk loop is exercised).
2. `write_sidecar` then `read_sidecar` round-trips. No `.part` file is left behind.
   `read_sidecar` returns `None` when there is no sidecar.
3. `copy_origin` carries over `origin_url`, `origin_sha256` and `downloaded_at_utc`, and
   sets `derived_by`. If the source file has no sidecar, the origin fields are null.
4. `record_source_files` writes one row per path with the correct `sha256` and
   `size_bytes`. Running it again for the same source replaces its rows rather than
   adding more.
5. A file with no sidecar gets NULL origin columns, and the build does not fail.
6. **Changed after download:** write a sidecar, then change one byte of the file.
   `record_source_files` warns (`pytest.warns`), and it records the new `sha256`.
7. `data_fingerprint` is the same whatever order rows are inserted in. It changes if any
   one file's `sha256` changes. It is NULL for an empty table.

`tests/test_net.py`:

8. `download_to_file` (with a faked `urlopen`) returns the SHA-256 of the served bytes and
   writes a sidecar with `origin_url` = the URL and `derived_by` = null. A failed or
   rejected download writes **no** sidecar.

`tests/test_stats19.py` / `tests/test_aadf.py` / `tests/test_weather.py`:

9. A split per-year STATS19 file's sidecar has the combined file's `origin_url` and
   `origin_sha256`, and a "split by collision_year" `derived_by`.
10. The AADF CSV's sidecar points to the zip. The merged weather year file's sidecar names
    CDS and has a null `origin_sha256` (using the existing weather merge test fixtures).

`tests/test_provenance.py`:

11. `crossroads_meta` has a non-empty `data_fingerprint` after a fixture build.
12. **Coverage:** a full offline fixture build (the `_seed_full_cache` harness from
    `tests/test_console.py`). Every `source_id` in `source_ingest_log` or
    `quality_exemptions` has at least one `source_files` row.
13. **Reproducible:** building twice from the same seeded cache gives identical
    `source_files` rows and an identical `data_fingerprint`.

`tests/test_schema_doc.py`:

14. Add `source_files` to `CORE_TABLES` and `COLUMN_GUARDED`. The fast test checks the
    `SCHEMA_VERSION` 5 declaration.

## Docs

- **`docs/schema.md`:** declare schema version 5. Add a `source_files` section in the same
  commented `CREATE TABLE` style as `source_ingest_log`. Add `data_fingerprint` to the
  `crossroads_meta` block, and say which columns are excluded from the reproducibility
  guarantee (`downloaded_at_utc` only).
- **`README.md`**, in "Crossroads-UK is built to be trusted": change the provenance bullet
  so it also says the database records a fingerprint of every input file, plus one overall
  data fingerprint. Add one example query:

  ```sql
  SELECT data_fingerprint FROM crossroads_meta;      -- one value to quote in a paper
  SELECT * FROM source_files ORDER BY source_id, file_name;   -- every input file
  ```

  Add one sentence explaining that two databases with the same version, parameters **and**
  data fingerprint were built from byte-identical data. If the fingerprints differ, DfT (or
  another publisher) may have re-issued a file. Compare `source_files` to see which one.
- **`docs/methodology.md`:** a short "Data fingerprints" section covering why the
  fingerprints exist (publishers re-issue files under the same name), what NULL origin
  columns mean (a file cached before this feature), and how to get full provenance (clear
  the cache and rebuild).
- **`docs/spec.md` §2:** add one clause to the reproducibility bullet: "…and identical input
  files, which the database records as `source_files` fingerprints and a single
  `data_fingerprint`."
- **`CHANGELOG.md`:** add to the existing `## [Unreleased]` entry's `### Added`:

  ```markdown
  ### Added
  - **Data fingerprints.** Every database now records exactly which input files built it:
    a new `source_files` table holds a SHA-256 fingerprint, size, source URL and download
    date for every file each source read, and `crossroads_meta.data_fingerprint` gives one
    overall value to quote. Publishers such as DfT sometimes re-issue a file under the same
    name, so two builds with the same version and parameters can differ; comparing data
    fingerprints now shows whether they used identical data. Files cached by earlier
    versions are fingerprinted too, but their source URL and download date are unknown
    (NULL) until the cache is cleared and rebuilt.
  ```
