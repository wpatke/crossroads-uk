# AADF test fixtures

Trimmed sample for testing only — **not** the full dataset.

- **Publisher:** Department for Transport (DfT), Road Traffic Statistics
- **Dataset:** AADF (Annual Average Daily Flow) by count point — the single national file
- **Licence:** Open Government Licence v3.0
- **Source:** https://storage.googleapis.com/dft-statistics/road-traffic/downloads/data-gov-uk/dft_traffic_counts_aadf.zip
  (listed on https://roadtraffic.dft.gov.uk/downloads)
- **Downloaded:** 2026-07-14

## Row counts

- `dft_traffic_counts_aadf_sample.csv` — 14 count-point rows

## Filter used

Hartlepool (LAD `E06000001`) count points on the **A689** and **A179**, years **2022–2023**:

- A179 — 5 count points × 2 years = 10 rows
- A689 — 2 count points × 2 years = 4 rows

These roads and this local authority match the committed STATS19 collision fixture
(`tests/fixtures/stats19/`), whose A689 (`first_road_class=3, first_road_number=689`) and
A179 (`=3, 179`) Hartlepool collisions let an end-to-end test join collisions to traffic
counts on (road × local authority).

## Dropped points

None. All 14 chosen count points were verified to fall **inside** the committed ONS LAD
sample polygon (`tests/fixtures/ons/lad_2025/lad_sample.geojson`) via `ST_Contains`, each
stamping to `E06000001` (Hartlepool).

## Header note (national vs per-LA)

The national file carries **34 columns**. It has three columns beyond the per-LA download
format — `region_ons_code`, `local_authority_code`, and `road_category` — and uses
mixed-case names for the goods-vehicle counts (`LGVs`, `HGVs_2_rigid_axle` … `all_HGVs`).
The fixture keeps the national header **verbatim** so the transformer is exercised against
the real production layout. The silver projection reads only the columns it types; the rest
stay in the bronze copy.

## How it was produced

1. Downloaded the national zip from the URL above to a scratch directory (not the repo) and
   unzipped its single member `dft_traffic_counts_aadf.csv`.
2. Selected the Hartlepool A689/A179 rows for 2022–2023 with DuckDB, header verbatim,
   ordered by `road_name, year, count_point_id`.
3. Verified every selected count point stamps inside the committed LAD sample polygon
   (`ST_Contains`), resolving to `E06000001`.

Reproduction recipe (`NAT` = the unzipped national CSV in a scratch dir):

```python
import duckdb
c = duckdb.connect(); c.execute("INSTALL spatial; LOAD spatial")
NAT = "/scratch/dft_traffic_counts_aadf.csv"
c.execute(f"""
  COPY (
    SELECT * FROM read_csv('{NAT}', header=true, all_varchar=true)
    WHERE local_authority_name='Hartlepool' AND road_name IN ('A689','A179')
      AND year IN ('2022','2023')
    ORDER BY road_name, year, count_point_id
  ) TO 'tests/fixtures/aadf/dft_traffic_counts_aadf_sample.csv' (HEADER, DELIMITER ',')
""")
```

## Attribution

Contains public sector information licensed under the Open Government Licence v3.0.
Source: Department for Transport, Road Traffic Statistics.
