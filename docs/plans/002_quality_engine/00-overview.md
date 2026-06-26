# Data Quality & Audit Engine — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Build the shared, source-agnostic **quality infrastructure** (`src/crossroads/quality.py`) defined in `docs/spec.md` §9 — bronze/silver/gold conventions, the `data_quality_log` exclusion ledger, the `quarantine_raw` table, and the three build-end invariants — and wire it into `Client.build()`, so every later ingestion step (Steps 3, 4, 6) is correct-by-construction and audited identically.

## Context & Objective

**What exists today (Step 1, "Project Foundation", already merged).** The installable `crossroads` package is in place:
- `src/crossroads/transformers/base.py` — the `BaseTransformer` ABC: a `source_id` property plus `is_active(**kwargs)`, `extract(cache_dir, **kwargs)`, and `transform_and_load(con, cache_dir)`.
- `src/crossroads/registry.py` — `Registry` discovers concrete `BaseTransformer` subclasses in `crossroads.transformers` via `pkgutil` + `inspect`, sorted by `source_id`; `get_active(**kwargs)` filters on `is_active`.
- `src/crossroads/client.py` — `init_engine(...)` returns a `Client`; `Client.build(**kwargs)` opens a DuckDB connection (`self.con`), runs `for t in self.registry.get_active(**kwargs): t.extract(...); t.transform_and_load(...)`, and returns `self`. With **zero transformers**, `build()` is a clean no-op.
- `tests/conftest.py` — a shared `con` fixture (fresh in-memory DuckDB, closed after the test).
- `pyproject.toml` — package `crossroads-uk`, import `crossroads`, Hatchling backend, `requires-python = ">=3.11"`, single runtime dep `duckdb>=1.0`, dev dep `pytest`.

There is **no** `quality.py`, no `data_quality_log`, no `quarantine_raw`, and `build()` performs no auditing. There are **no real transformers yet** — the first one (`spatial.py`) arrives in Step 3. This step therefore proves itself entirely against **synthetic** bronze/silver fixtures, exactly as the master plan requires.

**What changes.** This step creates one new module, `src/crossroads/quality.py`, holding:
- The `SourceQuality` / `Dimension` manifest dataclasses and the `QualityExemption` opt-out — what a source uses to declare what the engine audits (or, explicitly, that it should not be).
- The shared audit tables (`source_ingest_log`, `data_quality_log`, `quarantine_raw`, `quality_exemptions`), their writer helpers, and a `reset_source_audit` helper that clears a source's audit rows before it is (re)built.
- A gold-view helper.
- A `check_schema_contract(...)` pre-check and the three invariant checks (conservation, flag/ledger agreement, reject-rate tripwire) as aggregate SQL, the `resolve_quality_specs(...)` coverage gate, a `run_invariants(...)` orchestrator, and a small exception hierarchy.

And it edits **two** existing files:
- `src/crossroads/transformers/base.py` — adds a **concrete** `quality_spec()` method to `BaseTransformer` (inherited default returns `None` = "undecided"), making the audit obligation a visible, self-documenting part of the transformer contract.
- `src/crossroads/client.py` — creates the audit tables at the start of `build()`, clears each active source's prior audit rows (`reset_source_audit`) before running the extract/transform loop, resolves each active transformer's three-state audit decision (audit / explicit exemption / undecided), and runs the invariants at the end of `build()` (fatal on violation).

**The goal.** After this step, every active transformer **must make a conscious audit decision** or the build complains:
- returning a `SourceQuality` gets the source audited automatically on every build — the build halts loudly if rows go missing, flags disagree with the ledger, or a reject rate exceeds its ceiling;
- returning a `QualityExemption(reason=...)` opts the source out explicitly, with the reason recorded in the database as an auditable artifact;
- and the inherited `None` default (nobody decided) is rejected — a hard failure once the first real source lands, and a loud warning in the interim (see "Coverage enforcement" below).

## Approach / Architecture

### The data-quality model (spec §9), made concrete

Three layers, **keep-in-place** (no row is ever deleted):

- **Bronze — `<source>_raw`** (e.g. `stats19_raw`): a faithful, append-only copy of every row that could be structured from the source. The source transformer creates and fills it (thin-helper decision: the engine does **not** generate bronze/silver DDL — column shapes differ wildly per source).
- **Silver — typed facts** (e.g. `collisions`): **1:1 with bronze**. Each silver table carries a stable `source_row_key VARCHAR` plus, per validated dimension, a clean typed column and a boolean flag column (`<dimension>_valid`). When a value fails its rule, the clean column is `NULL` and the flag is `FALSE`; the raw value is preserved and a `data_quality_log` row is written. The row is **never removed**.
- **Gold — clean views** (e.g. `collisions_spatial`): `CREATE VIEW ... AS SELECT * FROM <silver> WHERE <flag1> AND <flag2> ...`. Created with the engine's `create_clean_view(...)` helper.

