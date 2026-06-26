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
