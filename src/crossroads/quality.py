"""Source-agnostic data-quality infrastructure (spec §9).

This module owns the SHARED audit machinery that every data source reuses:
the manifest dataclasses a source uses to declare what should be audited,
the shared audit tables and their writers, and the build-end invariant
checks. It deliberately does NOT generate bronze/silver
table DDL — column shapes differ per source, so each transformer writes its
own bronze/silver tables and simply describes them with a SourceQuality.
"""

import logging
from dataclasses import dataclass

from crossroads.sql import sql_str

# Default reject-rate ceiling (spec §9-3). A source/dimension may override it;
# a build() call may override the default globally. 0.05 == 5%.
DEFAULT_REJECT_CEILING = 0.05

# Default quarantine-rate ceiling (spec §9). Quarantine (a line that could not be
# structured into bronze at all) is a DIFFERENT signal from a field-level reject: a
# structurally-broken line in a well-formed government CSV should be near-zero, so this
# ceiling is deliberately much tighter than the reject ceiling. 0.005 == 0.5% — still
# ~100x headroom over a handful of stray lines in a large file, but it catches a real
# upstream format change (a large fraction failing to parse) far sooner than 5% would.
DEFAULT_QUARANTINE_CEILING = 0.005


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
    quality_exemptions table so the opt-out is auditable, not silent.
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
# Shared audit tables and their writer helpers
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
    # How many rows each source READ from its source files. The build-end
    # conservation invariant compares this against bronze + quarantine counts.
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


