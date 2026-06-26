# Stage 03 — Invariants, Coverage Gate & Build Integration
> Part of "Data Quality & Audit Engine". You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Stages 01 and 02 are complete:
- `src/crossroads/quality.py` exports `DEFAULT_REJECT_CEILING`, `Dimension`, `SourceQuality`, `QualityExemption`, `create_clean_view`, `ensure_quality_tables`, `record_source_rows`, `log_exclusion`, `quarantine_row`, `record_exemption`.
- `BaseTransformer` has a concrete `quality_spec()` returning `None` by default.
- `tests/test_quality.py` exists and `python -m pytest -q` is green.
- `src/crossroads/client.py` is still the Step 1 version: `build()` opens `self.con`, runs the transformer loop, returns `self` — with **no** quality code.

Verify: `python -c "from crossroads.quality import ensure_quality_tables, record_exemption, QualityExemption"` succeeds.

## Objective

Add to `quality.py`: a `check_schema_contract(...)` pre-check (the silver table must carry the manifest's declared columns), the three build-end invariant checks (aggregate SQL), a `run_invariants(...)` orchestrator that runs the pre-check then the three invariants, the `resolve_quality_specs(...)` coverage gate (with the `UNDECIDED_QUALITY_SPEC_IS_FATAL` flag), and the exception hierarchy (including `SchemaContractError` and `UndecidedQualitySpecError`). Then wire them into `Client.build()` so every build creates the audit tables, resolves each active transformer's three-state `quality_spec()` decision (audit / explicit exemption / undecided), and runs the invariants (fatal on violation). Prove the whole path with synthetic fixtures, including an end-to-end `build()` driven by a synthetic transformer for each of the three decision states.

## Implementation Steps

### 1. Append the exception hierarchy, invariant checks, and coverage gate to `src/crossroads/quality.py`

Add `import logging` to the **top** of `src/crossroads/quality.py` (beside the existing `from dataclasses import ...`). Then append the rest below the Stage 02 helpers.

```python
# Coverage-gate escalation flag (see resolve_quality_specs). The END STATE is to
# FAIL the build when an active transformer's quality_spec() is undecided (None).
# Step 2 ships False (warn only) because no real transformer exists yet.
# ESCALATION TRIGGER: flip to True in Step 3, the moment the first real
# transformer (spatial.py) lands and proves the SourceQuality shape end-to-end.
UNDECIDED_QUALITY_SPEC_IS_FATAL = False


class QualityInvariantError(Exception):
    """Base for all build-halting data-quality failures (spec §9)."""


class ConservationError(QualityInvariantError):
    """source_rows != bronze + quarantine, or bronze != silver (rows vanished)."""


class FlagLedgerAgreementError(QualityInvariantError):
    """A silver flag and the exclusion ledger disagree about a rejected row."""


class RejectRateExceededError(QualityInvariantError):
    """A source/dimension reject rate exceeded its configured ceiling."""


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
        check_flag_ledger_agreement(con, spec)
        check_reject_rates(con, spec, default_ceiling)


def resolve_quality_specs(con, transformers,
                          undecided_fatal=UNDECIDED_QUALITY_SPEC_IS_FATAL):
    """Coverage gate: turn each active transformer's quality_spec() decision into
    the list of SourceQuality manifests to audit, enforcing that every active
    source made a CONSCIOUS choice (spec §9 — no source quietly escapes auditing).

    Per transformer, quality_spec() returns one of three things:
      • SourceQuality(...)        -> collected for run_invariants().
      • QualityExemption(reason=) -> recorded in quality_exemptions (and logged);
                                     deliberately not audited.
      • None (inherited default)  -> 'undecided'. If undecided_fatal, raise
                                     UndecidedQualitySpecError; else log a warning
                                     (the interim behaviour — see the module flag).
    Any other return type is a programming error -> TypeError.
    """
    log = logging.getLogger("crossroads.quality")
    specs = []
    for transformer in transformers:
        decision = transformer.quality_spec()
        if isinstance(decision, SourceQuality):
            specs.append(decision)
        elif isinstance(decision, QualityExemption):
            record_exemption(con, transformer.source_id, decision.reason)
            log.info("[%s] quality exemption recorded: %s",
                     transformer.source_id, decision.reason)
        elif decision is None:
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
                f"SourceQuality, QualityExemption, or None; got "
                f"{type(decision).__name__}."
            )
    return specs
```

> Note on a multi-dimension subtlety the executing model should be aware of: the reject-rate denominator is the **whole** silver table (`total`), per spec §9-3 (`rejected / total`). Each dimension's `rejected` count is independent, so a row failing two dimensions counts once in each dimension's numerator — this is intended.

### 2. Wire the engine into `src/crossroads/client.py`

Edit `Client.build()`. The current method is:

```python
    def build(self, **kwargs) -> "Client":
        self.con = duckdb.connect(self.database_path)
        for transformer in self.registry.get_active(**kwargs):
            transformer.extract(self.cache_dir, **kwargs)
            transformer.transform_and_load(self.con, self.cache_dir)
        return self
```

Replace it with (and add `from crossroads import quality` near the top, beside the existing `from crossroads.registry import Registry`):

```python
    def build(self, **kwargs) -> "Client":
        """Open the database and run the extract/transform_and_load loop.

        ``**kwargs`` (e.g. ``years=[...]``, ``include_weather=True``,
        ``spatial_grain="local_authority"``) are forwarded to each transformer's
        ``is_active`` and ``extract`` methods. An optional ``reject_ceiling``
        kwarg overrides the global default reject-rate ceiling. Returns ``self``.

        At build end the shared data-quality invariants run (spec §9); any
        violation raises and halts the build.
        """
        self.con = duckdb.connect(self.database_path)
        # Create the shared audit tables up-front so transformers can write to them.
        quality.ensure_quality_tables(self.con)

        active = self.registry.get_active(**kwargs)
        for transformer in active:
            # Clear this source's rows from the shared audit tables before it is
            # (re)built, so a re-build against an existing on-disk database stays
            # idempotent (log_exclusion / quarantine_row are plain appends). The
            # transformer is responsible for recreating its own bronze/silver.
            quality.reset_source_audit(self.con, transformer.source_id)
            transformer.extract(self.cache_dir, **kwargs)
            transformer.transform_and_load(self.con, self.cache_dir)

        # Coverage gate: resolve each active source's quality_spec() decision
        # (audit / explicit exemption / undecided), then run the build-end
        # invariants (conservation, flag/ledger agreement, reject-rate tripwire).
        # Both the gate and the invariants are fatal on violation.
        specs = quality.resolve_quality_specs(self.con, active)
        default_ceiling = kwargs.get("reject_ceiling") or quality.DEFAULT_REJECT_CEILING
        quality.run_invariants(self.con, specs, default_ceiling=default_ceiling)
        return self
```

Do **not** change `registry.py` or the `transformers/` package in this stage (`base.py` was already given its concrete `quality_spec()` in Stage 01). The provider-plugin purity holds: `client.py` still names no concrete source.

## Testing & Verification

Append to `tests/test_quality.py`. A small helper builds a synthetic source so each test is self-contained.

```python
from crossroads.quality import (
    SourceQuality, Dimension, QualityExemption,
    check_schema_contract,
    check_conservation, check_flag_ledger_agreement, check_reject_rates,
    run_invariants, resolve_quality_specs,
    SchemaContractError,
    ConservationError, FlagLedgerAgreementError, RejectRateExceededError,
    UndecidedQualitySpecError,
)


def _make_clean_source(con, n_valid=9, n_invalid=1, source_rows=None):
    """Create a synthetic, internally-consistent bronze+silver source.

    Silver has n_valid rows with geom_valid=TRUE and n_invalid rows with
    geom_valid=FALSE; each FALSE row has a matching reject_dimension ledger
    entry. Bronze mirrors silver 1:1. Returns the SourceQuality manifest.
    """
    ensure_quality_tables(con)
    total = n_valid + n_invalid
    con.execute("CREATE TABLE synth_raw (source_row_key VARCHAR)")
    con.execute("CREATE TABLE synth (source_row_key VARCHAR, geom_valid BOOLEAN)")
    for i in range(n_valid):
        key = f"v{i}"
        con.execute("INSERT INTO synth_raw VALUES (?)", [key])
        con.execute("INSERT INTO synth VALUES (?, TRUE)", [key])
    for i in range(n_invalid):
        key = f"x{i}"
        con.execute("INSERT INTO synth_raw VALUES (?)", [key])
        con.execute("INSERT INTO synth VALUES (?, FALSE)", [key])
        log_exclusion(
            con, source_id="synth", source_row_key=key, column_name="geom",
            rule_id="synth.geom.bad", rule_desc="bad geom",
            severity="reject_dimension", raw_value="-1",
        )
    record_source_rows(con, "synth", source_rows if source_rows is not None else total)
    return SourceQuality(
        source_id="synth", bronze_table="synth_raw", silver_table="synth",
        dimensions=(Dimension("geom", "geom_valid", ("synth.geom.bad",)),),
    )


# --- Invariant unit/integration checks (called directly) ---

def test_clean_source_passes_all_invariants(con):
    spec = _make_clean_source(con, n_valid=9, n_invalid=1)
    run_invariants(con, [spec])  # must not raise


def test_schema_contract_passes_when_columns_present(con):
    spec = _make_clean_source(con, n_valid=3, n_invalid=0)
    check_schema_contract(con, spec)  # key_column + flag_column present -> no raise


def test_schema_contract_fails_when_key_column_missing(con):
    ensure_quality_tables(con)
    con.execute("CREATE TABLE s_raw (k VARCHAR)")
    # silver lacks the manifest's key_column ('source_row_key').
    con.execute("CREATE TABLE s (wrong_key VARCHAR, geom_valid BOOLEAN)")
    record_source_rows(con, "s", 0)
    spec = SourceQuality(
        source_id="s", bronze_table="s_raw", silver_table="s",
        dimensions=(Dimension("geom", "geom_valid", ("s.geom.bad",)),),
    )
    with pytest.raises(SchemaContractError):
        check_schema_contract(con, spec)
    # run_invariants surfaces it FIRST, before conservation/agreement/reject-rate.
    with pytest.raises(SchemaContractError):
        run_invariants(con, [spec])


def test_schema_contract_fails_when_flag_column_missing(con):
    ensure_quality_tables(con)
    con.execute("CREATE TABLE s_raw (k VARCHAR)")
    # silver has the key_column but not the dimension's flag_column.
    con.execute("CREATE TABLE s (source_row_key VARCHAR)")
    record_source_rows(con, "s", 0)
    spec = SourceQuality(
        source_id="s", bronze_table="s_raw", silver_table="s",
        dimensions=(Dimension("geom", "geom_valid", ("s.geom.bad",)),),
    )
    with pytest.raises(SchemaContractError):
        check_schema_contract(con, spec)


def test_schema_contract_distinguishes_missing_table(con):
    ensure_quality_tables(con)
    # The silver table was never created -> a TABLE-missing error, not a
    # column-missing error. The message must say so, to point the engineer right.
    spec = SourceQuality(
        source_id="s", bronze_table="s_raw", silver_table="never_created",
        dimensions=(Dimension("geom", "geom_valid", ("s.geom.bad",)),),
    )
    with pytest.raises(SchemaContractError, match="does not exist"):
        check_schema_contract(con, spec)


def test_conservation_fails_on_missing_silver_row(con):
    spec = _make_clean_source(con, n_valid=9, n_invalid=1)
    # Drop one silver row -> bronze(10) != silver(9): keep-in-place violated.
    con.execute("DELETE FROM synth WHERE source_row_key = 'v0'")
    with pytest.raises(ConservationError):
        check_conservation(con, spec)


def test_conservation_fails_on_unaccounted_source_rows(con):
    spec = _make_clean_source(con, n_valid=9, n_invalid=1, source_rows=12)
    # source claimed 12 rows but only 10 landed (bronze) + 0 quarantined.
    with pytest.raises(ConservationError):
        check_conservation(con, spec)


def test_agreement_fails_flag_without_ledger(con):
    spec = _make_clean_source(con, n_valid=9, n_invalid=0)
    # Flip a valid row to FALSE without logging a ledger entry.
    con.execute("UPDATE synth SET geom_valid = FALSE WHERE source_row_key = 'v0'")
    with pytest.raises(FlagLedgerAgreementError):
        check_flag_ledger_agreement(con, spec)


def test_agreement_fails_ledger_without_flag(con):
    spec = _make_clean_source(con, n_valid=9, n_invalid=0)
    # Log a reject_dimension entry for a row whose flag is still TRUE.
    log_exclusion(
        con, source_id="synth", source_row_key="v0", column_name="geom",
        rule_id="synth.geom.bad", rule_desc="bad geom",
        severity="reject_dimension", raw_value="-1",
    )
    with pytest.raises(FlagLedgerAgreementError):
        check_flag_ledger_agreement(con, spec)


def test_reject_rate_within_ceiling_passes(con):
    spec = _make_clean_source(con, n_valid=99, n_invalid=1)  # 1% < 5%
    check_reject_rates(con, spec, default_ceiling=0.05)  # must not raise


def test_reject_rate_over_ceiling_fails(con):
    spec = _make_clean_source(con, n_valid=8, n_invalid=2)  # 20% > 5%
    with pytest.raises(RejectRateExceededError):
        check_reject_rates(con, spec, default_ceiling=0.05)


def test_per_dimension_ceiling_override(con):
    # 20% rejected, but this dimension explicitly tolerates up to 50%.
    ensure_quality_tables(con)
    con.execute("CREATE TABLE s_raw (k VARCHAR)")
    con.execute("CREATE TABLE s (source_row_key VARCHAR, ok BOOLEAN)")
    for i in range(8):
        con.execute("INSERT INTO s_raw VALUES (?)", [f"v{i}"])
        con.execute("INSERT INTO s VALUES (?, TRUE)", [f"v{i}"])
    for i in range(2):
        con.execute("INSERT INTO s_raw VALUES (?)", [f"x{i}"])
        con.execute("INSERT INTO s VALUES (?, FALSE)", [f"x{i}"])
    record_source_rows(con, "s", 10)
    spec = SourceQuality(
        source_id="s", bronze_table="s_raw", silver_table="s",
        dimensions=(Dimension("d", "ok", (), reject_ceiling=0.5),),
    )
    check_reject_rates(con, spec, default_ceiling=0.05)  # 0.2 <= 0.5 -> passes
```

### Coverage-gate tests — `resolve_quality_specs`

Append these. They use tiny stand-in transformers (the gate only calls
`.quality_spec()` and reads `.source_id`).

```python
class _FakeTransformer:
    """Minimal stand-in: the coverage gate only needs source_id + quality_spec()."""

    def __init__(self, source_id, decision):
        self.source_id = source_id
        self._decision = decision

    def quality_spec(self):
        return self._decision


def test_resolve_collects_sourcequality(con):
    ensure_quality_tables(con)
    spec = SourceQuality(source_id="s", bronze_table="s_raw", silver_table="s")
    out = resolve_quality_specs(con, [_FakeTransformer("s", spec)])
    assert out == [spec]


def test_resolve_records_exemption_and_skips_audit(con):
    ensure_quality_tables(con)
    out = resolve_quality_specs(
        con, [_FakeTransformer("agg", QualityExemption(reason="aggregates rows"))]
    )
    assert out == []  # nothing to audit
    row = con.execute(
        "SELECT source_id, reason FROM quality_exemptions"
    ).fetchone()
    assert row == ("agg", "aggregates rows")


def test_resolve_undecided_warns_in_interim(con):
    ensure_quality_tables(con)
    # Default flag is False (interim) -> a None decision warns, does not raise.
    out = resolve_quality_specs(con, [_FakeTransformer("u", None)])
    assert out == []


def test_resolve_undecided_is_fatal_when_enabled(con):
    ensure_quality_tables(con)
    with pytest.raises(UndecidedQualitySpecError):
        resolve_quality_specs(
            con, [_FakeTransformer("u", None)], undecided_fatal=True
        )


def test_resolve_rejects_wrong_type(con):
    ensure_quality_tables(con)
    with pytest.raises(TypeError):
        resolve_quality_specs(con, [_FakeTransformer("bad", "not a spec")])
```

### Integration test (PRIMARY) — end-to-end via `client.build()`

This proves the full wiring: a synthetic transformer creates bronze/silver on the build connection, records its count, logs exclusions, and exposes `quality_spec()`. We inject it into the client's registry (the registry stores instances in `self._transformers`).

```python
import crossroads
from crossroads.transformers.base import BaseTransformer


class _SynthTransformer(BaseTransformer):
    """A synthetic source used only to exercise build() integration."""

    def __init__(self, n_valid=9, n_invalid=1, source_rows=None):
        self._n_valid = n_valid
        self._n_invalid = n_invalid
        self._source_rows = source_rows

    @property
    def source_id(self):
        return "synth"

    def extract(self, cache_dir, **kwargs):
        pass  # nothing to download for a synthetic source

    def transform_and_load(self, con, cache_dir):
        con.execute("CREATE TABLE synth_raw (source_row_key VARCHAR)")
        con.execute("CREATE TABLE synth (source_row_key VARCHAR, geom_valid BOOLEAN)")
        for i in range(self._n_valid):
            key = f"v{i}"
            con.execute("INSERT INTO synth_raw VALUES (?)", [key])
            con.execute("INSERT INTO synth VALUES (?, TRUE)", [key])
        for i in range(self._n_invalid):
            key = f"x{i}"
            con.execute("INSERT INTO synth_raw VALUES (?)", [key])
            con.execute("INSERT INTO synth VALUES (?, FALSE)", [key])
            log_exclusion(
                con, source_id="synth", source_row_key=key, column_name="geom",
                rule_id="synth.geom.bad", rule_desc="bad geom",
                severity="reject_dimension", raw_value="-1",
            )
        total = self._n_valid + self._n_invalid
        record_source_rows(
            con, "synth",
            self._source_rows if self._source_rows is not None else total,
        )

    def quality_spec(self):
        return SourceQuality(
            source_id="synth", bronze_table="synth_raw", silver_table="synth",
            dimensions=(Dimension("geom", "geom_valid", ("synth.geom.bad",)),),
        )


def _client_with(transformer):
    client = crossroads.init_engine()  # in-memory
    client.registry._transformers = [transformer]  # inject synthetic source
    return client


def test_build_runs_invariants_and_succeeds_on_clean_source():
    client = _client_with(_SynthTransformer(n_valid=9, n_invalid=1))
    result = client.build()
    assert result is client
    # The synthetic source's tables and the audit tables all exist.
    assert client.con.execute("SELECT count(*) FROM synth").fetchone()[0] == 10
    assert client.con.execute(
        "SELECT count(*) FROM data_quality_log"
    ).fetchone()[0] == 1
    client.close()


def test_build_halts_when_invariant_violated():
    # source claims 12 rows but only 10 land -> conservation failure.
    client = _client_with(_SynthTransformer(n_valid=9, n_invalid=1, source_rows=12))
    with pytest.raises(ConservationError):
        client.build()
    client.close()


def test_empty_build_still_succeeds():
    # No transformers -> audit tables created, zero invariants, clean success.
    client = crossroads.init_engine()
    client.build()
    assert client.con.execute(
        "SELECT count(*) FROM data_quality_log"
    ).fetchone()[0] == 0
    client.close()


class _ExemptTransformer(BaseTransformer):
    """A deliberately non-conserving source that opts out via QualityExemption."""

    @property
    def source_id(self):
        return "exempt"

    def extract(self, cache_dir, **kwargs):
        pass

    def transform_and_load(self, con, cache_dir):
        # Aggregating shape: 3 bronze rows collapse to 1 silver row, so
        # count(bronze) != count(silver) by design -> conservation would fail
        # IF it ran. The exemption is what makes this a clean build.
        con.execute("CREATE TABLE exempt_raw (k VARCHAR)")
        con.execute("INSERT INTO exempt_raw VALUES ('a'), ('b'), ('c')")
        con.execute("CREATE TABLE exempt (k VARCHAR)")
        con.execute("INSERT INTO exempt VALUES ('agg')")

    def quality_spec(self):
        return QualityExemption(reason="aggregates 3 bronze rows into 1 silver row")


def test_build_with_exemption_succeeds_and_records_reason():
    client = _client_with(_ExemptTransformer())
    client.build()  # must NOT raise, despite bronze(3) != silver(1)
    row = client.con.execute(
        "SELECT source_id, reason FROM quality_exemptions"
    ).fetchone()
    assert row == ("exempt", "aggregates 3 bronze rows into 1 silver row")
    client.close()


class _UndecidedTransformer(BaseTransformer):
    """A source that forgot to override quality_spec() (inherits None)."""

    @property
    def source_id(self):
        return "undecided"

    def extract(self, cache_dir, **kwargs):
        pass

    def transform_and_load(self, con, cache_dir):
        pass
    # quality_spec() is inherited -> returns None ("undecided").


def test_build_with_undecided_source_warns_in_interim(caplog):
    # Interim flag (UNDECIDED_QUALITY_SPEC_IS_FATAL = False): undecided warns,
    # build still succeeds, nothing audited or exempted.
    client = _client_with(_UndecidedTransformer())
    with caplog.at_level("WARNING", logger="crossroads.quality"):
        client.build()
    assert any("undecided" in r.message.lower() for r in caplog.records)
    assert client.con.execute(
        "SELECT count(*) FROM quality_exemptions"
    ).fetchone()[0] == 0
    client.close()
```

> When Step 3 flips `UNDECIDED_QUALITY_SPEC_IS_FATAL` to `True`, the assertion in `test_build_with_undecided_source_warns_in_interim` will need updating to expect an `UndecidedQualitySpecError` instead — that test is the tripwire that makes the escalation visible.

### Integration test (PRIMARY) — re-build against the same on-disk database is idempotent

This is the proof that `reset_source_audit` closes the duplicate-audit gap. The
transformer recreates its **own** bronze/silver with `CREATE OR REPLACE` (its
responsibility), and the **engine** clears the shared audit tables each build.
The transformer also quarantines a row each build — the case that would break
conservation (`quarantine_raw` is counted raw) if the reset were missing.

```python
class _RebuildableTransformer(BaseTransformer):
    """Recreates its own bronze/silver idempotently and writes one exclusion +
    one quarantine row per build. reject_ceiling is loosened so the 1-of-2 FALSE
    flag does not trip the reject-rate tripwire (not what this test exercises)."""

    @property
    def source_id(self):
        return "rb"

    def extract(self, cache_dir, **kwargs):
        pass

    def transform_and_load(self, con, cache_dir):
        # The transformer owns its bronze/silver DDL -> recreate idempotently.
        con.execute("CREATE OR REPLACE TABLE rb_raw (source_row_key VARCHAR)")
        con.execute("INSERT INTO rb_raw VALUES ('a'), ('b')")
        con.execute("CREATE OR REPLACE TABLE rb (source_row_key VARCHAR, ok BOOLEAN)")
        con.execute("INSERT INTO rb VALUES ('a', TRUE), ('b', FALSE)")
        log_exclusion(
            con, source_id="rb", source_row_key="b", column_name="x",
            rule_id="rb.x.bad", rule_desc="bad", severity="reject_dimension",
            raw_value="-1",
        )
        quarantine_row(con, source_id="rb", raw_text="broken,line", reason="bad row")
        # 2 rows landed in bronze + 1 quarantined = 3 rows read from source.
        record_source_rows(con, "rb", 3)

    def quality_spec(self):
        return SourceQuality(
            source_id="rb", bronze_table="rb_raw", silver_table="rb",
            dimensions=(Dimension("x", "ok", ("rb.x.bad",), reject_ceiling=1.0),),
        )


def test_rebuild_against_same_file_is_idempotent(tmp_path):
    db_path = str(tmp_path / "rb.db")

    def run_once():
        client = crossroads.init_engine(database_path=db_path)
        client.registry._transformers = [_RebuildableTransformer()]
        client.build()  # must not raise on EITHER run
        return client

    first = run_once()
    first.close()
    second = run_once()  # second build against the SAME on-disk database

    # Each shared audit table holds this source's rows exactly once — not doubled.
    # (Without reset_source_audit, quarantine_raw would have 2 rows and the
    #  conservation invariant would have raised on this second build.)
    for table in ("source_ingest_log", "data_quality_log", "quarantine_raw"):
        assert second.con.execute(
            f"SELECT count(*) FROM {table} WHERE source_id = 'rb'"
        ).fetchone()[0] == 1
    second.close()
```

> If injecting via `client.registry._transformers` proves brittle in your environment, the equivalent alternative is to monkeypatch `client.registry.get_active = lambda **kw: [transformer]`. Either is acceptable; the goal is to drive a real `build()` with a manifest-bearing transformer without committing a synthetic module into `crossroads.transformers`.

### Stage ship-readiness checklist
- [ ] `python -m pytest -q` is green — the full suite (Step 1 tests, Stages 01–02 tests, and all Stage 03 tests).
- [ ] The existing Step 1 `test_client.py` tests still pass (empty build is still a clean success; audit tables now also exist).
- [ ] A clean synthetic source passes all three invariants via both direct calls and `client.build()`.
- [ ] Each invariant fails on its specific seeded violation and raises the **specific** exception type.
- [ ] The schema-contract pre-check raises `SchemaContractError` when the silver table omits the `key_column` or a dimension's `flag_column`, and `run_invariants` surfaces it before the three invariants.
- [ ] A second `build()` against the same on-disk database does not raise and does not duplicate audit rows (the `reset_source_audit` call is present at the top of the build loop).
- [ ] The coverage gate resolves all three decision states: `SourceQuality` → audited; `QualityExemption` → recorded in `quality_exemptions` and skipped; `None` → warns in the interim (and raises `UndecidedQualitySpecError` when `undecided_fatal=True`).
- [ ] `UNDECIDED_QUALITY_SPEC_IS_FATAL` is `False` in this step (warn-only interim), with the escalation trigger documented for Step 3.
- [ ] `client.py` and `registry.py` still name no concrete data source.

## End State / Handoff

The Data Quality & Audit Engine is complete and proven against synthetic fixtures. `quality.py` exports the manifest dataclasses (`SourceQuality`, `Dimension`, `QualityExemption`), the shared audit tables + writers, the gold-view helper, the `check_schema_contract` pre-check, the three invariant checks, `run_invariants`, the `resolve_quality_specs` coverage gate + `UNDECIDED_QUALITY_SPEC_IS_FATAL` flag, and the `QualityInvariantError` hierarchy (incl. `SchemaContractError` and `UndecidedQualitySpecError`). `Client.build()` creates the audit tables, resolves each active transformer's `quality_spec()` decision, and runs the schema pre-check + invariants fatally at build end.

**What Step 3 (Spatial Infrastructure) may now assume:** dropping a real `BaseTransformer` into `transformers/` that writes bronze/silver tables, calls `record_source_rows`/`log_exclusion`/`quarantine_row`, builds gold views via `create_clean_view`, and **overrides `quality_spec()` to return a `SourceQuality`** will be **audited automatically on every build**, halting on any conservation, flag/ledger, or reject-rate violation. Silver tables must carry the manifest's `key_column` (default `source_row_key`) and each dimension's `flag_column` — and this is now **enforced**: a missing column raises `SchemaContractError` before any invariant runs, so the convention fails fast with a named error rather than a cryptic binder error. A source that legitimately cannot be conserved returns a `QualityExemption(reason=...)` instead.

**Re-build idempotency contract (split responsibility).** The engine clears the shared audit tables it owns (`reset_source_audit`, per source, at the top of the build loop), so re-running `build()` against an existing on-disk file does not accumulate ledger/quarantine rows. In return, a transformer **must recreate its own bronze/silver tables idempotently** — `CREATE OR REPLACE TABLE` (or `DROP`+`CREATE`), never a bare `CREATE TABLE` (which errors on the second build) and never `CREATE TABLE IF NOT EXISTS` + `INSERT` (which would append/double bronze). This is documented in the `BaseTransformer.transform_and_load` docstring (Stage 01).

> **Step 3 escalation action (do not forget):** when `spatial.py` lands and returns a real `SourceQuality`, flip `UNDECIDED_QUALITY_SPEC_IS_FATAL` to `True` in `quality.py`, and update `test_build_with_undecided_source_warns_in_interim` to expect `UndecidedQualitySpecError`. This converts the undecided-source warning into a hard build failure, completing the enforced-coverage guarantee.

## Failure Modes & Rollback

- **`reject_ceiling` forwarded to transformers:** the global override rides in `build(**kwargs)`, which is also forwarded to transformer `extract`/`is_active`. Those accept `**kwargs` and ignore unknown keys, so this is harmless. If a future transformer adds a conflicting `reject_ceiling` parameter, revisit.
- **Empty silver table:** `check_reject_rates` returns early (rate 0); `check_conservation` handles 0==0. No division by zero.
- **Missing silver *table* vs. missing silver *column* (distinct messages):** `check_schema_contract` reports these separately so the error points the engineer the right way. A silver table that was never created has no columns in `information_schema.columns`, so the check raises `SchemaContractError` with a **"does not exist"** message (rather than misleadingly blaming missing columns). A table that exists but omits a declared column raises `SchemaContractError` naming the absent `key_column` / `flag_column`. Either way it fires before the three invariants, replacing the raw DuckDB binder/catalog error.
- **Case sensitivity:** column matching is case-insensitive (DuckDB identifier semantics), so a case difference between manifest and DDL is not a false positive.
- **Dimension with empty `rule_ids`:** agreement skips it (nothing to reconcile); reject-rate still applies. Intentional — a dimension may be reject-rate-monitored without ledger reconciliation.
- **Undecided source silently passes when it shouldn't:** that is the *interim* behaviour by design (`UNDECIDED_QUALITY_SPEC_IS_FATAL = False`); it is a warning, not a free pass. The Step 3 escalation (above) closes it. Do not leave it warn-only past Step 3.
- **`quality_spec()` returns an unexpected type:** the gate raises `TypeError` — a programming error in the transformer, surfaced loudly rather than silently ignored.
- **Re-build accumulates audit rows / second build raises:** the `reset_source_audit` call at the top of the build loop clears the engine-owned audit tables per source. If a second on-disk build raises `ConservationError` (doubled `quarantine_raw`) or `FlagLedgerAgreementError` (stale ledger keys), either that call is missing, or the transformer used a bare `CREATE TABLE` / append-style bronze load instead of recreating its bronze/silver idempotently.
- **Injection brittleness in the build() test:** use the `get_active` monkeypatch alternative noted above.
- **Rollback:** revert `client.py` to the Step 1 version (remove the `quality` import and restore the original `build()` body), and remove the Stage 03 additions from `quality.py` and `tests/test_quality.py`. Stages 01–02 (including the `base.py` `quality_spec()` method) remain intact and green.
