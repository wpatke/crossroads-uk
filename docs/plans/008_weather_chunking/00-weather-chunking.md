# ERA5-Land Download Chunking — Per-Month Requests + Yearly Merge
> Engineer: execute step by step, exactly as written.

Split the ERA5-Land weather download from one oversized whole-year request into twelve per-month requests that stay under the Copernicus CDS per-request cost limit, then merge them back into the single yearly cache file the rest of the pipeline already expects — so a full year of weather can actually be downloaded and built.

---

## Context & Objective

**What exists.** The weather source is `Era5WeatherTransformer` in
[src/crossroads/transformers/weather.py](../../../src/crossroads/transformers/weather.py).
Its `extract()` loops the requested years and, for each year with no cached file, calls
`_download(year, dest)`. `_download` issues **one** `cdsapi` `retrieve()` for the whole
year — all 12 months × days 1–31 × 24 hours × 2 variables (`2m_temperature`,
`total_precipitation`) over the UK bounding box (`UK_AREA`), writing one NetCDF file
`era5_land_{year}.nc`. `transform_and_load()` then reads a **list** of yearly `.nc` files
(`_load_bronze(con, nc_paths)` already loops over a list and concatenates), derives silver,
and runs the build invariants.

**The problem.** The new CDS backend (`ecmwf-datastores-client`) rejects a whole-year
request with HTTP 403 and the body `cost limits exceeded / Your request is too large,
please reduce your selection.` The download never completes, so weather cannot be built for
any year. Observed traceback ends in
`RuntimeError: Copernicus rejected the ERA5-Land download ... licence has not been accepted`
— which is a **misdiagnosis** (see Bug 2 below), because the real cause is request size.

**Two defects to fix.**

1. **Blocker — request too large.** `_download` must split the yearly request into
   per-month requests (each ~1/12 the cost), each well under the CDS limit, then merge the
   twelve monthly files into the existing `era5_land_{year}.nc` so nothing downstream
   changes.
2. **Misclassification.** `_looks_like_licence_error()`
   ([weather.py:79](../../../src/crossroads/transformers/weather.py:79)) matches on the bare
   substrings `"403"` and `"forbidden"`. A "request too large" rejection is **also** a 403,
   so it is wrongly relabelled as a licence problem by `_licence_message()`. The heuristic
   must match on licence wording only, and must not swallow cost/size errors.

**The goal.** After this change, `client.build(datasets=["era5_weather"], years=[2022])`
downloads 2022 weather as twelve monthly requests, merges them into `era5_land_2022.nc`, and
completes the build. A killed build resumes without re-fetching completed months. The
loader, the committed test fixture, and the offline integration test are unchanged.

---

## Acceptance Criteria

1. `Era5WeatherTransformer._download(year, dest)` issues **exactly 12** `cdsapi.retrieve()`
   calls for a full year — one per calendar month — each requesting a single month, and
   produces the yearly merged file at `dest`.
2. Each monthly request dict contains: `variable == ["2m_temperature",
   "total_precipitation"]`, `year == str(year)`, a single month value, days 1–31, all 24
   hours, `area == UK_AREA`, `data_format == "netcdf"`.
3. The twelve monthly files are concatenated on the time axis into `era5_land_{year}.nc`,
   sorted ascending in time, with no duplicated or missing hours across month boundaries.
4. **Resume:** re-running after an interruption skips months whose `.nc` file already exists
   and re-fetches only the missing months.
5. **Atomic writes:** every download and the merge write to a `*.part` temp path and
   `os.rename` to the final name only on success; a killed download leaves no file that the
   cache would treat as complete.
6. **Bug 2 fixed:** a CDS error whose text is `cost limits exceeded / request too large` is
   **not** reported as a licence error; a genuine licence-not-accepted error still is.
7. The existing offline integration test `test_weather_only_build_offline` still passes
   unchanged (proves the loader/fixture contract is intact).
8. New offline tests (below) pass with the `[weather]` extra installed. The one live test is
   skipped by default.

---

## Scope

**In scope**
- Rewriting `_download` into a per-month download + merge inside `weather.py` only.
- Per-month caching, atomic `.part` writes, resume-on-rerun.
- Tightening the licence-error heuristic and adding a distinct "too large" message.
- Offline tests for request shape, chunk count, merge correctness, atomicity, resume, and
  error classification; plus one opt-in live smoke test.

