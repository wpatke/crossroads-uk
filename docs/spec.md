# Crossroads-UK: A Reproducible Pipeline for Unifying UK Road-Safety, Weather, and Boundary Data

Crossroads-UK automates the downloading, cleansing, and unification of UK government datasets, including transport records, meteorological history, and geographic boundaries.

The project is an open-source, Python orchestration pipeline merging all data into a single local DuckDB database. Road safety is where Crossroads-UK starts, not where it stops: the engine is dataset-agnostic by design (§4), and any UK public dataset can be added as a new source without touching the core. 

This project was inspired by a suggestion from Dr. Robin Lovelace at [ropensci/stats19](https://github.com/ropensci/stats19/issues/230). Crossroads-UK intends to expand on this core idea.

AI usage and development workflows are detailed in [AI_DISCLOSURE.md](../AI_DISCLOSURE.md).

---

## 1. Code-Only Architecture

Crossroads-UK rejects the pattern of distributing heavy, pre-baked, multi-gigabyte database files. Instead, users specify what data they would like to download from government sources; the pipeline ingests and compiles it into a DuckDB database file on their local machine.

Upon execution, the framework builds and compiles the analytical database on the fly using the host machine's hardware resources. This ensures complete parameter flexibility, data freshness, and a variable analytical scope.

- **Dynamic Extraction**: The runtime engine accepts structural execution arguments (e.g., `years: list[int]`, `regions: list[str]`) from the user environment.
- **Decoupled Ingestion**: Isolated data transformers fetch raw public domain source files (CSVs, ZIPs, Shapefiles) directly to a local hardware cache, streaming them directly into an in-process columnar database.

---

## 2. Scientific Rigor & Data Fidelity

While every software project inherently aims for accuracy, Crossroads-UK explicitly acknowledges that a failure in data fidelity is not a minor application bug — a silently dropped or misattributed record biases every downstream query, and that bias is invisible to the researcher. By defining the impact of failure in these terms, the project focuses its engineering effort on absolute data parity and verifiable correctness.

The software enforces a **zero unaccounted loss** policy backed by multi-system cross-verification — records that fail validation are flagged and logged, never silently deleted (the full model is defined in §10, Data Quality & Cleansing):

- **Differential Testing Against Gold Standards (Where Possible)**: For data components where an established academic baseline exists, the engine's ingestion pipelines will be audited against the existing package. Automated validation scripts execute identical queries across both environments to assert absolute parity in row counts, categorical distributions, and filtering outcomes. Where no pre-existing baseline tool exists (such as our custom gridded meteorological and spatial boundary intersections), the engine relies entirely on strict internal constraints, metadata validation, and regression testing.

- **Deterministic Conversions**: Every spatial re-projection and temporal floor must be mathematically deterministic. The library includes precision assertions to guarantee that raw values map identically across coordinate systems (e.g., EPSG:4326 to EPSG:27700) without geometric shifting or decimal drift.

- **Auditability & Row Parity**: The ingestion pipeline reconciles row counts against the raw source on every run, asserting the conservation invariant `source_rows == clean_rows + quarantined_rows`. Expected, rule-based rejections are logged but are not fatal; the engine halts only when rows are *unaccounted* for (an unexplained discrepancy that signals a bug) or when a source's reject rate exceeds its configured ceiling. The full model is defined in §10.

- **Reproducibility**: By relying on local-first, version-controlled ingestion code rather than fluctuating cloud databases, any researcher running a given version of Crossroads-UK with the same parameters will generate a structurally identical database file, satisfying the core scientific requirement of computational reproducibility. This code base is subject to change so strong versioning is essential.

- **Upstream Trust Boundary**: The pipeline assumes that third-party dependencies (specifically DuckDB and its native Spatial Extension) have already been extensively tested. Crossroads-UK does not replicate tests for primitive database operations, indexing mechanics, or core coordinate transformation mathematics (e.g., the underlying PROJ library calculations). For this reason, dependency selection must be made with great care. However, the implementation of these third-party libraries on Crossroads-UK will be tested extensively.

---

## 3. Data Ingestion & Transformation Matrix

Public civic datasets use incompatible spatial, temporal, and geometric conventions. Crossroads-UK resolves these mismatches entirely at the ingestion layer.

### A. Spatial Standardization (In-Database Projection)

To eliminate runtime query lag, all geometries are reprojected once at ingestion and never at query time.

**The Problem:** DfT Stats19 logs accidents using British National Grid Eastings and Northings (meters, EPSG:27700), while meteorological gridded datasets use global coordinates (decimal degrees, EPSG:4326).

**The Engine Solution:** Utilizing compiled SQL expressions via the native DuckDB Spatial Extension, all incoming coordinate arrays are permanently re-projected to EPSG:27700 at ingestion. This bypasses slow Python memory loops and compiles optimized spatial bounding-box R-Trees directly on disk.

### B. Temporal Grain Alignment

**The Problem:** Collision events are recorded at the exact minute of occurrence, whereas environmental weather data (ERA5-Land grids) are logged in discrete hourly increments. Furthermore, future data sources (e.g., high-frequency traffic loops or vehicle telematics) will introduce disparate temporal grains ranging from seconds to months. Permanent truncation at ingestion discards information and invalidates sub-hourly analysis.

**The Engine Solution:** Crossroads-UK maintains the raw, pristine minute-level timestamp for absolute data fidelity, alongside deterministic interval keys (such as hourly or daily) generated dynamically to support clean relational joins.

#### Temporal Zone Standardization

Crossroads-UK is scoped to the UK, so every record lies within a single civil time zone. That scope — not any one use case — is what makes **UK local time** a coherent universal frame for the database: it is meaningful for every source, in a way it would not be for a global dataset. Which temporal columns a source carries then follows from §2 fidelity, not convenience:

- **`*_local` — present for every source.** Stored in UK civil time (IANA zone `Europe/London`: GMT in winter, BST in summer). It is the common surface for cross-source joins, because every source can populate it faithfully: a UTC-native source converts UTC→local (total and deterministic), and a local-native source already is local.
- **`*_utc` — present only where the source natively records a true instant.** It is never reconstructed from local time: a wall-clock reading carries no DST offset (the autumn fall-back hour is ambiguous, the spring-forward hour absent), so inventing a UTC instant for a local-native source is forbidden under §2.

| Source kind | Temporal columns |
|-------------|------------------|
| Local-native (e.g. STATS19 collisions) | `*_local` |
| UTC-native (e.g. ERA5-Land weather) | `*_utc` + `*_local` (derived) |

Rules: every temporal column carries a mandatory zone suffix (`_local` / `_utc`) — a bare `timestamp` is a defect; columns are stored as naive `TIMESTAMP` (not `TIMESTAMP WITH TIME ZONE`, whose rendering depends on the session setting and would make the database environment-dependent). Machine-stamped provenance timestamps (e.g. `ingested_at`) are UTC and excluded from these rules and from the §2 reproducibility guarantee.

### C. Shifting Geopolitical Boundaries

Local authority borders shift across decades due to demographic shifts and decennial ONS census re-alignments. Crossroads-UK manages this boundary drift via two configurable architectural modes:

- **Retrospective Snapshots (Default)**: Evaluates historical event points against a definitive, modern geometric layout (e.g., ONS 2024 Boundaries) to satisfy standard localized policy inquiries.
- **Temporally Sliced Range Joins (Advanced)**: Appends discrete temporal limits (`valid_from` / `valid_to`) to the boundary polygon indexes, ensuring that an event point is relationally mapped exclusively to the municipal layout that physically existed on the day of the incident.

---

## 4. Modular Data Architecture

To prevent the ingestion engine from becoming a brittle collection of ad-hoc scripts, Crossroads-UK enforces a strict Provider-Plugin architecture. The orchestrator (`client.py`) interacts exclusively with an abstract data interface. New data sources (e.g., traffic flow, census demographics) can be introduced by dropping a new module into `transformers/` without modifying core engine logic.

### The Base Transformer Interface

Every data source must inherit from `BaseTransformer` and implement three deterministic phases:

```python
# src/crossroads/transformers/base.py
from abc import ABC, abstractmethod
import duckdb

class BaseTransformer(ABC):
    @property
    @abstractmethod
    def source_id(self) -> str:
        """Unique identifier for the datasource (e.g., 'stats19', 'era5_weather')."""
        pass

    def is_active(self, **kwargs) -> bool:
        """Whether this source should run for a given build(**kwargs) call.

        Defaults to True (always run). Sources gated behind a build flag override
        this, e.g. a weather source returns kwargs.get("include_weather", False).
        """
        return True

    @abstractmethod
    def extract(self, cache_dir: str, **kwargs) -> None:
        """Stream/download raw files directly to local hardware cache."""
        pass

    @abstractmethod
    def transform_and_load(self, con: duckdb.DuckDBPyConnection, cache_dir: str) -> None:
        """Execute zero-loss transformations and load directly into target DuckDB tables."""
        pass
```

### Dynamic Orchestration Ingestion Loop

The orchestration engine uses Python's module inspection to dynamically discover available transformers. When `client.build()` is executed, it runs a closed injection loop:

```python
# Internal orchestrator execution flow
for transformer in self.registry.get_active(**kwargs):
    transformer.extract(self.cache_dir, **kwargs)
    transformer.transform_and_load(self.con, self.cache_dir)
```

---

## 5. Initial Implementation Roadmap

The spatial normalization layer must be fully established before processing environmental datasets. Stats19 records natively utilize the British National Grid (EPSG:27700), whereas meteorological grids (ERA5-Land) are indexed by global decimal degrees (EPSG:4326). 

The spatial transformer implements the foundational coordinate re-projection pipeline and constructs the essential bounding-box spatial indices. Attempting to match environmental grids against raw Stats19 coordinates without this normalized baseline forces an unindexed spatiotemporal cross-join, causing severe memory thrashing and pipeline failure when scaling across decades of data.

| Phase | Core Focus | Explicit Data Inputs & Sources                                                                                                                                                             | Critical Deliverable / Validation Metric |
|-------|------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|------------------------------------------|
| **1** | Spatial Infrastructure & Boundaries | Download and ingest Local Authority Districts (LAD) and Counties/Unitary Authorities (CTYUA) boundaries as **BGC (Generalised Clipped)** Shapefiles, natively projected in **EPSG:27700**. | Compile/load DuckDB Spatial Extension. Initialize the boundary tables (LAD and CTYUA) and build the foundational R-Tree spatial indices. |
| **2** | Accident Ingestion & Normalization | Stream the complete historical and modern tranches of `stats19` CSV datasets (including Collision, Vehicle, and Casualty files).                                                           | Process raw inputs. Cast Eastings/Northings into native EPSG:27700 geometry points on the fly, flag legacy `0`/`-1` coordinate sentinels (set `geom = NULL`, `geom_valid = FALSE`, and write a `data_quality_log` entry — never delete), and execute point-in-polygon spatial joins against the Phase 1 ONS boundary tables. |
| **3** | Interactive Console Architecture | Orchestrates and exposes the query layers built across Phase 1 and Phase 2.                                                                                                                | Deploy an interactive, multi-stage CLI engine. Gathers user parameters (years, regional ONS codes) via wizard prompts to trigger optimized analytical query execution loops. |
| **4** | Meteorological Grid Integration | Ingest ERA5-Land gridded reanalysis data downloaded via the Copernicus cdsapi in NetCDF format (.nc).                                                                                      | Reproject meteorological grid cell centroids into EPSG:27700. Execute a spatiotemporal join to stamp each accident record with localized weather metrics (precipitation, temperature) matching the hourly grain. |

---

## 6. The Interactive Console Engine

Crossroads-UK provides an interactive, terminal-native console application that abstracts the underlying orchestration pipeline. The console is built as an evolutionary interface. Initially, this is just a data-compilation wizard.

---

## 7. Repository Blueprint

```text
crossroads-uk/
│
├── docs/
│   └── plans/                              # Implementation plans — subdirectories named sequentially (examples below)
│       ├── 001_spatial_infrastructure/     # Phase 1: ONS boundaries & DuckDB spatial
│       ├── 002_stats19_ingestion/          # Phase 2: collision/vehicle/casualty pipeline
│       ├── 003_console_architecture/       # Phase 3: CLI wizard
│       └── 004_weather_integration/        # Phase 4: ERA5-Land grid matching
│
├── src/
│   └── crossroads/
│       ├── __init__.py
│       ├── client.py                       # Primary pipeline orchestrator & database controller
│       ├── console.py                      # Wizard state machine
│       ├── registry.py                     # Transformer discovery & injection
│       ├── quality.py                      # data_quality_log & quarantine writer; build-end invariant checks
│       │
│       └── transformers/                   # Dedicated ingestion & cleansing modules
│           ├── base.py                     # BaseTransformer ABC
│           ├── spatial.py                  # ONS LAD/CTYUA boundary ingestion
│           ├── stats19.py                  # DfT collision/vehicle/casualty
│           └── weather.py                  # ERA5-Land NetCDF grid matching
│
├── tests/                                  # Automated Verification Suites
│   ├── conftest.py                         # Shared DuckDB fixture
│   ├── test_spatial.py
│   ├── test_stats19.py
│   └── test_quality.py                     # Conservation invariant assertions
│
├── AI_DISCLOSURE.md
├── CLAUDE.md
├── LICENSE
├── pyproject.toml                          # Package dependencies & metadata
└── README.md
```

---

## 8. Target Deployment Flow

```python
import crossroads as cr

# 1. Initialize the local engine instance
client = cr.init_engine(database_path="local_analytics.db")

# 2. Compile database via modular registry
client.build(
    years=[2022, 2023, 2024],
    include_weather=True,
    spatial_grain="local_authority"
)
```

---

## 9. Data Quality & Cleansing

Source data contains records that are wrong, incomplete, or out of range. Crossroads-UK never deletes such a record: it is retained, marked with the reason it cannot be used, and excluded only from the analyses it would corrupt. This makes the question *"which records were not processed, and why?"* answerable with a single query — a requirement for the project to be academically defensible.

### Three-layer model (keep-in-place)

| Layer | Table(s) | Contains | Rule |
|-------|----------|----------|------|
| **Raw landing (bronze)** | `<source>_raw` (e.g. `stats19_raw`) | A faithful, append-only copy of every downloaded source row — original column names, permissive/source-native types. | Never edited. Guarantees the raw record always exists in the database and the pipeline is fully re-derivable. |
| **Validated facts (silver)** | `collisions`, `weather`, … | **Every** bronze row, 1:1 — nothing removed. Each cleansed field appears twice: the preserved raw value, and a typed/clean column that is `NULL` when the source value fails its validation rule, plus per-dimension boolean flags (e.g. `geom_valid`). Spatial joins run only where the relevant flag is `TRUE`. | Keep-in-place: a record with bad coordinates still exists here for temporal and severity analysis; only its spatial fields are `NULL`/`FALSE`. |
| **Clean views (gold)** | `collisions_spatial`, … (DuckDB `VIEW`s) | Filtered projections, e.g. `SELECT * FROM collisions WHERE geom_valid`. | What researchers query by default, so valid-only analysis never depends on remembering a filter. |

### The exclusion ledger

A single `data_quality_log` table is the queryable, reproducible answer to *"what was not processed, and why?"* — the computational analog of a PRISMA exclusion flow. One row per rule violation:

```text
data_quality_log(
  source_id      VARCHAR,   -- e.g. 'stats19'
  source_row_key VARCHAR,   -- natural/composite key of the offending row
  column_name    VARCHAR,   -- field that failed (NULL for whole-row issues)
  rule_id        VARCHAR,   -- stable id, e.g. 'stats19.coord.sentinel'
  rule_desc      VARCHAR,   -- human-readable reason
  severity       VARCHAR,   -- 'reject_dimension' | 'warn'
  raw_value      VARCHAR,   -- the value that failed
  ingested_at    TIMESTAMP
)
```

Rows that cannot be structured at all (e.g. a malformed CSV line that never reaches bronze) are written to a separate `quarantine_raw(source_id, raw_text, reason, ingested_at)`. This must be rare.

### Invariants (asserted on every build)

All three run as aggregate SQL count checks (single `O(rows)` scans in DuckDB), not per-row Python loops, so the audit never becomes the bottleneck.

1. **Conservation (hard, fatal):** `source_rows == clean_rows + quarantined_rows`, where `clean_rows` are the silver rows retained (keep-in-place means `count(<source>_raw) == count(silver)`) and `quarantined_rows` are `quarantine_raw` rows. A mismatch means rows vanished unaccounted — a bug — and halts the build.
2. **Flag/ledger agreement (hard, fatal):** every silver row with a `NULL`ed clean column has at least one matching `data_quality_log` entry, and vice versa.
3. **Reject-rate tripwire (configurable, fatal above ceiling):** for each source and dimension, `rejected / total` must be ≤ a configured ceiling (**default: 5%**). Exceeding it fails the build as a regression tripwire that catches silent upstream format changes. Below it, rejections are logged, never fatal.

### Halt semantics

The build halts **only** on (a) a conservation-invariant failure, (b) a flag/ledger disagreement, or (c) a reject-rate ceiling breach. Expected, rule-based, logged rejections are not fatal. This is the operational definition of the **zero unaccounted loss** policy stated in §2, and it supersedes any notion of halting on individual cleansed records.

### Worked example — Stats19 coordinate sentinels

> A Stats19 collision with Easting/Northing of `0` or `-1` (DfT's "data missing or out of range") is retained in full: `easting`/`northing` raw values are preserved, the derived `geom` is `NULL`, `geom_valid = FALSE`, and a `data_quality_log` row is written with `rule_id = 'stats19.coord.sentinel'`. The record remains available for temporal and severity analysis and is simply excluded from the `collisions_spatial` view. Nothing is deleted; the exclusion is counted and explained.

### Best-practice grounding

This model follows established conventions rather than inventing one: **raw-data immutability** (reproducible-research practice — clean via code, never edit source); the **bronze/silver/gold** layering; and **edit-and-flag** official-statistics doctrine, in which failing records are flagged with a reason rather than silently dropped. The domain precedent is decisive — DfT Stats19 itself encodes `-1` as the explicit category *"data missing or out of range,"* so flagging (not deletion) is what preserves parity with the source. Documented, reproducible exclusions are the same discipline as a PRISMA "n excluded, and why" flow.

---

## 10. AI & Engineering Governance
Crossroads-UK embraces AI as a core efficiency asset of modern systems engineering. The full development loop is explicitly detailed in [AI_DISCLOSURE.md](../AI_DISCLOSURE.md).
