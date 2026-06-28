# Stage 03 — Temporal Boundary Slicing
> Part of Spatial Infrastructure & Boundaries. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

> **Superseded detail (shapefile → GeoJSON):** This document originally described committing ONS boundary
> samples as shapefiles (`.shp/.shx/.dbf/.prj`) and reading them via `ST_Read`. The implemented design
> instead downloads and commits **GeoJSON** (ONS publishes ArcGIS FeatureServer GeoJSON, not shapefile
> ZIPs); the shapefile fixtures were removed as unused. Treat shapefile-specific steps as historical.

> **Superseded detail (registry → JSON manifest, plan 004):** The vintage registry no longer lives as
> hard-coded `Vintage(...)` tuples in `spatial.py`. Plan `004_boundary_vintages` moved it into a committed
> JSON manifest, `src/crossroads/transformers/ons_boundaries.json`, loaded by `_load_vintages(source_id)`.
> That manifest is **already fully populated** with every published edition (15 LAD + 11 CTYUA), vintage
> **labels are now `"YYYY-MM"`** (e.g. `"2024-12"`, `"2025-12"`, not `"2024"`), and `valid_to` is
> **derived automatically** by chaining each edition to the next. As a result:
> - **Part A** (add a second committed vintage) is effectively already satisfied — committed GeoJSON
>   fixtures exist for two editions per type and share area codes (see Part A below).
> - **Part B** (extend the registry / chain validity windows) is **already done** by plan 004 — no code
>   change is needed; this stage only verifies it.
> - **Part C** (the `boundary_mode` switch in `_vintages_for`) is the **only real remaining work**, and it
>   is now **year-scoped**: temporal loads the editions overlapping `build(years=...)` (years before a
>   source's earliest edition fall back to it with a warning). Unscoped temporal still spans the full
>   registry, which is why the end-to-end test uses a vintage subset — see below.

## Prerequisites / Starting State
Plan 004 is done: the registry is manifest-driven and fully populated. Verify:
```bash
source .venv/bin/activate
python -m pytest -q          # green
```
Confirm the current state by reading `src/crossroads/transformers/spatial.py`:
- `_load_vintages(source_id)` reads `ons_boundaries.json`, sorts vintages ascending by `valid_from`
  (so `self.vintages[-1]` is the newest edition), derives `valid_to` by chaining, and builds the
  GeoJSON query URL.
- `LADBoundaryTransformer.vintages` has **15** entries (`"2016-12"` … `"2025-12"`);
  `CTYUABoundaryTransformer.vintages` has **11** (`"2018-12"` … `"2025-12"`). The newest of each is
  `"2025-12"` with `valid_to is None`.
- `_BoundaryTransformer._vintages_for(**kwargs)` **still returns `(self.vintages[-1],)`** (snapshot only).
  This is the stub Part C replaces.
- `extract()` already iterates whatever `_vintages_for(**kwargs)` returns, downloads/seeds each
  `source_file`, and stores the resolved set on `self._vintages_to_load`; `transform_and_load()` already
  builds bronze/silver across that set and stamps `valid_from`/`valid_to` from each `Vintage`. So once
  `_vintages_for` returns more than one vintage, the rest of the pipeline already handles it.

Committed GeoJSON fixtures (from Stage 02 / plan 004):
- `tests/fixtures/ons/lad_2024/lad_sample.geojson` (cols `LAD24CD`/`LAD24NM`, 3 features)
- `tests/fixtures/ons/lad_2025/lad_sample.geojson` (cols `LAD25CD`/`LAD25NM`, 3 features, **same area
  codes** `E06000001..3` as 2024)
- `tests/fixtures/ons/ctyua_2024/…` and `ctyua_2025/…` (the CTYUA equivalents, 2 features each)

## Objective
Implement spec §3C boundary drift: a `build(boundary_mode=...)` kwarg selects `"snapshot"` (default,
latest vintage only) or `"temporal"` (the editions whose
validity windows overlap the requested build `years`, or every edition when no `years` are given — each
stamped with its `[valid_from, valid_to)` window). Prove temporal mode with real multi-vintage data and confirm
conservation/agreement/reject-rate hold across vintages (the composite `source_row_key = "<code>|<label>"`
keeps the same area code unique per vintage).

The actual range join of collision points against validity windows is **Step 4** — this stage only builds
the temporally-aware boundary base layer.

## Implementation Steps

### A. Vintage fixtures (already present — just confirm)

No new fixture is required to prove the temporal mechanics. The committed `lad_2024` and `lad_2025`
GeoJSON fixtures already hold the **same three area codes** (`E06000001`, `E06000002`, `E06000003`) under
two different editions, which is exactly what a temporal test needs: the same `area_code` appearing across
two vintages so the composite key (`area_code || '|' || label`) is exercised.

> The `lad_2025` fixture is the `lad_2024` geometry with its columns renamed `LAD24*`→`LAD25*` (a faithful
> structural stand-in, not a genuinely re-drawn boundary). That is sufficient to prove the temporal
> *machinery*. If you later want a more *realistic* multi-vintage test (genuine boundary change between
> editions), add a real earlier edition fixture using the same recipe as the runbook
> `docs/maintenance/updating-ons-boundaries.md` and seed it the same way as below — but this is optional
> and not required for this stage.

### B. Vintage registry (already done by plan 004 — just verify)

The registry already lists every edition with chained validity windows; there is **nothing to edit**.
Confirm the timeline is contiguous (this is what Part B used to do by hand):
```bash
source .venv/bin/activate
python - <<'PY'
from crossroads.transformers.spatial import LADBoundaryTransformer
v = LADBoundaryTransformer().vintages
print("count:", len(v), "newest:", v[-1].label, "newest valid_to:", v[-1].valid_to)
for a, b in zip(v, v[1:]):
    assert a.valid_to == b.valid_from, (a.label, a.valid_to, b.valid_from)
assert v[-1].valid_to is None
print("validity windows chain contiguously")
PY
```
Expected: `count: 15 newest: 2025-12 newest valid_to: None` and "validity windows chain contiguously".

### C. Make `_vintages_for` mode-aware + year-scoped (the actual work)

Add `import warnings` to the top of `spatial.py`, then replace the snapshot-only stub in
`_BoundaryTransformer`:
```python
def _vintages_for(self, **kwargs):
    """Which vintages this build loads, per spec §3C boundary drift.

      boundary_mode='snapshot' (default) -> latest vintage only.
      boundary_mode='temporal'           -> editions whose validity window overlaps
                                            the requested build years (kwargs['years']);
                                            if no years are given, every edition.

    Years before this source's earliest edition have no ONS boundary coverage: the
    earliest edition is used as a stand-in and a warning flags this to the researcher.
    Unknown modes fall back to snapshot (non-spatial builds pass arbitrary kwargs
    through, so an unrelated value must never break a build)."""
    if kwargs.get("boundary_mode", "snapshot") != "temporal":
        return (self.vintages[-1],)                 # snapshot / default / unknown

    years = kwargs.get("years")
    if not years:
        return tuple(self.vintages)                 # temporal, unscoped -> all editions

    # Half-open windows [valid_from, valid_to). Select any edition overlapping the
    # requested span [Jan 1 of the earliest year, Dec 31 of the latest year]. Dates
    # are 'YYYY-MM-DD' strings, which compare correctly lexicographically.
    lo = f"{min(years)}-01-01"
    hi = f"{max(years)}-12-31"
    selected = [v for v in self.vintages
                if v.valid_from <= hi and (v.valid_to is None or v.valid_to > lo)]

    earliest = self.vintages[0]                     # sorted oldest-first
    if lo < earliest.valid_from:
        # Requested years reach before this source's ONS boundary coverage.
        warnings.warn(
            f"{self.source_id}: requested years start {lo[:4]} but the earliest ONS "
            f"boundary edition is {earliest.label} (from {earliest.valid_from}); using "
            f"{earliest.label} as a stand-in for the earlier years.",
            stacklevel=2,
        )
        if earliest not in selected:
            selected.append(earliest)

    selected.sort(key=lambda v: v.valid_from)       # keep newest selected edition last
    return tuple(selected)
```
No other transformer code changes. Confirm the kwargs reach here: `client.build(**kwargs)` forwards
`**kwargs` to each transformer's `extract(cache_dir, **kwargs)`, which calls `_vintages_for(**kwargs)` —
so both `boundary_mode` and `years` propagate without touching `client.py`.

**Pre-coverage fallback is per source, not a fixed 2016.** The stand-in is each source's *own* earliest
edition (`self.vintages[0]`): LAD coverage starts at `2016-12`, but **CTYUA starts at `2018-12`** (ONS
published no UK-wide CTYUA before then). So a build for 2017 collisions uses LAD `2016-12` and CTYUA
`2018-12`, each warning that the request predates real coverage. The warning is a build-time
`warnings.warn` to whoever runs the build (the researcher); it does not alter the output tables, so
determinism holds. (If a durable, queryable record is wanted later, it can be added to an audit table —
out of scope here.)

> **Production cost & year scoping.** Temporal mode fetches only the editions overlapping
> `build(years=...)` — a handful of years pulls a handful of editions, not the whole back-catalogue. Only
> an **unscoped** temporal build (`boundary_mode="temporal"` with no `years`) loads every edition (up to 26
> GeoJSON files, cached after the first run). Snapshot stays the default, fetching exactly one edition per
> type.

> **Cache seeding for temporal tests.** `extract()` skips the download when a vintage's `source_file` is
> already in the cache. A temporal build over the full 15-vintage registry would therefore try to download
> the 13 editions that have no committed fixture. To keep tests **offline**, the temporal test restricts
> the transformer to the two vintages that *do* have fixtures (`"2024-12"`, `"2025-12"`) and seeds both
> (see the test helper below). Do **not** attempt to commit fixtures for all 15 editions.

## Testing & Verification

Add to `tests/test_spatial.py` (it already imports `os`, `shutil`, `pytest`, `crossroads`, `FIXTURES`,
and the two transformer classes).

```python
def _two_vintage_lad():
    """A LAD transformer restricted to the two editions that have committed
    fixtures, so temporal mode can be tested fully offline. Setting .vintages on
    the instance shadows the manifest-loaded class attribute; _vintages_for reads
    self.vintages, so temporal mode then resolves exactly these two."""
    t = LADBoundaryTransformer()
    t.vintages = tuple(v for v in t.vintages if v.label in ("2024-12", "2025-12"))
    return t


def _seed_cache_temporal_lad(cache_dir, vintages):
    """Seed each given vintage's source_file from its matching committed fixture."""
    os.makedirs(cache_dir, exist_ok=True)
    fixture_for = {
        "2024-12": ("lad_2024", "lad_sample"),
        "2025-12": ("lad_2025", "lad_sample"),
    }
    for v in vintages:
        sub, stem = fixture_for[v.label]
        src = os.path.join(FIXTURES, sub, stem + ".geojson")
        shutil.copy(src, os.path.join(cache_dir, v.source_file))


def test_snapshot_mode_loads_latest_vintage_only(tmp_path):
    # Default mode: only the newest LAD vintage (2025-12) is loaded.
    client = _boundary_client(tmp_path)          # snapshot seed (newest only)
    client.build()                               # no boundary_mode -> snapshot
    vintages = [r[0] for r in client.con.execute(
        "SELECT DISTINCT vintage FROM lad_boundaries ORDER BY vintage"
    ).fetchall()]
    assert vintages == ["2025-12"]
    # The latest vintage is current (valid_to IS NULL).
    assert client.con.execute(
        "SELECT count(*) FROM lad_boundaries WHERE valid_to IS NOT NULL"
    ).fetchone()[0] == 0
    client.close()


def test_temporal_mode_loads_all_vintages_with_windows(tmp_path):
    t = _two_vintage_lad()
    cache = str(tmp_path / "cache")
    _seed_cache_temporal_lad(cache, t.vintages)
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [t]
    client.build(boundary_mode="temporal")

    vintages = [r[0] for r in client.con.execute(
        "SELECT DISTINCT vintage FROM lad_boundaries ORDER BY vintage"
    ).fetchall()]
    assert vintages == ["2024-12", "2025-12"]

    # The newest vintage is current (open window); the earlier one is closed.
    assert client.con.execute(
        "SELECT valid_to FROM lad_boundaries WHERE vintage = '2025-12' LIMIT 1"
    ).fetchone()[0] is None
    assert client.con.execute(
        "SELECT valid_to FROM lad_boundaries WHERE vintage = '2024-12' LIMIT 1"
    ).fetchone()[0] is not None

    # Composite key keeps the same area code unique across vintages.
    dupe_keys = client.con.execute(
        "SELECT count(*) - count(DISTINCT source_row_key) FROM lad_boundaries"
    ).fetchone()[0]
    assert dupe_keys == 0
    # The same area_code appears under both vintages (fixtures share codes).
    shared = client.con.execute(
        "SELECT count(*) FROM ("
        "  SELECT area_code FROM lad_boundaries GROUP BY area_code HAVING count(*) = 2"
        ")"
    ).fetchone()[0]
    assert shared == 3
    client.close()


def test_temporal_mode_passes_invariants(tmp_path):
    # Conservation/agreement/reject-rate must hold across multiple vintages.
    t = _two_vintage_lad()
    cache = str(tmp_path / "cache")
    _seed_cache_temporal_lad(cache, t.vintages)
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [t]
    client.build(boundary_mode="temporal")   # raises if any invariant fails
    # bronze == silver (keep-in-place) across both vintages.
    b = client.con.execute("SELECT count(*) FROM ons_lad_raw").fetchone()[0]
    s = client.con.execute("SELECT count(*) FROM lad_boundaries").fetchone()[0]
    assert b == s and s == 6                  # 3 (2024-12) + 3 (2025-12)
    client.close()


# --- year-scoped selection (pure logic over the real registry; no build/network) ---

def test_temporal_year_scoping_selects_overlapping_editions():
    # Request 2020-2021: only the editions whose windows overlap that span load.
    picked = {v.label for v in LADBoundaryTransformer()._vintages_for(
        boundary_mode="temporal", years=[2020, 2021])}
    assert picked == {"2019-12", "2020-12", "2021-05", "2021-12"}


def test_temporal_years_before_coverage_use_earliest_and_warn():
    # 2014-2015 precede the earliest LAD edition (2016-12): stand in + warn.
    t = LADBoundaryTransformer()
    with pytest.warns(UserWarning, match="earliest ONS boundary edition"):
        picked = [v.label for v in t._vintages_for(
            boundary_mode="temporal", years=[2014, 2015])]
    assert picked == ["2016-12"]


def test_temporal_unscoped_loads_every_edition():
    t = LADBoundaryTransformer()
    assert len(t._vintages_for(boundary_mode="temporal")) == len(t.vintages) == 15


def test_snapshot_ignores_years():
    # Default (snapshot) mode loads the latest edition regardless of years.
    picked = [v.label for v in LADBoundaryTransformer()._vintages_for(years=[2014])]
    assert picked == ["2025-12"]
```

Run:
```bash
source .venv/bin/activate
python -m pytest -q
```
Expected: all green, snapshot (default) and temporal both proven.

**Stage ship-readiness checklist:**
- [ ] Part B confirmed: registry has 15 LAD / 11 CTYUA editions with contiguous chained windows (Step B
      verification script passes). No registry edit was needed.
- [ ] `_vintages_for` honours `boundary_mode`; temporal scopes to editions overlapping `build(years=...)`,
      or all editions when no `years` are given.
- [ ] Year-scoped: `build(years=[…])` loads only overlapping editions; a request reaching before a
      source's earliest edition uses that earliest edition as a stand-in and emits a warning.
- [ ] `build(boundary_mode="temporal", years=...)` reaches `_vintages_for` via the existing kwargs
      forwarding (no `client.py` change).
- [ ] Snapshot test: latest vintage only (`"2025-12"`), `valid_to IS NULL`.
- [ ] Temporal test: both fixtured vintages loaded, windows correct, composite keys unique, 3 shared codes.
- [ ] Temporal build passes all Step 2 invariants (bronze == silver across vintages).
- [ ] No new dependency; `client.py`/`registry.py`/`quality.py` untouched in this stage.

## End State / Handoff
Boundaries can be loaded as a single modern snapshot (default) or as multiple temporally-windowed
vintages (`boundary_mode="temporal"`, scoped to the editions overlapping `build(years=...)` — or all
editions if no years are given, with years before a source's earliest edition falling back to that
earliest edition plus a warning), each silver row carrying `valid_from`/`valid_to` and a
vintage-unique `source_row_key = "<code>|<label>"` (label format `"YYYY-MM"`). Conservation and agreement
hold in both modes. Stage 04 may assume this schema and add R-Tree indices over the (possibly
multi-vintage) `geom` column. Step 4 will consume `valid_from`/`valid_to` for temporally-sliced
point-in-polygon range joins.

## Failure Modes & Rollback
- **Temporal test tries to hit the network:** `_vintages_for("temporal")` returned the full 15-vintage
  registry instead of the restricted set. Ensure the test uses `_two_vintage_lad()` (which shadows
  `.vintages` with the two fixtured editions) and seeds both `source_file`s.
- **Snapshot test sees more than one vintage:** `_vintages_for` isn't honouring the default — confirm the
  kwarg name is `boundary_mode` and the default branch returns only `self.vintages[-1]`.
- **`shared == 3` fails:** the `lad_2024`/`lad_2025` fixtures no longer share all three area codes. Re-check
  the fixtures (the 2025 fixture should be the 2024 geometry with columns renamed `LAD24*`→`LAD25*`), or
  relax to `shared >= 1` and document why.
- **Label assertion mismatch (`"2024"` vs `"2024-12"`):** these tests use the plan-004 `"YYYY-MM"` labels.
  If you see `"2024"`, the manifest or loader was reverted — re-check `ons_boundaries.json` and
  `_load_vintages`.
- **`valid_to` typing:** the `CASE … THEN DATE 'YYYY-MM-DD' … ELSE NULL` yields a DATE column (mixing DATE
  and untyped NULL is fine in DuckDB; if it complains, `CAST(NULL AS DATE)`).
- **Pre-coverage warning misfires:** it should fire only when `min(years)` precedes the source's earliest
  `valid_from`. If it never fires, confirm `years` is reaching `_vintages_for` (it rides `build(**kwargs)`);
  if it always fires, check the string-date comparison (`'YYYY-01-01' < earliest.valid_from`).
- **`years` mixed types:** `min`/`max` need a homogeneous list. The build passes `years=[ints]`; a list of
  strings also works, but do not mix ints and strings.
- **Rollback:** revert `_vintages_for` to the snapshot-only stub and delete the temporal tests/helpers.
  Snapshot behaviour (the manifest-driven latest-edition load) remains, and the registry is untouched.
</content>