**Out of scope (do NOT do in this plan)**
- Parallel/concurrent downloads (explicitly deferred by the user).
- Automatic in-process retry/backoff per month (deferred until real runs show it is needed;
  it also depends on the Bug 2 fix so it never retries a licence/too-large error).
- Any change to `transform_and_load`, `_load_bronze`, `_derive_silver_and_ledger`,
  `quality_spec`, the loader, the committed fixture, or `client.py`.
- The full-year **load** memory footprint (see Performance & Open Questions) — flagged, not
  solved here.

---

## Constraints

- **Single module.** All production changes are in
  [src/crossroads/transformers/weather.py](../../../src/crossroads/transformers/weather.py).
  Do not edit `client.py`, `registry.py`, `quality.py`, or the base transformer.
- **Dependencies:** use only what the `[weather]` extra already provides — `cdsapi`,
  `xarray`, `netCDF4`. **Do not** add `dask`. This forbids `xarray.open_mfdataset` (which
  requires dask); use eager `xarray.open_dataset` + `xarray.concat` instead.
- **Lazy imports:** keep `import cdsapi` / `import xarray` inside the methods that use them,
  exactly as the current code does, so the module imports without the extra for discovery.
- **Keep it simple** and match the file's existing style (module-level helper functions for
  messages/heuristics; plain-language comments explaining *why*). Prefer the direct
  implementation over any abstraction.
- **No git commits or staging** without explicit user permission.
- **Determinism:** merge output must be deterministic (sort on the time axis; iterate months
  in fixed order).

---

## Approach / Architecture

**Chosen design: per-month download, merge into the existing yearly cache file.**

`extract()` is unchanged: it still checks for `era5_land_{year}.nc` and, if absent, calls
`_download(year, yearly_path)`. All new behaviour lives *inside* `_download`:

```
_download(year, dest):                      # dest = <cache>/era5_land_{year}.nc
    client = cdsapi.Client()                # (missing-key handling unchanged)
    for month in 1..12:
        mpath = <cache>/era5_land_{year}_{MM}.nc
        if not exists(mpath):               # RESUME: skip completed months
            _download_month(client, year, month, mpath)   # atomic .part -> rename
    _merge_months([12 mpaths], dest)        # xr.concat on time, atomic .part -> rename
    remove the 12 monthly files             # cleanup; yearly file is now the cache key
```

Because the yearly merged file keeps the name `era5_land_{year}.nc`, the loader, the
committed fixture, and the offline integration test (which seeds that exact filename) are all
untouched. This is the least-churn option and localises the entire change to the download
path.

**Why per-month.** The CDS cost/size limit scales with the request's field/grid volume. A
month is ~1/12 of a year, comfortably under the limit for the UK box + 2 variables. Days 1–31
and all 24 hours are still requested per month; CDS returns only the timestamps that exist
(February yields 28/29 days), so no special-casing of month lengths is required.

**Why merge (not "keep 12 files as the cache").** The rest of the system is built around one
file per year; keeping monthly files as the cache unit would force fixture/test rework and
make a full-year *offline* build impossible to seed from the single-month sample fixture.
Merging keeps the yearly-file contract. (Decision confirmed with the requester.)

**Why atomic `.part` writes.** Cache-resume trusts `os.path.exists(mpath)` to mean "this
month is complete." A download killed mid-write would leave a truncated `.nc` that passes
that check and would be silently stitched into the merge as a corrupt month. Writing to
`dest + ".part"` and renaming only on success closes that hole (rename is atomic on the same
filesystem).

**Alternatives rejected**
- *Monthly files as the cache unit (loader reads all 12):* rejected — forces loader/fixture/
  test changes and breaks single-month offline seeding.
- *In-memory concatenation during load (no yearly file written):* rejected — changes the
  loader and the offline seeding path for no benefit here.
- *`xarray.open_mfdataset` for the merge:* rejected — requires `dask`, a new dependency.
- *Automatic retry/backoff now:* deferred (see Scope).

**Data flow (unchanged downstream):** 12 monthly `.nc` → merged `era5_land_{year}.nc` →
`_load_bronze` (list of yearly files) → bronze `era5_weather_raw` → silver `weather` → gold
`weather_clean` → §9 invariants.

