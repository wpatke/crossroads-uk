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

# British National Grid envelope, for verifying geometry really is EPSG:27700 in tests.
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000


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

    def transform_and_load(self, con, cache_dir):
        years = getattr(self, "_years", None) or []
        if not years:
            return   # defensive: is_active gates on years, so this is unreachable in practice

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

        # --- GOLD: valid-link projections (spec §9 clean views). collisions_spatial
        # is added in Stage 04 once lad_code/ctyua_code are stamped.
        create_clean_view(con, "vehicles_clean", self.VEHICLE_SILVER, ["link_valid"])
        create_clean_view(con, "casualties_clean", self.CASUALTY_SILVER, ["link_valid"])

    # --- silver derivations (factored so tests can drive them on a synthetic bronze) ---
    def _derive_collision_silver(self, con):
        """Collision silver: keep-in-place 1:1, with typed coordinates, an EPSG:27700
        geom point, a naive local datetime, and the ledger rows for missing/invalid
        values. Bad values are flagged + logged, never dropped (spec §9)."""
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
            f"  SELECT "
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
        """Vehicle silver: keep-in-place 1:1, linked to collisions by accident_index.
        A vehicle whose accident_index has no matching collision is flagged
        link_valid = FALSE and logged (orphan), never dropped (spec §9)."""
        acc = self._coalesce_present(con, self.VEHICLE_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx_expr = acc.replace(" AS accident_index", "")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.VEHICLE_SILVER} AS "
            f"SELECT ({idx_expr}) || '|' || vehicle_reference AS source_row_key, "
            f"       {acc}, vehicle_reference, "
            f"       (({idx_expr}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) "
            f"         AS link_valid "
            f"FROM {self.VEHICLE_BRONZE}"
        )
        self._log_orphans(con, self.VEHICLE_SILVER, self.VEHICLE_SID, self.VEHICLE_LINK_RULE)

    def _derive_casualty_silver(self, con):
        """Casualty silver: keep-in-place 1:1, linked to collisions by accident_index
        (also carries vehicle_reference for a finer casualty-to-vehicle link). Orphans
        are flagged link_valid = FALSE and logged, never dropped (spec §9)."""
        acc = self._coalesce_present(con, self.CASUALTY_BRONZE,
                                     ["collision_index", "accident_index"], "accident_index")
        idx_expr = acc.replace(" AS accident_index", "")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.CASUALTY_SILVER} AS "
            f"SELECT ({idx_expr}) || '|' || vehicle_reference || '|' || casualty_reference "
            f"         AS source_row_key, "
            f"       {acc}, vehicle_reference, casualty_reference, "
            f"       (({idx_expr}) IN (SELECT accident_index FROM {self.COLLISION_SILVER})) "
            f"         AS link_valid "
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
