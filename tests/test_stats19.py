"""Tests for the STATS19 transformer (stats19.py).

Offline: the cache is pre-seeded with committed sample CSVs so extract() finds the
files and performs no network download.
"""
import os
import shutil

import pytest
import crossroads
from crossroads.transformers.stats19 import Stats19Transformer

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
YEARS = [2023]


def _seed_cache(cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    for ftype in ("collision", "vehicle", "casualty"):
        name = f"dft-road-casualty-statistics-{ftype}-2023.csv"
        shutil.copy(os.path.join(FIXTURES, name), os.path.join(cache_dir, name))


def _stats19_client(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_cache(cache)
    client = crossroads.init_engine(cache_dir=cache)     # in-memory DB, seeded cache
    client.registry._transformers = [Stats19Transformer()]   # stats19 only this stage
    return client


def test_bronze_and_minimal_silver_end_to_end(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)     # runs invariants; raises if conservation fails

    for bronze, silver in (
        ("stats19_collision_raw", "collisions"),
        ("stats19_vehicle_raw", "vehicles"),
        ("stats19_casualty_raw", "casualties"),
    ):
        b = client.con.execute(f"SELECT count(*) FROM {bronze}").fetchone()[0]
        s = client.con.execute(f"SELECT count(*) FROM {silver}").fetchone()[0]
        assert b > 0 and b == s, f"{bronze}={b} must equal {silver}={s} (keep-in-place)"

    # Three audit units recorded a source-row count each.
    sids = {r[0] for r in client.con.execute(
        "SELECT DISTINCT source_id FROM source_ingest_log").fetchall()}
    assert {"stats19_collision", "stats19_vehicle", "stats19_casualty"} <= sids
    client.close()


def test_source_row_keys_are_unique(tmp_path):
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    for silver in ("collisions", "vehicles", "casualties"):
        dupes = client.con.execute(
            f"SELECT count(*) - count(DISTINCT source_row_key) FROM {silver}"
        ).fetchone()[0]
        assert dupes == 0, f"{silver} has duplicate source_row_key values"
    client.close()


def test_identity_normalized_to_accident_index(tmp_path):
    # Canonical silver identity is accident_index, never NULL for the sample.
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    nulls = client.con.execute(
        "SELECT count(*) FROM collisions WHERE accident_index IS NULL"
    ).fetchone()[0]
    assert nulls == 0
    client.close()


def test_stats19_inactive_without_years():
    # No years -> nothing to ingest -> transformer is skipped.
    assert Stats19Transformer().is_active() is False
    assert Stats19Transformer().is_active(years=[2023]) is True


def test_quality_spec_declares_three_units():
    specs = Stats19Transformer().quality_spec()
    assert len(specs) == 3
    ids = {s.source_id for s in specs}
    assert ids == {"stats19_collision", "stats19_vehicle", "stats19_casualty"}


def test_collision_reference_alias_branch(con):
    # Prove the collision_* -> accident_* normalization on a synthetic modern-schema
    # bronze (no committed collision_* fixture needed). Independent reimplementation of
    # the reference package's index-normalization behaviour (inspiration only, no copy).
    from crossroads.quality import ensure_quality_tables
    ensure_quality_tables(con)
    con.execute(
        "CREATE TABLE stats19_collision_raw AS "
        "SELECT * FROM (VALUES ('2024A1','2024','ref1')) "
        "AS t(collision_index, collision_year, collision_reference)")
    t = Stats19Transformer()
    t._derive_collision_silver(con)
    row = con.execute(
        "SELECT accident_index, accident_year, source_row_key FROM collisions").fetchone()
    assert row[0] == "2024A1" and row[1] == "2024" and row[2] == "2024A1"


@pytest.mark.integration
def test_download_real_dft_sample(tmp_path):
    t = Stats19Transformer()
    cache = str(tmp_path / "cache")
    t.extract(cache, years=[2023])
    assert os.path.exists(os.path.join(
        cache, "dft-road-casualty-statistics-collision-2023.csv"))
