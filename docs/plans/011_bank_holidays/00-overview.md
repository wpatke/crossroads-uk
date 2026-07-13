# GOV.UK Bank Holidays — Plan Overview
> Multi-stage plan. Each session gets THIS overview + ONE stage file. Read this first.
> Each stage's End State is the contract — adapt steps to reality and note deviations.

Add a new `bank_holidays` data source that (Stage 01) loads the live GOV.UK bank-holidays
feed into a standalone dimension table keyed on `(date, division)`, and (Stage 02) stamps a
tri-state `is_bank_holiday` flag (TRUE / FALSE / NULL-for-unknown) onto each collision, using
the collision's LAD code to pick the correct UK holiday division.

---

## Context & Objective

**What exists.** Crossroads-UK is a DuckDB ingestion pipeline with a Provider-Plugin
architecture (spec §4). Every source is a `BaseTransformer` subclass in
`src/crossroads/transformers/`, discovered automatically by
[`src/crossroads/registry.py`](../../../src/crossroads/registry.py) — no core edit is needed
to add a source. Existing sources: `spatial.py` (ONS LAD/CTYUA boundaries, always-on
infrastructure), `stats19.py` (collisions/vehicles/casualties), `weather.py` (ERA5-Land).

STATS19 already **stamps** enrichment columns onto its `collisions` silver table AFTER building
it: `_spatial_stamp` fills `lad_code`/`ctyua_code` via point-in-polygon; `_weather_stamp` fills
`temperature_c`/`precipitation_mm` from the weather grid; `_solar_stamp` fills solar angles. Each
stamp is an `UPDATE ... FROM (subquery)` guarded so a missing dependency table just leaves the
columns NULL and warns (see [`stats19.py`](../../../src/crossroads/transformers/stats19.py)
lines ~693–760). This is the exact pattern the bank-holiday stamp follows.

The quality engine ([`src/crossroads/quality.py`](../../../src/crossroads/quality.py)) requires
every active source to declare a `quality_spec()` returning either `SourceQuality(...)` (audited)
or `QualityExemption(reason=...)` (deliberately not audited, recorded in `quality_exemptions`).

**What changes.**
- New file `src/crossroads/transformers/bank_holidays.py` — one `BaseTransformer` that downloads
  `https://www.gov.uk/bank-holidays.json`, flattens it to a `bank_holidays` dimension table, and
  returns a `QualityExemption` (it is a reference/lookup dimension from a live, non-reproducible
  feed; the keep-in-place conservation model does not apply — see "Quality treatment" below).
- Edit `src/crossroads/transformers/stats19.py` — add an `is_bank_holiday BOOLEAN` column to the
  collision silver, add `"bank_holidays"` to `depends_on`, add a `_bank_holiday_stamp(con)`
  method, and call it after `_spatial_stamp` (it needs `lad_code`).
- New committed test fixture + tests; schema and data-source docs updated.

