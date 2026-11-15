"""Tests for data fingerprints (crossroads.provenance). Offline, no fixtures needed."""
import hashlib
import json
import os

import duckdb
import pytest

from crossroads import provenance


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return str(path)


def test_sha256_file_matches_hashlib_small_and_multi_chunk(tmp_path):
    # Small file, and a file bigger than one 1 MB chunk, so the chunk loop is exercised.
    for data in (b"hello", os.urandom(provenance._CHUNK * 2 + 7)):
        path = _write(tmp_path / "f.bin", data)
        assert provenance.sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_sidecar_round_trips_and_leaves_no_part_file(tmp_path):
    path = _write(tmp_path / "f.csv", b"a\n")
    assert provenance.read_sidecar(path) is None           # no sidecar yet
    provenance.write_sidecar(path, origin_url="https://example/f.csv", origin_sha256="abc")
    note = provenance.read_sidecar(path)
    assert note["origin_url"] == "https://example/f.csv"
    assert note["origin_sha256"] == "abc"
    assert note["derived_by"] is None
    assert note["downloaded_at_utc"]                        # stamped with the time
    assert sorted(os.listdir(tmp_path)) == ["f.csv", "f.csv.source.json"]


def test_copy_origin_carries_over_the_original_download(tmp_path):
    zip_path = _write(tmp_path / "data.zip", b"zip")
    provenance.write_sidecar(zip_path, origin_url="https://example/data.zip", origin_sha256="z1")
    csv_path = _write(tmp_path / "data.csv", b"csv")
    provenance.copy_origin(csv_path, zip_path, "extracted from the downloaded zip")

    zip_note, csv_note = provenance.read_sidecar(zip_path), provenance.read_sidecar(csv_path)
    assert csv_note["origin_url"] == "https://example/data.zip"
    assert csv_note["origin_sha256"] == "z1"
    assert csv_note["downloaded_at_utc"] == zip_note["downloaded_at_utc"]   # not "now"
    assert csv_note["derived_by"] == "extracted from the downloaded zip"


def test_copy_origin_without_source_sidecar_records_unknown(tmp_path):
    src = _write(tmp_path / "old.zip", b"zip")               # cached before fingerprints
    out = _write(tmp_path / "old.csv", b"csv")
    provenance.copy_origin(out, src, "extracted from the downloaded zip")
    note = provenance.read_sidecar(out)
    assert note == {"origin_url": None, "origin_sha256": None, "downloaded_at_utc": None,
                    "derived_by": "extracted from the downloaded zip"}


def test_record_source_files_writes_and_replaces_rows(tmp_path):
    con = duckdb.connect()
    a = _write(tmp_path / "a.csv", b"aaa")
    b = _write(tmp_path / "b.csv", b"bb")
    provenance.write_sidecar(a, origin_url="https://example/a.csv",
                             origin_sha256=hashlib.sha256(b"aaa").hexdigest())
    provenance.record_source_files(con, "src", [a, b])
    rows = con.execute("SELECT file_name, sha256, size_bytes, origin_url FROM source_files "
                       "ORDER BY file_name").fetchall()
    assert rows == [("a.csv", hashlib.sha256(b"aaa").hexdigest(), 3, "https://example/a.csv"),
                    # b.csv has no sidecar: still fingerprinted, origin unknown (NULL).
                    ("b.csv", hashlib.sha256(b"bb").hexdigest(), 2, None)]
    # Recording the same source again replaces its rows rather than adding more.
    provenance.record_source_files(con, "src", [b])
    assert con.execute("SELECT count(*) FROM source_files").fetchone()[0] == 1


def test_record_source_files_warns_if_file_changed_after_download(tmp_path):
    con = duckdb.connect()
    path = _write(tmp_path / "f.csv", b"original")
    provenance.write_sidecar(path, origin_url="https://example/f.csv",
                             origin_sha256=hashlib.sha256(b"original").hexdigest())
    _write(path, b"edited!!")                                # changed after download
    with pytest.warns(UserWarning, match="has changed since it was downloaded"):
        provenance.record_source_files(con, "src", [path])
    # The recorded fingerprint is the file as it is NOW, the data actually used.
    assert con.execute("SELECT sha256 FROM source_files").fetchone()[0] == \
        hashlib.sha256(b"edited!!").hexdigest()


def test_data_fingerprint_depends_only_on_the_files(tmp_path):
    con = duckdb.connect()
    assert provenance.data_fingerprint(con) is None          # no table yet
    provenance.ensure_source_files_table(con)
    assert provenance.data_fingerprint(con) is None          # empty table

    a = _write(tmp_path / "a.csv", b"a")
    b = _write(tmp_path / "b.csv", b"b")
    provenance.record_source_files(con, "s1", [a])
    provenance.record_source_files(con, "s2", [b])
    first = provenance.data_fingerprint(con)

    # Same files recorded in the other order -> the same fingerprint.
    con2 = duckdb.connect()
    provenance.record_source_files(con2, "s2", [b])
    provenance.record_source_files(con2, "s1", [a])
    assert provenance.data_fingerprint(con2) == first

    # Change one file's contents -> a different fingerprint.
    _write(b, b"B")
    provenance.record_source_files(con, "s2", [b])
    assert provenance.data_fingerprint(con) != first


def test_sidecar_is_readable_json_for_a_person(tmp_path):
    # The sidecar is plain, indented JSON so someone opening the cache folder can read it.
    path = _write(tmp_path / "f.csv", b"x")
    provenance.write_sidecar(path, origin_url="https://example/f.csv", origin_sha256="abc")
    with open(path + provenance.SIDECAR_SUFFIX) as fh:
        text = fh.read()
    assert json.loads(text)["origin_url"] == "https://example/f.csv"
    assert "\n  " in text                                    # indented, one key per line
