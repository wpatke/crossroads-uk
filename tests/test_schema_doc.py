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
    "aadf", "aadf_clean",
    "collisions_spatial", "crossroads_meta", "data_quality_log", "quarantine_raw",
    "source_ingest_log", "codebook", "column_manifest",
]

# Tables whose EVERY column must be documented (silver + provenance/quality + reference).
COLUMN_GUARDED = [
    "collisions", "vehicles", "casualties", "weather", "lad_boundaries", "ctyua_boundaries",
    "aadf",
    "crossroads_meta", "data_quality_log", "quarantine_raw", "source_ingest_log",
    "quality_exemptions", "stats19_completeness", "codebook", "column_manifest",
]
# Bronze copies of source columns — documented as a category, excluded from the column guard.
# "aadf_" (with the trailing underscore) excludes the aadf_raw bronze while leaving the silver
# table "aadf" itself column-guarded above.
EXCLUDED_PREFIXES = ("stats19_", "ons_", "era5_", "aadf_")  # *_raw bronze tables


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
def test_documented_columns_match_built_database(tmp_path, monkeypatch):
    """Build the full offline fixture DB (weather + stats19 + ONS) and assert every real
    column of the guarded tables appears in docs/schema.md."""
    # The weather source imports xarray, which lives in the optional [weather] extra.
    # Skip cleanly (rather than crashing) if it isn't installed. The release CI job
    # installs .[dev,weather], so the drift guard still runs there — see .github/workflows/tests.yml.
    pytest.importorskip("xarray")
    # A configured CDS key bypasses the wizard's credential prompt so this build stays
    # offline and hermetic. Without it the wizard falls through to getpass, which crashes
    # under pytest on a machine with no ~/.cdsapirc (e.g. CI). The fake key is never used —
    # weather data comes from the seeded .nc fixture. Mirrors test_wizard_builds_weather_offline.
    monkeypatch.setenv("CDSAPI_URL", "https://example.invalid/api")
    monkeypatch.setenv("CDSAPI_KEY", "test-key")
    cache = str(tmp_path / "cache")
    _seed_full_cache(cache)
    # Seed weather too, so the weather table exists in the built DB.
    shutil.copy(
        os.path.join(ROOT, "tests", "fixtures", "weather", "era5_land_sample.nc"),
        os.path.join(cache, "era5_land_2023.nc"),
    )
    db = str(tmp_path / "full.duckdb")
    # Menu order is source_id: 1=aadf, 2=bank_holidays, 3=weather (era5_weather), 4=stats19.
    # "1,3-4" builds aadf+weather+stats19 (the tables this drift guard checks).
    reader, writer, _ = scripted([db, "1,3-4", "2023", "snapshot", "y"])
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