**Goal.** A researcher who selects `bank_holidays` gets (a) a standalone `bank_holidays` table to
join freely, and (b) when STATS19 is built alongside, a correct per-nation `is_bank_holiday` flag
on every collision — TRUE on a bank holiday for that collision's nation, FALSE on a known
non-holiday, and **NULL wherever the answer is genuinely unknown** (feed doesn't cover the date,
or the nation can't be determined).

---

## Approach / Architecture (shared by all stages)

### The GOV.UK feed
`GET https://www.gov.uk/bank-holidays.json` returns one JSON object with exactly three top-level
keys — the UK's three bank-holiday **divisions** — each holding an events array:

```json
{
  "england-and-wales": { "division": "england-and-wales",
    "events": [ {"title": "New Year’s Day", "date": "2023-01-02", "notes": "Substitute day", "bunting": true}, ... ] },
  "scotland":          { "division": "scotland",          "events": [ ... ] },
  "northern-ireland":  { "division": "northern-ireland",  "events": [ ... ] }
}
```
`date` is ISO `YYYY-MM-DD`. The three divisions genuinely differ (e.g. Scotland's 2 January and
St Andrew's Day; Northern Ireland's Battle of the Boyne), which is why a single flag would be wrong
for a UK-wide dataset — the flag must be resolved **per division**.

### Two halves, one source
1. **Dimension table (Stage 01).** Flatten the three arrays into one silver table
   `bank_holidays(source_row_key, date DATE, division, title, notes, bunting)` — one row per
   `(date, division)` event. Standalone and useful on its own.
2. **Collision stamp (Stage 02).** STATS19 consumes the `bank_holidays` table (the same way it
   consumes `weather`) to fill `collisions.is_bank_holiday`. The division for a collision is
   derived from its **ONS LAD code prefix** (a stable GSS convention):
   - `E…` or `W…` → `england-and-wales` (Wales observes the England-and-Wales calendar)
   - `S…` → `scotland`
   - `N…` → `northern-ireland`
   - anything else / NULL `lad_code` → division unknown → `is_bank_holiday = NULL`

   No extra nation-lookup table is introduced — the code prefix already carries the nation.
   (STATS19 is GB-only, so `N…` never appears in practice, but the mapping stays correct.)

### The tri-state rule (locked requirement)
`is_bank_holiday` must distinguish "known not a holiday" from "no data":
- **NULL** if the division can't be determined, the date didn't parse, or the collision's date
  falls **outside the feed's coverage** for that division.
- **TRUE** if the date is a bank holiday in that division.
- **FALSE** only if the date is within coverage for that division and is *not* a holiday.

**Coverage** is per division, defined by the years present in the feed: a date is "covered" iff its
year is within `[min(year), max(year)]` of that division's events (the feed publishes contiguous
whole years). A date outside that range → NULL, never FALSE. The GOV.UK feed spans only a rolling
recent window (~2018 onward, ~1 year ahead), while STATS19 goes back decades, so most historical
collisions will correctly resolve to NULL. This coverage gap is expected and documented, not a bug.

### Quality treatment — `QualityExemption`
`bank_holidays` is a reference/lookup dimension downloaded from a **live** feed, so it does not fit
the spec §2 reproducibility guarantee or the keep-in-place silver-fact model, and it has no reject
dimensions (every published event is a valid date). `quality_spec()` therefore returns
`QualityExemption(reason=...)`, which `resolve_quality_specs` records in the `quality_exemptions`
table (auditable, not silent). The exemption path is fully supported today
([`quality.py`](../../../src/crossroads/quality.py) `resolve_quality_specs`), so the source needs no
conservation/reject-rate wiring and must **not** call `record_source_rows`.

The `is_bank_holiday` stamp on `collisions` adds **no new audit dimension** — exactly like
`_weather_stamp`/`_solar_stamp`, whose NULLs are legitimate "no data", not rejections. NULL here is
a first-class value, so it is never logged to `data_quality_log`.

### Alternatives rejected
- **Single `is_bank_holiday` flag from england-and-wales only** — factually wrong for Scottish/NI
  collisions; rejected per the divisions decision.
- **A dedicated nation-lookup reference table** — needless data; the LAD code prefix already
  encodes the nation deterministically.
- **`SourceQuality` with zero reject dimensions** (audited, conservation-checked) — plausible, but
  there is no existing zero-dimension `SourceQuality` precedent to lean on and a live feed is
  non-reproducible; `QualityExemption` is the explicitly-supported, lower-risk mechanism for a
  reference/lookup table.
- **Committing a pinned JSON snapshot as the sole source** — rejected per the sourcing decision:
  real builds fetch live (spec §1 freshness); tests use a committed fixture.

### Data flow
```
extract():  GET gov.uk/bank-holidays.json ──▶ <cache>/bank-holidays.json   (skip if present; tests pre-seed)
transform:  json ──▶ bank_holidays_raw (bronze, VARCHAR dates) ──▶ bank_holidays (silver, typed) ──▶ bank_holidays_clean (gold view)
stamp:      collisions.lad_code ─(prefix)▶ division ; collisions.datetime_local ─(date)▶ lookup in bank_holidays ▶ is_bank_holiday (TRUE/FALSE/NULL)
```

---

## Cross-Cutting Constraints (every stage follows these)

- **Stdlib-only download.** Use `urllib.request` (like `stats19.py`); do **not** add a dependency
  or require pandas/xarray. Parse JSON with the stdlib `json` module.