**Deviation discovered during execution (zip envelope).** The live Copernicus CADS backend
delivers ERA5-Land as a **ZIP wrapping a single `data_0.nc`**, even for
`data_format="netcdf"` — the downloaded file is not a raw NetCDF, so `xr.open_dataset`
raises `did not find a match in any of xarray's currently installed IO backends`. The
offline tests missed this because they wrote genuine `.nc` stubs. Fix: a
`_normalize_to_netcdf(path)` helper (module-level) unwraps the zip in place to a plain
`.nc` (atomic write + `os.replace`; idempotent; no-op on a real `.nc`), called inside
`_download_month` right after a successful `retrieve` and before the atomic rename. The
regression tests now deliver the **zip** form (`_write_stub_month_zip`) so the real delivery
shape is exercised end-to-end. Verified against a real 2022 build: 107,712,960 weather rows
(12,296 cells × 8,760 hours), all §9 invariants green, ~108M-row load peaked ~3.2 GB.

---

## Implementation Steps

All edits are in
[src/crossroads/transformers/weather.py](../../../src/crossroads/transformers/weather.py)
unless stated otherwise. Line numbers below reference the file as it stands today; adapt if
they have shifted, matching on the surrounding code.

### Step 1 — Fix the licence-error heuristic and add a "too large" classifier (Bug 2)

Replace the existing `_looks_like_licence_error` (currently
[weather.py:79-87](../../../src/crossroads/transformers/weather.py:79)) and add a new
`_looks_like_too_large_error` helper plus a `_too_large_message` message builder. Place the
new message builder next to `_licence_message` and the new heuristic next to the licence
heuristic.

```python
def _too_large_message(exc):
    """Actionable steps when CDS rejects a request as too large (cost limit exceeded)."""
    return (
        "Copernicus rejected the request because it is too large (cost limit exceeded).\n\n"
        "Crossroads already downloads ERA5-Land one month at a time to stay under this\n"
        "limit, so this usually means the requested area or variable list was widened.\n"
        "Reduce the request size and re-run the build.\n\n"
        f"(underlying cdsapi error: {exc})"
    )


def _looks_like_too_large_error(exc):
    """Heuristic: is this CDS failure a cost/size rejection (a 403 that is NOT a licence
    problem)? CDS phrases it as 'cost limits exceeded' / 'request is too large' /
    'reduce your selection'."""
    text = str(exc).lower()
    return any(word in text for word in ("cost limit", "too large", "reduce your selection"))


def _looks_like_licence_error(exc):
    """Heuristic: does this cdsapi failure look like an unaccepted-licence rejection?

    Match on licence wording ONLY. The previous version also matched bare '403' /
    'forbidden', but a 'request too large' rejection is also a 403 — that made this
    function swallow cost/size errors and report them as a licence problem. A cost/size
    error is explicitly excluded here.
    """
    if _looks_like_too_large_error(exc):
        return False
    text = str(exc).lower()
    return any(word in text for word in
               ("licence", "license", "not accepted", "terms of use"))
```

Expected result: a size/cost 403 now classifies as "too large", a licence 403 still
classifies as licence, and neither steals the other's message.

### Step 2 — Add the per-month filename and request builder

Keep the existing `_filename(year)` (yearly file) unchanged. Add, in the class, next to it:

```python
    def _month_filename(self, year, month):
        return f"era5_land_{year}_{month:02d}.nc"

    @staticmethod
    def _build_request(year, month):
        """The cdsapi request dict for ONE month of ERA5-Land over the UK. Days 1-31 and
        all 24 hours are always requested; CDS returns only the timestamps that exist for
        the month (e.g. February yields 28/29 days), so month length needs no special case."""
        return {
            "variable": ["2m_temperature", "total_precipitation"],
            "year": str(year),
            "month": [f"{month:02d}"],
            "day": [f"{d:02d}" for d in range(1, 32)],
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": UK_AREA,
            "data_format": "netcdf",
        }
```

### Step 3 — Add the atomic per-month downloader

Add this method to the class:

```python
    def _download_month(self, client, year, month, dest):
        """Retrieve ONE month to `dest`, written atomically: download to a '.part' temp
        file and rename to the final name only on success. A killed download therefore
        never leaves a partial .nc that the resume check (os.path.exists) would mistake
        for a complete month."""
        tmp = dest + ".part"
        try:
            client.retrieve("reanalysis-era5-land", self._build_request(year, month), tmp)
        except Exception as exc:
            if os.path.exists(tmp):
                os.remove(tmp)                       # never leave a partial temp behind
            # Give targeted help for the two common post-auth failures; otherwise let the
            # real error through unchanged (network, disk, etc. are self-explanatory).
            if _looks_like_licence_error(exc):
                raise RuntimeError(_licence_message(exc)) from exc
            if _looks_like_too_large_error(exc):
                raise RuntimeError(_too_large_message(exc)) from exc
            raise
        os.rename(tmp, dest)                          # atomic promote on success
```

### Step 4 — Add the atomic monthly-file merge

Add this method to the class:

```python
    def _merge_months(self, month_paths, dest):
        """Concatenate the monthly NetCDF files into a single yearly file at `dest`,
        sorted on the time axis, written atomically. Eager xarray only (no dask, so no
        open_mfdataset). Tolerates the 'valid_time' or 'time' coordinate name across CDS
        vintages, the same way _load_bronze does."""
        import xarray as xr                            # lazy: real download/merge only
        datasets = [xr.open_dataset(p) for p in sorted(month_paths)]
        tmp = dest + ".part"
        try:
            first = datasets[0]
            tname = "valid_time" if ("valid_time" in first.variables
                                     or "valid_time" in first.coords) else "time"
            combined = xr.concat(datasets, dim=tname).sortby(tname)
            try:
                combined.to_netcdf(tmp)
            finally:
                combined.close()
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)                          # do not leave a partial merge
            raise
        finally:
            for ds in datasets:
                ds.close()
        os.rename(tmp, dest)                            # atomic promote on success
```

### Step 5 — Rewrite `_download` as the per-year orchestrator

Replace the body of `_download`
([weather.py:118-153](../../../src/crossroads/transformers/weather.py:118)). Keep the
signature `_download(self, year, dest)` so `extract()` needs no change. Derive the cache
directory from `dest`.

```python
    def _download(self, year, dest):
        """Download one year of ERA5-Land as twelve per-month NetCDF files, then merge
        them into `dest` (era5_land_{year}.nc).

        A whole-year request is rejected by CDS as too large (cost limit), so we request
        one month at a time. Each month is cached and written atomically, so an
        interrupted build resumes without re-fetching completed months. cdsapi and xarray
        are imported lazily so the module and the offline tests do not require the
        [weather] extra."""
        import cdsapi                                   # lazy: real download only
        cache_dir = os.path.dirname(dest)

        # cdsapi raises a bare, cryptic exception when it can't find the API key
        # (no ~/.cdsapirc and no CDSAPI_* env vars). Translate it into setup steps.
        try:
            client = cdsapi.Client()
        except Exception as exc:
            raise RuntimeError(_missing_key_message(exc)) from exc

        month_paths = []
        for month in range(1, 13):
            mpath = os.path.join(cache_dir, self._month_filename(year, month))
            if not os.path.exists(mpath):               # RESUME: skip completed months
                self._download_month(client, year, month, mpath)
            month_paths.append(mpath)

        self._merge_months(month_paths, dest)           # writes dest atomically
        for mpath in month_paths:                        # cleanup: yearly file is the cache key now
            os.remove(mpath)
```

Note: `extract()` at
[weather.py:110-116](../../../src/crossroads/transformers/weather.py:110) is unchanged — it
still short-circuits when `era5_land_{year}.nc` exists, which is why the offline integration
test (which seeds that file) still skips all downloading.

### Step 6 — Register the `live` pytest marker

In [pyproject.toml](../../../pyproject.toml), find `[tool.pytest.ini_options]` and its
`markers` list (the existing `integration` marker lives there). Add:

```
    "live: opt-in tests that hit the real Copernicus CDS (needs ~/.cdsapirc and CROSSROADS_RUN_LIVE=1); skipped by default",
```

If no `markers` key exists, add one containing both the existing `integration` marker and the
new `live` marker. This prevents a `PytestUnknownMarkWarning`.

---

## Testing & Verification