Two shared, **source-agnostic** audit tables (one set per database, created idempotently at build start):

```
data_quality_log(
  source_id      VARCHAR,    -- e.g. 'stats19'
  source_row_key VARCHAR,    -- natural/composite key of the offending silver row
  column_name    VARCHAR,    -- field that failed (NULL for whole-row issues)
  rule_id        VARCHAR,    -- stable id, e.g. 'stats19.coord.sentinel'
  rule_desc      VARCHAR,    -- human-readable reason
  severity       VARCHAR,    -- 'reject_dimension' | 'warn'
  raw_value      VARCHAR,    -- the value that failed
  ingested_at    TIMESTAMP DEFAULT current_timestamp
)

quarantine_raw(
  source_id   VARCHAR,
  raw_text    VARCHAR,       -- the unparseable source line
  reason      VARCHAR,
  ingested_at TIMESTAMP DEFAULT current_timestamp
)
```

Plus two small accounting tables:

```
source_ingest_log(            -- needed by the conservation invariant (see below)
  source_id   VARCHAR,
  source_rows BIGINT,         -- count of rows the transformer READ from the source
  ingested_at TIMESTAMP DEFAULT current_timestamp
)

quality_exemptions(           -- the auditable record of which sources opted OUT
  source_id   VARCHAR,
  reason      VARCHAR,         -- the QualityExemption(reason=...) text
  ingested_at TIMESTAMP DEFAULT current_timestamp
)
```

> **Why `source_ingest_log` exists.** The conservation invariant (spec §9-1) is `source_rows == clean_rows + quarantined_rows`. `clean_rows = count(silver) = count(bronze)` (keep-in-place), and `quarantined_rows = count(quarantine_raw)`. But `source_rows` — the number of rows the transformer actually *read from the source file* — is independent runtime information; without it the engine cannot detect rows lost **between reading the source and landing in bronze**. So a transformer records that observed count via `record_source_rows(con, source_id, n)`. Synthetic tests pass `n` directly.

> **Why `quality_exemptions` exists.** A `QualityExemption` is a source-level decision to *not* run the invariants. Per spec §9 the database itself must answer "what was not processed, and why?" — so a deliberate opt-out is recorded here, one row per exempted source, making the built database self-describing about its own audit coverage. (Build also emits a `logging` line; the table is the durable, queryable record.)

### How a source declares what to audit — the three-state contract (locked decision)

The engine never names a concrete source. Instead, **every** transformer declares its audit surface through a concrete `quality_spec()` method on `BaseTransformer`. That method has **three** legal return states, not two:

```python
@dataclass(frozen=True)
class Dimension:
    name: str                          # e.g. "geom"
    flag_column: str                   # silver boolean column, e.g. "geom_valid"
    rule_ids: tuple[str, ...]          # data_quality_log rule_ids that null THIS dimension
    reject_ceiling: float | None = None  # None -> DEFAULT_REJECT_CEILING (0.05)

@dataclass(frozen=True)
class SourceQuality:
    source_id: str
    bronze_table: str                  # e.g. "stats19_raw"
    silver_table: str                  # e.g. "collisions"
    dimensions: tuple[Dimension, ...] = ()
    key_column: str = "source_row_key" # the stable key shared by silver + ledger

@dataclass(frozen=True)
class QualityExemption:
    reason: str  # e.g. "aggregates many bronze rows into one silver row; conservation N/A"
```

`BaseTransformer.quality_spec()` is **concrete** (not abstract); its inherited default returns `None`:

```python
def quality_spec(self) -> "SourceQuality | QualityExemption | None":
    """Declare this source's audit surface for the quality engine.
    Override to return one of:
      • SourceQuality(...)        -> this source is audited.
      • QualityExemption(reason=) -> deliberately NOT audited, with a written reason.
    The inherited default (None) means 'undecided' and FAILS the build for an
    active transformer — so a new source must make a conscious choice."""
    return None
```

| `quality_spec()` returns | Meaning | Build behaviour |
|--------------------------|---------|-----------------|
| `None` (inherited default) | nobody decided | **fail loud** (warning during the interim — see below) |
| `SourceQuality(...)` | audit this source | run the three invariants |
| `QualityExemption(reason=...)` | deliberately not audited | pass; record the reason in `quality_exemptions` |