def write_build_metadata(con, *, parameters):
    """Stamp a single-row crossroads_meta table describing what built this database.

    Provenance only — like the reference tables (codebook, manifest, boundaries), it has no
    source_id, no bronze/silver pair, and is NOT part of the conservation invariant. Re-running
    build() replaces the row (CREATE OR REPLACE), so the table always reflects the latest build.

    built_at_utc differs run-to-run by design, so it is a provenance field explicitly excluded
    from spec §2's "structurally identical database" guarantee (which already carves out
    machine-stamped provenance timestamps). It does not weaken reproducibility; it records it.
    """
    import json
    import crossroads  # lazy import: avoids a circular import at module load time

    con.execute(
        "CREATE OR REPLACE TABLE crossroads_meta ("
        " crossroads_version VARCHAR,"
        " schema_version INTEGER,"
        " built_at_utc TIMESTAMP,"
        " parameters VARCHAR)"
    )
    con.execute(
        "INSERT INTO crossroads_meta VALUES (?, ?, now() AT TIME ZONE 'UTC', ?)",
        [crossroads.__version__, crossroads.SCHEMA_VERSION,
         json.dumps(parameters, default=str, sort_keys=True)],
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


def load_csv_bronze_with_quarantine(con, *, bronze_table, paths, read_opts, source_id):
    """Create `bronze_table` from rejects-capturing read_csv(s) and quarantine every
    rejected line (spec §9 — an unparseable line is recorded in quarantine_raw, never
    silently dropped and never fatal). Returns (loaded_rows, quarantined_rows).

    `paths` is a list of source CSV file paths. `read_opts` is the per-file column-options
    string (e.g. "header=true, all_varchar=true"). all_varchar=true means type casts never
    fail, so a "reject" here is a STRUCTURAL problem (wrong column count, unterminated
    quote) — exactly "a row that cannot be structured into bronze" (spec §9). Rows that
    parse but hold bad VALUES reach bronze as strings and are handled later by silver
    validation + data_quality_log.

    Each file is loaded into its own staging table with rejects captured, then the staging
    tables are combined with UNION ALL BY NAME (columns matched by name, missing filled
    NULL — the same result the old union_by_name=true gave). This per-file shape is
    REQUIRED: DuckDB rejects the rejects_table option together with union_by_name, so the
    name-union is done at the SQL level instead. DuckDB also auto-creates a companion scan
    table whose default name ('reject_scans') collides on the second store_rejects load in
    one connection, so both the rejects and scan tables are named uniquely per staging load
    and dropped after use. Identifiers are code-controlled; paths are escaped with sql_str;
    no row values are ever interpolated.
    """
    quarantined = 0
    stages = []
    for i, path in enumerate(paths):
        stage = f"{bronze_table}_stage_{i}"
        rej = f"{stage}_rejects"
        scan = f"{rej}_scan"
        for t in (stage, rej, scan):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.execute(
            f"CREATE TABLE {stage} AS SELECT * FROM read_csv({sql_str(path)}, {read_opts}, "
            f"ignore_errors=true, store_rejects=true, "
            f"rejects_table='{rej}', rejects_scan='{scan}')")
        # One reject_errors row per bad VALUE; a single bad line can appear more than once
        # (e.g. several offending columns). Group by (file_id, line) — the physical source
        # line — so each rejected line is quarantined exactly once, while two distinct lines
        # with identical text still count as two (grouping by csv_line would undercount them
        # and break conservation).
        rejected = con.execute(
            f"SELECT any_value(csv_line), any_value(error_message) FROM {rej} "
            f"GROUP BY file_id, line").fetchall()
        for raw_line, reason in rejected:
            quarantine_row(con, source_id=source_id, raw_text=raw_line,
                           reason=f"unparseable CSV line: {reason}")
        quarantined += len(rejected)
        con.execute(f"DROP TABLE IF EXISTS {rej}")
        con.execute(f"DROP TABLE IF EXISTS {scan}")
        stages.append(stage)
    # Combine the per-file staging tables, matching columns by NAME (fills absent columns
    # with NULL) — the historical accident_* / modern collision_* tranches coexist exactly
    # as they did under union_by_name.
    union_sql = " UNION ALL BY NAME ".join(f"SELECT * FROM {s}" for s in stages)
    con.execute(f"CREATE OR REPLACE TABLE {bronze_table} AS {union_sql}")
    for s in stages:
        con.execute(f"DROP TABLE IF EXISTS {s}")
    loaded = con.execute(f"SELECT count(*) FROM {bronze_table}").fetchone()[0]
    return loaded, quarantined


def record_exemption(con, source_id, reason):
    """Record a source's deliberate opt-out from the quality invariants.

    Idempotent per source: any existing exemption for this source_id is removed
    first, so re-running build() does not accumulate duplicate rows. The
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

    The engine calls this at the top of the build loop for each active source.
    It makes a re-build of the same on-disk database idempotent per
    source: log_exclusion / quarantine_row are plain appends, so without this
    reset they would accumulate across builds and break the invariants.

    Resets only the tables the ENGINE owns. Each transformer is responsible for
    recreating its OWN bronze/silver tables idempotently (CREATE OR REPLACE /
    DROP+CREATE) in transform_and_load. Table names come from a code-controlled
    tuple (trusted interpolation); the source_id is a bound parameter.
    """
    for table in _SOURCE_AUDIT_TABLES:
        con.execute(f"DELETE FROM {table} WHERE source_id = ?", [source_id])


# ---------------------------------------------------------------------------
# Invariants, coverage gate, and exception hierarchy
# ---------------------------------------------------------------------------

# Coverage-gate escalation flag (see resolve_quality_specs). Every active
# transformer must return SourceQuality or QualityExemption from quality_spec().
# spatial.py proves the SourceQuality shape end-to-end, so None ("undecided")
# is fatal.
UNDECIDED_QUALITY_SPEC_IS_FATAL = True    # always enforced


class QualityInvariantError(Exception):
    """Base for all build-halting data-quality failures (spec §9)."""


class ConservationError(QualityInvariantError):
    """source_rows != bronze + quarantine, or bronze != silver (rows vanished)."""


class FlagLedgerAgreementError(QualityInvariantError):
    """A silver flag and the exclusion ledger disagree about a rejected row."""


class RejectRateExceededError(QualityInvariantError):
    """A source/dimension reject rate exceeded its configured ceiling."""


class QuarantineRateExceededError(QualityInvariantError):
    """quarantined / source_rows exceeded the quarantine ceiling — a likely upstream
    format change, not the rare stray line the keep-in-place design tolerates."""


class SchemaContractError(QualityInvariantError):
    """A silver table is missing a column its quality manifest relies on — the
    key_column, or a dimension's flag_column. Raised BEFORE the three invariants
    so a forgotten silver-schema convention fails with a clear, named error
    instead of a cryptic DuckDB binder error deep inside the agreement SQL."""


class UndecidedQualitySpecError(QualityInvariantError):
    """An active transformer's quality_spec() returned None (undecided): it must
    return a SourceQuality(...) to be audited or a QualityExemption(reason=...)
    to opt out explicitly."""


def _count(con, table):
    """Row count of a table (identifier interpolated — code-supplied, trusted)."""
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _table_columns(con, table):
    """The set of column names in a table, via DuckDB's information schema.

    The table name is passed as a bound parameter (it is matched as a value here,
    not interpolated as an identifier). Identifiers compare case-insensitively in
    DuckDB, so callers should compare lower-cased.
    """
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ?",
        [table],
    ).fetchall()
    return {r[0].lower() for r in rows}