All tests go in
[tests/test_weather.py](../../../tests/test_weather.py), which already
`pytest.importorskip("xarray")` at module top, so the whole file is skipped without the
`[weather]` extra. Run offline tests with:

```bash
cd ~/Documents/Code/Crossroads
source .venv/bin/activate
pip install -e '.[weather]'
python -m pytest -q tests/test_weather.py
python -m pytest -m integration -q tests/test_weather.py
```

### Shared test helper (add near the top of the test module, after imports)

A tiny synthetic single-month NetCDF writer, so tests can exercise the real merge path
without the network:

```python
def _write_stub_month_nc(path, year, month, n_hours=3):
    """Write a minimal ERA5-Land-shaped NetCDF for one month: a 2x2 grid, `n_hours`
    consecutive hours starting at midnight UTC on the 1st. Deterministic (values derived
    from indices), no network, no randomness."""
    import numpy as np
    import pandas as pd
    import xarray as xr
    times = pd.date_range(f"{year}-{month:02d}-01 00:00", periods=n_hours, freq="h")
    lat = [54.7, 54.8]
    lon = [-1.2, -1.1]
    shape = (n_hours, len(lat), len(lon))
    t2m = np.full(shape, 288.15)                 # 15 C, in Kelvin
    tp = np.full(shape, 0.001)                   # 1 mm, in metres
    ds = xr.Dataset(
        {"t2m": (("valid_time", "latitude", "longitude"), t2m),
         "tp":  (("valid_time", "latitude", "longitude"), tp)},
        coords={"valid_time": times, "latitude": lat, "longitude": lon},
    )
    ds["t2m"].attrs["units"] = "K"
    ds["tp"].attrs["units"] = "m"
    ds.to_netcdf(path)
    ds.close()
```

### Integration test 1 (PRIMARY) — chunk count + merge correctness

```python
@pytest.mark.integration
def test_download_year_chunks_and_merges(tmp_path, monkeypatch):
    cache = str(tmp_path); calls = []

    class FakeClient:
        def retrieve(self, name, request, target):
            assert name == "reanalysis-era5-land"
            (month,) = request["month"]                 # one month per call
            calls.append(month)
            _write_stub_month_nc(target, 2022, int(month), n_hours=3)

    monkeypatch.setattr("cdsapi.Client", lambda *a, **k: FakeClient())

    dest = os.path.join(cache, "era5_land_2022.nc")
    Era5WeatherTransformer()._download(2022, dest)

    # 12 calls, one per month, in order
    assert calls == [f"{m:02d}" for m in range(1, 13)]
    # yearly merged file exists; monthly parts cleaned up; no leftover .part files
    assert os.path.exists(dest)
    assert not any(f.endswith(".part") for f in os.listdir(cache))
    assert not any(f.startswith("era5_land_2022_") for f in os.listdir(cache))

    import xarray as xr
    ds = xr.open_dataset(dest)
    try:
        tname = "valid_time" if "valid_time" in ds.coords else "time"
        times = ds[tname].values
        assert len(times) == 12 * 3                     # all months merged
        assert (times[:-1] <= times[1:]).all()          # sorted, no reordering
        assert len(set(times.tolist())) == len(times)   # no duplicate hours
    finally:
        ds.close()
```

### Integration test 2 — resume skips completed months

```python
@pytest.mark.integration
def test_download_resumes_completed_months(tmp_path, monkeypatch):
    cache = str(tmp_path); calls = []
    # Pre-seed months 01..06 as if a previous run had completed them.
    for m in range(1, 7):
        _write_stub_month_nc(os.path.join(cache, f"era5_land_2022_{m:02d}.nc"), 2022, m)

    class FakeClient:
        def retrieve(self, name, request, target):
            (month,) = request["month"]; calls.append(month)
            _write_stub_month_nc(target, 2022, int(month), n_hours=3)

    monkeypatch.setattr("cdsapi.Client", lambda *a, **k: FakeClient())
    Era5WeatherTransformer()._download(2022, os.path.join(cache, "era5_land_2022.nc"))

    assert calls == [f"{m:02d}" for m in range(7, 13)]   # only the missing half fetched
```

### Unit test 3 — atomic write leaves no partial file on failure

