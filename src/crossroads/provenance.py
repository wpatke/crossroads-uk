"""Data fingerprints: record exactly which input files built a database.

Publishers such as DfT sometimes re-issue a file under the same name, so two builds with the
same Crossroads version and parameters can still have used different data. To make that
checkable, every build records, for each file a source reads:

  * a fingerprint (SHA-256) of the file's bytes, computed at build time from the file
    actually read, so it is always true even if the cache was edited by hand;
  * where the file came from and when, read from a small "sidecar" note written next to the
    file (<file>.source.json) when Crossroads downloaded or created it.

These go in the `source_files` table (one row per file per source), and
`data_fingerprint()` combines them into one value for `crossroads_meta`.

A file with no sidecar (cached by an earlier version, or seeded by a test) still gets its
real fingerprint; its origin columns are NULL, never guessed.
"""

import hashlib
import json
import os
import warnings
from datetime import datetime, timezone

# The sidecar note sits next to the file it describes: "<file>.source.json".
SIDECAR_SUFFIX = ".source.json"

# Files are hashed 1 MB at a time, so even a multi-GB file is never held in memory.
_CHUNK = 1024 * 1024


def sha256_file(path):
    """Fingerprint of a file: the SHA-256 of every byte, as 64 hex characters."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now():
    """The current time in UTC, as ISO 8601 text without a zone suffix (it is always UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _write_note(path, note):
    """Write the sidecar dict for `path`. Written to a .part file first and moved into
    place, like every other cache file, so a crash never leaves a half-written sidecar."""
    sidecar = path + SIDECAR_SUFFIX
    tmp = sidecar + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(note, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, sidecar)


def write_sidecar(path, *, origin_url, origin_sha256, derived_by=None):
    """Write <path>.source.json saying where `path` came from, timestamped now.

    origin_url / origin_sha256: the URL the data came from and the fingerprint of the file
        downloaded from it (None if there is no single downloaded file to fingerprint).
    derived_by: None for a direct download; otherwise a short plain-English note on how
        the file was made (e.g. "merged from 12 monthly CDS requests").
    """
    _write_note(path, {"origin_url": origin_url, "origin_sha256": origin_sha256,
                       "downloaded_at_utc": _utc_now(), "derived_by": derived_by})


def read_sidecar(path):
    """The sidecar's contents as a dict, or None if `path` has no sidecar (a file cached
    before fingerprints existed, or seeded by a test)."""
    sidecar = path + SIDECAR_SUFFIX
    if not os.path.exists(sidecar):
        return None
    with open(sidecar, encoding="utf-8") as fh:
        return json.load(fh)


def copy_origin(derived_path, from_path, derived_by):
    """Write a sidecar for a file made from another file (split, unzip, merge).

    The derived file keeps the ORIGINAL download's URL, fingerprint and time (the day the
    data left the publisher), plus `derived_by` saying how it was made. If `from_path` has
    no sidecar, the origin is unknown, so those fields are written as null.
    """
    origin = read_sidecar(from_path) or {}
    _write_note(derived_path, {"origin_url": origin.get("origin_url"),
                               "origin_sha256": origin.get("origin_sha256"),
                               "downloaded_at_utc": origin.get("downloaded_at_utc"),
                               "derived_by": derived_by})


def ensure_source_files_table(con):
    """Create the source_files table if it does not exist yet (idempotent)."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS source_files ("
        " source_id VARCHAR,"
        " file_name VARCHAR,"
        " sha256 VARCHAR,"
        " size_bytes BIGINT,"
        " origin_url VARCHAR,"
        " origin_sha256 VARCHAR,"
        " downloaded_at_utc TIMESTAMP,"
        " derived_by VARCHAR)"
    )


def record_source_files(con, source_id, paths):
    """Replace this source's rows in source_files with one row per file in `paths`.

    Each row holds the file's fingerprint computed NOW (from the file actually read), its
    size, and whatever its sidecar says about where it came from. Warns if a direct download
    no longer matches the fingerprint taken while downloading it, i.e. the file was changed
    after download. The build carries on: the recorded sha256 is still the truth about
    which data was used.
    """
    ensure_source_files_table(con)
    con.execute("DELETE FROM source_files WHERE source_id = ?", [source_id])
    for path in paths:
        sha = sha256_file(path)
        note = read_sidecar(path) or {}
        if (note.get("derived_by") is None and note.get("origin_sha256")
                and note["origin_sha256"] != sha):
            warnings.warn(
                f"{source_id}: {os.path.basename(path)} has changed since it was downloaded "
                f"(its fingerprint no longer matches the one taken at download). The build "
                f"uses the file as it is now; delete it to download a fresh copy.",
                stacklevel=2)
        con.execute(
            "INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [source_id, os.path.basename(path), sha, os.path.getsize(path),
             note.get("origin_url"), note.get("origin_sha256"),
             note.get("downloaded_at_utc"), note.get("derived_by")])


def data_fingerprint(con):
    """One SHA-256 over every source_files row, or None if there are none.

    Built from sorted "source_id|file_name|sha256" lines, so it depends only on which files
    were used: the same files give the same value on any machine, in any order.
    """
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = 'source_files'"
    ).fetchone()[0]
    if not exists:
        return None
    rows = con.execute(
        "SELECT source_id, file_name, sha256 FROM source_files").fetchall()
    if not rows:
        return None
    lines = sorted(f"{sid}|{name}|{sha}" for sid, name, sha in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