def check_schema_contract(con, spec):
    """Pre-flight (fatal): the silver table must carry every column the manifest
    relies on — the key_column (used by flag/ledger agreement and the keep-in-place
    key join) and each dimension's flag_column (used by agreement and reject-rate).

    Run by run_invariants BEFORE the three invariants. Without it, a silver table
    that omits a declared column surfaces as an opaque DuckDB binder error inside
    the agreement/reject-rate SQL; here it is a clear, named contract violation.

    Two distinct failures are reported separately so the error points the engineer
    the right way:
      • the silver table does not exist at all (it has no columns) -> "table missing";
      • the table exists but lacks a declared column -> "column(s) missing".
    Without the split, a wholly-missing table would mislead by complaining about
    missing columns when the real fix is to create the table.
    """
    existing = _table_columns(con, spec.silver_table)
    if not existing:
        # No columns reported -> the table itself is absent (or empty-of-columns,
        # which DuckDB does not allow), so this is a missing-table error.
        raise SchemaContractError(
            f"[{spec.source_id}] silver table '{spec.silver_table}' does not exist. "
            f"The transformer must create its silver table before the build-end "
            f"invariants run."
        )
    required = [spec.key_column] + [dim.flag_column for dim in spec.dimensions]
    missing = sorted({col for col in required if col.lower() not in existing})
    if missing:
        raise SchemaContractError(
            f"[{spec.source_id}] silver table '{spec.silver_table}' is missing "
            f"required column(s) {missing} declared in its quality manifest "
            f"(key_column / dimension flag_column). Every silver table must carry "
            f"the manifest's key_column and each dimension's flag_column."
        )


def check_conservation(con, spec):
    """Invariant 1 (fatal): source_rows == clean_rows + quarantined_rows.

    Two checks:
      A. keep-in-place identity: count(bronze) == count(silver).
      B. conservation sum: source_rows == count(bronze) + count(quarantine).
    source_rows is the count the transformer recorded via record_source_rows.
    """
    bronze = _count(con, spec.bronze_table)
    silver = _count(con, spec.silver_table)
    if bronze != silver:
        raise ConservationError(
            f"[{spec.source_id}] keep-in-place violated: "
            f"bronze {spec.bronze_table}={bronze} != silver {spec.silver_table}={silver}"
        )
    quarantined = con.execute(
        "SELECT count(*) FROM quarantine_raw WHERE source_id = ?",
        [spec.source_id],
    ).fetchone()[0]
    source_rows = con.execute(
        "SELECT coalesce(sum(source_rows), 0) FROM source_ingest_log "
        "WHERE source_id = ?",
        [spec.source_id],
    ).fetchone()[0]
    if source_rows != bronze + quarantined:
        raise ConservationError(
            f"[{spec.source_id}] conservation violated: source_rows={source_rows} "
            f"!= bronze({bronze}) + quarantine({quarantined}) = {bronze + quarantined}. "
            f"Rows are unaccounted for — this is a bug, not an expected rejection."
        )