```python
def test_download_month_atomic_on_failure(tmp_path):
    dest = os.path.join(str(tmp_path), "era5_land_2022_02.nc")

    class FailingClient:
        def retrieve(self, name, request, target):
            with open(target, "w") as fh:               # simulate a partial write...
                fh.write("partial")
            raise RuntimeError("network died mid-download")

    with pytest.raises(RuntimeError, match="network died"):
        Era5WeatherTransformer()._download_month(FailingClient(), 2022, 2, dest)

    assert not os.path.exists(dest)                      # final name never created
    assert not os.path.exists(dest + ".part")           # partial temp cleaned up
```

### Unit test 4 — request shape per month

```python
def test_build_request_shape():
    req = Era5WeatherTransformer._build_request(2022, 2)
    assert req["variable"] == ["2m_temperature", "total_precipitation"]
    assert req["year"] == "2022"
    assert req["month"] == ["02"]                        # single, zero-padded month
    assert len(req["day"]) == 31 and len(req["time"]) == 24
    assert req["area"] == [61.0, -8.5, 49.5, 2.0]
    assert req["data_format"] == "netcdf"
```

### Unit test 5 — error classification (Bug 2)

```python
def test_too_large_not_misclassified_as_licence():
    from crossroads.transformers.weather import (
        _looks_like_licence_error, _looks_like_too_large_error)
    too_large = RuntimeError(
        "403 Client Error: Forbidden ... cost limits exceeded "
        "Your request is too large, please reduce your selection.")
    licence = RuntimeError(
        "403 Client Error: Forbidden ... required licences not accepted; "
        "accept the terms of use")
    assert _looks_like_too_large_error(too_large) is True
    assert _looks_like_licence_error(too_large) is False    # the bug being fixed
    assert _looks_like_licence_error(licence) is True        # genuine licence still caught
```

### Update existing test — `test_download_builds_correct_cds_request`

The current test (`tests/test_weather.py`, the `test_download_builds_correct_cds_request`
function) asserts a **single** `retrieve()` with 12 months and calls `_download(2023,
"/tmp/...")` with a no-op FakeClient (which now breaks, because `_download` merges and the
no-op writes no files). **Delete that test** — its intent is fully covered by
`test_build_request_shape` (request shape) and `test_download_year_chunks_and_merges` (12
calls + merge). Do not leave a FakeClient that writes nothing, because the merge step needs
real monthly files.

### Regression — existing offline build must still pass unchanged

Run `test_weather_only_build_offline` (already in the file). It seeds `era5_land_2023.nc`
and must still build green, proving `extract()`'s yearly short-circuit and the loader
contract are intact. Do not modify this test.

### Live smoke test (opt-in, skipped by default)

```python
import os as _os

_LIVE = pytest.mark.skipif(
    not (_os.path.exists(_os.path.expanduser("~/.cdsapirc"))
         and _os.environ.get("CROSSROADS_RUN_LIVE")),
    reason="live CDS test: set CROSSROADS_RUN_LIVE=1 and provide ~/.cdsapirc to run")


@pytest.mark.live
@_LIVE
def test_live_two_month_download_and_merge(tmp_path):
    """The one thing the synthetic fixture cannot prove: that a REAL CDS monthly download
    parses, and that concatenating two adjacent real months yields no duplicate or missing
    hours at the month boundary. Run manually once:
        CROSSROADS_RUN_LIVE=1 pytest -m live -q tests/test_weather.py
    """
    import xarray as xr
    tx = Era5WeatherTransformer()
    import cdsapi
    client = cdsapi.Client()
    p1 = os.path.join(str(tmp_path), "era5_land_2022_01.nc")
    p2 = os.path.join(str(tmp_path), "era5_land_2022_02.nc")
    tx._download_month(client, 2022, 1, p1)
    tx._download_month(client, 2022, 2, p2)
    dest = os.path.join(str(tmp_path), "era5_land_2022.nc")
    tx._merge_months([p1, p2], dest)

    ds = xr.open_dataset(dest)
    try:
        tname = "valid_time" if "valid_time" in ds.coords else "time"
        times = ds[tname].values
        assert (times[:-1] < times[1:]).all()           # strictly increasing: no dup, sorted
        assert len(times) == (31 + 28) * 24             # Jan + Feb 2022 hourly, complete
        assert "t2m" in ds.variables and "tp" in ds.variables
    finally:
        ds.close()
```