**Why a concrete base method (not abstract, not a duck-typed optional).** Crossroads is open-source and academic: future contributors add their own transformers, and the quality engine is what guarantees no rows silently vanish or get mis-flagged. So the obligation must be both *visible* and *enforced*:
- A concrete method on `BaseTransformer` makes the contract self-documenting — anyone subclassing it sees `quality_spec()` and its docstring and understands they must declare their audit surface.
- It must be **concrete**, not `@abstractmethod`: an abstract method would (a) make the Step 1 registry test doubles abstract and break those tests, and (b) cause `Registry._discover` (`registry.py:37`) to *silently skip* any contributor transformer that forgot to implement it — dropping it from discovery entirely, which is strictly worse than an unaudited run and the opposite of the coverage we want. A concrete default keeps the source discoverable so the build-time gate can see it and fail loudly on the missing decision.
- A single `None` cannot mean both "I forgot" and "I deliberately opted out"; if it did, the build would have to fail on both (no opt-out possible) or pass on both (no enforcement). So the opt-out is a *positive, distinct* declaration (`QualityExemption`) carrying a written reason, separate from the undecided default. This preserves a genuine opt-out for sources that legitimately don't fit the keep-in-place invariants (e.g. an aggregating/reshaping transformer where `count(bronze) == count(silver)` is false by design, or a static reference/lookup loader) — and every such exemption is reasoned and recorded, not a silent omission.

**Coverage enforcement (where).** At build time, against active transformers, just before `run_invariants`. The helper `resolve_quality_specs(con, transformers)` inspects each active transformer's `quality_spec()`, returns the `SourceQuality` list to audit, records every `QualityExemption` in `quality_exemptions`, and on an undecided `None` either raises `UndecidedQualitySpecError` (fatal) or logs a warning — gated by the module flag `UNDECIDED_QUALITY_SPEC_IS_FATAL`. A return value of any other type raises `TypeError`.

**Warn → fail escalation (interim).** The end state is *fail* on undecided. But there are no real transformers yet, so Step 2 ships with `UNDECIDED_QUALITY_SPEC_IS_FATAL = False` (warn only). **Escalation trigger:** flip it to `True` in Step 3, the moment the first real transformer (`spatial.py`) lands and proves the `SourceQuality` shape end-to-end. This is written down here and in Stage 03 so "warn" does not become permanent by inertia.

### The three invariants (all aggregate SQL — single `O(rows)` scans, no Python row loops)

For each collected `SourceQuality` spec, `run_invariants` first runs a **schema-contract pre-check** (`check_schema_contract`): it queries `information_schema.columns` and raises `SchemaContractError` if the silver table is missing the manifest's `key_column` or any dimension's `flag_column`. This fails fast with a named error instead of letting a forgotten silver-schema convention surface as a cryptic DuckDB binder error deep inside the agreement SQL. Then the three invariants run:

1. **Conservation (fatal).** Two SQL count checks:
   - keep-in-place identity: `count(bronze_table) == count(silver_table)`;
   - conservation sum: `sum(source_ingest_log.source_rows for source) == count(bronze_table) + count(quarantine_raw for source)`.
   A mismatch means rows vanished unaccounted → raise `ConservationError`.
2. **Flag/ledger agreement (fatal).** Per dimension, the set of silver rows with `flag_column = FALSE` must equal the set of `source_row_key`s in `data_quality_log` for this source whose `rule_id IN dimension.rule_ids` and `severity = 'reject_dimension'`. Checked as two anti-join counts (silver-flagged-but-not-logged, logged-but-not-flagged); either non-zero → raise `FlagLedgerAgreementError`.
3. **Reject-rate tripwire (configurable, fatal above ceiling).** Per dimension, `rejected / total` (where `rejected = count(silver WHERE flag = FALSE)`, `total = count(silver)`) must be `<= dimension.reject_ceiling or DEFAULT_REJECT_CEILING (0.05)`. A global `build(reject_ceiling=...)` override, when supplied, replaces the default for dimensions that did not set their own. Over ceiling → raise `RejectRateExceededError`.

`'warn'`-severity ledger rows never null a clean column and never participate in agreement or reject-rate; they are informational only.

### Build integration

