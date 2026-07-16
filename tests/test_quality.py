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


# ---------------------------------------------------------------------------
# Stage 03 — Invariants, coverage gate, and build integration
# ---------------------------------------------------------------------------

from crossroads.quality import (
    check_schema_contract,
    check_conservation, check_flag_ledger_agreement, check_reject_rates,
    run_invariants, resolve_quality_specs,
    SchemaContractError,
    ConservationError, FlagLedgerAgreementError, RejectRateExceededError,
    UndecidedQualitySpecError,
)

import crossroads
from crossroads.transformers.base import BaseTransformer


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
    spec = _make_clean_source(con, n_valid=99, n_invalid=1)  # 1% < 5% default ceiling
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


# --- Coverage-gate tests — resolve_quality_specs ---

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


def test_resolve_undecided_warns_when_not_fatal(con):
    ensure_quality_tables(con)
    # Pass undecided_fatal=False explicitly to test the warn-only code path
    # (the module default is now True since spatial.py landed in Step 3).
    out = resolve_quality_specs(con, [_FakeTransformer("u", None)], undecided_fatal=False)
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


# --- Integration tests — end-to-end via client.build() ---

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
        con.execute("CREATE OR REPLACE TABLE synth_raw (source_row_key VARCHAR)")
        con.execute("CREATE OR REPLACE TABLE synth (source_row_key VARCHAR, geom_valid BOOLEAN)")
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
    client = _client_with(_SynthTransformer(n_valid=99, n_invalid=1))  # 1% < 5% ceiling
    result = client.build()
    assert result is client
    # The synthetic source's tables and the audit tables all exist.
    assert client.con.execute("SELECT count(*) FROM synth").fetchone()[0] == 100
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
    # Explicitly no transformers -> audit tables created, zero invariants, clean success.
    client = crossroads.init_engine()
    client.registry._transformers = []  # bypass auto-discovery of spatial transformers
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
        con.execute("CREATE OR REPLACE TABLE exempt_raw (k VARCHAR)")
        con.execute("INSERT INTO exempt_raw VALUES ('a'), ('b'), ('c')")
        con.execute("CREATE OR REPLACE TABLE exempt (k VARCHAR)")
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


def test_build_with_undecided_source_is_fatal():
    # From Step 3 on, UNDECIDED_QUALITY_SPEC_IS_FATAL is True: an active source
    # that never overrode quality_spec() (inherits None) halts the build.
    client = _client_with(_UndecidedTransformer())
    with pytest.raises(UndecidedQualitySpecError):
        client.build()
    client.close()


# --- Re-build idempotency test ---

class _RebuildableTransformer(BaseTransformer):
    """Recreates its own bronze/silver idempotently and writes one exclusion +
    one quarantine row per build. reject_ceiling is loosened so the single FALSE
    flag does not trip the reject-rate tripwire (not what this test exercises); the
    bronze is 300 rows so the single quarantined line (1/301 ~= 0.33%) stays under the
    0.5% quarantine-rate tripwire, which is also not what this test exercises."""

    @property
    def source_id(self):
        return "rb"

    def extract(self, cache_dir, **kwargs):
        pass

    def transform_and_load(self, con, cache_dir):
        # The transformer owns its bronze/silver DDL -> recreate idempotently.
        con.execute("CREATE OR REPLACE TABLE rb_raw (source_row_key VARCHAR)")
        con.execute("INSERT INTO rb_raw SELECT 'r' || i FROM range(300) t(i)")
        con.execute("CREATE OR REPLACE TABLE rb (source_row_key VARCHAR, ok BOOLEAN)")
        con.execute("INSERT INTO rb SELECT 'r' || i, (i <> 0) FROM range(300) t(i)")  # r0 FALSE
        log_exclusion(
            con, source_id="rb", source_row_key="r0", column_name="x",
            rule_id="rb.x.bad", rule_desc="bad", severity="reject_dimension",
            raw_value="-1",
        )
        quarantine_row(con, source_id="rb", raw_text="broken,line", reason="bad row")
        # 300 rows landed in bronze + 1 quarantined = 301 rows read from source.
        record_source_rows(con, "rb", 301)

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


# --- Stage 04-prep (Step 4) — multi-spec generalization ---

