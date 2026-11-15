# Data Sources, Licences & Attribution

Crossroads-UK does not redistribute data. It downloads each dataset directly from its
official publisher to your machine at build time. You are therefore the licensee of the
data you download, and the attribution obligations below are **yours to honour when you
publish** any analysis derived from a Crossroads-UK database.

**There is no licence to click through in the wizard.** See "Why the build does not
gate on licences" at the bottom for the reasoning.

---

## 1. DfT STATS19 — Road Safety Data

- **Publisher:** UK Department for Transport (DfT).
- **Dataset:** Road Safety Open Dataset (collision, vehicle, casualty records).
- **Licence:** [Open Government Licence v3.0 (OGL)](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
- **Required attribution** (reproduce when you publish):
  > Contains public sector information licensed under the Open Government Licence v3.0.
  > Source: Department for Transport, Road Safety Open Dataset.
- **Downloads** (all from `https://data.dft.gov.uk/road-accidents-safety-data/`, listed on
  DfT's [Road Safety Data](https://www.gov.uk/government/statistics/road-safety-data) page):
  - Single-year files, `dft-road-casualty-statistics-{collision,vehicle,casualty}-{year}.csv`.
    DfT publishes these for its most recent years only (2021–2025 when checked in November
    2026), and the window moves forward each year. Crossroads tries this file first.
  - Combined files, `dft-road-casualty-statistics-{collision,vehicle,casualty}-1979-latest-published-year.csv`
    (about 4.3 GB in total), used for every year without a single-year file. Each is
    downloaded once into the cache. The requested years are then split out into per-year
    cache files using the `collision_year` column, which is in all three files, so a
    year's vehicles and casualties always land with its collisions. Each record's text is
    copied unchanged.
- **Older years:** DfT did not collect every field in every year. Fields not yet collected
  are `-1` or blank, so they are NULL after cleaning. For example, the LSOA of the collision
  starts in 1999, the injury-based severity fields in 2004, and driver/casualty IMD deciles
  in 2016. So for a build spanning many years, `stats19_completeness` reports these as partly
  missing. That figure is accurate, but it blends years when the field was not collected
  with years when it was.
- **Coordinates before 1989 are less reliable.** In 1979–1988, 4–19% of collisions that
  have coordinates fall outside every local authority, against about 0.1–0.3% from 1989.
  The worst years are 1979 (19%), 1980–81 (about 13.5%) and 1986 (11%). The cause is
  incorrect grid references in DfT's early data, not missing ones, so these rows keep
  `geom_valid = TRUE` but get no `lad_code`. Some may also have been placed in the wrong
  area. For area-level analysis of these years, check how many collisions have
  `lad_code IS NULL`.

## 2. ONS Boundaries — Local Authority Districts & Counties/Unitary Authorities

- **Publisher:** Office for National Statistics (ONS); geometry derived from Ordnance Survey.
- **Dataset:** LAD and CTYUA boundaries (Generalised Clipped, EPSG:27700).
- **Licence:** [Open Government Licence v3.0 (OGL)](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/),
  and the underlying Ordnance Survey Crown copyright / database right.
- **Required attribution** (reproduce when you publish):
  > Contains National Statistics data © Crown copyright and database right [year].
  > Contains OS data © Crown copyright and database right [year].
  > Source: Office for National Statistics, licensed under the Open Government Licence v3.0.

## 3. Copernicus ERA5-Land — Meteorological Reanalysis

- **Publisher:** Copernicus Climate Change Service (C3S) / ECMWF, via the Climate Data Store (CDS).
- **Dataset:** ERA5-Land hourly reanalysis (2 m temperature, total precipitation).
- **Licence:** [Copernicus Licence](https://cds.climate.copernicus.eu) — you must accept
  the ERA5-Land licence once in your CDS account before the data can be downloaded. A
  working CDS API key implies you have done so; Crossroads surfaces a clear message if not.
- **Required attribution** (reproduce when you publish):
  > Generated using Copernicus Climate Change Service information [year].
  > Neither the European Commission nor ECMWF is responsible for any use of the
  > Copernicus information or data it contains.

Replace `[year]` with the year you downloaded/used the data.

---

## 4. GOV.UK Bank Holidays

- **Publisher:** Government Digital Service (GDS), GOV.UK.
- **Dataset:** UK bank holidays as a JSON feed at
  [https://www.gov.uk/bank-holidays.json](https://www.gov.uk/bank-holidays.json), carrying the
  three UK divisions — england-and-wales, scotland, northern-ireland — which genuinely differ.
- **Licence:** [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
  — attribution, not acceptance (nothing to click); attribute to GOV.UK / GDS when you publish.
- **Loaded into:** the `bank_holidays` dimension table (see [docs/schema.md](schema.md)), and used to
  stamp `collisions.is_bank_holiday` per the collision's nation.
- **Caveat — not reproducible, rolling window:** this feed is *live* and spans only a recent window
  (roughly 2018 onward, about a year ahead), so this source is **deliberately exempt from the spec §2
  reproducibility guarantee** (recorded in the `quality_exemptions` table). Collision dates outside
  the feed's coverage resolve to `is_bank_holiday = NULL` (unknown) — never `FALSE` — so a historical
  STATS19 collision predating the feed is correctly marked "no data", not "not a holiday".

---

## 5. DfT AADF — Traffic Counts

- **Publisher:** UK Department for Transport (DfT), Road Traffic Statistics.
- **Dataset:** Annual Average Daily Flow (AADF) by count point — daily traffic volumes per
  road link per year, major roads counted and minor roads partly estimated. Downloaded as one
  national zipped CSV from [https://roadtraffic.dft.gov.uk/downloads](https://roadtraffic.dft.gov.uk/downloads).
- **Licence:** [Open Government Licence v3.0 (OGL)](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
- **Required attribution** (reproduce when you publish):
  > Contains public sector information licensed under the Open Government Licence v3.0.
  > Source: Department for Transport, Road Traffic Statistics.
- **Loaded into:** the `aadf` table (see [docs/schema.md](schema.md)), with each count point
  stamped with an ONS `lad_code`/`ctyua_code` by point-in-polygon join. The FULL year history
  (2000 onward) is always loaded regardless of the build's `years` — the file is a single national
  artifact, and traffic volume is denominator data for the risk metric, so slicing it by year would
  discard useful context for no size benefit.
- **Caveats:**
  - **Counted vs estimated flows.** `estimation_method` distinguishes DfT-counted from
    DfT-estimated (modelled) flows; both are retained (keep-in-place), so filter on it when your
    analysis needs counted-only volumes.
  - **Boundary attribution.** In temporal mode each annual count is attributed to the area
    boundaries in force at its mid-year (1 July) point; this is exact except in a year the boundary
    itself changed. For a risk metric, compare like years so collisions and counts resolve to the
    same boundary vintage. The wizard shows this note and asks you to confirm when temporal mode is
    chosen together with traffic counts.

---

## Why the build does not gate on licences

- **Copernicus** acceptance is performed on the CDS portal, not in this tool. Possessing a
  valid API key implies acceptance; the un-accepted case already produces an actionable
  error at download time.
- **OGL** (STATS19, ONS, GOV.UK bank holidays) requires **attribution, not acceptance** — there
  is nothing to click. The obligation is on you at publication, and is documented above so you can
  meet it.

Crossroads-UK therefore informs rather than blocks: the wizard points you here, and you
remain responsible for honouring these licences in any published work.
