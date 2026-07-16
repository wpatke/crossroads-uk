"""Reviewer point 1: file paths interpolated into SQL must be escaped, so an install
or cache path containing an apostrophe (e.g. C:\\Users\\Tom O'Brien\\...) does not
produce malformed SQL and crash the build before it starts.

See docs/plans/014_review_fixes/01-sql-escaping.md. Offline: the cache is pre-seeded
with committed sample CSVs, so no network download occurs.
"""
import os
import shutil

import duckdb

import crossroads
from crossroads.sql import sql_str
from crossroads.transformers.stats19 import Stats19Transformer
from crossroads.transformers.aadf import AadfTransformer, CSV_CACHE_FILE
from crossroads.quality import ensure_quality_tables

STATS19_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
AADF_SAMPLE = os.path.join(os.path.dirname(__file__), "fixtures", "aadf",
                           "dft_traffic_counts_aadf_sample.csv")


def test_sql_str_doubles_quotes():
    assert sql_str("plain") == "'plain'"
    assert sql_str("O'Brien") == "'O''Brien'"     # the reviewer's case
    assert sql_str("a'b'c") == "'a''b''c'"
    assert sql_str("") == "''"


def _apostrophe_dir(tmp_path):
    """A cache directory whose name contains an apostrophe, like a Windows user folder."""
    d = str(tmp_path / "Tom O'Brien")
    os.makedirs(d, exist_ok=True)
    return d


def test_stats19_build_survives_apostrophe_in_path(tmp_path):
    # Seed the three STATS19 CSVs into an apostrophe-containing cache dir, then build.
    # Before the fix this raised a DuckDB Parser Error from the unescaped read_csv path.
    cache = _apostrophe_dir(tmp_path)
    for ftype in ("collision", "vehicle", "casualty"):
        name = f"dft-road-casualty-statistics-{ftype}-2023.csv"
        shutil.copy(os.path.join(STATS19_FIXTURES, name), os.path.join(cache, name))

    client = crossroads.init_engine(cache_dir=cache)      # in-memory DB, seeded cache
    client.registry._transformers = [Stats19Transformer()]    # stats19 only
    client.build(years=[2023])            # loads codebook + manifest + 3 bronzes from the path
    n = client.con.execute("SELECT count(*) FROM collisions").fetchone()[0]
    assert n > 0
    client.close()


def test_aadf_build_survives_apostrophe_in_path(tmp_path):
    # The AADF bronze read_csv path must also parse from an apostrophe-containing dir.
    cache = _apostrophe_dir(tmp_path)
    shutil.copy(AADF_SAMPLE, os.path.join(cache, CSV_CACHE_FILE))

    con = duckdb.connect()
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")   # ST_Point + R-Tree
    ensure_quality_tables(con)                                    # for record_source_rows
    t = AadfTransformer()
    t.extract(cache, years=[2023])        # offline: CSV already seeded, no download
    t.transform_and_load(con, cache)      # bronze read_csv path must parse cleanly
    n = con.execute("SELECT count(*) FROM aadf").fetchone()[0]
    assert n > 0
    con.close()
