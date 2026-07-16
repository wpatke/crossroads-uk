"""DfT AADF traffic counts — Annual Average Daily Flow by count point (spec §3, §5).

The Department for Transport publishes one national "AADF by count point" file: the
annual average daily flow of traffic (vehicles per day, averaged over the year) on each
major-road link, one row per link per year, from 2000 onward. Adding traffic volume lets
downstream analysis turn a raw collision count into a rate ("collisions per million
vehicle-km"): collisions and count points both carry a road identity and a stamped local
authority code, so the risk join is a plain SQL join on (road name × local authority).

Coordinates are OSGR eastings/northings — natively EPSG:27700, so ST_Point casts them
directly (never reproject, spec §3A). Downloads use only the standard library (urllib to
fetch the zip, stdlib zipfile to unzip) — no new dependency. For offline tests the cache
is pre-seeded with the committed sample CSV, so no network access occurs.

Design notes:
  • Full history is always landed — the file is one artifact covering every year, so there
    is no per-year slicing. is_active() still gates on `years` so a boundary-only build
    does not trigger the ~40 MB download.
  • Minor-road figures may carry estimation_method = 'Estimated' (a modelled value rather
    than a physical count). These are kept and exposed, never dropped — estimation_method
    and estimation_method_detailed travel into silver so a researcher can filter them.
  • LAD/CTYUA area stamping honours the build's `boundary_mode`, exactly as the collision
    source does: snapshot uses the latest boundary vintage; temporal resolves each count to
    the vintage in force at a mid-year (1 July) date derived from the count's `year` — an
    approximation only in a year a boundary actually changed (see _boundary_predicate).
  • The national header carries 34 columns. Beyond the columns silver types, the raw copy
    also holds region_id/region_ons_code, local_authority_id/local_authority_code,
    road_category, latitude/longitude, link_length_miles, and the per-axle HGV splits;
    these stay in the bronze table only. The goods-vehicle counts use mixed-case names in
    the source (LGVs, all_HGVs); DuckDB resolves identifiers case-insensitively, so the
    lower-cased names in the silver SELECT bind to them.
"""

import os
import shutil
import warnings
import zipfile

from crossroads.transformers.base import BaseTransformer
from crossroads.quality import (
    SourceQuality, Dimension, record_source_rows, log_exclusion, create_clean_view,
)
from crossroads.sql import sql_str
from crossroads.net import download_to_file

# The single national AADF file. Public, Open Government Licence v3.0
# (listed on https://roadtraffic.dft.gov.uk/downloads).
AADF_ZIP_URL = ("https://storage.googleapis.com/dft-statistics/road-traffic/"
                "downloads/data-gov-uk/dft_traffic_counts_aadf.zip")
ZIP_CACHE_FILE = "dft_traffic_counts_aadf.zip"
CSV_CACHE_FILE = "dft_traffic_counts_aadf.csv"   # canonical extracted name in the cache

# British National Grid envelope, used to verify coordinates really are EPSG:27700.
# (DuckDB GEOMETRY stores no SRID, so we sanity-check coordinate ranges — same
# convention as the other spatial sources.)
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000

# Ledger rule ids for the two audited dimensions.
COORD_RULE = "aadf.coord.invalid"
COUNT_RULE = "aadf.count.invalid"


