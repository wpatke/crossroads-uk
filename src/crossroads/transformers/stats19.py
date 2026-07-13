"""DfT STATS19 road collision ingestion — Collision, Vehicle, and Casualty
(spec §3, §5 Phase 2).

One module, one concrete transformer (Stats19Transformer) that owns all three
related tables. STATS19 must be built in dependency order (collision silver first,
then vehicle/casualty linkage, then the spatial stamp of collisions), so a single
transformer drives the whole pipeline and declares THREE audit units to the quality
engine via a tuple from quality_spec() (see docs/plans/004_stats19_ingestion).

Coordinates are OSGR eastings/northings — natively EPSG:27700, so cast (never
reproject). Downloads use only the standard library (urllib); CSVs are read
in-database with read_csv. For offline tests the cache is pre-seeded with the
committed sample CSVs, so no network access occurs.
"""

import os
import urllib.request
import urllib.error
import warnings

from crossroads.transformers.base import BaseTransformer
from crossroads.quality import (
    SourceQuality, Dimension, record_source_rows, log_exclusion, create_clean_view,
)

# DfT publishes per-year CSVs under this base. The per-year filename template is
# code-controlled; live filenames were verified at implementation time (2020-2024
# are published individually; earlier years are not).
DFT_BASE_URL = "https://data.dft.gov.uk/road-accidents-safety-data"
_FILE_TEMPLATE = "dft-road-casualty-statistics-{ftype}-{year}.csv"

# Missing/out-of-range coordinate sentinels (spec §9). A coordinate equal to any of
# these — or blank / non-numeric — is treated as missing: typed value NULL, geom NULL,
# geom_valid FALSE, logged, and the row is retained (never deleted). Declared here as
# the shared constant.
COORD_SENTINELS = ("-1", "0")

# Sentinels for the broad numeric clean. A numeric column equal to any of
# these — or blank / non-numeric — becomes NULL. A coded column uses the codebook's
# is_missing set instead. (Note: DfT leaves missing longitude/latitude blank, so '' is
# the sentinel that fires in practice; '-1' is inert for those but harmless.)
NUMERIC_SENTINELS = ("-1", "")

# British National Grid envelope, for verifying geometry really is EPSG:27700 in tests.
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000

# Reference data (shared lookups), derived independently from DfT's published data guide
# (Road Safety Open Dataset Data Guide, 2024 edition, OGL v3.0) and committed under
# src/crossroads/reference/. They ship in the wheel like reference/ons_boundaries.json
# and load once per build. NEITHER is an audited source — they are static lookups.
_REFERENCE_DIR = os.path.join(os.path.dirname(__file__), "..", "reference")
_CODEBOOK_PATH = os.path.join(_REFERENCE_DIR, "stats19_codebook.csv")
_COLUMN_MANIFEST_PATH = os.path.join(_REFERENCE_DIR, "stats19_columns.csv")


