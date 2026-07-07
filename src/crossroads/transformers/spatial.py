"""ONS Local Authority District (LAD) and County/Unitary Authority (CTYUA)
boundary ingestion — the EPSG:27700 geometric base layer (spec §3, Phase 1).

One module, two concrete transformers (LAD, CTYUA) sharing an abstract base.
Each is independently audited by the quality engine via its own SourceQuality.
Boundaries are native EPSG:27700 (ONS BGC) and are NOT reprojected — only
validated.

**Note on download format:** The ONS BGC datasets are published as ArcGIS
FeatureServer (WFS) services rather than as direct shapefile ZIP downloads.
The url field in each Vintage points to the FeatureServer GeoJSON query
endpoint; extract() downloads the GeoJSON file directly using urllib. ST_Read
reads the GeoJSON via GDAL. For offline tests, the cache is pre-seeded with the
committed .geojson fixture files, so no network access occurs.
"""

import json
import os
import urllib.parse
import urllib.request
import warnings
from abc import abstractmethod
from dataclasses import dataclass

from crossroads.transformers.base import BaseTransformer
from crossroads.quality import (
    SourceQuality, Dimension, create_clean_view,
    record_source_rows, log_exclusion,
)

# British National Grid envelope, used to verify geometry really is EPSG:27700.
# (DuckDB GEOMETRY stores no SRID, so we sanity-check coordinate ranges.)
BNG_MIN_E, BNG_MAX_E = 0, 700_000
BNG_MIN_N, BNG_MAX_N = 0, 1_300_000

# The vintage registry lives in a committed JSON manifest alongside the other
# static reference data (src/crossroads/reference/), so adding a new ONS edition
# is a data change, not a code change.
_MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "..", "reference", "ons_boundaries.json"
)


def _build_query_url(feature_server, code_col, name_col):
    """Build the ONS FeatureServer GeoJSON query endpoint for one vintage.

    Downloads all features, only the code/name columns, reprojected to EPSG:27700.
    """
    params = urllib.parse.urlencode({
        "where": "1=1",
        "outFields": f"{code_col},{name_col}",
        "outSR": "27700",
        "f": "geojson",
    })
    return feature_server.rstrip("/") + "/0/query?" + params


def _load_vintages(source_id):
    """Load one source's vintages from the JSON manifest, newest LAST.

    Sorts ascending by valid_from so vintages[-1] is the latest edition (the
    snapshot). valid_to is derived by chaining: each vintage is valid until the
    next edition's valid_from; the latest vintage is open-ended (valid_to = None).
    """
    with open(_MANIFEST_PATH, encoding="utf-8") as f:
        rows = json.load(f)[source_id]
    rows = sorted(rows, key=lambda r: r["valid_from"])
    vintages = []
    for i, r in enumerate(rows):
        valid_to = rows[i + 1]["valid_from"] if i + 1 < len(rows) else None
        vintages.append(Vintage(
            label=r["label"],
            url=_build_query_url(r["feature_server"], r["code_col"], r["name_col"]),
            source_file=r["source_file"],
            code_col=r["code_col"],
            name_col=r["name_col"],
            valid_from=r["valid_from"],
            valid_to=valid_to,
        ))
    return tuple(vintages)


@dataclass(frozen=True)
class Vintage:
    """One published boundary vintage: where to get it, how its columns are
    named, and the validity window it represents (spec §3C temporal drift).

    source_file is the filename the transformer looks for in the cache; for
    offline tests it must match the committed fixture filename. For production
    downloads, extract() saves the downloaded file under this name.
    """

    label: str              # e.g. "2024"
    url: str                # ONS FeatureServer GeoJSON query endpoint URL
    source_file: str        # filename in the cache dir (e.g. "lad_sample.geojson")
    code_col: str           # ONS area-code column, e.g. "LAD24CD"
    name_col: str           # ONS area-name column, e.g. "LAD24NM"
    valid_from: str         # 'YYYY-MM-DD' this layout takes effect
    valid_to: str | None    # 'YYYY-MM-DD' superseded, or None if current


