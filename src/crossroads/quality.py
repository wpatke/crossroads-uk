"""Source-agnostic data-quality infrastructure (spec §9).

This module owns the SHARED audit machinery that every data source reuses:
the manifest dataclasses a source uses to declare what should be audited,
the shared audit tables and their writers (Stage 02), and the build-end
invariant checks (Stage 03). It deliberately does NOT generate bronze/silver
table DDL — column shapes differ per source, so each transformer writes its
own bronze/silver tables and simply describes them with a SourceQuality.
"""

from dataclasses import dataclass

# Default reject-rate ceiling (spec §9-3). A source/dimension may override it;
# a build() call may override the default globally. 0.05 == 5%.
DEFAULT_REJECT_CEILING = 0.05


@dataclass(frozen=True)
class Dimension:
    """One validated aspect of a silver table (e.g. geometry, a date field).

    A dimension pairs a boolean flag column in the silver table with the
    data_quality_log rule_ids that explain why that flag is FALSE.
    """

    name: str                       # human label, e.g. "geom"
    flag_column: str                # silver boolean column, e.g. "geom_valid"
    rule_ids: tuple[str, ...]       # ledger rule_ids that null THIS dimension
    reject_ceiling: float | None = None  # None -> use the engine/global default


@dataclass(frozen=True)
class SourceQuality:
    """A source's declaration of what the quality engine should audit.

    A transformer returns one of these from quality_spec() to be audited. The
    engine never names a source itself; this manifest is the only coupling.
    """

    source_id: str                          # e.g. "stats19"
    bronze_table: str                       # faithful raw copy, e.g. "stats19_raw"
    silver_table: str                       # typed facts, e.g. "collisions"
    dimensions: tuple[Dimension, ...] = ()  # validated dimensions (may be empty)
    key_column: str = "source_row_key"      # stable key shared by silver + ledger


@dataclass(frozen=True)
class QualityExemption:
    """A source's DELIBERATE opt-out from the quality invariants, on the record.

    A transformer returns this from quality_spec() when it legitimately does not
    fit the keep-in-place invariants — e.g. it aggregates many bronze rows into
    one silver row (so count(bronze) == count(silver) is false by design), or it
    loads a static reference/lookup table. The reason is recorded in the
    quality_exemptions table (Stage 02) so the opt-out is auditable, not silent.
    """

    reason: str  # e.g. "aggregates many bronze rows into one silver row; conservation N/A"


def create_clean_view(con, view_name, silver_table, flag_columns):
    """Create (or replace) a GOLD view: silver rows where every flag is TRUE.

    Example (spec §9): create_clean_view(con, "collisions_spatial",
        "collisions", ["geom_valid"]) builds a view of valid-geometry rows.

    Identifiers (view/table/column names) are interpolated into SQL because
    DuckDB cannot bind identifiers as parameters. These come from code-supplied
    manifests, NOT from end users, so this is a trusted interpolation. Row
    VALUES are never interpolated anywhere in this module — always bound with ?.
    """
    # No flags -> the view is just every silver row (WHERE TRUE).
    where = " AND ".join(flag_columns) if flag_columns else "TRUE"
    con.execute(
        f"CREATE OR REPLACE VIEW {view_name} AS "
        f"SELECT * FROM {silver_table} WHERE {where}"
    )
