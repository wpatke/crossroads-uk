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
handles both .geojson (production download) and .shp (committed test fixtures)
transparently via GDAL. For offline tests, the cache is pre-seeded with the
committed .geojson fixture files, so no network access occurs.
"""

import os
import urllib.request
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

    # --- vintage selection (snapshot only in Stage 02; Stage 03 adds multi-vintage) ---
    def _vintages_for(self, **kwargs):
        # Stage 02: snapshot only -> the latest vintage. Stage 03 widens this.
        return (self.vintages[-1],)

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
            f"  geom, "
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


# Production URL: full UK LAD BGC dataset, all features, EPSG:27700, GeoJSON.
# The ONS FeatureServer maxRecordCount is 2000, well above the 361 LAD features,
# so a single request returns the entire dataset.
_LAD_2024_URL = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Local_Authority_Districts_December_2024_Boundaries_UK_BGC/"
    "FeatureServer/0/query"
    "?where=1%3D1&outFields=LAD24CD,LAD24NM&outSR=27700&f=geojson"
)

# Production URL: full UK CTYUA BGC dataset (218 features), EPSG:27700, GeoJSON.
_CTYUA_2024_URL = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Counties_and_Unitary_Authorities_December_2024_Boundaries_UK_BGC/"
    "FeatureServer/0/query"
    "?where=1%3D1&outFields=CTYUA24CD,CTYUA24NM&outSR=27700&f=geojson"
)


class LADBoundaryTransformer(_BoundaryTransformer):
    """Ingests ONS Local Authority District boundaries (snapshot, latest vintage)."""

    source_id = "ons_lad"
    bronze_table = "ons_lad_raw"
    silver_table = "lad_boundaries"
    clean_view = "lad_boundaries_clean"
    vintages = (
        Vintage(
            label="2024",
            url=_LAD_2024_URL,
            # source_file matches the committed test fixture so offline tests skip
            # the download. For production, extract() saves the downloaded GeoJSON
            # under this same name in the cache directory.
            source_file="lad_sample.geojson",
            code_col="LAD24CD",
            name_col="LAD24NM",
            valid_from="2024-12-01",
            valid_to=None,
        ),
    )


class CTYUABoundaryTransformer(_BoundaryTransformer):
    """Ingests ONS Counties and Unitary Authority boundaries (snapshot, latest vintage)."""

    source_id = "ons_ctyua"
    bronze_table = "ons_ctyua_raw"
    silver_table = "ctyua_boundaries"
    clean_view = "ctyua_boundaries_clean"
    vintages = (
        Vintage(
            label="2024",
            url=_CTYUA_2024_URL,
            source_file="ctyua_sample.geojson",
            code_col="CTYUA24CD",
            name_col="CTYUA24NM",
            valid_from="2024-12-01",
            valid_to=None,
        ),
    )