def test_resolve_quality_specs_flattens_tuple(con):
    from crossroads.quality import (
        ensure_quality_tables, resolve_quality_specs, declared_source_ids,
        SourceQuality,
    )
    from crossroads.transformers.base import BaseTransformer
    ensure_quality_tables(con)

    class MultiUnit(BaseTransformer):
        source_id = "multi"
        def extract(self, cache_dir, **kwargs): pass
        def transform_and_load(self, con, cache_dir): pass
        def quality_spec(self):
            return (
                SourceQuality("multi_a", "a_raw", "a", key_column="source_row_key"),
                SourceQuality("multi_b", "b_raw", "b", key_column="source_row_key"),
            )

    t = MultiUnit()
    specs = resolve_quality_specs(con, [t])
    assert {s.source_id for s in specs} == {"multi_a", "multi_b"}
    assert declared_source_ids(t) == ["multi_a", "multi_b"]


def test_resolve_quality_specs_mixed_tuple(con):
    # A tuple may mix an audited unit with an explicit opt-out. The SourceQuality
    # is collected for auditing; the QualityExemption is recorded (not audited).
    from crossroads.quality import (
        ensure_quality_tables, resolve_quality_specs, declared_source_ids,
        SourceQuality, QualityExemption,
    )
    from crossroads.transformers.base import BaseTransformer
    ensure_quality_tables(con)

    class Mixed(BaseTransformer):
        source_id = "mixed"
        def extract(self, cache_dir, **kwargs): pass
        def transform_and_load(self, con, cache_dir): pass
        def quality_spec(self):
            return (
                SourceQuality("mixed_a", "a_raw", "a", key_column="source_row_key"),
                QualityExemption(reason="aggregate unit; conservation N/A"),
            )

    t = Mixed()
    specs = resolve_quality_specs(con, [t])

    # Only the SourceQuality is returned for auditing.
    assert [s.source_id for s in specs] == ["mixed_a"]

    # The exemption is recorded under the transformer's own source_id (there is
    # no separate audit source for an opt-out).
    assert con.execute(
        "SELECT source_id, reason FROM quality_exemptions"
    ).fetchall() == [("mixed", "aggregate unit; conservation N/A")]

    # declared_source_ids reports the audited unit(s) only — the exemption
    # contributes no id, and the SourceQuality means we don't fall back.
    assert declared_source_ids(t) == ["mixed_a"]


def test_declared_source_ids_falls_back_without_source_quality(con):
    # A transformer that only opts out (no SourceQuality) has no audit-specific
    # id, so declared_source_ids falls back to its own source_id — this is the id
    # Client.build uses to reset the shared audit tables before a (re)build.
    from crossroads.quality import (
        ensure_quality_tables, declared_source_ids, QualityExemption,
    )
    from crossroads.transformers.base import BaseTransformer
    ensure_quality_tables(con)

    class OptOut(BaseTransformer):
        source_id = "optout"
        def extract(self, cache_dir, **kwargs): pass
        def transform_and_load(self, con, cache_dir): pass
        def quality_spec(self):
            return QualityExemption(reason="static lookup; no source rows")

    assert declared_source_ids(OptOut()) == ["optout"]


def test_resolve_rejects_wrong_type_inside_tuple(con):
    # A bad element anywhere in the tuple is still a programming error — each
    # element is validated independently, so the string here raises TypeError.
    ensure_quality_tables(con)
    spec = SourceQuality(source_id="ok", bronze_table="ok_raw", silver_table="ok")
    with pytest.raises(TypeError):
        resolve_quality_specs(con, [_FakeTransformer("bad", (spec, "not a spec"))])


def test_resolve_undecided_inside_tuple_is_fatal_when_enabled(con):
    # A None element inside a tuple hits the same undecided path as a lone None:
    # fatal when enabled, even though a valid SourceQuality sits alongside it.
    ensure_quality_tables(con)
    spec = SourceQuality(source_id="ok", bronze_table="ok_raw", silver_table="ok")
    with pytest.raises(UndecidedQualitySpecError):
        resolve_quality_specs(
            con, [_FakeTransformer("u", (spec, None))], undecided_fatal=True
        )