- **Offline tests.** `extract()` skips the download when the cache file already exists, so tests
  seed `<cache>/bank-holidays.json` from a committed fixture and never touch the network.
- **Idempotent build.** Recreate this source's tables with `CREATE OR REPLACE TABLE` (never bare
  `CREATE TABLE` or `IF NOT EXISTS` + `INSERT`), per the `BaseTransformer` contract.
- **Determinism at query time.** Never compute holidays or coverage relative to "today"; drive
  everything from the feed's own dates.
- **No git commits/staging** without explicit user permission (CLAUDE.md).
- **Plan storage.** This plan lives in `docs/plans/011_bank_holidays/` (next sequential dir).
- **Keep it simple, comment in plain language** (CLAUDE.md) — match the density of the file you edit.
- **Row values are always bound with `?`**, never string-interpolated (quality.py convention);
  identifiers (table/column names) may be interpolated from code constants only.

---

## Stage Map (do in order)

| NN | Title | Summary | Deliverable / End State | Depends on | File |
|----|-------|---------|-------------------------|-----------|------|
| 01 | Bank-holidays source & dimension table | New `bank_holidays.py` transformer: download the feed, load bronze→silver→gold `bank_holidays` table keyed `(date, division)`; `QualityExemption`. Committed JSON fixture + offline test. | A build selecting `bank_holidays` produces a populated `bank_holidays` table with all three divisions and typed dates; all §9 invariants pass; standalone integration test green. | — | `01-bank-holidays-source.md` |
| 02 | Collision `is_bank_holiday` stamp | Add `is_bank_holiday BOOLEAN` to collision silver; add `"bank_holidays"` to STATS19 `depends_on`; add guarded `_bank_holiday_stamp`; map LAD-prefix→division; tri-state (TRUE/FALSE/NULL). | Building STATS19 + `bank_holidays` stamps a correct per-nation tri-state flag; STATS19-only build leaves it all NULL; unit test proves TRUE/FALSE/NULL + division routing. | 01 | `02-collision-stamp.md` |

Stage 01 delivers value alone (the standalone dimension table). Stage 02 is purely additive and
leaves Stage 01 untouched.

---

## Global Testing & Ship

- **Stage 01 (attaches to Stage 01):** offline integration test `tests/test_bank_holidays.py` —
  build with `datasets=["bank_holidays"]` against the seeded fixture; assert the `bank_holidays`
  table exists, has rows for all three divisions, dates are typed `DATE`, a known holiday row is
  present, and the build's §9 invariants pass (the exemption is recorded in `quality_exemptions`).
- **Stage 02 (attaches to Stage 02):**
  - **Unit** (deterministic, no calendar dependence): drive `_bank_holiday_stamp` on a synthetic
    `collisions` + synthetic `bank_holidays` and assert every tri-state case: TRUE (holiday in
    nation), FALSE (in-coverage non-holiday), FALSE for the *same date* in a different nation where
    it isn't a holiday (division routing), NULL out-of-coverage, NULL for unknown/NULL `lad_code`,
    and Wales→england-and-wales routing.
  - **Combined build (offline):** build STATS19 + `bank_holidays` together (registry restricted,
    caches seeded) and assert the `is_bank_holiday` column exists, `count(*)` is unchanged by the
    stamp, and at least one collision resolves to a determinable (non-NULL) value.
  - **Guard:** a STATS19-only build (no `bank_holidays` table) leaves `is_bank_holiday` all NULL and
    warns — proving the missing-dependency guard.
- **Full suite:** `pip install -e '.[dev]'` then `pytest -q` stays green after each stage.

---

## Open Questions / Risks
- **Coverage granularity.** Coverage is defined by whole calendar years present per division
  (contiguous by construction of the feed). If GOV.UK ever published a non-contiguous run, a gap
  year inside `[min,max]` would resolve to FALSE rather than NULL — currently impossible with the
  real feed; not worth guarding until observed.
- **LAD prefix stability.** The E/W/S/N GSS prefix is a long-standing ONS convention; cross-country
  `K…` codes are never assigned to a single LAD a collision maps to, so they fall to NULL (safe).
- **Live-feed drift (accepted).** Real builds are non-reproducible for this one source by design
  (documented via the `QualityExemption` reason and `data-sources.md`); tests pin a committed fixture.