class AadfTransformer(BaseTransformer):
    """Loads the national DfT AADF-by-count-point file into bronze/silver/gold."""

    source_id = "aadf"
    display_name = "traffic counts (AADF)"     # friendly wizard-menu label

    # Ordering only: run after the boundary sources so the LAD/CTYUA stamp finds their
    # tables. Alphabetically 'aadf' would otherwise sort first and run before them.
    # An optional edge to a boundary source that is not active this build is simply inert.
    depends_on = ("ons_lad", "ons_ctyua")

    BRONZE = "aadf_raw"        # faithful all-string copy of the national CSV
    SILVER = "aadf"           # typed facts, LAD/CTYUA stamped, R-Tree indexed
    CLEAN_VIEW = "aadf_clean"  # gold view: rows with valid geometry AND a valid count

    def is_active(self, **kwargs):
        # Same gate as the collision/weather sources: nothing to build without years, so a
        # boundary-only build must not trigger the ~40 MB download. When active, AADF still
        # lands its FULL history (the file is one artifact; there is no per-year slice).
        return bool(kwargs.get("years"))

    # --- extract (real download; offline tests pre-seed the cache) ---------
    def extract(self, cache_dir, **kwargs):
        os.makedirs(cache_dir, exist_ok=True)
        # Capture the boundary mode for the later area stamp, exactly as the collision
        # source does. The engine calls extract() immediately before transform_and_load()
        # on the same instance, so stashing it on self is safe.
        self._boundary_mode = kwargs.get("boundary_mode", "snapshot")
        csv_path = os.path.join(cache_dir, CSV_CACHE_FILE)
        if os.path.exists(csv_path):        # offline-friendly: already extracted/seeded
            return
        zip_path = os.path.join(cache_dir, ZIP_CACHE_FILE)
        if not os.path.exists(zip_path):    # download the zip if it is not cached yet
            self._download(AADF_ZIP_URL, zip_path)
        self._unzip_single_csv(zip_path, csv_path)

    def _download(self, url, dest):
        """Fetch the zip to `dest`, streamed and atomic (download to a temp, then rename) so
        an interrupted download never leaves a half-file the cache check would trust, with a
        socket timeout so a stalled endpoint fails fast rather than hanging. A zip cannot be
        parse-checked mid-download; it is validated instead by opening it in
        _unzip_single_csv (a corrupt archive raises there)."""
        download_to_file(url, dest)

    def _unzip_single_csv(self, zip_path, csv_dest):
        """Extract the single CSV member of the national zip to the canonical cache name.

        The member's internal name is not relied upon: we take whatever the one `.csv`
        member is called and write it to CSV_CACHE_FILE. If the zip ever holds more than one
        CSV, fail loudly (a silent pick would land the wrong data). Extraction goes via a
        temp file + os.replace so an interrupted unzip cannot leave a half-file behind."""
        with zipfile.ZipFile(zip_path) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if len(members) != 1:
                raise ValueError(
                    f"[aadf] expected exactly one .csv member in {zip_path}, "
                    f"found {members}")
            tmp = csv_dest + ".part"
            with zf.open(members[0]) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.replace(tmp, csv_dest)

    # --- transform_and_load ------------------------------------------------
    def transform_and_load(self, con, cache_dir):
        path = os.path.join(cache_dir, CSV_CACHE_FILE)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"[aadf] no cached CSV at {path}; extract() must run first "
                f"(or seed the cache in tests).")
        # BRONZE: faithful, header-driven, all-string copy. No year filter (full history).
        # Path is cache-derived (trusted); no row values are interpolated.
        con.execute(
            f"CREATE OR REPLACE TABLE {self.BRONZE} AS "
            f"SELECT * FROM read_csv({sql_str(path)}, header=true, all_varchar=true)")
        # SILVER (typed, keep-in-place 1:1), then the exclusion ledger, then the area stamp.
        self._derive_silver(con)
        self._log_rejections(con)
        self._stamp_area_codes(con)
        # CONSERVATION: record how many rows were read from the source file.
        n = con.execute(f"SELECT count(*) FROM {self.BRONZE}").fetchone()[0]
        record_source_rows(con, self.source_id, n)
        # GOLD: valid-geometry AND valid-count projection.
        create_clean_view(con, self.CLEAN_VIEW, self.SILVER, ["geom_valid", "count_valid"])
        # INDEX: bounding-box R-Tree over the silver geometry, for fast point-in-polygon
        # joins (spec §5). The silver table was just CREATE OR REPLACE'd (dropping any prior
        # index); the DROP is belt-and-suspenders. NULL geoms are skipped without error.
        con.execute(f"DROP INDEX IF EXISTS {self.SILVER}_geom_rtree")
        con.execute(
            f"CREATE INDEX {self.SILVER}_geom_rtree ON {self.SILVER} USING RTREE (geom)")

    def _derive_silver(self, con):
        """Typed silver, 1:1 with bronze. Factored out so a test can drive it against a
        synthetic bronze (like spatial._derive_silver_and_ledger).

        Two-level select: the inner CTE types the coordinates/counts (SQL cannot reference a
        sibling alias in the same SELECT list), the outer builds geom + flags from the typed
        values. Eastings/northings ARE EPSG:27700, so ST_Point casts them directly (spec
        §3A). A coordinate that is blank, non-numeric, or outside the BNG envelope becomes
        geom NULL -> geom_valid FALSE; the row is kept (never deleted). source_row_key is
        count_point_id|year (unique per link per year)."""
        # The EPSG:27700 envelope predicate, written once and interpolated into both the
        # geom CASE and geom_valid (identifiers/constants only — never row values). It reads
        # the typed easting/northing CTE aliases below.
        envelope = (f"easting IS NOT NULL AND northing IS NOT NULL "
                    f"AND easting BETWEEN {BNG_MIN_E} AND {BNG_MAX_E} "
                    f"AND northing BETWEEN {BNG_MIN_N} AND {BNG_MAX_N}")
        con.execute(
            f"CREATE OR REPLACE TABLE {self.SILVER} AS "
            f"WITH typed AS ("
            f"  SELECT "
            f"    count_point_id AS count_point_id_raw, year AS year_raw, "
            f"    TRY_CAST(count_point_id AS INTEGER) AS count_point_id, "
            f"    TRY_CAST(year AS INTEGER)           AS year, "
            f"    region_name, local_authority_name, "
            f"    road_name, road_type, "
            f"    start_junction_road_name, end_junction_road_name, "
            # raw coordinate twins + typed values (kept-in-place: the raw text survives)
            f"    easting  AS easting_raw, northing AS northing_raw, "
            f"    TRY_CAST(easting  AS INTEGER) AS easting, "
            f"    TRY_CAST(northing AS INTEGER) AS northing, "
            f"    TRY_CAST(link_length_km AS DOUBLE) AS link_length_km, "
            f"    estimation_method, estimation_method_detailed, "
            # headline traffic count: raw twin + typed value
            f"    all_motor_vehicles AS all_motor_vehicles_raw, "
            f"    TRY_CAST(all_motor_vehicles AS BIGINT) AS all_motor_vehicles, "
            # remaining per-class volumes, typed (not separately flagged)
            f"    TRY_CAST(pedal_cycles AS BIGINT) AS pedal_cycles, "
            f"    TRY_CAST(two_wheeled_motor_vehicles AS BIGINT) AS two_wheeled_motor_vehicles, "
            f"    TRY_CAST(cars_and_taxis AS BIGINT) AS cars_and_taxis, "
            f"    TRY_CAST(buses_and_coaches AS BIGINT) AS buses_and_coaches, "
            f"    TRY_CAST(lgvs AS BIGINT) AS lgvs, "
            f"    TRY_CAST(all_hgvs AS BIGINT) AS all_hgvs "
            f"  FROM {self.BRONZE}"
            f") "
            f"SELECT "
            f"  count_point_id_raw || '|' || year_raw AS source_row_key, "
            f"  count_point_id, year, "
            f"  region_name, local_authority_name, "
            f"  road_name, road_type, "
            f"  start_junction_road_name, end_junction_road_name, "
            f"  easting_raw, northing_raw, easting, northing, "
            f"  CASE WHEN {envelope} "
            f"       THEN ST_Point(easting, northing)::GEOMETRY ELSE NULL END AS geom, "
            f"  ({envelope}) AS geom_valid, "
            f"  link_length_km, estimation_method, estimation_method_detailed, "
            f"  all_motor_vehicles_raw, all_motor_vehicles, "
            # a valid headline count is present and non-negative
            f"  (all_motor_vehicles IS NOT NULL AND all_motor_vehicles >= 0) AS count_valid, "
            f"  pedal_cycles, two_wheeled_motor_vehicles, cars_and_taxis, "
            f"  buses_and_coaches, lgvs, all_hgvs, "
            # stamped after creation by the point-in-polygon join
            f"  CAST(NULL AS VARCHAR) AS lad_code, "
            f"  CAST(NULL AS VARCHAR) AS ctyua_code "
            f"FROM typed"
        )

    def _log_rejections(self, con):
        """One reject_dimension ledger row per FALSE flag, so flag/ledger agreement holds
        (spec §9). Aggregate scan + a bounded Python loop over the FALSE rows — the exact
        shape spatial.py uses. On real DfT data both sets are near-empty."""
        bad_geom = con.execute(
            f"SELECT source_row_key, easting_raw, northing_raw FROM {self.SILVER} "
            f"WHERE geom_valid = FALSE").fetchall()
        for key, e, n in bad_geom:
            log_exclusion(
                con, source_id=self.source_id, source_row_key=key,
                column_name="geom", rule_id=COORD_RULE,
                rule_desc="easting/northing missing, non-numeric, or outside the "
                          "EPSG:27700 envelope",
                severity="reject_dimension", raw_value=f"{e},{n}")
        bad_count = con.execute(
            f"SELECT source_row_key, all_motor_vehicles_raw FROM {self.SILVER} "
            f"WHERE count_valid = FALSE").fetchall()
        for key, raw in bad_count:
            log_exclusion(
                con, source_id=self.source_id, source_row_key=key,
                column_name="all_motor_vehicles", rule_id=COUNT_RULE,
                rule_desc="all_motor_vehicles missing, non-numeric, or negative",
                severity="reject_dimension", raw_value=str(raw))

    def _table_exists(self, con, name):
        return con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [name]).fetchone()[0] > 0

    def _boundary_predicate(self, mode):
        """Extra ON-clause for the point-in-polygon join, honouring the build's mode.

        snapshot (default): only the current boundary vintage (valid_to IS NULL).
        temporal: the vintage whose [valid_from, valid_to) window contains a mid-year date
                  derived from each count's `year`. AADF carries only a year (no day), so we
                  look up the vintage in force on 1 July of that year. UK boundary changes
                  take effect 1 April, so 1 July picks the vintage in force for most of the
                  count year; the only approximation is the single year a boundary changed.
                  A NULL year yields a NULL date, so that row matches no vintage (unstamped).
        """
        if mode == "temporal":
            return ("AND b.valid_from <= make_date(c2.year, 7, 1) "
                    "AND (b.valid_to IS NULL "
                    "     OR make_date(c2.year, 7, 1) < b.valid_to)")
        return "AND b.valid_to IS NULL"

    def _stamp_area_codes(self, con):
        """Stamp lad_code/ctyua_code onto valid count points via point-in-polygon against
        the boundary silver tables — the same UPDATE shape the collision source uses.
        Defensive: if a boundary table is absent (e.g. an aadf-only build/test), leave that
        code NULL and warn; the pipeline still succeeds. area_code is aggregated with min()
        for a deterministic result even if polygons were to overlap (they should not within
        one vintage). ST_Contains uses the boundary R-Tree to stay fast (spec §5)."""
        mode = getattr(self, "_boundary_mode", "snapshot")
        pred = self._boundary_predicate(mode)
        for code_col, btable in (("lad_code", "lad_boundaries"),
                                 ("ctyua_code", "ctyua_boundaries")):
            if not self._table_exists(con, btable):
                warnings.warn(
                    f"aadf: boundary table {btable} not found; {code_col} left NULL "
                    f"(build boundaries alongside aadf to enable the spatial join).",
                    stacklevel=2)
                continue
            con.execute(
                f"UPDATE {self.SILVER} AS a SET {code_col} = m.area_code "
                f"FROM ("
                f"  SELECT c2.source_row_key AS k, min(b.area_code) AS area_code "
                f"  FROM {self.SILVER} c2 JOIN {btable} b "
                f"    ON c2.geom IS NOT NULL AND b.geom_valid = TRUE "
                f"       AND ST_Contains(b.geom, c2.geom) {pred} "
                f"  GROUP BY c2.source_row_key"
                f") m WHERE a.source_row_key = m.k"
            )

    def quality_spec(self):
        return SourceQuality(
            source_id=self.source_id,
            bronze_table=self.BRONZE,
            silver_table=self.SILVER,
            dimensions=(
                Dimension("geom", "geom_valid", (COORD_RULE,)),
                Dimension("count", "count_valid", (COUNT_RULE,)),
            ),
            key_column="source_row_key",
        )
