# Stage 01 — Schema, Manifest & Contract
> Part of "Data Quality & Audit Engine". You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

Before starting, verify the Step 1 foundation is present and green:
- `src/crossroads/client.py`, `src/crossroads/registry.py`, `src/crossroads/transformers/base.py` exist.
- `tests/conftest.py` provides a `con` fixture (in-memory DuckDB).
- `src/crossroads/quality.py` does **not** exist yet.
- Running `python -m pytest -q` from the repo root (venv active) passes.

If `quality.py` already exists, you are resuming — reconcile against the End State below rather than overwriting blindly.

## Objective

Two pieces of the data-quality **contract**:
1. Create `src/crossroads/quality.py` with the manifest dataclasses (`Dimension`, `SourceQuality`), the `QualityExemption` opt-out dataclass, the `DEFAULT_REJECT_CEILING` constant, and the gold-view helper `create_clean_view(...)`.
2. Edit `src/crossroads/transformers/base.py` to add a **concrete** `quality_spec()` method to `BaseTransformer` whose inherited default returns `None` ("undecided").

This stage introduces **no tables and no invariants** — only the data model a source uses to declare its audit surface (or opt out), the visible base-class contract, and the one helper that builds clean (gold) views. Enforcement of the decision arrives in Stage 03.

## Implementation Steps

### 1. Create `src/crossroads/quality.py`

Create the file with a module docstring and these contents. Keep comments plain-language.

```python
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
```

### 2. Add the concrete `quality_spec()` method to `BaseTransformer`

Edit `src/crossroads/transformers/base.py`. Add the method below to the `BaseTransformer` class (place it after `is_active`, before `extract`). Do **not** make it abstract, and do **not** import from `crossroads.quality` — the return annotation is a forward-reference string, so no import is needed and no import cycle is created (`quality.py` must stay free of any dependency on `base.py`).

```python
    def quality_spec(self) -> "SourceQuality | QualityExemption | None":
        """Declare this source's audit surface for the quality engine
        (conservation, flag/ledger agreement, reject-rate invariants).

        Override to return one of:
          • SourceQuality(...)         -> this source is audited.
          • QualityExemption(reason=)  -> deliberately NOT audited, with a written
                                          reason (e.g. it aggregates rows so the
                                          conservation invariant does not apply).

        The inherited default below (None) means 'undecided'. For an ACTIVE
        transformer that is enforced at build time: a real source must make a
        conscious choice (see crossroads.quality.resolve_quality_specs). Keeping
        this concrete (not @abstractmethod) means a subclass that forgets to
        override it stays discoverable by the registry and fails loudly at the
        coverage gate, rather than being silently dropped from discovery.
        """
        return None
```

> The annotation names `SourceQuality` / `QualityExemption` only as a string. If you want the names importable for tooling without a runtime import, you may add `from __future__ import annotations` at the top of `base.py` — optional, not required.

### 3. Document the re-build idempotency contract on `transform_and_load`

Still in `src/crossroads/transformers/base.py`, extend the existing `transform_and_load` abstract method's docstring so the contributor contract is visible. Replace its current one-line docstring:

```python
        """Execute zero-loss transformations and load into target DuckDB tables."""
```

with:

```python
        """Execute zero-loss transformations and load into target DuckDB tables.

        Must be idempotent across re-builds: recreate this source's own bronze and
        silver tables with ``CREATE OR REPLACE TABLE`` (or ``DROP``+``CREATE``),
        never a bare ``CREATE TABLE`` (errors on the second build) nor
        ``CREATE TABLE IF NOT EXISTS`` + ``INSERT`` (would double the rows). The
        engine clears the SHARED audit tables for this source before this runs
        (see ``crossroads.quality.reset_source_audit``); recreating bronze/silver
        is the transformer's half of keeping a re-build idempotent.
        """
```

This is a comment/contract change only — it does not alter behaviour, so no test targets it directly; the re-build idempotency is proven end-to-end in Stage 03.

### 4. Do not touch any other file

No changes to `client.py`, `registry.py`, `pyproject.toml`, or `__init__.py` in this stage. Build-time enforcement of the `quality_spec()` decision is Stage 03. `quality.py` does not need to be re-exported from `crossroads/__init__.py`.

## Testing & Verification

### Integration test (PRIMARY) — gold view filters correctly

Create `tests/test_quality.py`:

```python
import duckdb
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
```

(The `con` fixture comes from the existing `tests/conftest.py`.)

### Stage ship-readiness checklist
- [ ] `python -c "import crossroads.quality"` succeeds (run with the venv active).
- [ ] `python -m pytest -q` is green — the new `test_quality.py` **and** all pre-existing Step 1 tests (especially `test_registry.py`, whose `BaseTransformer` doubles must still be discovered — adding a *concrete* `quality_spec()` must not make them abstract).
- [ ] `src/crossroads/quality.py` contains `DEFAULT_REJECT_CEILING`, `Dimension`, `SourceQuality`, `QualityExemption`, and `create_clean_view` — and **no** table creation or invariant code (those are Stages 02/03).
- [ ] `BaseTransformer.quality_spec()` exists, is **not** abstract, and returns `None` by default; `base.py` imports nothing from `crossroads.quality`.
- [ ] `BaseTransformer.transform_and_load`'s docstring documents the re-build idempotency contract (recreate bronze/silver with `CREATE OR REPLACE`).

## End State / Handoff

`src/crossroads/quality.py` exists and exports `DEFAULT_REJECT_CEILING`, the frozen `Dimension`, `SourceQuality`, and `QualityExemption` dataclasses, and `create_clean_view(con, view_name, silver_table, flag_columns)`. `BaseTransformer` now has a concrete `quality_spec()` returning `None` by default. `tests/test_quality.py` exists and is green, and all Step 1 tests still pass. The next stage (02) may assume these symbols exist and will add `ensure_quality_tables` plus the writer helpers to the **same** module.

## Failure Modes & Rollback

- **Frozen-dataclass test:** assigning to a frozen dataclass raises `dataclasses.FrozenInstanceError` (a subclass of `Exception`); the broad `pytest.raises(Exception)` is intentional to avoid importing the specific class.
- **Registry doubles vanish from discovery:** if any Step 1 `test_registry.py` test starts failing because a transformer is no longer discovered, you made `quality_spec()` `@abstractmethod` by mistake — it must be a **concrete** method. Remove the `@abstractmethod` decorator.
- **Import cycle:** if `import crossroads` fails after editing `base.py`, you added an `import` from `crossroads.quality` into `base.py`. Remove it — the return annotation must remain a forward-reference string.
- **Rollback:** delete `src/crossroads/quality.py` and `tests/test_quality.py`, and remove the `quality_spec()` method from `src/crossroads/transformers/base.py`. The repo then returns exactly to the Step 1 end state.