def check_quarantine_rate(con, spec, ceiling=DEFAULT_QUARANTINE_CEILING):
    """Invariant (fatal above ceiling): quarantined / source_rows <= ceiling.

    Quarantine (rows that could not be structured into bronze at all) must be rare
    (spec §9). Making malformed lines non-fatal must not let a changed upstream format
    quietly dump half the file into quarantine, so this tripwire keeps a flood fatal.
    source_rows is the independently-counted source total; quarantined is the
    quarantine_raw count for this source. An empty source (0 rows) passes.
    """
    source_rows = con.execute(
        "SELECT coalesce(sum(source_rows), 0) FROM source_ingest_log WHERE source_id = ?",
        [spec.source_id],
    ).fetchone()[0]
    if source_rows == 0:
        return
    quarantined = con.execute(
        "SELECT count(*) FROM quarantine_raw WHERE source_id = ?",
        [spec.source_id],
    ).fetchone()[0]
    rate = quarantined / source_rows
    if rate > ceiling:
        raise QuarantineRateExceededError(
            f"[{spec.source_id}] quarantine rate {rate:.4f} ({quarantined}/{source_rows}) "
            f"exceeds ceiling {ceiling:.4f}. This may signal a silent upstream format change."
        )


def check_flag_ledger_agreement(con, spec):
    """Invariant 2 (fatal): per dimension, silver flag==FALSE set == ledger set.

    For each dimension: the set of silver rows whose flag_column is FALSE must
    equal the set of source_row_keys in data_quality_log for this source whose
    rule_id is one of the dimension's rule_ids AND severity='reject_dimension'.
    Checked as two anti-join counts (each must be 0). 'warn' rows are ignored.
    """
    for dim in spec.dimensions:
        if not dim.rule_ids:
            # No rule_ids declared -> nothing to reconcile for this dimension.
            continue
        placeholders = ", ".join("?" for _ in dim.rule_ids)
        rule_params = list(dim.rule_ids)

        # (a) silver rows flagged FALSE but with NO matching ledger entry.
        orphan_silver = con.execute(
            f"SELECT count(*) FROM {spec.silver_table} s "
            f"WHERE s.{dim.flag_column} = FALSE AND NOT EXISTS ("
            f"  SELECT 1 FROM data_quality_log l "
            f"  WHERE l.source_id = ? AND l.source_row_key = s.{spec.key_column} "
            f"    AND l.severity = 'reject_dimension' "
            f"    AND l.rule_id IN ({placeholders}))",
            [spec.source_id, *rule_params],
        ).fetchone()[0]

        # (b) ledger entries with NO matching FALSE-flagged silver row.
        orphan_ledger = con.execute(
            f"SELECT count(*) FROM ("
            f"  SELECT DISTINCT source_row_key FROM data_quality_log "
            f"  WHERE source_id = ? AND severity = 'reject_dimension' "
            f"    AND rule_id IN ({placeholders})) l "
            f"WHERE NOT EXISTS ("
            f"  SELECT 1 FROM {spec.silver_table} s "
            f"  WHERE s.{spec.key_column} = l.source_row_key "
            f"    AND s.{dim.flag_column} = FALSE)",
            [spec.source_id, *rule_params],
        ).fetchone()[0]

        if orphan_silver or orphan_ledger:
            raise FlagLedgerAgreementError(
                f"[{spec.source_id}.{dim.name}] flag/ledger disagreement: "
                f"{orphan_silver} silver row(s) flagged FALSE without a ledger entry, "
                f"{orphan_ledger} ledger entr(ies) without a FALSE-flagged silver row."
            )


def check_reject_rates(con, spec, default_ceiling):
    """Invariant 3 (configurable, fatal above ceiling): rejected/total <= ceiling.

    Per dimension: rejected = count(silver WHERE flag = FALSE), total =
    count(silver). The ceiling is the dimension's own reject_ceiling if set,
    else default_ceiling. An empty silver table has rate 0 (passes).
    """
    total = _count(con, spec.silver_table)
    if total == 0:
        return
    for dim in spec.dimensions:
        rejected = con.execute(
            f"SELECT count(*) FROM {spec.silver_table} "
            f"WHERE {dim.flag_column} = FALSE"
        ).fetchone()[0]
        rate = rejected / total
        ceiling = dim.reject_ceiling if dim.reject_ceiling is not None else default_ceiling
        if rate > ceiling:
            raise RejectRateExceededError(
                f"[{spec.source_id}.{dim.name}] reject rate {rate:.4f} "
                f"({rejected}/{total}) exceeds ceiling {ceiling:.4f}. "
                f"This may signal a silent upstream format change."
            )


