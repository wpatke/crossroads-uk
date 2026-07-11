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

## Why the build does not gate on licences

- **Copernicus** acceptance is performed on the CDS portal, not in this tool. Possessing a
  valid API key implies acceptance; the un-accepted case already produces an actionable
  error at download time.
- **OGL** (STATS19, ONS) requires **attribution, not acceptance** — there is nothing to
  click. The obligation is on you at publication, and is documented above so you can meet it.

Crossroads-UK therefore informs rather than blocks: the wizard points you here, and you
remain responsible for honouring these licences in any published work.