`Client.build()` gains, with **no change to the provider-plugin purity** (it still never names a source):
1. After `self.con = duckdb.connect(...)`: `quality.ensure_quality_tables(self.con)`.
2. For each active transformer, **first** `quality.reset_source_audit(self.con, transformer.source_id)` (clears that source's stale audit rows so a re-build is idempotent), then `extract` → `transform_and_load`.
3. `specs = quality.resolve_quality_specs(self.con, active_transformers)` — the coverage gate: collects `SourceQuality` manifests, records `QualityExemption`s, and warns/raises on undecided.
4. Before `return self`: `quality.run_invariants(self.con, specs, default_ceiling=kwargs.get("reject_ceiling") or quality.DEFAULT_REJECT_CEILING)`. Any invariant violation raises and propagates out of `build()` (the build halts, per spec §9 "Halt semantics").

An empty build (zero transformers → zero specs) creates the empty audit tables and runs zero invariants — still a clean success, so the existing Step 1 `build()` tests stay green.

### Alternatives rejected
- *Naming-convention-only auditing* (infer bronze as `<source>_raw`, flags by `*_valid` suffix): silver table names vary (`collisions`, `weather`) and dimension→rule_id mapping can't be inferred, so agreement becomes guesswork. Rejected.
- *A duck-typed optional `quality_spec()` (not on the base class)*: keeps `base.py` untouched, but auditing becomes an invisible, opt-in convention — a contributor can ship a complete, working transformer that produces an *unaudited* dataset without ever realising the engine exists. Rejected in favour of a visible, enforced contract.
- *An `@abstractmethod quality_spec()`*: would enforce a decision, but a forgotten implementation makes the class abstract, so `Registry._discover` (`registry.py:37`) silently drops it from discovery and it never runs at all — worse than unaudited. It would also break the Step 1 registry test doubles. Rejected in favour of a **concrete** default + a build-time coverage gate.
- *An opinionated bronze/silver schema generator (column-spec DSL)*: a ~200-line abstraction built on one hypothetical source before Steps 3/4 reveal real column shapes. Rejected for thin helpers (`docs/spec.md` "keep it simple"; CLAUDE.md).
- *Per-row Python validation loops*: would make the audit the bottleneck; spec §9 mandates aggregate SQL. Rejected.

## Cross-Cutting Constraints (every stage follows these)

- **Single module:** all quality code lives in `src/crossroads/quality.py` (spec §7 blueprint names it). No sub-package.
- **No new dependencies:** `duckdb` + `pytest` only. Do not add anything to `pyproject.toml`.
- **Provider-plugin purity** (spec §4): `client.py` and `registry.py` must never name a concrete source. The `quality_spec()` method added to `base.py` is source-agnostic (it names no concrete source); the manifest is the only coupling, and it is supplied *by* the source.
- **Aggregate SQL only** (spec §9): every invariant is a `SELECT count(...)`-style scan, never a per-row Python loop.
- **Keep-in-place** (spec §9): helpers and checks must never delete or mutate a landed row; bad data is flagged and logged.
- **Determinism / reproducibility** (spec §2, master plan): no wall-clock or randomness in engine *logic*. The one timestamp, `ingested_at`, is **provenance metadata**, written via a DuckDB column `DEFAULT current_timestamp` (the database records it, not Python), and is explicitly **excluded** from the structural-reproducibility guarantee. Tests must never assert on `ingested_at` values.
- **SQL identifier interpolation:** table/column names are interpolated into SQL strings from the manifest (trusted, code-supplied — not user input). Row *values* are always passed as bound `?` parameters. Document this trust boundary in code comments.
- **Style:** plain-language comments, simple code (CLAUDE.md). Match the existing module docstring / comment density in `client.py` and `registry.py`.
- **Git discipline:** never stage or commit without explicit user permission (CLAUDE.md).

## Stage Map (sequential — do in order)

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Schema, manifest & contract | The `Dimension` / `SourceQuality` / `QualityExemption` dataclasses and `DEFAULT_REJECT_CEILING` in `quality.py`; the `create_clean_view(...)` gold-view helper; the concrete `quality_spec()` method added to `BaseTransformer` (default `None`). | `crossroads.quality` imports; dataclasses construct and are frozen; a plain `BaseTransformer` subclass inherits `quality_spec() == None`; `create_clean_view` filters a synthetic silver table to flag-valid rows. Step 1 registry tests stay green. `pytest` green. | 01 (Step 1 foundation) | `01-layered-schema-helpers.md` |
| 02 | Exclusion ledger & quarantine | `ensure_quality_tables(con)` creating `source_ingest_log`, `data_quality_log`, `quarantine_raw`, `quality_exemptions`; writer helpers `record_source_rows`, `log_exclusion`, `quarantine_row`, `record_exemption`. | Tables created idempotently with the spec §9 schema; writers insert rows readable back via SQL; `ingested_at` auto-populates. `pytest` green. | Stage 01 | `02-ledger-quarantine.md` |
| 03 | Invariants, coverage gate & build integration | The three invariant checks + `run_invariants(...)`; the `resolve_quality_specs(...)` coverage gate + `UNDECIDED_QUALITY_SPEC_IS_FATAL` flag; the exception hierarchy (incl. `UndecidedQualitySpecError`), wired into `Client.build()`. | Synthetic fixtures: clean data passes; a missing silver row fails conservation; a flag without a ledger entry (and vice versa) fails agreement; an over-ceiling reject rate fails the tripwire; an exemption is recorded and skips invariants; an undecided source warns (interim); an end-to-end `build()` runs the audit. `pytest` green. | Stages 01, 02 | `03-invariants-build-integration.md` |

## Global Testing & Ship

All tests are **real and runnable** (manual testing is not relied upon). A new `tests/test_quality.py` holds the synthetic fixtures and assertions; it reuses the existing `con` fixture from `tests/conftest.py`. From the repo root with the venv active:

```bash
python -m pytest -q
```
Expected at the end of **every** stage: all tests pass, zero failures, zero errors (including the pre-existing Step 1 tests).

The **end-to-end ship proof** for this step attaches to **Stage 03**: a synthetic `BaseTransformer` subclass defined in the test creates bronze + silver tables on the build connection, records its source count, logs exclusions, and overrides `quality_spec()` to return a `SourceQuality`. Running `client.build()` then exercises the *entire* path — table creation → coverage gate → all three invariants — proving a future real source will be audited automatically. Sibling tests cover the other two decision states (a `QualityExemption` source builds cleanly and is recorded in `quality_exemptions`; an undecided `None` source warns in the interim and would fail once `UNDECIDED_QUALITY_SPEC_IS_FATAL` flips) and a seeded violation that makes `build()` raise the specific quality exception. No real source data is in scope; data-fidelity-against-source assertions arrive with the first real transformer in Step 3.

## Open Questions / Risks

- **`ingested_at` vs. reproducibility.** Resolved: provenance metadata via DB-side `DEFAULT current_timestamp`, excluded from the structural-reproducibility guarantee; never asserted in tests. (Documented above and in each stage.)
- **On-disk re-builds accumulating audit rows.** `ensure_quality_tables` is `CREATE … IF NOT EXISTS`, so a second `build()` against the same on-disk file keeps the prior run's audit rows. The append-only writers (`log_exclusion`, `quarantine_row`) would then duplicate — corrupting the ledger and breaking conservation (`quarantine_raw` is counted raw) on the next build. **Resolved with a split responsibility:** the engine clears the audit tables it owns via `reset_source_audit(con, source_id)` at the top of the build loop (Stage 02/03), and each transformer must recreate its **own** bronze/silver idempotently (`CREATE OR REPLACE`), documented in the `BaseTransformer.transform_and_load` contract (Stage 01). Proven end-to-end by a same-file re-build test in Stage 03. This is deliberately consistent with the coverage-gate philosophy — the engine enforces what it owns rather than relying on every contributor to remember cleanup.
- **Silver `source_row_key` is a hard convention — now enforced.** Agreement and the keep-in-place key join require every silver table to carry the manifest's `key_column`, and the agreement/reject-rate SQL requires each dimension's `flag_column`. Rather than relying on Steps 3/4 to remember, `run_invariants` opens with `check_schema_contract`, which queries `information_schema.columns` and raises `SchemaContractError` if any declared column is absent — so a transformer that forgets the convention fails fast with a named, actionable error. A wholly-missing silver table is reported with a distinct "does not exist" message (so it isn't mistaken for a missing column). Documented in Stage 03; synthetic fixtures cover the missing-table, missing-`key_column`, and missing-`flag_column` cases.
- **`reject_ceiling` flows through `build(**kwargs)`.** The global override rides in the same `**kwargs` forwarded to transformers; transformers accept `**kwargs` and ignore unknown keys, so this is harmless. Noted in Stage 03.
- **Warn → fail escalation must not stall.** Step 2 ships `UNDECIDED_QUALITY_SPEC_IS_FATAL = False` (warn only) because no real transformer exists yet. The trigger to flip it to `True` is the landing of the first real transformer (`spatial.py`) in Step 3. Recorded here and in Stage 03 so the interim does not become permanent; the Step 3 plan must action it.
- **Recording exemptions in a table (vs. logging only).** This plan records every `QualityExemption` in a `quality_exemptions` table *and* emits a `logging` line, so the built database is self-describing about its own audit coverage (spec §9: the DB answers "what was not processed, and why?"). If a reviewer prefers logging-only, dropping the table and `record_exemption` is a small, contained change — but the database would then no longer answer the coverage question by query.