### Ship-readiness checklist

- [ ] `python -m pytest -q tests/test_weather.py` green with `[weather]` installed.
- [ ] `python -m pytest -m integration -q tests/test_weather.py` green.
- [ ] `python -m pytest -q` (full suite, `[weather]` installed) green; the `live` test is
      collected but skipped.
- [ ] Manual real run for one year: `python -c "import crossroads as cr;
      c=cr.init_engine(database_path='crossroads_2022.db');
      c.build(datasets=['stats19','era5_weather'], years=[2022],
      boundary_mode='snapshot'); c.close()"` completes without the "too large" 403.
- [ ] (Optional, once) `CROSSROADS_RUN_LIVE=1 python -m pytest -m live -q
      tests/test_weather.py` green.

---

## Performance

- **Requests:** 12 sequential CDS jobs per year instead of 1. Wall-clock is dominated by the
  shared CDS queue, not local work; this plan does not change that (parallelism is out of
  scope).
- **Merge:** `xarray.concat` + `to_netcdf` over 12 monthly files. Eager (no dask), so it
  reads the monthly arrays to write the yearly file. For the UK box at 0.1° this is roughly
  a 116×106 grid over hourly data — heavy but one-off per year, and comparable to what the
  loader already does.
- **Load (⚠ pre-existing, not introduced here):** `_load_bronze` calls xarray `to_dataframe`
  on the whole yearly file, materialising ~100M cell-hour rows for a full UK year as a pandas
  frame before inserting to DuckDB. This is the largest memory consumer and, because the
  whole-year download never previously succeeded, it is exercised at full scale for the first
  time by this change. See Failure Modes and Open Questions.

---

## Failure Modes

| Failure | Guardrail / recovery |
|---|---|
| Download killed mid-write leaves a truncated `.nc` | Atomic `.part` → rename: the final name only appears on success, so resume never treats a partial as complete. |
| Build interrupted after N months | Completed month files remain; re-run skips them (resume) and fetches only the rest. |
| Merge fails partway (corrupt/short month) | Yearly `.part` removed on error; monthly files remain; re-run re-merges. |
| CDS licence not accepted | Still classified and reported by `_licence_message` (unchanged behaviour). |
| CDS "request too large" (e.g. someone widened `UK_AREA`) | Now correctly reported via `_too_large_message`, not mislabelled as a licence issue. |
| Missing `~/.cdsapirc` / API key | `_missing_key_message` (unchanged). |
| **Full-year load OOM** in `_load_bronze` | **Not solved here.** Mitigation: build one year at a time (`years=[2022]`). Flagged as an Open Question for a possible follow-up (stream months into bronze). |
| Incomplete current-year months (future dates) | Out of scope: this pipeline targets complete historical years; a partial current year may error on missing months. Documented, not handled. |

---

## Rollback

Single-module, additive change with no schema or database migration. To undo:
`git checkout -- src/crossroads/transformers/weather.py tests/test_weather.py pyproject.toml`
(if uncommitted) or revert the commit. Delete any cached `era5_land_*.nc` /
`era5_land_*_*.nc` / `*.part` files in the build's `cache_dir` so a subsequent run re-downloads
cleanly. No other component depends on the internal download methods.

---

## Open Questions

1. **Full-year load memory (the real remaining risk).** Enabling whole-year downloads makes
   `_load_bronze` materialise ~100M rows for a UK year for the first time. If it OOMs on
   typical hardware, the fix is a separate, small follow-up: stream each month into bronze via
   `INSERT` (or read the yearly file in time chunks) instead of one `to_dataframe`. Do we want
   that as a Stage 2 now, or defer until a real full-year build proves whether it is needed?
2. **Keep vs delete monthly files after merge.** This plan deletes them (the yearly file is
   the durable cache; re-runs short-circuit on it). If you would rather retain them for
   inspection/extra resilience, drop the cleanup loop in Step 5 and update
   `test_download_year_chunks_and_merges` accordingly (it currently asserts they are gone).
3. **Merged-file disk churn.** During merge, both the 12 monthly files and the yearly file
   exist briefly. Acceptable for one year; worth revisiting only if multi-decade builds become
   common.