def run_invariants(con, specs, default_ceiling=DEFAULT_REJECT_CEILING):
    """Run all checks for every source spec. Raises on first failure.

    Per spec, a schema-contract pre-check runs FIRST (the silver table must carry
    the manifest's declared columns), then the three invariants. Called at the end
    of Client.build(); any raised QualityInvariantError halts the build (spec §9
    halt semantics). With an empty specs list this is a no-op (a zero-transformer
    build still succeeds).
    """
    for spec in specs:
        check_schema_contract(con, spec)   # fail fast on a missing silver column
        check_conservation(con, spec)
        check_quarantine_rate(con, spec)   # flood of unparseable lines -> fatal
        check_flag_ledger_agreement(con, spec)
        check_reject_rates(con, spec, default_ceiling)


def resolve_quality_specs(con, transformers,
                          undecided_fatal=UNDECIDED_QUALITY_SPEC_IS_FATAL):
    """Coverage gate: turn each active transformer's quality_spec() decision into
    the list of SourceQuality manifests to audit, enforcing that every active
    source made a CONSCIOUS choice (spec §9 — no source quietly escapes auditing).

    Per transformer, quality_spec() returns one of these:
      • SourceQuality(...)        -> collected for run_invariants().
      • QualityExemption(reason=) -> recorded in quality_exemptions (and logged);
                                     deliberately not audited.
      • None (inherited default)  -> 'undecided'. If undecided_fatal, raise
                                     UndecidedQualitySpecError; else log a warning
                                     (the interim behaviour — see the module flag).
      • a tuple/list of the above -> a transformer may declare MORE THAN ONE audit
                                     unit (e.g. STATS19's collision/vehicle/casualty).
                                     Each element is resolved independently; a lone
                                     value behaves exactly as before.
    Any other return type is a programming error -> TypeError.
    """
    log = logging.getLogger("crossroads.quality")
    specs = []
    for transformer in transformers:
        decision = transformer.quality_spec()
        # Normalise to a list so one transformer can declare several audit units.
        items = list(decision) if isinstance(decision, (tuple, list)) else [decision]
        for item in items:
            if isinstance(item, SourceQuality):
                specs.append(item)
            elif isinstance(item, QualityExemption):
                record_exemption(con, transformer.source_id, item.reason)
                log.info("[%s] quality exemption recorded: %s",
                         transformer.source_id, item.reason)
            elif item is None:
                msg = (f"[{transformer.source_id}] quality_spec() is undecided "
                       f"(returned None): an active source must return a "
                       f"SourceQuality(...) to be audited or a "
                       f"QualityExemption(reason=...) to opt out explicitly.")
                if undecided_fatal:
                    raise UndecidedQualitySpecError(msg)
                log.warning("%s [interim: warning only — will become fatal]", msg)
            else:
                raise TypeError(
                    f"[{transformer.source_id}] quality_spec() must return "
                    f"SourceQuality, QualityExemption, None, or a tuple/list of "
                    f"those; got {type(item).__name__}."
                )
    return specs


def declared_source_ids(transformer):
    """The audit source_ids a transformer will write rows under.

    For a transformer declaring SourceQuality(s), these are the specs' source_ids
    (a transformer may own several — e.g. STATS19). For a QualityExemption or an
    undecided (None) spec there is no separate audit source, so fall back to the
    transformer's own source_id. Used by Client.build to reset the shared audit
    tables for every source the transformer touches before it (re)runs.
    """
    decision = transformer.quality_spec()
    items = list(decision) if isinstance(decision, (tuple, list)) else [decision]
    ids = [item.source_id for item in items if isinstance(item, SourceQuality)]
    return ids or [transformer.source_id]
