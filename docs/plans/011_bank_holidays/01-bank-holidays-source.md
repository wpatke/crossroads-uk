# Stage 01 — Bank-holidays source & dimension table
> Part of GOV.UK Bank Holidays. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State
- No prior stage required. Verify the repo builds and tests pass first:
  - `pip install -e '.[dev]'`
  - `pytest -q` is green.
- Confirm these exist (patterns you will copy):
  - `src/crossroads/transformers/weather.py` — a downloading `BaseTransformer` (structure to mirror).
  - `src/crossroads/quality.py` exports `QualityExemption`, `create_clean_view`
    (grep: `grep -n "class QualityExemption\|def create_clean_view" src/crossroads/quality.py`).
  - `tests/fixtures/` contains `ons/`, `stats19/`, `weather/` fixture subdirs.

## Objective
Add a new `bank_holidays` transformer that downloads the GOV.UK bank-holidays feed and loads it into
a standalone `bank_holidays` dimension table keyed on `(date, division)`, exempt from the quality
invariants. No STATS19 changes in this stage.

## Implementation Steps

### 1. Create the transformer — `src/crossroads/transformers/bank_holidays.py`
New file. Follow the module docstring / structure conventions of `weather.py`. Content:

```python
"""GOV.UK bank holidays — a small reference/lookup dimension (spec §4 Provider-Plugin).

Downloads the live feed at https://www.gov.uk/bank-holidays.json and flattens it into a
`bank_holidays` dimension table, one row per (date, division). The feed carries the UK's
three bank-holiday divisions — england-and-wales, scotland, northern-ireland — which
genuinely differ, so the division is preserved (never collapsed to a single flag).

This is a REFERENCE/LOOKUP dimension from a LIVE feed: it does not fit the spec §2
reproducibility guarantee or the keep-in-place silver-fact model, and it has no reject
dimensions (every published event is a valid date). So quality_spec() returns a
QualityExemption (recorded in quality_exemptions), and this source is not audited.

Download uses only the standard library (urllib + json) — no new dependency. Offline
tests pre-seed the cache with a committed sample JSON, so extract() downloads nothing.
STATS19 optionally consumes the `bank_holidays` table to stamp collisions.is_bank_holiday
(see docs/plans/011_bank_holidays/02); this module never needs to know about collisions.
"""

import json
import os
import urllib.request

from crossroads.transformers.base import BaseTransformer
from crossroads.quality import QualityExemption, create_clean_view

# The live GOV.UK feed. Public, Open Government Licence v3.0.
FEED_URL = "https://www.gov.uk/bank-holidays.json"

# The three divisions the feed always carries. Used only to sanity-check a download.
DIVISIONS = ("england-and-wales", "scotland", "northern-ireland")


class BankHolidaysTransformer(BaseTransformer):
    """Loads the GOV.UK bank-holidays feed into the `bank_holidays` dimension table."""

    source_id = "bank_holidays"
    display_name = "bank holidays"           # friendly wizard-menu label

    BRONZE = "bank_holidays_raw"             # faithful flatten of the feed (dates as text)
    SILVER = "bank_holidays"                 # typed dimension, one row per (date, division)
    CLEAN_VIEW = "bank_holidays_clean"       # gold view (no reject flags -> all rows)

    CACHE_FILE = "bank-holidays.json"

    # --- extract (real download; offline tests pre-seed the cache) ---------
    def extract(self, cache_dir, **kwargs):
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, self.CACHE_FILE)
        if os.path.exists(path):             # offline-friendly: skip if cached/seeded
            return
        self._download(path)

    def _download(self, dest):
        """Fetch the feed to `dest`, written atomically (download to a temp, parse-check,
        then rename) so an interrupted or corrupt download never leaves a half-file that
        the extract() cache check would mistake for a good one."""
        tmp = dest + ".part"
        try:
            with urllib.request.urlopen(FEED_URL, timeout=60) as resp:
                raw = resp.read()
            data = json.loads(raw)                       # fail fast on a non-JSON error page
            if not all(d in data for d in DIVISIONS):    # smoke check: all divisions present
                raise ValueError(
                    f"bank-holidays feed missing expected divisions; got {sorted(data)}")
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, dest)                         # atomic promote on success
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # --- transform_and_load ------------------------------------------------
    def transform_and_load(self, con, cache_dir):
        path = os.path.join(cache_dir, self.CACHE_FILE)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"[bank_holidays] no cached feed at {path}; extract() must run first "
                f"(or seed the cache in tests).")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._load_bronze(con, data)
        self._derive_silver(con)
        # GOLD: no reject flags, so the clean view is every silver row (WHERE TRUE).
        create_clean_view(con, self.CLEAN_VIEW, self.SILVER, [])
        # NOTE: this source is quality-EXEMPT, so we deliberately do NOT call
        # record_source_rows here — there is no conservation invariant to feed.

    def _load_bronze(self, con, data):
        """Faithful bronze: one row per (division, event), dates kept as raw text. Values
        are not typed here (that is silver's job). Flatten in Python, then bulk-insert with
        bound parameters (row values are never string-interpolated)."""
        rows = []
        for division, block in data.items():
            for ev in block.get("events", []):
                rows.append((
                    division,
                    ev.get("date"),
                    ev.get("title"),
                    ev.get("notes", ""),
                    ev.get("bunting"),
                ))
        con.execute(
            f"CREATE OR REPLACE TABLE {self.BRONZE} "
            f"(division VARCHAR, date VARCHAR, title VARCHAR, notes VARCHAR, bunting BOOLEAN)")
        if rows:
            con.executemany(
                f"INSERT INTO {self.BRONZE} VALUES (?, ?, ?, ?, ?)", rows)

    def _derive_silver(self, con):
        """Typed dimension, 1:1 with bronze. ISO 'YYYY-MM-DD' casts straight to DATE.
        source_row_key = division|date|title (a stable, unique natural key per event).
        Factored out so a test can drive it against a synthetic bronze."""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.SILVER} AS "
            f"SELECT "
            f"  division || '|' || date || '|' || title AS source_row_key, "
            f"  CAST(date AS DATE) AS date, "
            f"  division, title, notes, bunting "
            f"FROM {self.BRONZE}"
        )

    def quality_spec(self):
        # Reference/lookup dimension from a LIVE feed: not reproducible (spec §2), no
        # keep-in-place fact semantics, no reject dimensions. Opt out explicitly, on
        # the record (recorded in quality_exemptions).
        return QualityExemption(
            reason=("reference/lookup dimension loaded from the live gov.uk bank-holidays "
                    "feed; non-reproducible (spec §2) and has no reject dimensions, so the "
                    "keep-in-place conservation invariant does not apply"))
```