class _BoundaryTransformer(BaseTransformer):
    """Shared LAD/CTYUA ingestion. Concrete subclasses set the identity:
    source_id, table names, gold-view name, and the vintage registry.

    This class is abstract (it does not override source_id) so the Registry
    does NOT discover it directly — only the concrete subclasses are discovered.
    """

    # Spatial boundaries are always-on infrastructure that other datasets join
    # against — never a dataset the researcher selects on its own. Keep them out of
    # the wizard menu and always active regardless of the user's dataset picks.
    user_selectable = False

    # --- identity, supplied by concrete subclasses ---
    @property
    @abstractmethod
    def source_id(self): ...

    @property
    @abstractmethod
    def bronze_table(self): ...

    @property
    @abstractmethod
    def silver_table(self): ...

    @property
    @abstractmethod
    def clean_view(self): ...

    @property
    @abstractmethod
    def vintages(self):
        """Tuple[Vintage, ...] newest LAST (the latest vintage is vintages[-1])."""

    GEOM_RULE = "ons.geom.invalid"

    def _vintages_for(self, **kwargs):
        """Which vintages this build loads, per spec §3C boundary drift.

          boundary_mode='snapshot' (default) -> latest vintage only.
          boundary_mode='temporal'           -> editions whose validity window overlaps
                                               the requested build years (kwargs['years']);
                                               if no years are given, every edition.

        Years before this source's earliest edition have no ONS boundary coverage: the
        earliest edition is used as a stand-in and a warning flags this to the researcher.
        Unknown modes fall back to snapshot (non-spatial builds pass arbitrary kwargs
        through, so an unrelated value must never break a build).
        """
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

    def extract(self, cache_dir, **kwargs):
        os.makedirs(cache_dir, exist_ok=True)
        wanted = self._vintages_for(**kwargs)
        for v in wanted:
            file_path = os.path.join(cache_dir, v.source_file)
            # Offline-friendly: if already cached (or test-seeded), skip download.
            if not os.path.exists(file_path):
                self._download_source(v, file_path)
        # Store resolved vintages so transform_and_load() picks up the same set.
        # The engine always calls extract() immediately before transform_and_load()
        # on the same instance (see client.py build loop), so instance state is safe.
        self._vintages_to_load = wanted

    def _download_source(self, vintage, dest_path):
        """Download a vintage from the ONS FeatureServer GeoJSON endpoint."""
        urllib.request.urlretrieve(vintage.url, dest_path)

    def transform_and_load(self, con, cache_dir):
        vintages = getattr(self, "_vintages_to_load", None) or self._vintages_for()

        # --- BRONZE: faithful copy of every feature, one block per vintage,
        # tagged with its vintage label. Identifiers (table, column names) come
        # from code-controlled Vintage constants (trusted interpolation); paths
        # are also code/cache-derived. Row values are never interpolated.
        selects = []
        for v in vintages:
            path = os.path.join(cache_dir, v.source_file)
            selects.append(
                f"SELECT '{v.label}' AS vintage, "
                f"{v.code_col} AS area_code, {v.name_col} AS area_name, geom "
                f"FROM ST_Read('{path}')"
            )
        bronze_sql = " UNION ALL ".join(selects)
        con.execute(f"CREATE OR REPLACE TABLE {self.bronze_table} AS {bronze_sql}")

        # --- SILVER + LEDGER: delegated to _derive_silver_and_ledger so tests
        # can call it directly against a pre-built bronze (e.g. with a degenerate
        # geometry row) without needing real fixture files.
        self._derive_silver_and_ledger(con, vintages)

        # --- CONSERVATION: record how many rows were read from the source.
        n = con.execute(f"SELECT count(*) FROM {self.bronze_table}").fetchone()[0]
        record_source_rows(con, self.source_id, n)

        # --- GOLD: valid-geometry view.
        create_clean_view(con, self.clean_view, self.silver_table, ["geom_valid"])

        # --- INDEX: bounding-box R-Tree over the silver geometry. This is what
        # makes Step 4's point-in-polygon joins fast (spec §5: avoids an unindexed
        # spatial cross-join). The silver table was just (re)created via
        # CREATE OR REPLACE, which drops any prior index with it, so we create a
        # fresh one with a deterministic name. The DROP is belt-and-suspenders
        # (DuckDB has no CREATE INDEX IF NOT EXISTS for custom indexes).
        # Identifiers are code-controlled constants (trusted interpolation).
        # NULL geometry is fine: a flagged-invalid boundary carries geom IS NULL
        # (spec §9) and DuckDB's RTREE skips NULL rows without error.
        index_name = f"{self.silver_table}_geom_rtree"
        con.execute(f"DROP INDEX IF EXISTS {index_name}")
        con.execute(
            f"CREATE INDEX {index_name} ON {self.silver_table} USING RTREE (geom)"
        )

    def _validity_case_sql(self, vintages):
        """Build CASE expressions mapping vintage label to valid_from / valid_to.

        Labels and dates are code-controlled constants (trusted interpolation).
        """
        vf = "CASE vintage " + " ".join(
            f"WHEN '{v.label}' THEN DATE '{v.valid_from}'" for v in vintages
        ) + " END"
        vt = "CASE vintage " + " ".join(
            f"WHEN '{v.label}' THEN "
            + (f"DATE '{v.valid_to}'" if v.valid_to else "NULL")
            for v in vintages
        ) + " END"
        return vf, vt

    def _derive_silver_and_ledger(self, con, vintages):
        """Derive the silver table and write ledger entries.

        Factored out so tests can call it directly against a pre-built bronze
        (e.g. a synthetic bronze with a degenerate geometry row).
        """
        vf_case, vt_case = self._validity_case_sql(vintages)
        con.execute(
            f"CREATE OR REPLACE TABLE {self.silver_table} AS "
            f"SELECT "
            f"  area_code || '|' || vintage AS source_row_key, "
            f"  area_code, area_name, vintage, "
            # Cast to a plain GEOMETRY. ST_Read on the ONS GeoJSON (downloaded
            # with outSR=27700) yields a CRS-qualified GEOMETRY('EPSG:27700')
            # type; DuckDB's RTREE index only accepts a bare GEOMETRY column.
            # The cast strips the CRS label without touching the coordinates,
            # matching spec §3A (DuckDB GEOMETRY stores no SRID — we sanity-check
            # EPSG:27700 by coordinate range, not by an embedded label).
            f"  geom::GEOMETRY AS geom, "
            f"  (geom IS NOT NULL AND ST_IsValid(geom)) AS geom_valid, "
            f"  {vf_case} AS valid_from, "
            f"  {vt_case} AS valid_to "
            f"FROM {self.bronze_table}"
        )
        bad = con.execute(
            f"SELECT source_row_key, area_code FROM {self.silver_table} "
            f"WHERE geom_valid = FALSE"
        ).fetchall()
        for key, code in bad:
            log_exclusion(
                con, source_id=self.source_id, source_row_key=key,
                column_name="geom", rule_id=self.GEOM_RULE,
                rule_desc="boundary geometry is NULL or fails ST_IsValid",
                severity="reject_dimension", raw_value=str(code),
            )

    def quality_spec(self):
        return SourceQuality(
            source_id=self.source_id,
            bronze_table=self.bronze_table,
            silver_table=self.silver_table,
            dimensions=(Dimension("geom", "geom_valid", (self.GEOM_RULE,)),),
            key_column="source_row_key",
        )


class LADBoundaryTransformer(_BoundaryTransformer):
    """Ingests ONS Local Authority District boundaries (snapshot, latest vintage)."""

    source_id = "ons_lad"
    bronze_table = "ons_lad_raw"
    silver_table = "lad_boundaries"
    clean_view = "lad_boundaries_clean"
    vintages = _load_vintages("ons_lad")


class CTYUABoundaryTransformer(_BoundaryTransformer):
    """Ingests ONS Counties and Unitary Authority boundaries (snapshot, latest vintage)."""

    source_id = "ons_ctyua"
    bronze_table = "ons_ctyua_raw"
    silver_table = "ctyua_boundaries"
    clean_view = "ctyua_boundaries_clean"
    vintages = _load_vintages("ons_ctyua")
