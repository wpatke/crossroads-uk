# Stage 02 — Exclusion Ledger & Quarantine
> Part of "Data Quality & Audit Engine". You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Stage 01 is complete:
- `src/crossroads/quality.py` exists with `DEFAULT_REJECT_CEILING`, `Dimension`, `SourceQuality`, `QualityExemption`, `create_clean_view`.
- `BaseTransformer` has a concrete `quality_spec()` returning `None` by default.
- `tests/test_quality.py` exists and `python -m pytest -q` is green.

Verify by running `python -c "from crossroads.quality import SourceQuality, QualityExemption, create_clean_view"` — it must succeed.

## Objective

Add the **shared audit tables** and their writer helpers to `src/crossroads/quality.py`:
- `ensure_quality_tables(con)` — idempotently creates `source_ingest_log`, `data_quality_log`, `quarantine_raw`, `quality_exemptions` (spec §9 schema).
- `record_source_rows(con, source_id, source_rows)` — records how many rows a source read (feeds the conservation invariant; idempotent per source).
- `log_exclusion(con, ...)` — writes one `data_quality_log` row (a rule violation).
- `quarantine_row(con, ...)` — writes one `quarantine_raw` row (an unparseable source line).
- `record_exemption(con, source_id, reason)` — records a source's deliberate opt-out in `quality_exemptions` (idempotent per source); consumed by the Stage 03 coverage gate.
- `reset_source_audit(con, source_id)` — deletes a source's rows from **all four** shared audit tables, so a re-build against an existing on-disk database is idempotent per source rather than accumulating duplicate audit rows. Called by the engine at the top of the build loop (wired in Stage 03).

No invariants and no `client.py` changes in this stage — those are Stage 03.

> **Why `reset_source_audit` is needed.** `log_exclusion` and `quarantine_row` are plain appends, called from each transformer's `transform_and_load`. On a second `build()` against the same on-disk file, `ensure_quality_tables` is a no-op (the tables already exist with the prior run's rows), so without a reset those two tables accumulate duplicates — corrupting the audit ledger and breaking the conservation invariant (`quarantine_raw` is counted raw) on the next build. Rather than make every contributor remember to clean up in `transform_and_load`, the **engine** clears the audit tables it owns, once per source, before that source is rebuilt. (The transformer remains responsible for recreating its **own** bronze/silver tables idempotently — see the `BaseTransformer` contract note in Stage 01.)

## Implementation Steps

### 1. Append the table-creation + writer helpers to `src/crossroads/quality.py`

Add the following below the Stage 01 code. Keep comments plain-language.

```python
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
```

> Note: `reset_source_audit` already clears `source_ingest_log` and `quality_exemptions`, so the delete-before-insert inside `record_source_rows` / `record_exemption` becomes redundant when called via the build loop. It is kept anyway — it makes those two writers independently idempotent (latest-value-wins) regardless of how they are invoked, at zero cost.

> Design note (severity values): the two severities are `'reject_dimension'` and `'warn'`, matching spec §9. Only `'reject_dimension'` rows participate in the Stage 03 flag/ledger agreement and reject-rate checks; `'warn'` rows are informational. Pass these as plain strings (no enum) to keep the code simple.

### 2. Do not change `client.py` yet

Build integration is Stage 03. This stage only adds reusable helpers.

## Testing & Verification

### Integration tests (PRIMARY) — tables created, writers round-trip

Append to `tests/test_quality.py`:

```python
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
```

### Stage ship-readiness checklist
- [ ] `python -m pytest -q` is green (new + Stage 01 + Step 1 tests).
- [ ] Calling `ensure_quality_tables` twice does not raise (idempotent); all **four** tables exist.
- [ ] `record_source_rows` and `record_exemption` for the same source twice each leave exactly one row with the latest value.
- [ ] `data_quality_log` / `quarantine_raw` / `quality_exemptions` rows read back with the exact bound values; `ingested_at` is non-NULL.
- [ ] `reset_source_audit(con, sid)` removes that source's rows from all four audit tables and leaves other sources untouched.

## End State / Handoff

`src/crossroads/quality.py` now also exports `ensure_quality_tables`, `record_source_rows`, `log_exclusion`, `quarantine_row`, `record_exemption`, and `reset_source_audit`. The four shared audit tables can be created idempotently, written to with bound parameters, and cleared per source. Stage 03 may assume these exist and will add the invariant checks that read from them, the coverage gate that calls `record_exemption`, and the `client.build()` wiring (which calls `reset_source_audit` at the top of the build loop).

## Failure Modes & Rollback

- **Double-count on on-disk re-build:** the two append-only writers (`log_exclusion`, `quarantine_row`) do **not** self-deduplicate — by design, a single build may legitimately write many ledger/quarantine rows per source. Idempotency across *builds* is instead provided by `reset_source_audit`, which the engine calls once per source before that source is rebuilt (Stage 03). `record_source_rows` / `record_exemption` additionally self-dedupe within a build. If you observe duplicate audit rows growing across re-builds of the same on-disk file, the `reset_source_audit` call was omitted from the build loop — restore it.
- **`current_timestamp` default unsupported:** DuckDB supports `DEFAULT current_timestamp`. If a very old DuckDB rejects it, the `duckdb>=1.0` floor in `pyproject.toml` is the contract — do not downgrade; report instead.
- **Asserting on `ingested_at` value:** never do this — it is wall-clock provenance and breaks determinism guarantees. Only assert it is non-NULL.
- **Rollback:** remove the four helpers (and their tests) added in this stage; `quality.py` returns to the Stage 01 end state. No other file changed.
