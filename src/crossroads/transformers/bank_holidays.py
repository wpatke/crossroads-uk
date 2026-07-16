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

from crossroads.transformers.base import BaseTransformer
from crossroads.quality import QualityExemption, create_clean_view
from crossroads.net import download_to_file

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
        """Fetch the feed to `dest`, streamed and atomic, with a socket timeout so a stalled
        endpoint fails fast rather than hanging. A validator parses the JSON and confirms all
        divisions are present BEFORE the file is promoted, so a non-JSON error page or a
        truncated feed never reaches the cache."""
        def _validate(tmp):
            with open(tmp, "r", encoding="utf-8") as fh:
                data = json.load(fh)                     # fail fast on a non-JSON error page
            if not all(d in data for d in DIVISIONS):    # smoke check: all divisions present
                raise ValueError(
                    f"bank-holidays feed missing expected divisions; got {sorted(data)}")
        download_to_file(FEED_URL, dest, timeout=60, validator=_validate)

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
