# ONS Boundary Test Fixtures

Trimmed GeoJSON boundary samples for offline integration testing. Do not use these for analysis.

## Contents

| Directory | File(s) | Features | Source dataset |
|-----------|---------|----------|----------------|
| `lad_2024/` | `lad_sample.geojson` | 3 polygons | Local Authority Districts (December 2024) Boundaries UK BGC |
| `ctyua_2024/` | `ctyua_sample.geojson` | 2 polygons | Counties and Unitary Authorities (December 2024) Boundaries UK BGC |

The `.geojson` files are the fixture format used by the transformer tests. (Shapefile sidecars
— `.shp/.shx/.dbf/.prj` — were previously committed but removed as unused; the transformers read
GeoJSON only.)

## Source

- **Publisher:** Office for National Statistics (ONS) Open Geography Portal
- **Vintage:** December 2024
- **Projection:** British National Grid (EPSG:27700) — native BGC projection, not reprojected
- **Licence:** Open Government Licence v3.0 (OGL v3) — https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/

## How these were produced

Each fixture is the first N rows (ordered by area code) of the full ONS BGC dataset, retrieved from
the ONS ArcGIS FeatureServer GeoJSON endpoint and saved as compact GeoJSON. The files are committed
so the fixtures work offline.

These samples are for testing only. The transformers download the full ONS datasets at run time from
the ONS ArcGIS FeatureServer (see the Vintage registry in `spatial.py`).