class Stats19Transformer(BaseTransformer):
    """Ingests STATS19 Collision/Vehicle/Casualty into three bronze/silver pairs.

    The engine calls extract() then transform_and_load() back-to-back on this same
    instance, so extract() stashes the resolved build parameters (years, boundary
    mode) on self for transform_and_load() to read — the same hand-off spatial.py uses.
    """

    source_id = "stats19"          # registry identity; audit units are the three below

    # STATS19 stamps codes from the boundary tables, weather metrics, and the
    # bank-holiday flag onto its own collisions table, so those sources must import
    # first. Declared explicitly rather than relying on source_id alphabetical order.
    # Optional: any of these not selected this build is skipped (guarded at ETL time).
    # "era5_weather"/"bank_holidays" are included so the ordering is correct whenever
    # each source is active; when one isn't selected, its edge is simply inert.
    depends_on = ("bank_holidays", "era5_weather", "ons_lad", "ons_ctyua")

    # --- audit source_ids (one per bronze/silver pair) ---
    COLLISION_SID = "stats19_collision"
    VEHICLE_SID = "stats19_vehicle"
    CASUALTY_SID = "stats19_casualty"

    # --- table names ---
    COLLISION_BRONZE, COLLISION_SILVER = "stats19_collision_raw", "collisions"
    VEHICLE_BRONZE, VEHICLE_SILVER = "stats19_vehicle_raw", "vehicles"
    CASUALTY_BRONZE, CASUALTY_SILVER = "stats19_casualty_raw", "casualties"

    # --- reference tables (static lookups, not audited sources) ---
    CODEBOOK_TABLE = "codebook"
    COLUMN_MANIFEST_TABLE = "column_manifest"

    # --- completeness report (stats19-owned report table, not an audited source) ---
    COMPLETENESS_TABLE = "stats19_completeness"

    # --- ledger rules for the collision dimensions (also referenced by quality_spec) ---
    COORD_RULE = "stats19.coord.sentinel"
    DATETIME_RULE = "stats19.datetime.invalid"

    # --- ledger rules for the vehicle/casualty link dimension ---
    VEHICLE_LINK_RULE = "stats19.link.orphan_vehicle"
    CASUALTY_LINK_RULE = "stats19.link.orphan_casualty"

    # --- ledger rules for the CORE severity dimensions (also in quality_spec) ---
    COLLISION_SEVERITY_RULE = "stats19.collision_severity.missing"
    CASUALTY_SEVERITY_RULE = "stats19.casualty_severity.missing"

    def is_active(self, **kwargs):
        # Nothing to ingest without years; a no-years build (e.g. boundary-only)
        # simply skips STATS19. A real build passes years (spec §8 target flow).
        return bool(kwargs.get("years"))

    def _filename(self, ftype, year):
        return _FILE_TEMPLATE.format(ftype=ftype, year=year)

    def _looks_like_stats19_csv(self, path):
        """Cheap content check: a real STATS19 file's header row names the index column.

        Catches a 200-OK HTML error page, a truncated download, or a login redirect that a
        status-code check alone would miss. Historical files use 'accident_index'; 2020+
        files use 'collision_index' -- accept either. This is a smoke detector, not full
        schema validation (read_csv + the row-conservation invariants are the deeper check).
        """
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline().lstrip("﻿").lower()   # lstrip tolerates a UTF-8 BOM
        return "accident_index" in first or "collision_index" in first

    def _fetch(self, url, dest):
        """Download `url` to `dest` safely, then validate before it reaches the cache.

        Fetch to a temporary sibling ('<dest>.part'), confirm it looks like a STATS19 CSV,
        then atomically move it into place. A missing year (HTTP 404) or any other HTTP error
        becomes a researcher-friendly ValueError; a non-CSV body (e.g. an error page returned
        as 200 OK) is rejected. In every failure case the partial file is removed, so a bad
        response can never poison the cache for a later run (extract() skips re-downloading
        any file that already exists).
        """
        tmp = dest + ".part"
        try:
            urllib.request.urlretrieve(url, tmp)
        except urllib.error.HTTPError as exc:
            if os.path.exists(tmp):
                os.remove(tmp)
            if exc.code == 404:
                raise ValueError(
                    f"No STATS19 data found at {url} (HTTP 404). DfT usually publishes a "
                    f"year's data partway through the following year, so this year may not "
                    f"be available yet -- try an earlier year."
                ) from exc
            raise ValueError(f"Failed to download {url} (HTTP {exc.code}).") from exc
        # Validate the bytes we actually got, not just the HTTP status.
        if not self._looks_like_stats19_csv(tmp):
            os.remove(tmp)
            raise ValueError(
                f"Downloaded file from {url} is not a STATS19 CSV (the server may have "
                f"returned an error page instead of data). Nothing was cached."
            )
        os.replace(tmp, dest)   # atomic within the same directory; cache holds only valid files

    def extract(self, cache_dir, **kwargs):
        os.makedirs(cache_dir, exist_ok=True)
        self._years = [int(y) for y in (kwargs.get("years") or [])]
        self._boundary_mode = kwargs.get("boundary_mode", "snapshot")
        for year in self._years:
            for ftype in ("collision", "vehicle", "casualty"):
                path = os.path.join(cache_dir, self._filename(ftype, year))
                # Offline-friendly: if already cached (or test-seeded), skip download.
                if not os.path.exists(path):
                    url = f"{DFT_BASE_URL}/{self._filename(ftype, year)}"
                    self._fetch(url, path)   # validates + atomic-moves; raises ValueError if unavailable

    def _cached_files(self, cache_dir, ftype):
        """Cached CSV paths for one file type across the resolved years (existing only)."""
        paths = [os.path.join(cache_dir, self._filename(ftype, y)) for y in self._years]
        return [p for p in paths if os.path.exists(p)]

    def _load_bronze(self, con, bronze_table, files):
        """Faithful all-string bronze from one or more CSVs.

        read_csv with union_by_name lets historical (accident_*) and modern
        (collision_*) tranches coexist (absent columns become NULL); all_varchar
        preserves raw values exactly. Paths are cache-derived (trusted); values
        are never interpolated.
        """
        if not files:
            raise FileNotFoundError(
                f"[stats19] no cached CSVs for {bronze_table}; extract() must run "
                f"first (years={self._years}).")
        paths_sql = "[" + ", ".join(f"'{p}'" for p in files) + "]"
        con.execute(
            f"CREATE OR REPLACE TABLE {bronze_table} AS "
            f"SELECT * FROM read_csv({paths_sql}, union_by_name=true, all_varchar=true)"
        )

    def _load_codebook(self, con):
        """Load the committed codebook CSV into the `codebook` reference table.

        codebook(variable, code, label, is_missing) maps STATS19 integer codes to DfT
        labels and marks missing/unknown sentinels. Reference data, NOT an audited
        source. Read all-string then cast so `code` keeps '-1'/'07' exactly and
        is_missing is a real BOOLEAN. CREATE OR REPLACE keeps a same-file rebuild
        idempotent. Path is code-controlled (trusted); no row values are interpolated.
        """
        if not os.path.exists(_CODEBOOK_PATH):
            raise FileNotFoundError(
                f"[stats19] codebook reference file missing: {_CODEBOOK_PATH}. "
                f"It ships in the package under src/crossroads/reference/.")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CODEBOOK_TABLE} AS "
            f"SELECT CAST(variable AS VARCHAR) AS variable, "
            f"       CAST(code AS VARCHAR)     AS code, "
            f"       CAST(label AS VARCHAR)    AS label, "
            f"       CAST(is_missing AS BOOLEAN) AS is_missing "
            f"FROM read_csv('{_CODEBOOK_PATH}', header=true, all_varchar=true)")

    def _load_column_manifest(self, con):
        """Load the committed column manifest into the `column_manifest` reference table.

        column_manifest(tbl, col, kind, dtype) classifies EVERY column of every file:
        kind in {identity, geo, datetime, coded, numeric, text}; dtype is the target
        type for numeric/coded. Single source of truth for how the keep-in-place silver
        clean treats each column. Reference data, NOT an audited source. The CSV
        headers are `table,column,...`; alias them to tbl/col (both reserved-ish).
        """
        if not os.path.exists(_COLUMN_MANIFEST_PATH):
            raise FileNotFoundError(
                f"[stats19] column manifest missing: {_COLUMN_MANIFEST_PATH}. "
                f"It ships in the package under src/crossroads/reference/.")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.COLUMN_MANIFEST_TABLE} AS "
            f'SELECT CAST("table" AS VARCHAR)  AS tbl, '
            f'       CAST("column" AS VARCHAR) AS col, '
            f"       CAST(kind AS VARCHAR)     AS kind, "
            f"       CAST(dtype AS VARCHAR)    AS dtype "
            f"FROM read_csv('{_COLUMN_MANIFEST_PATH}', header=true, all_varchar=true)")

    def _coalesce_present(self, con, table, candidates, alias):
        """Build `COALESCE(<present candidates>) AS alias` over only columns that
        exist in `table` (else `NULL AS alias`). Handles the accident_*/collision_*
        rename without erroring on an absent column. Identifiers are code-controlled.
        """
        existing = {r[0].lower() for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall()}
        present = [c for c in candidates if c.lower() in existing]
        if not present:
            return f"NULL AS {alias}"
        if len(present) == 1:
            return f"{present[0]} AS {alias}"
        return "COALESCE(" + ", ".join(present) + f") AS {alias}"

    # --- broad keep-in-place clean: carry EVERY bronze column into silver ---
    # These sit next to _coalesce_present because they share the "only touch what's present,
    # from a trusted manifest" idea. Column/kind/dtype come from the committed manifest
    # (code-controlled), so interpolating them is safe; no row values are ever interpolated.
    def _bronze_columns(self, con, table):
        """Lower-cased set of column names present in a table (bronze or silver)."""
        return {r[0].lower() for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table]).fetchall()}

    def _clean_fragment(self, col, kind, dtype):
        """SELECT fragment that carries one bronze column into silver, cleaned per kind.

        coded   -> full missing set (codebook is_missing) -> NULL, else INTEGER code kept.
        numeric -> NUMERIC_SENTINELS/blank/non-numeric -> NULL, else typed to dtype
                   (this is the path longitude/latitude take, typed to DOUBLE).
        text    -> carried verbatim (free-text; normalization deferred).
        other   -> identity/any unrecognised kind carried raw (e.g. a child table's
                   accident_year/accident_reference, which no bespoke SELECT emits).
        The cleaned value stays a CODE/NUMBER, never a label; NULL means 'empty cell'.
        """
        if kind == "coded":
            # Null the codebook's is_missing codes, PLUS a bare -1. -1 is DfT's universal
            # "Data missing or out of range" sentinel, so every codebook-covered variable
            # already flags it; treating it as missing here too means a codebook gap (e.g.
            # enhanced_severity_collision, which has no codebook rows and is all -1 in 2023)
            # can't silently leak a -1 into a cleaned code. It never nulls a real value:
            # no STATS19 coded field uses -1 as a meaningful category.
            return (f"CASE WHEN {col} = '-1' OR {col} IN (SELECT code FROM {self.CODEBOOK_TABLE} "
                    f"WHERE variable = '{col}' AND is_missing) "
                    f"THEN NULL ELSE TRY_CAST({col} AS INTEGER) END AS {col}")
        if kind == "numeric":
            typ = dtype or "INTEGER"
            sent = ", ".join(f"'{s}'" for s in NUMERIC_SENTINELS)
            return (f"CASE WHEN {col} IN ({sent}) THEN NULL "
                    f"ELSE TRY_CAST({col} AS {typ}) END AS {col}")
        return f"{col} AS {col}"   # text/identity (and any unrecognised kind): carry raw

    def _placeholder_fragment(self, col, kind, dtype):
        """Typed NULL for a manifest column ABSENT from this bronze (a future-year drop or
        the pre/post-2024 rename), so silver's schema stays stable across year selections."""
        typ = "INTEGER" if kind == "coded" else (dtype or "INTEGER") if kind == "numeric" else "VARCHAR"
        return f"CAST(NULL AS {typ}) AS {col}"

    def _broad_fragments(self, con, bronze_table, table_kind, exclude):
        """Deterministic list of broad-clean SELECT fragments for one file.

        Reads column_manifest for `table_kind` and skips the columns the bespoke path
        already emits, listed in `exclude` -- the SINGLE authority for what bespoke owns
        (renamed or verbatim). Every other manifest column gets a cleaned fragment (or a
        typed NULL placeholder if absent from this bronze). Invariant: a column reaches
        silver via exactly ONE path -- bespoke (in `exclude`) OR broad. We do NOT filter
        by kind: doing so would silently drop columns no bespoke SELECT produces
        (longitude/latitude, and the child tables' accident_year/accident_reference).
        ORDER BY col keeps rebuilds structurally identical (spec §2).
        """
        present = self._bronze_columns(con, bronze_table)
        exclude = {c.lower() for c in exclude}
        rows = con.execute(
            f"SELECT col, kind, dtype FROM {self.COLUMN_MANIFEST_TABLE} "
            f"WHERE tbl = ? ORDER BY col", [table_kind]).fetchall()
        frags = []
        for col, kind, dtype in rows:
            if col.lower() in exclude:
                continue
            frags.append(self._clean_fragment(col, kind, dtype) if col.lower() in present
                         else self._placeholder_fragment(col, kind, dtype))
        return frags

    # --- CORE severity audit: promote the two headline severity outcomes out
    # of the broad loop into the same formal audit geom/datetime/link already have. ---
    def _core_severity_fragments(self, con, bronze_table, column, aliases):
        """Return (raw_expr, cleaned_expr, valid_expr) for a CORE severity column.

        Same missing-set semantics as the broad coded clean (_clean_fragment): a bare -1
        (DfT's universal "missing" sentinel, guarded even when the codebook omits it for
        this variable) OR any codebook is_missing code -> NULL, else the code kept as
        INTEGER. Keeping the -1 guard means a codebook gap can never leak a raw -1 into the
        cleaned severity. PLUS a <col>_raw twin (the raw code inline, for the ledger) and a
        <col>_valid flag. `aliases` coalesces the pre/post-2024 rename over the present
        columns (e.g. legacy accident_severity -> collision_severity). column/aliases are
        trusted constants (interpolated); no row values are interpolated.

        Graceful degradation: if NO alias is present (the tranche dropped the column
        entirely), carry stable typed NULLs with <col>_valid = TRUE — an absent column is
        not a per-row rejection, so the reject gate and ledger stay clean, matching how the
        broad placeholder and _coalesce_present treat an absent column. Real STATS19 always
        carries both severities, so this branch only guards synthetic/future-drop inputs.
        """
        present = [a for a in aliases if a.lower() in self._bronze_columns(con, bronze_table)]
        if not present:
            warnings.warn(
                f"stats19: {column} absent from {bronze_table}; carried as NULL with "
                f"{column}_valid = TRUE (no rejections logged).", stacklevel=2)
            return (f"CAST(NULL AS VARCHAR) AS {column}_raw",
                    f"CAST(NULL AS INTEGER) AS {column}",
                    f"TRUE AS {column}_valid")
        raw = self._coalesce_present(con, bronze_table, present, f"{column}_raw")
        raw_ex = raw.replace(f" AS {column}_raw", "")        # bare expression, no alias
        cleaned = (f"CASE WHEN ({raw_ex}) = '-1' OR ({raw_ex}) IN (SELECT code FROM {self.CODEBOOK_TABLE} "
                   f"WHERE variable = '{column}' AND is_missing) "
                   f"THEN NULL ELSE TRY_CAST(({raw_ex}) AS INTEGER) END")
        return (f"({raw_ex}) AS {column}_raw",
                f"{cleaned} AS {column}",
                f"({cleaned}) IS NOT NULL AS {column}_valid")

    def _log_missing_codes(self, con, silver_table, source_id, column, rule_id):
        """One reject_dimension ledger row per <column>_valid = FALSE row, so flag/ledger
        agreement holds for the CORE severity field. Aggregate scan + a bounded Python loop
        over the FALSE set (mirrors _log_orphans and the geom/datetime ledger writes)."""
        bad = con.execute(
            f"SELECT source_row_key, {column}_raw FROM {silver_table} "
            f"WHERE {column}_valid = FALSE").fetchall()
        for key, raw in bad:
            log_exclusion(
                con, source_id=source_id, source_row_key=key,
                column_name=column, rule_id=rule_id,
                rule_desc="severity code is a missing/unknown sentinel or unparseable",
                severity="reject_dimension", raw_value=str(raw))

    # --- completeness report (broad "how complete is column X?" per cleaned column) ---
    def _ensure_completeness_table(self, con):
        """Create the completeness report table if absent (idempotent). stats19-owned
        report table, NOT an audited source. One row per cleaned column per source.
        missing_rate is n_missing/n_total (0..1)."""
        con.execute(
            f"CREATE TABLE IF NOT EXISTS {self.COMPLETENESS_TABLE} ("
            f"  source_id VARCHAR, column_name VARCHAR, kind VARCHAR, "
            f"  n_total BIGINT, n_present BIGINT, n_missing BIGINT, missing_rate DOUBLE)")

    def _write_completeness(self, con, silver_table, source_id, table_kind):
        """Write one completeness row per cleaned column (kind coded|numeric) of one file.

        A cleaned coded/numeric column is NULL exactly for its missing/unparseable values,
        so count(col) (which ignores NULL) is the present count and count(*) - count(col)
        is the missing count. Reads column_manifest for the cleaned columns present in the
        silver table, runs a SINGLE aggregate scan (count(*) + count(col) per column), then
        a bounded Python loop inserts ~one row per column. Idempotent per source: existing
        rows for this source_id are cleared first. Column identifiers come from the trusted
        manifest (interpolated); counts/values are bound with ?."""
        silver_cols = self._bronze_columns(con, silver_table)   # reuse the info_schema helper
        cols = [(c, k) for c, k in con.execute(
            f"SELECT col, kind FROM {self.COLUMN_MANIFEST_TABLE} "
            f"WHERE tbl = ? AND kind IN ('coded','numeric') ORDER BY col", [table_kind]).fetchall()
            if c.lower() in silver_cols]
        con.execute(f"DELETE FROM {self.COMPLETENESS_TABLE} WHERE source_id = ?", [source_id])
        if not cols:
            return
        # One scan: total row count plus a NULL-ignoring count per cleaned column.
        selects = ["count(*) AS n_total"] + [f"count({c}) AS present_{i}"
                                             for i, (c, _) in enumerate(cols)]
        agg = con.execute(f"SELECT {', '.join(selects)} FROM {silver_table}").fetchone()
        n_total = agg[0]
        for i, (col, kind) in enumerate(cols):
            n_present = agg[i + 1]
            n_missing = n_total - n_present
            rate = (n_missing / n_total) if n_total else 0.0
            con.execute(
                f"INSERT INTO {self.COMPLETENESS_TABLE} "
                f"(source_id, column_name, kind, n_total, n_present, n_missing, missing_rate) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                [source_id, col, kind, n_total, n_present, n_missing, rate])

    def transform_and_load(self, con, cache_dir):
        years = getattr(self, "_years", None) or []
        if not years:
            return   # defensive: is_active gates on years, so this is unreachable in practice

        # --- REFERENCE: load the codebook + column manifest before any silver clean, so
        # every Stage-06+ consumer finds them. Static lookups, not audited sources. ---
        self._load_codebook(con)
        self._load_column_manifest(con)

        # --- BRONZE (×3): faithful copies; record rows read for conservation. ---
        for sid, bronze, ftype in (
            (self.COLLISION_SID, self.COLLISION_BRONZE, "collision"),
            (self.VEHICLE_SID, self.VEHICLE_BRONZE, "vehicle"),
            (self.CASUALTY_SID, self.CASUALTY_BRONZE, "casualty"),
        ):
            self._load_bronze(con, bronze, self._cached_files(cache_dir, ftype))
            n = con.execute(f"SELECT count(*) FROM {bronze}").fetchone()[0]
            record_source_rows(con, sid, n)

        # --- SILVER (×3): keep-in-place 1:1. Collision silver must be derived FIRST —
        # vehicle/casualty silver compute link_valid by joining to the collisions table. ---
        self._derive_collision_silver(con)
        self._derive_vehicle_silver(con)
        self._derive_casualty_silver(con)

        # --- COMPLETENESS: one queryable row per cleaned column per source. Rigour lives in
        # the formal dimensions; this is the broad "how complete is column X?" report. ---
        self._ensure_completeness_table(con)
        for silver, sid, kind in (
            (self.COLLISION_SILVER, self.COLLISION_SID, "collision"),
            (self.VEHICLE_SILVER,   self.VEHICLE_SID,   "vehicle"),
            (self.CASUALTY_SILVER,  self.CASUALTY_SID,  "casualty"),
        ):
            self._write_completeness(con, silver, sid, kind)

        # --- GOLD: valid-link projections (spec §9 clean views). ---
        create_clean_view(con, "vehicles_clean", self.VEHICLE_SILVER, ["link_valid"])
        create_clean_view(con, "casualties_clean", self.CASUALTY_SILVER, ["link_valid"])

        # --- SPATIAL STAMP: valid collision points -> LAD/CTYUA codes. ---
        self._spatial_stamp(con)

        # --- WEATHER STAMP (optional): fill temperature_c/precipitation_mm from the
        # weather grid if it was built this run (the registry orders weather first).
        self._weather_stamp(con)

        # --- SOLAR STAMP: fill solar_elevation_deg/solar_azimuth_deg for every collision
        # with a geom + datetime, computed mathematically (NOAA). Always-on, no download.
        self._solar_stamp(con)

        # --- BANK-HOLIDAY STAMP (optional): fill is_bank_holiday from the bank_holidays
        # dimension if it was built this run. Needs lad_code (set by _spatial_stamp above).
        self._bank_holiday_stamp(con)

        # --- GOLD: the valid-geometry collision projection (spec §9 worked example). ---
        create_clean_view(con, "collisions_spatial", self.COLLISION_SILVER, ["geom_valid"])

        # --- INDEX: R-Tree on collision geometry for downstream spatial queries.
        # Built AFTER the stamp UPDATE so the index is not maintained during the update.
        # collisions was CREATE OR REPLACE'd (dropping any prior index); the DROP is
        # belt-and-suspenders. NULL geom rows are skipped by the RTREE without error.
        con.execute("DROP INDEX IF EXISTS collisions_geom_rtree")
        con.execute(
            f"CREATE INDEX collisions_geom_rtree ON {self.COLLISION_SILVER} USING RTREE (geom)")

        # --- LABELS (opt-in, NEVER stored): code->label views alongside the coded tables. ---
        self._create_labelled_views(con)

    # --- silver derivations (factored so tests can drive them on a synthetic bronze) ---
    def _derive_collision_silver(self, con):
        """Collision silver: FULL keep-in-place. Existing identity/geom/datetime/lad/ctyua
        logic UNCHANGED (typed coordinates, an EPSG:27700 geom point, a naive local
        datetime, the ledger rows); PLUS every remaining bronze column carried + cleaned
        per the column manifest (coded/numeric missing set -> NULL + typed; text raw).
        Codes kept, never labelled. Bad values are flagged + logged, never dropped (§9)."""
        # accident_reference candidates include collision_ref_no: verified against
        # live 2020-2024 DfT files, whose reference column is named collision_ref_no
        # (not collision_reference as the older accident_* convention would suggest).
        acc = self._coalesce_present(con, self.COLLISION_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        yr = self._coalesce_present(con, self.COLLISION_BRONZE,
                                    ["collision_year", "accident_year"], "accident_year")
        ref = self._coalesce_present(con, self.COLLISION_BRONZE,
                                     ["collision_ref_no", "collision_reference", "accident_reference"],
                                     "accident_reference")
        # accident_index doubles as the source_row_key (globally unique).
        idx_expr = acc.replace(" AS accident_index", "")

        sentinels_sql = ", ".join(f"'{s}'" for s in COORD_SENTINELS)

        # Columns the bespoke logic consumes -> keep OUT of the broad loop. collision_severity
        # (and its legacy name accident_severity) is now CORE-audited below, so it is excluded
        # here too — otherwise it would be defined twice (SQL duplicate column).
        # longitude/latitude are NOT here -> they fall to the broad loop and are carried as
        # DOUBLE (manifest geo->numeric). Only the OSGR easting/northing, which bespoke turns
        # into easting/northing/geom, are excluded.
        exclude = {"accident_index", "collision_index", "accident_year", "collision_year",
                   "accident_reference", "collision_reference", "collision_ref_no",
                   "location_easting_osgr", "location_northing_osgr", "date", "time",
                   "collision_severity", "accident_severity"}
        # CORE severity audit: raw twin + cleaned INTEGER + valid flag.
        sev_raw, sev_clean, sev_valid = self._core_severity_fragments(
            con, self.COLLISION_BRONZE, "collision_severity",
            ["collision_severity", "accident_severity"])
        broad = self._broad_fragments(con, self.COLLISION_BRONZE, "collision", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""

        # Two-level select: the inner CTE types the coordinates and parses dates (SQL
        # cannot reference a sibling alias in the same SELECT list), the outer builds
        # geom / datetime / flags from those typed values. OSGR eastings/northings ARE
        # EPSG:27700, so ST_Point casts them directly — no reprojection (spec §3A).
        # A coordinate that is a sentinel ('-1'/'0'), blank, or non-numeric becomes
        # NULL (TRY_CAST) -> geom NULL -> geom_valid FALSE. date is DfT 'DD/MM/YYYY',
        # time 'HH:MM' (may be blank); a missing time falls back to midnight and is not
        # a rejection — only an unparseable DATE nulls datetime_local.
        con.execute(
            f"CREATE OR REPLACE TABLE {self.COLLISION_SILVER} AS "
            f"WITH typed AS ("
            f"  SELECT *, "                        # expose every raw bronze column to the broad fragments
            f"    ({idx_expr}) AS source_row_key, {acc}, {yr}, {ref}, "
            f"    location_easting_osgr  AS easting_raw, "
            f"    location_northing_osgr AS northing_raw, "
            f"    CASE WHEN location_easting_osgr IN ({sentinels_sql}, '') THEN NULL "
            f"         ELSE TRY_CAST(location_easting_osgr AS DOUBLE) END AS easting, "
            f"    CASE WHEN location_northing_osgr IN ({sentinels_sql}, '') THEN NULL "
            f"         ELSE TRY_CAST(location_northing_osgr AS DOUBLE) END AS northing, "
            f"    date AS date_raw, time AS time_raw, "
            f"    TRY_STRPTIME(date, '%d/%m/%Y') AS date_parsed, "
            f"    TRY_STRPTIME(date || ' ' || time, '%d/%m/%Y %H:%M') AS datetime_parsed "
            f"  FROM {self.COLLISION_BRONZE}"
            f") "
            f"SELECT "
            f"  source_row_key, accident_index, accident_year, accident_reference, "
            f"  easting_raw, northing_raw, easting, northing, "
            f"  CASE WHEN easting IS NULL OR northing IS NULL THEN NULL "
            f"       ELSE ST_Point(easting, northing)::GEOMETRY END AS geom, "
            f"  (easting IS NOT NULL AND northing IS NOT NULL) AS geom_valid, "
            f"  date_raw, time_raw, "
            # Prefer the full datetime; fall back to midnight when only the date parsed.
            f"  COALESCE(datetime_parsed, date_parsed) AS datetime_local, "
            f"  (date_parsed IS NOT NULL) AS datetime_valid, "
            # Filled by the spatial stamp; present now so the schema is stable.
            f"  CAST(NULL AS VARCHAR) AS lad_code, "
            f"  CAST(NULL AS VARCHAR) AS ctyua_code, "
            # Filled by _weather_stamp when a weather table exists; NULL otherwise
            # (mirrors lad_code — collisions always carry these columns). DOUBLE:
            # Celsius and millimetres.
            f"  CAST(NULL AS DOUBLE) AS temperature_c, "
            f"  CAST(NULL AS DOUBLE) AS precipitation_mm, "
            # Filled by _solar_stamp: the sun's apparent elevation and azimuth at the
            # collision's place/time, computed mathematically (NOAA solar position). NULL
            # until stamped, and left NULL where geom or datetime_local is missing. DOUBLE
            # degrees: elevation above horizon (refraction-corrected, negative = night),
            # azimuth clockwise from true north (0=N/90=E/180=S/270=W).
            f"  CAST(NULL AS DOUBLE) AS solar_elevation_deg, "
            f"  CAST(NULL AS DOUBLE) AS solar_azimuth_deg, "
            # Filled by _bank_holiday_stamp when a bank_holidays table exists: TRUE if the
            # collision's date is a bank holiday in its nation, FALSE if a known non-holiday
            # in-coverage, NULL if unknown (no/unknown nation, no date, or date outside the
            # feed's coverage for that nation). NULL is a first-class "unknown", not a reject.
            f"  CAST(NULL AS BOOLEAN) AS is_bank_holiday, "
            # CORE severity audit: raw twin, cleaned INTEGER, valid flag.
            f"  {sev_raw}, {sev_clean}, {sev_valid} "
            f"  {broad_sql} "                       # every remaining bronze column, cleaned per manifest
            f"FROM typed"
        )

        # --- LEDGER: one reject_dimension row per FALSE flag, so flag/ledger agreement
        # holds. Aggregate scan + a small Python loop over the (bounded) FALSE rows.
        bad_geom = con.execute(
            f"SELECT source_row_key, easting_raw, northing_raw FROM {self.COLLISION_SILVER} "
            f"WHERE geom_valid = FALSE").fetchall()
        for key, e, n in bad_geom:
            log_exclusion(
                con, source_id=self.COLLISION_SID, source_row_key=key,
                column_name="geom", rule_id=self.COORD_RULE,
                rule_desc="easting/northing missing or out of range "
                          "(sentinel -1/0, blank, or non-numeric)",
                severity="reject_dimension", raw_value=f"{e},{n}")

        bad_dt = con.execute(
            f"SELECT source_row_key, date_raw FROM {self.COLLISION_SILVER} "
            f"WHERE datetime_valid = FALSE").fetchall()
        for key, d in bad_dt:
            log_exclusion(
                con, source_id=self.COLLISION_SID, source_row_key=key,
                column_name="datetime_local", rule_id=self.DATETIME_RULE,
                rule_desc="collision date is missing or unparseable",
                severity="reject_dimension", raw_value=str(d))

        # CORE severity ledger: one row per collision_severity_valid = FALSE.
        self._log_missing_codes(con, self.COLLISION_SILVER, self.COLLISION_SID,
                                "collision_severity", self.COLLISION_SEVERITY_RULE)

    def _derive_vehicle_silver(self, con):
        """Vehicle silver: FULL keep-in-place. Existing identity + link_valid UNCHANGED;
        PLUS every remaining bronze column carried + cleaned per the manifest. A vehicle
        whose accident_index has no matching collision is flagged link_valid = FALSE and
        logged (orphan), never dropped (spec §9)."""
        acc = self._coalesce_present(con, self.VEHICLE_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx_expr = acc.replace(" AS accident_index", "")
        # Exclude ONLY the columns the bespoke SELECT below actually emits: accident_index
        # (coalesced from either name) + vehicle_reference. accident_year/accident_reference
        # are NOT bespoke-produced here, so they fall to the broad loop and are carried
        # (carried raw, matching how collision carries them) -- otherwise they'd vanish.
        exclude = {"accident_index", "collision_index", "vehicle_reference"}
        broad = self._broad_fragments(con, self.VEHICLE_BRONZE, "vehicle", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.VEHICLE_SILVER} AS "
            f"SELECT ({idx_expr}) || '|' || vehicle_reference AS source_row_key, "
            f"       {acc}, vehicle_reference, "
            f"       (({idx_expr}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) "
            f"         AS link_valid "
            f"       {broad_sql} "
            f"FROM {self.VEHICLE_BRONZE}"
        )
        self._log_orphans(con, self.VEHICLE_SILVER, self.VEHICLE_SID, self.VEHICLE_LINK_RULE)

    def _derive_casualty_silver(self, con):
        """Casualty silver: FULL keep-in-place. Existing identity + link_valid UNCHANGED;
        PLUS every remaining bronze column carried + cleaned per the manifest. casualty_severity
        is CORE-audited (raw twin + cleaned INTEGER + valid flag + ledger), so it is
        carved out of the broad loop. Linked to collisions by accident_index (also carries
        vehicle_reference for a finer casualty-to-vehicle link). Orphans are flagged
        link_valid = FALSE + logged (spec §9)."""
        acc = self._coalesce_present(con, self.CASUALTY_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx_expr = acc.replace(" AS accident_index", "")
        # As in vehicles: exclude the bespoke-emitted keys (accident_index +
        # vehicle_reference + casualty_reference), PLUS casualty_severity (now CORE-audited
        # below, so it must not also be defined by the broad loop). accident_year/
        # accident_reference fall to the broad loop and are carried, never dropped.
        exclude = {"accident_index", "collision_index", "vehicle_reference",
                   "casualty_reference", "casualty_severity"}
        # CORE severity audit: raw twin + cleaned INTEGER + valid flag.
        sev_raw, sev_clean, sev_valid = self._core_severity_fragments(
            con, self.CASUALTY_BRONZE, "casualty_severity", ["casualty_severity"])
        broad = self._broad_fragments(con, self.CASUALTY_BRONZE, "casualty", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CASUALTY_SILVER} AS "
            f"SELECT ({idx_expr}) || '|' || vehicle_reference || '|' || casualty_reference "
            f"         AS source_row_key, "
            f"       {acc}, vehicle_reference, casualty_reference, "
            f"       (({idx_expr}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) "
            f"         AS link_valid, "
            # CORE severity audit: raw twin, cleaned INTEGER, valid flag.
            f"       {sev_raw}, {sev_clean}, {sev_valid} "
            f"       {broad_sql} "
            f"FROM {self.CASUALTY_BRONZE}"
        )
        self._log_orphans(con, self.CASUALTY_SILVER, self.CASUALTY_SID, self.CASUALTY_LINK_RULE)
        # CORE severity ledger: one row per casualty_severity_valid = FALSE.
        self._log_missing_codes(con, self.CASUALTY_SILVER, self.CASUALTY_SID,
                                "casualty_severity", self.CASUALTY_SEVERITY_RULE)

    def _log_orphans(self, con, silver_table, source_id, rule_id):
        """Write one reject_dimension ledger row per link_valid = FALSE row in
        silver_table, so flag/ledger agreement holds for the link dimension."""
        orphans = con.execute(
            f"SELECT source_row_key, accident_index FROM {silver_table} "
            f"WHERE link_valid = FALSE").fetchall()
        for key, acc_idx in orphans:
            log_exclusion(
                con, source_id=source_id, source_row_key=key,
                column_name="accident_index", rule_id=rule_id,
                rule_desc="accident_index has no matching collision row",
                severity="reject_dimension", raw_value=str(acc_idx))

    def _table_exists(self, con, name):
        return con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [name]).fetchone()[0] > 0

    def _boundary_predicate(self, mode):
        """Extra ON-clause for the point-in-polygon join.

        snapshot (default): only the current boundary vintage (valid_to IS NULL).
        temporal: the vintage whose [valid_from, valid_to) window contains the
                  incident date (CAST(datetime_local AS DATE)); a NULL datetime
                  matches nothing (cannot be placed in time) and is left unstamped.
        """
        if mode == "temporal":
            return ("AND b.valid_from <= CAST(c2.datetime_local AS DATE) "
                    "AND (b.valid_to IS NULL "
                    "     OR CAST(c2.datetime_local AS DATE) < b.valid_to)")
        return "AND b.valid_to IS NULL"

    def _spatial_stamp(self, con):
        """Stamp lad_code/ctyua_code onto valid collision points via point-in-polygon
        against the boundary silver tables. Defensive: if a boundary table is
        absent (e.g. a stats19-only build/test), leave that code NULL and warn — the
        pipeline still succeeds. ST_Contains needs the boundary R-Tree (built alongside
        the boundary silver tables) to stay fast (spec §5). area_code is aggregated with min() for a deterministic
        result even if polygons were to overlap (they should not within one vintage)."""
        mode = getattr(self, "_boundary_mode", "snapshot")
        pred = self._boundary_predicate(mode)
        for code_col, btable in (("lad_code", "lad_boundaries"),
                                 ("ctyua_code", "ctyua_boundaries")):
            if not self._table_exists(con, btable):
                warnings.warn(
                    f"stats19: boundary table {btable} not found; {code_col} left NULL "
                    f"(build boundaries alongside stats19 to enable the spatial join).",
                    stacklevel=2)
                continue
            con.execute(
                f"UPDATE {self.COLLISION_SILVER} AS c SET {code_col} = m.area_code "
                f"FROM ("
                f"  SELECT c2.source_row_key AS k, min(b.area_code) AS area_code "
                f"  FROM {self.COLLISION_SILVER} c2 JOIN {btable} b "
                f"    ON c2.geom IS NOT NULL AND b.geom_valid = TRUE "
                f"       AND ST_Contains(b.geom, c2.geom) {pred} "
                f"  GROUP BY c2.source_row_key"
                f") m WHERE c.source_row_key = m.k"
            )

    def _weather_stamp(self, con):
        """Optionally stamp temperature_c/precipitation_mm onto valid collisions from an
        already-built weather grid. The exact shape of _spatial_stamp: if the weather
        table is absent (weather not selected/built this run), warn and leave the columns
        NULL — collisions still build. This is the 'optional dependency' guard at ETL time
        (the registry has already ordered weather before stats19 when both are active).

        Match (spec §3A/§3B): reproject each collision point back to lon/lat, round to the
        0.1° ERA5-Land grid index (grid_i, grid_j), and match the weather cell with the same
        index AND the same UK-local hour (weather.valid_time_local is pre-materialised, so
        no ICU is needed here). weather is unique per (cell, hour), so min() aggregates over
        at most one row — the same defensive GROUP BY _spatial_stamp uses for area_code."""
        if not self._table_exists(con, "weather"):
            warnings.warn(
                "stats19: weather table not found; temperature_c/precipitation_mm left NULL "
                "(build the weather dataset alongside stats19 to enable weather stamping).",
                stacklevel=2)
            return
        con.execute(
            f"UPDATE {self.COLLISION_SILVER} AS c "
            f"SET temperature_c = m.temperature_c, precipitation_mm = m.precipitation_mm "
            f"FROM ("
            f"  SELECT k, min(temperature_c) AS temperature_c, "
            f"         min(precipitation_mm) AS precipitation_mm "
            f"  FROM ("
            f"    SELECT c2.source_row_key AS k, w.temperature_c, w.precipitation_mm "
            f"    FROM ("
            f"      SELECT source_row_key, datetime_local, "
            f"             ST_Transform(geom, 'EPSG:27700', 'EPSG:4326', always_xy := true) AS ll "
            f"      FROM {self.COLLISION_SILVER} "
            f"      WHERE geom IS NOT NULL AND datetime_local IS NOT NULL"
            f"    ) c2 "
            f"    JOIN weather w "
            f"      ON w.grid_i = CAST(round(ST_Y(c2.ll) * 10) AS INTEGER) "
            f"     AND w.grid_j = CAST(round(ST_X(c2.ll) * 10) AS INTEGER) "
            f"     AND date_trunc('hour', w.valid_time_local) = date_trunc('hour', c2.datetime_local)"
            f"  ) j "
            f"  GROUP BY k"
            f") m WHERE c.source_row_key = m.k"
        )

    def _bank_holiday_stamp(self, con):
        """Stamp is_bank_holiday onto collisions from the bank_holidays dimension.

        Tri-state, per the locked requirement — "known not a holiday" must be distinct
        from "no data":
          • NULL  if the nation can't be determined (no/unknown lad_code), the date didn't
                  parse (datetime_local NULL), or the date is OUTSIDE the feed's coverage
                  for that nation.
          • TRUE  if the date is a bank holiday in that nation.
          • FALSE only if the date is within coverage for that nation and is not a holiday.

        Nation comes from the ONS LAD code prefix (a stable GSS convention):
          E…/W… -> england-and-wales   S… -> scotland   N… -> northern-ireland   else NULL.
        Coverage is per division: the [min(year), max(year)] span of that division's events
        in the feed (the feed publishes contiguous whole years). Set-based UPDATE, no loop.

        Guarded like _weather_stamp: if bank_holidays was not built this run (source not
        selected), warn and leave the column NULL — collisions still build."""
        if not self._table_exists(con, "bank_holidays"):
            warnings.warn(
                "stats19: bank_holidays table not found; is_bank_holiday left NULL "
                "(build the bank_holidays dataset alongside stats19 to enable the flag).",
                stacklevel=2)
            return
        con.execute(
            f"UPDATE {self.COLLISION_SILVER} AS c SET is_bank_holiday = m.val "
            f"FROM ("
            f"  WITH cov AS ("                              # per-division coverage years
            f"    SELECT division, min(year(date)) AS min_y, max(year(date)) AS max_y "
            f"    FROM bank_holidays GROUP BY division"
            f"  ), cold AS ("                               # each collision -> its nation + date
            f"    SELECT source_row_key AS k, "
            f"           CAST(datetime_local AS DATE) AS cdate, "
            f"           CASE "
            f"             WHEN lad_code LIKE 'E%' OR lad_code LIKE 'W%' THEN 'england-and-wales' "
            f"             WHEN lad_code LIKE 'S%' THEN 'scotland' "
            f"             WHEN lad_code LIKE 'N%' THEN 'northern-ireland' "
            f"             ELSE NULL END AS division "
            f"    FROM {self.COLLISION_SILVER}"
            f"  ) "
            f"  SELECT cold.k AS k, "
            f"    CASE "
            f"      WHEN cold.division IS NULL THEN NULL "                 # unknown nation
            f"      WHEN cold.cdate IS NULL THEN NULL "                    # no parsed date
            f"      WHEN cov.min_y IS NULL THEN NULL "                     # nation absent from feed
            f"      WHEN year(cold.cdate) NOT BETWEEN cov.min_y AND cov.max_y THEN NULL "  # out of coverage
            f"      WHEN EXISTS (SELECT 1 FROM bank_holidays bh "
            f"                   WHERE bh.division = cold.division AND bh.date = cold.cdate) THEN TRUE "
            f"      ELSE FALSE "                                           # in coverage, not a holiday
            f"    END AS val "
            f"  FROM cold LEFT JOIN cov ON cov.division = cold.division"
            f") m WHERE c.source_row_key = m.k"
        )

    def _solar_stamp(self, con):
        """Stamp solar_elevation_deg / solar_azimuth_deg onto every collision that has a
        geometry AND a parsed local datetime, computed mathematically (NOAA solar position
        algorithm) — no download, no new dependency, all in-database (spec §3A).

        Position: reproject the EPSG:27700 geom back to lon/lat (same call _weather_stamp
        uses). Instant: epoch(datetime_local AT TIME ZONE 'Europe/London') gives the true
        UTC instant in seconds — ICU resolves the Europe/London offset (GMT/BST), the same
        engine the weather source uses for valid_time_local. Only the resulting ANGLES are
        stored; no *_utc column is ever materialised (spec §2 keeps local-native sources
        free of reconstructed UTC instants). Rows without geom or datetime_local stay NULL —
        they inherit the already-audited geom_valid / datetime_valid flags, so this stamp
        adds no new quality dimension and no ledger rows.

        ICU is loaded here (idempotent) because a STATS19-only build may not have loaded it.
        acos arguments are clamped to [-1, 1] to absorb floating-point error near the horizon.
        The UPDATE is set-based over the whole table (no Python row loop).

        The CTE chain implements the standard NOAA solar-position math, split so each SELECT
        only references the previous CTE's columns (SQL cannot see sibling aliases): Julian
        century (jc) and UTC minutes-of-day from the instant; the sun's geometric mean
        longitude/anomaly, equation of centre, apparent longitude and obliquity; then
        declination + equation of time; then true solar time -> hour angle -> zenith; and
        finally the refraction-corrected elevation and the azimuth clockwise from north."""
        con.execute("INSTALL icu"); con.execute("LOAD icu")   # for AT TIME ZONE below
        con.execute(
            f"UPDATE {self.COLLISION_SILVER} AS c "
            f"SET solar_elevation_deg = m.elevation, solar_azimuth_deg = m.azimuth "
            f"FROM ("
            f"  WITH base AS ("
            f"    SELECT source_row_key AS k, "
            f"           ST_X(ll) AS lon, ST_Y(ll) AS lat, "
            f"           epoch(datetime_local AT TIME ZONE 'Europe/London') AS es "
            f"    FROM ("
            f"      SELECT source_row_key, datetime_local, "
            f"             ST_Transform(geom, 'EPSG:27700', 'EPSG:4326', always_xy := true) AS ll "
            f"      FROM {self.COLLISION_SILVER} "
            f"      WHERE geom IS NOT NULL AND datetime_local IS NOT NULL"
            f"    )"
            f"  ), astro AS ("      # date-only astronomical terms (independent of lat/lon)
            f"    SELECT k, lon, lat, mod(es, 86400) / 60.0 AS utc_min, "
            f"           (es / 86400.0 + 2440587.5 - 2451545.0) / 36525.0 AS jc "
            f"    FROM base"
            f"  ), sun AS ("
            f"    SELECT k, lon, lat, utc_min, jc, "
            f"           mod(280.46646 + jc*(36000.76983 + jc*0.0003032), 360) AS l0, "
            f"           357.52911 + jc*(35999.05029 - 0.0001537*jc) AS m, "
            f"           0.016708634 - jc*(0.000042037 + 0.0000001267*jc) AS ecc "
            f"    FROM astro"
            f"  ), sun2 AS ("
            f"    SELECT k, lon, lat, utc_min, jc, l0, m, ecc, "
            f"           sin(radians(m))*(1.914602 - jc*(0.004817 + 0.000014*jc)) "
            f"           + sin(radians(2*m))*(0.019993 - 0.000101*jc) "
            f"           + sin(radians(3*m))*0.000289 AS c "
            f"    FROM sun"
            f"  ), sun3 AS ("
            f"    SELECT k, lon, lat, utc_min, jc, l0, m, ecc, "
            f"           (l0 + c) - 0.00569 - 0.00478*sin(radians(125.04 - 1934.136*jc)) AS app_long, "
            f"           23 + (26 + (21.448 - jc*(46.815 + jc*(0.00059 - jc*0.001813)))/60)/60 "
            f"           + 0.00256*cos(radians(125.04 - 1934.136*jc)) AS obliq "
            f"    FROM sun2"
            f"  ), terms AS ("
            f"    SELECT k, lon, lat, utc_min, l0, m, ecc, "
            f"           degrees(asin(sin(radians(obliq))*sin(radians(app_long)))) AS declin, "
            f"           tan(radians(obliq/2)) * tan(radians(obliq/2)) AS vy "
            f"    FROM sun3"
            f"  ), solartime AS ("
            f"    SELECT k, lon, lat, declin, "
            f"           mod(utc_min "
            f"               + 4*degrees(vy*sin(2*radians(l0)) - 2*ecc*sin(radians(m)) "
            f"                 + 4*ecc*vy*sin(radians(m))*cos(2*radians(l0)) "
            f"                 - 0.5*vy*vy*sin(4*radians(l0)) - 1.25*ecc*ecc*sin(2*radians(m))) "
            f"               + 4*lon, 1440) AS tst "
            f"    FROM terms"
            f"  ), angles AS ("
            f"    SELECT k, lat, declin, "
            f"           CASE WHEN tst/4 < 0 THEN tst/4 + 180 ELSE tst/4 - 180 END AS ha "
            f"    FROM solartime"
            f"  ), zen AS ("
            f"    SELECT k, lat, declin, ha, "
            f"           degrees(acos(greatest(-1, least(1, "
            f"             sin(radians(lat))*sin(radians(declin)) "
            f"             + cos(radians(lat))*cos(radians(declin))*cos(radians(ha)))))) AS zenith "
            f"    FROM angles"
            f"  ) "
            f"  SELECT k, "
            f"    (90 - zenith) + CASE "
            f"       WHEN (90 - zenith) > 85     THEN 0 "
            f"       WHEN (90 - zenith) > 5      THEN (58.1/tan(radians(90 - zenith)) "
            f"           - 0.07/pow(tan(radians(90 - zenith)),3) "
            f"           + 0.000086/pow(tan(radians(90 - zenith)),5))/3600 "
            f"       WHEN (90 - zenith) > -0.575 THEN (1735 + (90 - zenith)*(-518.2 + (90 - zenith)*(103.4 "
            f"           + (90 - zenith)*(-12.79 + (90 - zenith)*0.711))))/3600 "
            f"       ELSE (-20.772/tan(radians(90 - zenith)))/3600 END AS elevation, "
            f"    CASE WHEN ha > 0 "
            f"      THEN mod(degrees(acos(greatest(-1, least(1, "
            f"           (sin(radians(lat))*cos(radians(zenith)) - sin(radians(declin))) "
            f"           / (cos(radians(lat))*sin(radians(zenith))))))) + 180, 360) "
            f"      ELSE mod(540 - degrees(acos(greatest(-1, least(1, "
            f"           (sin(radians(lat))*cos(radians(zenith)) - sin(radians(declin))) "
            f"           / (cos(radians(lat))*sin(radians(zenith))))))), 360) END AS azimuth "
            f"  FROM zen"
            f") m WHERE c.source_row_key = m.k"
        )

    def _create_labelled_views(self, con):
        """Opt-in code->label translation, at full breadth. Labels are NEVER stored: each
        *_labelled view joins the codebook to expose a <col>_label for every coded column
        ALONGSIDE the canonical coded silver table. Default surface = the coded table;
        'translation off' = query the silver table. No global flag, no stored label column.

        One LEFT JOIN per coded column, aliased, ON cb.variable='<col>' AND
        cb.code = CAST(s.<col> AS VARCHAR). codebook is unique on (variable, code) and each
        join filters variable, so joins are 1:1 -> the view row count == silver row count.
        Column/variable identifiers come from the trusted manifest (interpolated); no row
        values are interpolated. Views are lazy: the join cost is paid only when queried.

        After each view is built, REPORT (never halt) any undecoded codes -- a code that
        is present but whose label came back NULL -- in the codebook-COVERED coded columns,
        via warnings.warn. This is the build-time 'report loudly and continue': the build
        always succeeds, but a systematic decode gap (e.g. a future zero-padded code the
        INTEGER->VARCHAR join misses) or a stray junk code is surfaced, not silent. Uncovered
        columns (no codebook rows, e.g. enhanced_severity_collision) are skipped -- their
        NULL labels are expected. Mirrors the non-fatal warnings.warn already used in
        _spatial_stamp for a missing boundary table."""
        for silver, table_kind in ((self.COLLISION_SILVER, "collision"),
                                   (self.VEHICLE_SILVER,   "vehicle"),
                                   (self.CASUALTY_SILVER,  "casualty")):
            # Defensive: skip a silver table that doesn't exist (e.g. a focused unit test
            # that built only one table). A real build has all three. Mirrors the
            # missing-table guard in _spatial_stamp.
            if not self._table_exists(con, silver):
                continue
            silver_cols = self._bronze_columns(con, silver)     # reuse info_schema helper
            coded = [c for (c,) in con.execute(
                f"SELECT col FROM {self.COLUMN_MANIFEST_TABLE} "
                f"WHERE tbl = ? AND kind = 'coded' ORDER BY col", [table_kind]).fetchall()
                if c.lower() in silver_cols]
            selects, joins = ["s.*"], []
            for i, col in enumerate(coded):
                a = f"cb{i}"
                selects.append(f"{a}.label AS {col}_label")
                joins.append(
                    f"LEFT JOIN {self.CODEBOOK_TABLE} {a} "
                    f"  ON {a}.variable = '{col}' AND {a}.code = CAST(s.{col} AS VARCHAR)")
            con.execute(
                f"CREATE OR REPLACE VIEW {silver}_labelled AS "
                f"SELECT {', '.join(selects)} FROM {silver} s {' '.join(joins)}")

            # --- REPORT (non-fatal): warn on undecoded codes in COVERED columns. ---
            # A covered column has >=1 codebook row; only those are expected to decode.
            covered = [c for c in coded if con.execute(
                f"SELECT count(*) FROM {self.CODEBOOK_TABLE} WHERE variable = ?",
                [c]).fetchone()[0] > 0]
            if covered:
                # One scan of the view: an undecoded count per covered column.
                exprs = ", ".join(
                    f"count(*) FILTER (WHERE {c} IS NOT NULL AND {c}_label IS NULL)"
                    for c in covered)
                counts = con.execute(f"SELECT {exprs} FROM {silver}_labelled").fetchone()
                bad = [(c, n) for c, n in zip(covered, counts) if n]
                if bad:
                    warnings.warn(
                        f"stats19: {silver}_labelled has undecoded codes (code present but "
                        f"label NULL) in covered columns: "
                        + ", ".join(f"{c}={n}" for c, n in bad)
                        + ". Those rows show a blank label; check the codebook covers every "
                        f"code in use (e.g. a new or zero-padded code).", stacklevel=2)

    def quality_spec(self):
        # Three audit units. Collision declares geom/datetime + severity;
        # vehicle declares link; casualty declares link + severity.
        # Vehicle has no severity field, so it is unchanged.
        return (
            SourceQuality(
                self.COLLISION_SID, self.COLLISION_BRONZE, self.COLLISION_SILVER,
                dimensions=(
                    Dimension("geom", "geom_valid", (self.COORD_RULE,)),
                    Dimension("datetime", "datetime_valid", (self.DATETIME_RULE,)),
                    Dimension("severity", "collision_severity_valid",
                              (self.COLLISION_SEVERITY_RULE,)),
                ),
                key_column="source_row_key"),
            SourceQuality(
                self.VEHICLE_SID, self.VEHICLE_BRONZE, self.VEHICLE_SILVER,
                dimensions=(Dimension("link", "link_valid", (self.VEHICLE_LINK_RULE,)),),
                key_column="source_row_key"),
            SourceQuality(
                self.CASUALTY_SID, self.CASUALTY_BRONZE, self.CASUALTY_SILVER,
                dimensions=(
                    Dimension("link", "link_valid", (self.CASUALTY_LINK_RULE,)),
                    Dimension("severity", "casualty_severity_valid",
                              (self.CASUALTY_SEVERITY_RULE,)),
                ),
                key_column="source_row_key"),
        )
