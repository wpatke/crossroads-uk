"""docs/schema.md exists, declares the current schema version, and names the core tables.

Two tiers:
  * fast default-suite tests (CI-covered): the doc declares the current SCHEMA_VERSION and
    mentions the core tables by name;
  * an @pytest.mark.integration drift guard (run before release): builds the full offline
    fixture DB and asserts every real column of the silver/provenance/reference tables is
    documented, so the hand-written doc cannot silently diverge from what the code produces.
"""
import os
import re
import shutil

import pytest

import crossroads
from crossroads import console
# Reuse the console-test harness (scripted I/O + full-cache seeding). tests/ has no
# __init__.py, but conftest.py puts the repo root on sys.path so `tests.` imports resolve.
from tests.test_console import _seed_full_cache, scripted

ROOT = os.path.dirname(os.path.dirname(__file__))

# Named in the fast test: silver + gold + provenance/reference tables a researcher relies on.
CORE_TABLES = [
    "collisions", "vehicles", "casualties", "weather", "lad_boundaries", "ctyua_boundaries",
    "collisions_spatial", "crossroads_meta", "data_quality_log", "quarantine_raw",
    "source_ingest_log", "codebook", "column_manifest",
]

# Tables whose EVERY column must be documented (silver + provenance/quality + reference).
COLUMN_GUARDED = [
    "collisions", "vehicles", "casualties", "weather", "lad_boundaries", "ctyua_boundaries",
    "crossroads_meta", "data_quality_log", "quarantine_raw", "source_ingest_log",
    "quality_exemptions", "stats19_completeness", "codebook", "column_manifest",
]
# Bronze copies of source columns — documented as a category, excluded from the column guard.
EXCLUDED_PREFIXES = ("stats19_", "ons_", "era5_")  # *_raw bronze tables


def _schema_text():
    with open(os.path.join(ROOT, "docs", "schema.md"), encoding="utf-8") as fh:
        return fh.read()


def test_schema_doc_declares_current_version():
    text = _schema_text()
    m = re.search(r"Schema version:\D*(\d+)", text)
    assert m, "docs/schema.md must state 'Schema version: N'"
    assert int(m.group(1)) == crossroads.SCHEMA_VERSION


def test_schema_doc_mentions_core_tables():
    text = _schema_text()
    missing = [t for t in CORE_TABLES if t not in text]
    assert not missing, f"docs/schema.md does not document core tables: {missing}"


@pytest.mark.integration
def test_documented_columns_match_built_database(tmp_path):
    """Build the full offline fixture DB (weather + stats19 + ONS) and assert every real
    column of the guarded tables appears in docs/schema.md."""
    cache = str(tmp_path / "cache")
    _seed_full_cache(cache)
    # Seed weather too, so the weather table exists in the built DB.
    shutil.copy(
        os.path.join(ROOT, "tests", "fixtures", "weather", "era5_land_sample.nc"),
        os.path.join(cache, "era5_land_2023.nc"),
    )
    db = str(tmp_path / "full.duckdb")
    # Menu order is source_id: 1=bank_holidays, 2=weather (era5_weather), 3=stats19.
    # "2-3" builds weather+stats19 (the tables this drift guard checks).
    reader, writer, _ = scripted([db, "2-3", "2023", "snapshot", "y"])
    client = console.run_wizard(reader, writer, cache_dir=cache)
    try:
        text = _schema_text()
        problems = []
        for table in COLUMN_GUARDED:
            cols = [r[0] for r in client.con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='main' AND table_name=?", [table]).fetchall()]
            assert cols, f"{table} not found in the built DB — fixture/build changed"
            undocumented = [c for c in cols if c not in text]
            if undocumented:
                problems.append(f"{table}: undocumented columns {undocumented}")
        assert not problems, ("docs/schema.md drifted from the built database:\n"
                              + "\n".join(problems))
    finally:
        client.close()