Expected result: the file imports cleanly; the registry auto-discovers `BankHolidaysTransformer`
(no registry edit). Verify discovery:
```
python -c "from crossroads.registry import Registry; print([t.source_id for t in Registry().all()])"
```
→ the list includes `bank_holidays`.

### 2. Commit a test fixture — `tests/fixtures/bank_holidays/bank-holidays-sample.json`
A trimmed, real-shaped feed covering **2023** (so it aligns with the committed 2023 STATS19 fixture
used in Stage 02) with all three divisions. Keep it small but include a divergence between divisions
(a date that is a holiday in one nation but not another) so Stage 02's division-routing test has real
data to lean on. Exact content:

```json
{
  "england-and-wales": {
    "division": "england-and-wales",
    "events": [
      {"title": "New Year’s Day", "date": "2023-01-02", "notes": "Substitute day", "bunting": true},
      {"title": "Good Friday", "date": "2023-04-07", "notes": "", "bunting": false},
      {"title": "Easter Monday", "date": "2023-04-10", "notes": "", "bunting": true},
      {"title": "Christmas Day", "date": "2023-12-25", "notes": "", "bunting": true},
      {"title": "Boxing Day", "date": "2023-12-26", "notes": "", "bunting": true}
    ]
  },
  "scotland": {
    "division": "scotland",
    "events": [
      {"title": "New Year’s Day", "date": "2023-01-02", "notes": "Substitute day", "bunting": true},
      {"title": "2nd January", "date": "2023-01-03", "notes": "Substitute day", "bunting": true},
      {"title": "Good Friday", "date": "2023-04-07", "notes": "", "bunting": false},
      {"title": "Christmas Day", "date": "2023-12-25", "notes": "", "bunting": true},
      {"title": "Boxing Day", "date": "2023-12-26", "notes": "", "bunting": true}
    ]
  },
  "northern-ireland": {
    "division": "northern-ireland",
    "events": [
      {"title": "New Year’s Day", "date": "2023-01-02", "notes": "Substitute day", "bunting": true},
      {"title": "St Patrick’s Day", "date": "2023-03-17", "notes": "", "bunting": true},
      {"title": "Christmas Day", "date": "2023-12-25", "notes": "", "bunting": true},
      {"title": "Boxing Day", "date": "2023-12-26", "notes": "", "bunting": true}
    ]
  }
}
```
Note the deliberate divergence: **Easter Monday (2023-04-10)** is england-and-wales only (absent from
scotland and NI); **2nd January (2023-01-03)** is scotland only. Stage 02 uses these.