def test_resolve_undecided_inside_tuple_warns_when_not_fatal(con):
    # Warn-only path: the None is skipped with a warning and the sibling
    # SourceQuality is still collected for auditing.
    ensure_quality_tables(con)
    spec = SourceQuality(source_id="ok", bronze_table="ok_raw", silver_table="ok")
    out = resolve_quality_specs(
        con, [_FakeTransformer("u", (spec, None))], undecided_fatal=False
    )
    assert out == [spec]


# --- CSV bronze loader: reject capture + quarantine (spec §9) ---

def test_malformed_csv_line_is_quarantined_not_fatal(tmp_path, con):
    """A structurally-broken row (wrong column count) is skipped from bronze, recorded
    verbatim in quarantine_raw, and the load returns — it does NOT crash the build."""
    from crossroads.quality import load_csv_bronze_with_quarantine
    ensure_quality_tables(con)
    p = tmp_path / "sample.csv"
    p.write_text("a,b,c\n1,2,3\n4,5\n6,7,8\n")     # line "4,5" has only 2 columns -> reject
    loaded, quarantined = load_csv_bronze_with_quarantine(
        con, bronze_table="t_raw", paths=[str(p)],
        read_opts="header=true, all_varchar=true", source_id="t")
    assert loaded == 2 and quarantined == 1
    assert con.execute("SELECT count(*) FROM t_raw").fetchone()[0] == 2
    q = con.execute("SELECT raw_text FROM quarantine_raw WHERE source_id='t'").fetchall()
    assert any("4,5" in r[0] for r in q)           # the bad line was recorded verbatim


def test_quarantine_dedups_by_line_not_text(tmp_path, con):
    """Two DISTINCT source lines with identical text are quarantined as TWO rows, so the
    source total (bronze + quarantine) still balances (grouping by text would undercount)."""
    from crossroads.quality import load_csv_bronze_with_quarantine
    ensure_quality_tables(con)
    p = tmp_path / "dup.csv"
    p.write_text("a,b,c\n1,2,3\n4,5\n4,5\n7,8,9\n")  # two identical bad lines "4,5"
    loaded, quarantined = load_csv_bronze_with_quarantine(
        con, bronze_table="d_raw", paths=[str(p)],
        read_opts="header=true, all_varchar=true", source_id="d")
    assert loaded == 2 and quarantined == 2         # 2 good + 2 bad == 4 source rows


def test_clean_csv_quarantines_nothing(tmp_path, con):
    """A clean file loads fully and writes no quarantine rows."""
    from crossroads.quality import load_csv_bronze_with_quarantine
    ensure_quality_tables(con)
    p = tmp_path / "clean.csv"
    p.write_text("a,b,c\n1,2,3\n4,5,6\n")
    loaded, quarantined = load_csv_bronze_with_quarantine(
        con, bronze_table="c_raw", paths=[str(p)],
        read_opts="header=true, all_varchar=true", source_id="c")
    assert loaded == 2 and quarantined == 0
    assert con.execute(
        "SELECT count(*) FROM quarantine_raw WHERE source_id='c'").fetchone()[0] == 0


# --- Quarantine-rate tripwire (spec §9: quarantine must be rare) ---

def test_quarantine_rate_over_ceiling_fails(con):
    from crossroads.quality import check_quarantine_rate, QuarantineRateExceededError
    ensure_quality_tables(con)
    record_source_rows(con, "s", 10)
    for i in range(3):                              # 3/10 = 30% > 5%
        quarantine_row(con, source_id="s", raw_text=f"bad{i}", reason="x")
    spec = SourceQuality("s", "s_raw", "s_silver")
    with pytest.raises(QuarantineRateExceededError):
        check_quarantine_rate(con, spec)


def test_quarantine_rate_within_ceiling_passes(con):
    from crossroads.quality import check_quarantine_rate
    ensure_quality_tables(con)
    record_source_rows(con, "s", 1000)
    quarantine_row(con, source_id="s", raw_text="bad", reason="x")   # 1/1000 = 0.1% <= 0.5%
    check_quarantine_rate(con, SourceQuality("s", "s_raw", "s_silver"))  # must not raise


def test_quarantine_rate_empty_source_passes(con):
    from crossroads.quality import check_quarantine_rate
    ensure_quality_tables(con)
    record_source_rows(con, "s", 0)
    check_quarantine_rate(con, SourceQuality("s", "s_raw", "s_silver"))  # 0 rows -> no raise
