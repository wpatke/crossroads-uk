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


# ---------------------------------------------------------------------------
# Stage 02 — Shared audit tables and their writer helpers
# ---------------------------------------------------------------------------


def ensure_quality_tables(con):
    """Create the shared audit tables if they do not already exist.

    Idempotent: safe to call on every build(). These four tables are
    source-agnostic (one set per database). Bronze/silver tables are NOT
    created here — each source owns its own bronze/silver DDL.

    ingested_at uses a DuckDB DEFAULT of current_timestamp: the database
    stamps it, not Python. It is provenance metadata only and is explicitly
    excluded from the structural-reproducibility guarantee (spec §2) — never
    assert on its value in tests.
    """
    # How many rows each source READ from its source files. The conservation
    # invariant (Stage 03) compares this against bronze + quarantine counts.
    con.execute(
        "CREATE TABLE IF NOT EXISTS source_ingest_log ("
        " source_id VARCHAR,"
        " source_rows BIGINT,"
        " ingested_at TIMESTAMP DEFAULT current_timestamp)"
    )
    # The exclusion ledger: one row per rule violation (spec §9).
    con.execute(
        "CREATE TABLE IF NOT EXISTS data_quality_log ("
        " source_id VARCHAR,"
        " source_row_key VARCHAR,"
        " column_name VARCHAR,"
        " rule_id VARCHAR,"
        " rule_desc VARCHAR,"
        " severity VARCHAR,"
        " raw_value VARCHAR,"
        " ingested_at TIMESTAMP DEFAULT current_timestamp)"
    )
    # Rows that could not be structured into bronze at all (rare).
    con.execute(
        "CREATE TABLE IF NOT EXISTS quarantine_raw ("
        " source_id VARCHAR,"
        " raw_text VARCHAR,"
        " reason VARCHAR,"
        " ingested_at TIMESTAMP DEFAULT current_timestamp)"
    )
    # The auditable record of which sources DELIBERATELY opted out of the
    # invariants (one row per exempted source) and why. Spec §9: the database
    # itself answers "what was not processed, and why?".
    con.execute(
        "CREATE TABLE IF NOT EXISTS quality_exemptions ("
        " source_id VARCHAR,"
        " reason VARCHAR,"
        " ingested_at TIMESTAMP DEFAULT current_timestamp)"
    )


def record_source_rows(con, source_id, source_rows):
    """Record the number of rows a source READ from its source files.

    Idempotent per source: any existing row for this source_id is removed
    first, so re-running build() against an on-disk database does not
    double-count. Values are bound parameters (never interpolated).
    """
    con.execute("DELETE FROM source_ingest_log WHERE source_id = ?", [source_id])
    con.execute(
        "INSERT INTO source_ingest_log (source_id, source_rows) VALUES (?, ?)",
        [source_id, source_rows],
    )


def log_exclusion(con, *, source_id, source_row_key, rule_id, rule_desc,
                  severity, column_name=None, raw_value=None):
    """Write one exclusion-ledger row (a rule violation).

    severity is 'reject_dimension' (the value failed and its clean column is
    NULL / flag is FALSE) or 'warn' (informational; does not null a column).
    All values are bound parameters.
    """
    con.execute(
        "INSERT INTO data_quality_log "
        "(source_id, source_row_key, column_name, rule_id, rule_desc,"
        " severity, raw_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [source_id, source_row_key, column_name, rule_id, rule_desc,
         severity, raw_value],
    )


def quarantine_row(con, *, source_id, raw_text, reason):
    """Write one quarantine_raw row (an unparseable source line). Rare."""
    con.execute(
        "INSERT INTO quarantine_raw (source_id, raw_text, reason) "
        "VALUES (?, ?, ?)",
        [source_id, raw_text, reason],
    )


def record_exemption(con, source_id, reason):
    """Record a source's deliberate opt-out from the quality invariants.

    Idempotent per source: any existing exemption for this source_id is removed
    first, so re-running build() does not accumulate duplicate rows. The Stage 03
    coverage gate calls this whenever a transformer's quality_spec() returns a
    QualityExemption. Values are bound parameters.
    """
    con.execute("DELETE FROM quality_exemptions WHERE source_id = ?", [source_id])
    con.execute(
        "INSERT INTO quality_exemptions (source_id, reason) VALUES (?, ?)",
        [source_id, reason],
    )


# The four shared audit tables, all keyed by source_id.
_SOURCE_AUDIT_TABLES = (
    "source_ingest_log",
    "data_quality_log",
    "quarantine_raw",
    "quality_exemptions",
)


def reset_source_audit(con, source_id):
    """Clear all shared-audit rows for one source before it is (re)built.

    The engine calls this at the top of the build loop for each active source
    (Stage 03). It makes a re-build of the same on-disk database idempotent per
    source: log_exclusion / quarantine_row are plain appends, so without this
    reset they would accumulate across builds and break the invariants.

    Resets only the tables the ENGINE owns. Each transformer is responsible for
    recreating its OWN bronze/silver tables idempotently (CREATE OR REPLACE /
    DROP+CREATE) in transform_and_load. Table names come from a code-controlled
    tuple (trusted interpolation); the source_id is a bound parameter.
    """
    for table in _SOURCE_AUDIT_TABLES:
        con.execute(f"DELETE FROM {table} WHERE source_id = ?", [source_id])