Also add `tests/fixtures/bank_holidays/README.md` (mirror `tests/fixtures/weather/README.md`'s style):
one paragraph stating this is a trimmed snapshot of `https://www.gov.uk/bank-holidays.json`
(Open Government Licence v3.0), hand-reduced to 2023 for offline tests, and that real builds fetch the
live feed.

### 3. Integration test — `tests/test_bank_holidays.py`
New file. Offline, no network. Mirror the cache-seeding idiom used in `tests/test_stats19.py`
(`_seed_cache` copies fixtures into the build cache) and restrict the registry to just this
transformer so the build doesn't pull in ONS/other sources.

```python
"""Offline tests for the GOV.UK bank-holidays source (Stage 01)."""

import os
import shutil

import crossroads
from crossroads.transformers.bank_holidays import BankHolidaysTransformer

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "bank_holidays",
                       "bank-holidays-sample.json")


def _bh_client(tmp_path):
    cache = str(tmp_path / "cache")
    os.makedirs(cache, exist_ok=True)
    shutil.copy(FIXTURE, os.path.join(cache, "bank-holidays.json"))   # pre-seed: no download
    client = crossroads.init_engine(cache_dir=cache)
    client.registry._transformers = [BankHolidaysTransformer()]        # this source only
    return client


def test_bank_holidays_table_built_and_typed(tmp_path):
    client = _bh_client(tmp_path)
    client.build(datasets=["bank_holidays"])          # runs §9 invariants (exemption recorded)
    try:
        con = client.con
        # Table exists and has rows.
        n = con.execute("SELECT count(*) FROM bank_holidays").fetchone()[0]
        assert n > 0
        # All three divisions present.
        divs = {r[0] for r in con.execute(
            "SELECT DISTINCT division FROM bank_holidays").fetchall()}
        assert divs == {"england-and-wales", "scotland", "northern-ireland"}
        # `date` is a real DATE (typed silver), not text.
        dtype = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='bank_holidays' AND column_name='date'").fetchone()[0]
        assert dtype.upper() == "DATE"
        # A known england-and-wales holiday is present and correctly divisioned.
        hit = con.execute(
            "SELECT count(*) FROM bank_holidays "
            "WHERE division='england-and-wales' AND date=DATE '2023-04-10'").fetchone()[0]
        assert hit == 1
        # The exemption was recorded (source is deliberately not audited).
        ex = con.execute(
            "SELECT count(*) FROM quality_exemptions WHERE source_id='bank_holidays'").fetchone()[0]
        assert ex >= 1
    finally:
        client.close()


def test_bank_holidays_build_is_idempotent(tmp_path):
    """A second build over the same seeded cache reproduces the same row count (CREATE OR
    REPLACE, not a doubling INSERT)."""
    client = _bh_client(tmp_path)
    client.build(datasets=["bank_holidays"])
    n1 = client.con.execute("SELECT count(*) FROM bank_holidays").fetchone()[0]
    client.build(datasets=["bank_holidays"])
    n2 = client.con.execute("SELECT count(*) FROM bank_holidays").fetchone()[0]
    assert n1 == n2 and n1 > 0
    client.close()
```
Confirm the exact `init_engine` / `client.build` / `client.con` / `client.registry._transformers`
usage against `tests/test_stats19.py` (e.g. `_stats19_client`) and adapt names if they differ.

### 4. Document the source
- **`docs/data-sources.md`** — add a new numbered section after "## 3. Copernicus ERA5-Land …",
  matching the existing sections' format (source name, URL, licence, attribution, notes). State:
  URL `https://www.gov.uk/bank-holidays.json`; licence **Open Government Licence v3.0** (MIT-
  compatible, attribution to GOV.UK / Government Digital Service); and a **caveat** paragraph — this
  feed is *live* and spans only a rolling recent window (~2018 onward), so this source is
  deliberately exempt from the spec §2 reproducibility guarantee and historical dates outside the
  feed's range are unknown (see Stage 02's NULL semantics).
