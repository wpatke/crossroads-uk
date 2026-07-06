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
# geom_valid FALSE, logged, and the row is retained (never deleted). Used from
# Stage 02 onward; declared here as the shared constant.
COORD_SENTINELS = ("-1", "0")

# Sentinels for the broad numeric clean (Stage 06). A numeric column equal to any of
# these — or blank / non-numeric — becomes NULL. A coded column uses the codebook's
# is_missing set instead. (Note: DfT leaves missing longitude/latitude blank, so '' is
# the sentinel that fires in practice; '-1' is inert for those but harmless.)
NUMERIC_SENTINELS = ("-1", "")

# British National Grid envelope, for verifying geometry really is EPSG:27700 in tests.
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000

# Reference data (shared lookups), derived independently from DfT's published data guide
# (Road Safety Open Dataset Data Guide, 2024 edition, OGL v3.0) and committed under
# src/crossroads/reference/. They ship in the wheel like transformers/ons_boundaries.json
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

    # --- ledger rules for the collision dimensions (also referenced by quality_spec) ---
    COORD_RULE = "stats19.coord.sentinel"
    DATETIME_RULE = "stats19.datetime.invalid"

    # --- ledger rules for the vehicle/casualty link dimension ---
    VEHICLE_LINK_RULE = "stats19.link.orphan_vehicle"
    CASUALTY_LINK_RULE = "stats19.link.orphan_casualty"

    def is_active(self, **kwargs):
        # Nothing to ingest without years; a no-years build (e.g. boundary-only)
        # simply skips STATS19. A real build passes years (spec §8 target flow).
        return bool(kwargs.get("years"))

    def _filename(self, ftype, year):
        return _FILE_TEMPLATE.format(ftype=ftype, year=year)

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
                    urllib.request.urlretrieve(url, path)

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
        (Stage 06) treats each column. Reference data, NOT an audited source. The CSV
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

    # --- broad keep-in-place clean (Stage 06): carry EVERY bronze column into silver ---
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

        # --- GOLD: valid-link projections (spec §9 clean views). ---
        create_clean_view(con, "vehicles_clean", self.VEHICLE_SILVER, ["link_valid"])
        create_clean_view(con, "casualties_clean", self.CASUALTY_SILVER, ["link_valid"])

        # --- SPATIAL STAMP: valid collision points -> LAD/CTYUA codes. ---
        self._spatial_stamp(con)

        # --- GOLD: the valid-geometry collision projection (spec §9 worked example). ---
        create_clean_view(con, "collisions_spatial", self.COLLISION_SILVER, ["geom_valid"])

        # --- INDEX: R-Tree on collision geometry for downstream spatial queries.
        # Built AFTER the stamp UPDATE so the index is not maintained during the update.
        # collisions was CREATE OR REPLACE'd (dropping any prior index); the DROP is
        # belt-and-suspenders. NULL geom rows are skipped by the RTREE without error.
        con.execute("DROP INDEX IF EXISTS collisions_geom_rtree")
        con.execute(
            f"CREATE INDEX collisions_geom_rtree ON {self.COLLISION_SILVER} USING RTREE (geom)")

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
        # is broad-cleaned here as an ordinary coded column (Stage 07 promotes it later).
        # longitude/latitude are NOT here -> they fall to the broad loop and are carried as
        # DOUBLE (manifest geo->numeric). Only the OSGR easting/northing, which bespoke turns
        # into easting/northing/geom, are excluded.
        exclude = {"accident_index", "collision_index", "accident_year", "collision_year",
                   "accident_reference", "collision_reference", "collision_ref_no",
                   "location_easting_osgr", "location_northing_osgr", "date", "time"}
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
            # Filled by the Stage 04 spatial stamp; present now so the schema is stable.
            f"  CAST(NULL AS VARCHAR) AS lad_code, "
            f"  CAST(NULL AS VARCHAR) AS ctyua_code "
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
        is cleaned here as an ordinary coded column (Stage 07 promotes it later). Linked to
        collisions by accident_index (also carries vehicle_reference for a finer
        casualty-to-vehicle link). Orphans are flagged link_valid = FALSE + logged (spec §9)."""
        acc = self._coalesce_present(con, self.CASUALTY_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx_expr = acc.replace(" AS accident_index", "")
        # As in vehicles: exclude ONLY the bespoke-emitted keys (accident_index +
        # vehicle_reference + casualty_reference). accident_year/accident_reference fall to
        # the broad loop and are carried, never dropped.
        exclude = {"accident_index", "collision_index", "vehicle_reference", "casualty_reference"}
        broad = self._broad_fragments(con, self.CASUALTY_BRONZE, "casualty", exclude)
        broad_sql = (", " + ", ".join(broad)) if broad else ""
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CASUALTY_SILVER} AS "
            f"SELECT ({idx_expr}) || '|' || vehicle_reference || '|' || casualty_reference "
            f"         AS source_row_key, "
            f"       {acc}, vehicle_reference, casualty_reference, "
            f"       (({idx_expr}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) "
            f"         AS link_valid "
            f"       {broad_sql} "
            f"FROM {self.CASUALTY_BRONZE}"
        )
        self._log_orphans(con, self.CASUALTY_SILVER, self.CASUALTY_SID, self.CASUALTY_LINK_RULE)

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
        against the Step 3 boundary silver tables. Defensive: if a boundary table is
        absent (e.g. a stats19-only build/test), leave that code NULL and warn — the
        pipeline still succeeds. ST_Contains needs the boundary R-Tree (built in Step 3)
        to stay fast (spec §5). area_code is aggregated with min() for a deterministic
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

    def quality_spec(self):
        # Three audit units. Collision declares its geom/datetime dimensions
        # (Stage 02); vehicle/casualty declare their link dimension (Stage 03).
        return (
            SourceQuality(
                self.COLLISION_SID, self.COLLISION_BRONZE, self.COLLISION_SILVER,
                dimensions=(
                    Dimension("geom", "geom_valid", (self.COORD_RULE,)),
                    Dimension("datetime", "datetime_valid", (self.DATETIME_RULE,)),
                ),
                key_column="source_row_key"),
            SourceQuality(
                self.VEHICLE_SID, self.VEHICLE_BRONZE, self.VEHICLE_SILVER,
                dimensions=(Dimension("link", "link_valid", (self.VEHICLE_LINK_RULE,)),),
                key_column="source_row_key"),
            SourceQuality(
                self.CASUALTY_SID, self.CASUALTY_BRONZE, self.CASUALTY_SILVER,
                dimensions=(Dimension("link", "link_valid", (self.CASUALTY_LINK_RULE,)),),
                key_column="source_row_key"),
        )
