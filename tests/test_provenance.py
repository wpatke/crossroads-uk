"""crossroads_meta provenance stamp — fast, offline, real DuckDB (no fixtures needed)."""
import json

import duckdb

import crossroads
from crossroads import quality


def test_write_build_metadata_single_row():
    con = duckdb.connect(":memory:")
    params = {"datasets": ["stats19"], "years": [2023], "boundary_mode": "snapshot"}
    quality.write_build_metadata(con, parameters=params)

    rows = con.execute(
        "SELECT crossroads_version, schema_version, built_at_utc, parameters FROM crossroads_meta"
    ).fetchall()
    assert len(rows) == 1
    version, schema_version, built_at, params_json = rows[0]
    assert version == crossroads.__version__      # git-derived; not pinned to a literal
    assert schema_version == crossroads.SCHEMA_VERSION
    assert built_at is not None                          # UTC stamp present
    assert json.loads(params_json)["years"] == [2023]    # parameters captured faithfully


def test_write_build_metadata_is_idempotent():
    # A re-build must not accumulate rows — CREATE OR REPLACE keeps exactly one.
    con = duckdb.connect(":memory:")
    quality.write_build_metadata(con, parameters={"datasets": ["stats19"]})
    quality.write_build_metadata(con, parameters={"datasets": ["stats19", "era5_weather"]})
    n = con.execute("SELECT count(*) FROM crossroads_meta").fetchone()[0]
    assert n == 1
    latest = con.execute("SELECT parameters FROM crossroads_meta").fetchone()[0]
    assert "era5_weather" in latest                       # reflects the LATEST build


# --- data fingerprints: every input file is recorded (crossroads.provenance) -

def _fixture_build(tmp_path, name):
    """A full offline build (ONS boundaries, STATS19, AADF, bank holidays) from the
    committed fixtures, into its own in-memory database."""
    from tests.test_console import _seed_full_cache   # conftest puts the repo on sys.path
    cache = str(tmp_path / name)
    _seed_full_cache(cache)
    client = crossroads.init_engine(cache_dir=cache)
    client.build(datasets=["aadf", "bank_holidays", "stats19"], years=[2023])
    return client


def test_every_source_records_its_files_and_the_build_has_a_fingerprint(tmp_path):
    client = _fixture_build(tmp_path, "cache")
    con = client.con
    # Coverage: every source that ran (audited or exempt) recorded at least one file.
    ran = {r[0] for r in con.execute(
        "SELECT source_id FROM source_ingest_log UNION SELECT source_id FROM quality_exemptions"
    ).fetchall()}
    recorded = {r[0] for r in con.execute("SELECT DISTINCT source_id FROM source_files").fetchall()}
    assert ran and ran <= recorded, f"no source_files rows for {sorted(ran - recorded)}"
    # Every row has a real 64-character fingerprint; the seeded fixtures have no sidecar,
    # so their origin is recorded as unknown (NULL), never guessed.
    assert con.execute("SELECT count(*) FROM source_files WHERE length(sha256) <> 64"
                       ).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM source_files WHERE origin_url IS NOT NULL"
                       ).fetchone()[0] == 0
    fingerprint = con.execute("SELECT data_fingerprint FROM crossroads_meta").fetchone()[0]
    assert fingerprint is not None and len(fingerprint) == 64
    client.close()


def test_same_files_give_identical_fingerprints(tmp_path):
    # Two separate builds from identical caches record identical rows and fingerprint.
    a, b = _fixture_build(tmp_path, "cache_a"), _fixture_build(tmp_path, "cache_b")
    query = "SELECT * FROM source_files ORDER BY source_id, file_name"
    assert a.con.execute(query).fetchall() == b.con.execute(query).fetchall()
    meta = "SELECT data_fingerprint FROM crossroads_meta"
    assert a.con.execute(meta).fetchone() == b.con.execute(meta).fetchone()
    a.close(); b.close()