- **`docs/schema.md`** — add a `### `bank_holidays`` subsection under "## Silver: analytical tables"
  (place it after the `### `weather`` block), matching the `CREATE TABLE`-style column dictionary
  used by the other tables:
  ```sql
  CREATE TABLE bank_holidays (
      source_row_key VARCHAR,  -- stable key: division|date|title (unique per event)
      date           DATE,     -- the bank-holiday date (UK local calendar date), typed from the feed
      division       VARCHAR,  -- 'england-and-wales' | 'scotland' | 'northern-ireland'
      title          VARCHAR,  -- holiday name as published (e.g. 'Easter Monday')
      notes          VARCHAR,  -- feed notes (e.g. 'Substitute day'); '' when none
      bunting        BOOLEAN   -- feed 'bunting' flag (celebratory-day marker)
  );
  ```
  Add one line noting it is a reference/lookup dimension (quality-exempt, live feed). If a gold-views
  list section enumerates views, add `bank_holidays_clean` there too. If `tests/test_schema_doc.py`
  asserts documented columns match the built table, run it (below) and reconcile any mismatch.

## Testing & Verification

**Primary (integration, offline):**
```
pip install -e '.[dev]'
pytest -q tests/test_bank_holidays.py
```
Expected: both tests pass — the `bank_holidays` table builds from the seeded fixture with all three
divisions, a typed `DATE` column, a known holiday row, a recorded exemption, and an idempotent rebuild.

**Discovery smoke:**
```
python -c "from crossroads.registry import Registry; print('bank_holidays' in [t.source_id for t in Registry().all()])"
```
Expected: `True`.

**Schema-doc consistency (if present):**
```
pytest -q tests/test_schema_doc.py
```
Expected: green (reconcile `docs/schema.md` if it flags the new table).

**Full suite (no regressions):**
```
pytest -q
```
Expected: green.

**Stage ship-readiness checklist:**
- [ ] `bank_holidays.py` created; registry auto-discovers `bank_holidays` (no registry edit).
- [ ] `tests/fixtures/bank_holidays/bank-holidays-sample.json` (+ README) committed.
- [ ] `bank_holidays` table builds: 3 divisions, typed `DATE`, known holiday present.
- [ ] `quality_exemptions` has a `bank_holidays` row; build invariants pass.
- [ ] `record_source_rows` is NOT called for this source.
- [ ] `docs/data-sources.md` + `docs/schema.md` updated; `test_schema_doc.py` green.
- [ ] `pytest -q` green.

## End State / Handoff (the contract)
- `src/crossroads/transformers/bank_holidays.py` exists and is auto-discovered as source_id
  `bank_holidays`, `user_selectable=True`, `display_name="bank holidays"`.
- A build selecting `bank_holidays` produces:
  - bronze `bank_holidays_raw` (raw text dates), silver `bank_holidays` (typed `DATE`, one row per
    `(date, division)`), gold view `bank_holidays_clean`.
  - a `quality_exemptions` row for `bank_holidays`; all §9 invariants pass.
- Stage 02 may assume: the `bank_holidays` silver table exists with columns
  `(source_row_key, date DATE, division, title, notes, bunting)` and that `date`/`division` are the
  join keys for the collision stamp.

## Failure Modes & Rollback
- **Non-JSON / error-page download** → `_download` fails fast in `json.loads` and removes the temp;
  the cache stays clean. (Not exercised offline.)
- **Feed missing a division** → `_download` raises `ValueError` before promoting the file.
- **`create_clean_view` with `[]` flags** → produces `WHERE TRUE` (verified in `quality.py`); if a
  version rejects an empty list, create the view manually:
  `con.execute("CREATE OR REPLACE VIEW bank_holidays_clean AS SELECT * FROM bank_holidays")`.
- **Rollback:** delete `bank_holidays.py`, the fixture dir, `tests/test_bank_holidays.py`, and revert
  the two doc edits. Nothing else references this source in Stage 01, so removal is clean.
