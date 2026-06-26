import pytest

from crossroads import quality
from crossroads.quality import (
    Dimension, SourceQuality, QualityExemption, create_clean_view,
)
from crossroads.transformers.base import BaseTransformer


def test_module_constant():
    assert quality.DEFAULT_REJECT_CEILING == 0.05


def test_quality_exemption_holds_reason():
    ex = QualityExemption(reason="aggregates rows; conservation N/A")
    assert ex.reason == "aggregates rows; conservation N/A"
    with pytest.raises(Exception):
        ex.reason = "changed"  # frozen


class _MinimalTransformer(BaseTransformer):
    """A concrete transformer that implements only the required members."""

    @property
    def source_id(self):
        return "minimal"

    def extract(self, cache_dir, **kwargs):
        pass

    def transform_and_load(self, con, cache_dir):
        pass


def test_base_quality_spec_defaults_to_none():
    # The concrete base method exists and returns None ("undecided") unless
    # a subclass overrides it. This keeps the source discoverable; build-time
    # enforcement (Stage 03) is what turns 'undecided' into a failure.
    assert _MinimalTransformer().quality_spec() is None


def test_dimension_is_frozen():
    d = Dimension(name="geom", flag_column="geom_valid", rule_ids=("stats19.coord.sentinel",))
    assert d.reject_ceiling is None  # defaults to engine/global default
    with pytest.raises(Exception):
        d.flag_column = "other"  # frozen dataclass -> assignment fails


def test_source_quality_defaults():
    spec = SourceQuality(
        source_id="stats19",
        bronze_table="stats19_raw",
        silver_table="collisions",
        dimensions=(
            Dimension("geom", "geom_valid", ("stats19.coord.sentinel",)),
        ),
    )
    assert spec.key_column == "source_row_key"
    assert spec.dimensions[0].flag_column == "geom_valid"


def test_create_clean_view_filters_invalid_rows(con):
    # Build a tiny synthetic silver table: 3 rows, one with geom_valid = FALSE.
    con.execute(
        "CREATE TABLE collisions ("
        " source_row_key VARCHAR, geom_valid BOOLEAN)"
    )
    con.execute(
        "INSERT INTO collisions VALUES "
        "('a', TRUE), ('b', FALSE), ('c', TRUE)"
    )

    create_clean_view(con, "collisions_spatial", "collisions", ["geom_valid"])

    keys = [r[0] for r in con.execute(
        "SELECT source_row_key FROM collisions_spatial ORDER BY source_row_key"
    ).fetchall()]
    assert keys == ["a", "c"]  # the FALSE-flagged row 'b' is excluded


def test_create_clean_view_no_flags_passes_all(con):
    con.execute("CREATE TABLE t (k VARCHAR)")
    con.execute("INSERT INTO t VALUES ('x'), ('y')")
    create_clean_view(con, "t_clean", "t", [])
    assert con.execute("SELECT count(*) FROM t_clean").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Stage 02 — Shared audit tables and writer helpers
# ---------------------------------------------------------------------------

from crossroads.quality import (
    ensure_quality_tables,
    record_source_rows,
    log_exclusion,
    quarantine_row,
    record_exemption,
    reset_source_audit,
)


def test_ensure_quality_tables_idempotent(con):
    ensure_quality_tables(con)
    ensure_quality_tables(con)  # second call must not error
    # All four tables exist and are queryable (empty).
    for table in ("source_ingest_log", "data_quality_log", "quarantine_raw",
                  "quality_exemptions"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_record_source_rows_is_idempotent_per_source(con):
    ensure_quality_tables(con)
    record_source_rows(con, "stats19", 100)
    record_source_rows(con, "stats19", 150)  # overwrites, does not append
    rows = con.execute(
        "SELECT source_rows FROM source_ingest_log WHERE source_id = 'stats19'"
    ).fetchall()
    assert rows == [(150,)]


def test_log_exclusion_round_trips(con):
    ensure_quality_tables(con)
    log_exclusion(
        con,
        source_id="stats19",
        source_row_key="2023-0001",
        column_name="easting",
        rule_id="stats19.coord.sentinel",
        rule_desc="Easting/Northing of 0 or -1 (data missing or out of range)",
        severity="reject_dimension",
        raw_value="-1",
    )
    row = con.execute(
        "SELECT source_id, source_row_key, rule_id, severity, raw_value "
        "FROM data_quality_log"
    ).fetchone()
    assert row == ("stats19", "2023-0001", "stats19.coord.sentinel",
                   "reject_dimension", "-1")
    # ingested_at auto-populated (provenance metadata; value not asserted).
    assert con.execute(
        "SELECT ingested_at IS NOT NULL FROM data_quality_log"
    ).fetchone()[0] is True


def test_quarantine_row_round_trips(con):
    ensure_quality_tables(con)
    quarantine_row(
        con,
        source_id="stats19",
        raw_text="totally,broken,,line",
        reason="wrong column count",
    )
    row = con.execute(
        "SELECT source_id, raw_text, reason FROM quarantine_raw"
    ).fetchone()
    assert row == ("stats19", "totally,broken,,line", "wrong column count")


def test_record_exemption_is_idempotent_per_source(con):
    ensure_quality_tables(con)
    record_exemption(con, "reference_lookup", "static lookup table; no source rows")
    record_exemption(con, "reference_lookup", "updated reason")  # overwrites
    rows = con.execute(
        "SELECT source_id, reason FROM quality_exemptions"
    ).fetchall()
    assert rows == [("reference_lookup", "updated reason")]


def test_reset_source_audit_clears_only_that_source(con):
    ensure_quality_tables(con)
    # Seed two sources across all four audit tables.
    for sid in ("keep", "drop"):
        record_source_rows(con, sid, 1)
        log_exclusion(con, source_id=sid, source_row_key="k", rule_id="r.bad",
                      rule_desc="bad", severity="reject_dimension")
        quarantine_row(con, source_id=sid, raw_text="x", reason="bad")
        record_exemption(con, sid, "n/a")

    reset_source_audit(con, "drop")

    # 'drop' is gone from every shared audit table; 'keep' is untouched.
    for table in ("source_ingest_log", "data_quality_log",
                  "quarantine_raw", "quality_exemptions"):
        assert con.execute(
            f"SELECT count(*) FROM {table} WHERE source_id = 'drop'"
        ).fetchone()[0] == 0
        assert con.execute(
            f"SELECT count(*) FROM {table} WHERE source_id = 'keep'"
        ).fetchone()[0] == 1
