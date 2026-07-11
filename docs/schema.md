# Database Schema — Data Dictionary

The tables and views a Crossroads-UK build can create, with the meaning and derivation of
each column. The `CREATE TABLE` blocks below are **illustrative** — Crossroads builds these
tables dynamically (`CREATE OR REPLACE ... AS SELECT`), so this file documents the *result*;
it is never executed. Every built database also carries its own machine-readable schema
marker: `SELECT * FROM crossroads_meta` (see [`crossroads_meta`](#crossroads_meta)).

**Schema version:** 1  ·  See [methodology.md](methodology.md) for how the data is produced
and [spec.md §9](spec.md) for the keep-in-place quality model.

## Conventions (keep-in-place model)

Every cleansed field appears **twice**: the preserved raw value and a typed/clean column
that is `NULL` when the source value failed validation, plus (for audited fields) a boolean
`*_valid` flag. Spatial/severity analysis runs only where the flag is `TRUE`. Nothing is
deleted — failures are logged in `data_quality_log`.

- **`*_raw`** — the upstream value carried verbatim (a string), so the original is never lost.
- **coded columns** — a DfT **integer code**. Missing/unknown sentinels (and DfT's universal
  `-1`) become `NULL`; the code is decoded to its English label on demand via the `codebook`
  join, exposed by the `*_labelled` views. Codes are never stored as labels.
- **numeric columns** — typed to `INTEGER` or `DOUBLE`; blank / `-1` / non-numeric → `NULL`.
- **text columns** — carried raw (free-text or ONS string codes the integer codebook can't decode).
- **time** — `*_local` columns are UK civil time (`Europe/London`); `*_utc` exists only for
  UTC-native sources (weather); machine `ingested_at` / `built_at_utc` stamps are UTC provenance.
- **`source_row_key`** — a stable, unique per-row key shared by the silver table and the
  `data_quality_log`, so every rejection can be traced back to its row.

Column classifications (coded / numeric / text) come from `column_manifest`; code meanings
come from `codebook` (both documented below).

---

## Silver: analytical tables

### `collisions`

One row per reported collision (STATS19 Collision file, keep-in-place 1:1 with bronze).

```sql
CREATE TABLE collisions (
    source_row_key         VARCHAR,     -- stable row key; for collisions == accident_index (globally unique)
    accident_index         VARCHAR,     -- DfT natural key (accident_index / collision_index); primary identifier
    accident_year          VARCHAR,     -- DfT reporting year, carried raw
    accident_reference     VARCHAR,     -- DfT per-force reference (collision_ref_no / accident_reference), carried raw
    easting_raw            VARCHAR,     -- raw OSGR easting exactly as published (preserved)
    northing_raw           VARCHAR,     -- raw OSGR northing exactly as published (preserved)
    easting                DOUBLE,      -- typed OSGR easting, EPSG:27700 metres; NULL if sentinel(0/-1)/blank/non-numeric
    northing               DOUBLE,      -- typed OSGR northing, EPSG:27700 metres; NULL if missing
    geom                   GEOMETRY,    -- POINT(EPSG:27700) from easting/northing; NULL if either coordinate invalid
    geom_valid             BOOLEAN,     -- TRUE only when a real point was derived (drives collisions_spatial)
    date_raw               VARCHAR,     -- raw collision date as published (DD/MM/YYYY)
    time_raw               VARCHAR,     -- raw collision time as published (HH:MM; may be blank)
    datetime_local         TIMESTAMP,   -- collision timestamp, UK local; midnight fallback when only the date parsed
    datetime_valid         BOOLEAN,     -- TRUE when the DATE parsed (a missing time is not a rejection)
    lad_code               VARCHAR,     -- ONS LAD code stamped by point-in-polygon join; NULL if geom invalid / no boundary built
    ctyua_code             VARCHAR,     -- ONS CTYUA code stamped by point-in-polygon join; NULL if geom invalid / no boundary built
    temperature_c          DOUBLE,      -- 2 m air temperature (°C) from the ERA5-Land cell at the collision hour; NULL if weather not built / no match
    precipitation_mm       DOUBLE,      -- hourly precipitation (mm) from the ERA5-Land cell; NULL if weather not built / no match
    collision_severity_raw VARCHAR,     -- raw severity code as published (kept for the ledger)
    collision_severity     INTEGER,     -- cleaned code 1=Fatal 2=Serious 3=Slight (codebook); NULL if a missing sentinel
    collision_severity_valid BOOLEAN,   -- FALSE when the severity code was a missing/unparseable sentinel
    -- remaining columns are carried per column_manifest (coded/numeric/text), alphabetically:
    carriageway_hazards    INTEGER,     -- coded: object/animal in carriageway
    carriageway_hazards_historic INTEGER, -- coded: pre-2015 carriageway-hazards coding
    collision_adjusted_severity_serious DOUBLE, -- numeric: probabilistic serious-severity adjustment weight
    collision_adjusted_severity_slight  DOUBLE, -- numeric: probabilistic slight-severity adjustment weight
    collision_injury_based INTEGER,     -- coded: whether severity is injury-based (post-adjustment marker)
    day_of_week            INTEGER,     -- coded: 1=Sunday … 7=Saturday
    did_police_officer_attend_scene_of_accident INTEGER, -- coded: police attendance
    enhanced_severity_collision INTEGER, -- coded, but NO codebook coverage in the 2024 guide (label stays NULL)
    first_road_class       INTEGER,     -- coded: class of the first (primary) road
    first_road_number      INTEGER,     -- numeric: road number of the first road (0 = not applicable)
    junction_control       INTEGER,     -- coded: control at the junction
    junction_detail        INTEGER,     -- coded: junction type
    junction_detail_historic INTEGER,   -- coded: pre-2015 junction-detail coding
    latitude               DOUBLE,      -- numeric: DfT-published WGS84 latitude (EPSG:4326), carried raw-typed
    light_conditions       INTEGER,     -- coded: lighting at the time
    local_authority_district INTEGER,   -- coded: DfT integer LA district code
    local_authority_highway  VARCHAR,   -- text: ONS highway-authority string code, carried raw
    local_authority_highway_current VARCHAR, -- text: current ONS highway-authority string code, carried raw
    local_authority_ons_district VARCHAR,    -- text: ONS district string code (e.g. E06000036), carried raw
    longitude              DOUBLE,      -- numeric: DfT-published WGS84 longitude (EPSG:4326), carried raw-typed
    lsoa_of_accident_location VARCHAR,  -- text: ONS LSOA code of the collision location
    number_of_casualties   INTEGER,     -- numeric: count of casualties in this collision
    number_of_vehicles     INTEGER,     -- numeric: count of vehicles in this collision
    pedestrian_crossing    INTEGER,     -- coded: pedestrian-crossing facilities (current coding)
    pedestrian_crossing_human_control_historic INTEGER,       -- coded: pre-2015 human-controlled crossing
    pedestrian_crossing_physical_facilities_historic INTEGER, -- coded: pre-2015 physical crossing facilities
    police_force           INTEGER,     -- coded: reporting police force
    road_surface_conditions INTEGER,    -- coded: road surface state (dry/wet/snow/…)
    road_type              INTEGER,     -- coded: carriageway type (single/dual/roundabout/…)
    second_road_class      INTEGER,     -- coded: class of the second road at a junction
    second_road_number     INTEGER,     -- numeric: road number of the second road (0 = not applicable)
    special_conditions_at_site INTEGER, -- coded: special site conditions (signals out, roadworks, …)
    speed_limit            INTEGER,     -- numeric: posted speed limit (mph)
    trunk_road_flag        INTEGER,     -- coded: trunk-road indicator
    urban_or_rural_area    INTEGER,     -- coded: urban/rural classification
    weather_conditions     INTEGER      -- coded: weather at the time (DfT's own field; distinct from ERA5-Land)
);
```

### `vehicles`

One row per vehicle involved in a collision (STATS19 Vehicle file).

```sql
CREATE TABLE vehicles (
    source_row_key         VARCHAR,     -- stable row key: accident_index | vehicle_reference
    accident_index         VARCHAR,     -- parent collision key (link to collisions.accident_index)
    vehicle_reference      VARCHAR,     -- DfT vehicle number within the collision
    link_valid             BOOLEAN,     -- TRUE when accident_index matches a collision row; FALSE = orphan (logged, not dropped)
    accident_reference     VARCHAR,     -- carried raw
    accident_year          VARCHAR,     -- carried raw
    -- carried per column_manifest (coded/numeric/text), alphabetically:
    age_band_of_driver     INTEGER,     -- coded: banded driver age
    age_of_driver          INTEGER,     -- numeric: driver age in years
    age_of_vehicle         INTEGER,     -- numeric: vehicle age in years
    driver_distance_banding INTEGER,    -- coded: banded home-to-collision distance for the driver
    driver_imd_decile      INTEGER,     -- coded: Index of Multiple Deprivation decile of the driver's home
    engine_capacity_cc     INTEGER,     -- numeric: engine capacity in cc
    escooter_flag          INTEGER,     -- coded: e-scooter indicator (0/1)
    first_point_of_impact  INTEGER,     -- coded: where the vehicle was first hit
    generic_make_model     VARCHAR,     -- text: generic make/model string
    hit_object_in_carriageway INTEGER,  -- coded: object struck in the carriageway
    hit_object_off_carriageway INTEGER, -- coded: object struck off the carriageway
    journey_purpose_of_driver INTEGER,  -- coded: journey purpose (current coding)
    journey_purpose_of_driver_historic INTEGER, -- coded: pre-2015 journey-purpose coding
    junction_location      INTEGER,     -- coded: vehicle location relative to a junction
    lsoa_of_driver         VARCHAR,     -- text: ONS LSOA code of the driver's home
    propulsion_code        INTEGER,     -- coded: fuel/propulsion type
    sex_of_driver          INTEGER,     -- coded: driver sex
    skidding_and_overturning INTEGER,   -- coded: skidding/overturning event
    towing_and_articulation INTEGER,    -- coded: towing/articulation state
    vehicle_direction_from INTEGER,     -- coded: compass direction the vehicle was travelling from
    vehicle_direction_to   INTEGER,     -- coded: compass direction the vehicle was travelling to
    vehicle_leaving_carriageway INTEGER, -- coded: whether/how the vehicle left the carriageway
    vehicle_left_hand_drive INTEGER,    -- coded: left-hand-drive indicator
    vehicle_location_restricted_lane INTEGER,          -- coded: vehicle in a restricted lane (current coding)
    vehicle_location_restricted_lane_historic INTEGER, -- coded: pre-2015 restricted-lane coding
    vehicle_manoeuvre      INTEGER,     -- coded: manoeuvre at the time (current coding)
    vehicle_manoeuvre_historic INTEGER, -- coded: pre-2015 manoeuvre coding
    vehicle_type           INTEGER      -- coded: vehicle type (car/HGV/pedal cycle/…)
);
```

### `casualties`

One row per casualty (STATS19 Casualty file).

```sql
CREATE TABLE casualties (
    source_row_key         VARCHAR,     -- stable row key: accident_index | vehicle_reference | casualty_reference
    accident_index         VARCHAR,     -- parent collision key (link to collisions.accident_index)
    vehicle_reference      VARCHAR,     -- vehicle the casualty was in/associated with
    casualty_reference     VARCHAR,     -- DfT casualty number within the collision
    link_valid             BOOLEAN,     -- TRUE when accident_index matches a collision row; FALSE = orphan (logged, not dropped)
    casualty_severity_raw  VARCHAR,     -- raw severity code as published (kept for the ledger)
    casualty_severity      INTEGER,     -- cleaned code 1=Fatal 2=Serious 3=Slight (codebook); NULL if a missing sentinel
    casualty_severity_valid BOOLEAN,    -- FALSE when the severity code was a missing/unparseable sentinel
    accident_reference     VARCHAR,     -- carried raw
    accident_year          VARCHAR,     -- carried raw
    -- carried per column_manifest (coded/numeric/text), alphabetically:
    age_band_of_casualty   INTEGER,     -- coded: banded casualty age
    age_of_casualty        INTEGER,     -- numeric: casualty age in years
    bus_or_coach_passenger INTEGER,     -- coded: bus/coach passenger status
    car_passenger          INTEGER,     -- coded: car passenger status
    casualty_adjusted_severity_serious DOUBLE, -- numeric: probabilistic serious-severity adjustment weight
    casualty_adjusted_severity_slight  DOUBLE, -- numeric: probabilistic slight-severity adjustment weight
    casualty_class         INTEGER,     -- coded: driver/rider, passenger, or pedestrian
    casualty_distance_banding INTEGER,  -- coded: banded home-to-collision distance for the casualty
    casualty_imd_decile    INTEGER,     -- coded: Index of Multiple Deprivation decile of the casualty's home
    casualty_injury_based  INTEGER,     -- coded: whether severity is injury-based (post-adjustment marker)
    casualty_type          INTEGER,     -- coded: casualty road-user type
    enhanced_casualty_severity INTEGER, -- coded: enhanced (probability-based) casualty severity
    lsoa_of_casualty       VARCHAR,     -- text: ONS LSOA code of the casualty's home
    pedestrian_location    INTEGER,     -- coded: pedestrian location at the time
    pedestrian_movement    INTEGER,     -- coded: pedestrian movement/direction
    pedestrian_road_maintenance_worker INTEGER, -- coded: road-maintenance-worker indicator
    sex_of_casualty        INTEGER      -- coded: casualty sex
);
```

### `weather`

One row per ERA5-Land grid cell per hour (0.1° land reanalysis; only built with the
`weather` extra and a selected weather source). See [transformers/weather.py](../src/crossroads/transformers/weather.py).

```sql
CREATE TABLE weather (
    source_row_key         VARCHAR,     -- stable row key: grid_i | grid_j | YYYYMMDDHH (unique per cell-hour)
    latitude               DOUBLE,      -- ERA5-Land cell-centroid latitude (EPSG:4326 degrees)
    longitude              DOUBLE,      -- ERA5-Land cell-centroid longitude (EPSG:4326 degrees)
    grid_i                 INTEGER,     -- integer grid index round(latitude * 10) — STATS19's weather join key
    grid_j                 INTEGER,     -- integer grid index round(longitude * 10) — STATS19's weather join key
    geom                   GEOMETRY,    -- POINT(EPSG:27700) centroid, reprojected once from lon/lat
    geom_valid             BOOLEAN,     -- TRUE when coordinates were present (a centroid always reprojects)
    valid_time_utc         TIMESTAMP,   -- raw hourly instant, UTC (ERA5-Land is UTC-native)
    valid_time_local       TIMESTAMP,   -- same instant expressed in UK civil time (Europe/London)
    temperature_c          DOUBLE,      -- 2 m air temperature in °C (t2m − 273.15); NULL over sea/NaN cells
    precipitation_mm       DOUBLE       -- hourly total precipitation in mm (tp × 1000); NULL over sea/NaN cells
);
```

### `lad_boundaries`

One row per ONS Local Authority District boundary per vintage. See
[transformers/spatial.py](../src/crossroads/transformers/spatial.py).

```sql
CREATE TABLE lad_boundaries (
    source_row_key         VARCHAR,     -- stable row key: area_code | vintage
    area_code              VARCHAR,     -- ONS LAD code (e.g. E06000001)
    area_name              VARCHAR,     -- ONS LAD name
    vintage                VARCHAR,     -- boundary edition label (e.g. "2024")
    geom                   GEOMETRY,    -- boundary polygon, EPSG:27700 (native ONS BGC; not reprojected)
    geom_valid             BOOLEAN,     -- TRUE when geom is non-NULL and passes ST_IsValid (drives lad_boundaries_clean)
    valid_from             DATE,        -- date this vintage takes effect (temporal mode)
    valid_to               DATE         -- date this vintage was superseded; NULL for the current/open vintage
);
```

> **Snapshot builds:** in the default `snapshot` boundary mode only the current (open-ended)
> vintage is loaded, so `valid_to` is entirely `NULL` and the column may materialise as an
> untyped-NULL (`INTEGER`) placeholder. In `temporal` mode it carries real `DATE` values.

### `ctyua_boundaries`

One row per ONS County / Unitary Authority boundary per vintage — identical shape to
`lad_boundaries`.

```sql
CREATE TABLE ctyua_boundaries (
    source_row_key         VARCHAR,     -- stable row key: area_code | vintage
    area_code              VARCHAR,     -- ONS CTYUA code
    area_name              VARCHAR,     -- ONS CTYUA name
    vintage                VARCHAR,     -- boundary edition label (e.g. "2024")
    geom                   GEOMETRY,    -- boundary polygon, EPSG:27700 (native ONS BGC; not reprojected)
    geom_valid             BOOLEAN,     -- TRUE when geom is non-NULL and passes ST_IsValid (drives ctyua_boundaries_clean)
    valid_from             DATE,        -- date this vintage takes effect (temporal mode)
    valid_to               DATE         -- date this vintage was superseded; NULL for the current/open vintage
);
```

## Gold: clean views

Filtered projections researchers query by default — no new data, derived from silver:

- **`collisions_spatial`** — `SELECT * FROM collisions WHERE geom_valid` (valid-geometry
  collisions; the default spatial surface).
- **`<source>_clean`** (`vehicles_clean`, `casualties_clean`, `weather_clean`,
  `lad_boundaries_clean`, `ctyua_boundaries_clean`) — the silver table filtered to its own
  validity flag (`link_valid` for vehicles/casualties, `geom_valid` for weather/boundaries).
- **`<source>_labelled`** (`collisions_labelled`, `vehicles_labelled`, `casualties_labelled`)
  — the silver table plus an `<column>_label` text twin for every coded column, decoded via
  the `codebook` join. Labels are computed on demand, never stored; query the silver table for
  the "translation off" (codes-only) surface.

## Provenance, quality & reference tables

Source-agnostic audit and reference tables — one set per database. See [spec.md §9](spec.md).

### `crossroads_meta`

Single-row provenance stamp describing what built this database (Stage 07). Re-running
`build()` replaces the row.

```sql
CREATE TABLE crossroads_meta (
    crossroads_version     VARCHAR,     -- the git-derived crossroads package version that built this DB
    schema_version         INTEGER,     -- monotonic integer schema marker (== crossroads.SCHEMA_VERSION at build time)
    built_at_utc           TIMESTAMP,   -- build time, UTC (provenance only; excluded from the reproducibility guarantee)
    parameters             VARCHAR      -- JSON of the build parameters (datasets, years, boundary_mode)
);
```

### `data_quality_log`

The exclusion ledger: one row per rule violation — the queryable "why was this flagged?".

```sql
CREATE TABLE data_quality_log (
    source_id              VARCHAR,     -- audited source that logged the row (e.g. stats19_collision)
    source_row_key         VARCHAR,     -- key of the affected silver row (joins back to the silver table)
    column_name            VARCHAR,     -- the column/dimension the rule concerns (e.g. geom, datetime_local)
    rule_id                VARCHAR,     -- stable rule identifier (e.g. stats19.coord.sentinel)
    rule_desc              VARCHAR,     -- human-readable reason
    severity               VARCHAR,     -- 'reject_dimension' (nulls the value) or 'warn' (informational)
    raw_value              VARCHAR,     -- the offending raw value, for inspection
    ingested_at            TIMESTAMP    -- when the row was written, UTC (DB-stamped provenance)
);
```

### `quarantine_raw`

Source lines that could not be structured into bronze at all (rare).

```sql
CREATE TABLE quarantine_raw (
    source_id              VARCHAR,     -- source the unparseable line came from
    raw_text               VARCHAR,     -- the raw line/text that failed to parse
    reason                 VARCHAR,     -- why it was quarantined
    ingested_at            TIMESTAMP    -- when the row was written, UTC (DB-stamped provenance)
);
```

### `source_ingest_log`

How many rows each source READ from its source files — one row per source. Drives the
build-end conservation invariant (`source_rows == bronze + quarantine`).

```sql
CREATE TABLE source_ingest_log (
    source_id              VARCHAR,     -- audited source
    source_rows            BIGINT,      -- number of rows read from that source's files
    ingested_at            TIMESTAMP    -- when recorded, UTC (DB-stamped provenance)
);
```

### `quality_exemptions`

The auditable record of which sources DELIBERATELY opted out of the quality invariants and why.

```sql
CREATE TABLE quality_exemptions (
    source_id              VARCHAR,     -- source that opted out
    reason                 VARCHAR,     -- why the keep-in-place invariants do not apply
    ingested_at            TIMESTAMP    -- when recorded, UTC (DB-stamped provenance)
);
```

### `stats19_completeness`

A broad "how complete is column X?" report — one row per cleaned coded/numeric column per
STATS19 source.

```sql
CREATE TABLE stats19_completeness (
    source_id              VARCHAR,     -- STATS19 source (collision/vehicle/casualty)
    column_name            VARCHAR,     -- the cleaned column measured
    kind                   VARCHAR,     -- column kind from the manifest (coded/numeric)
    n_total                BIGINT,      -- total rows in the silver table
    n_present              BIGINT,      -- rows where the cleaned value is non-NULL
    n_missing              BIGINT,      -- rows where the cleaned value is NULL (n_total − n_present)
    missing_rate           DOUBLE       -- n_missing / n_total (0..1)
);
```

### `codebook`

Static reference lookup mapping STATS19 integer codes to DfT English labels. Not an audited
source. See [reference/README.md](../src/crossroads/reference/README.md).

```sql
CREATE TABLE codebook (
    variable               VARCHAR,     -- coded column name (e.g. collision_severity)
    code                   VARCHAR,     -- the integer code as a string (e.g. '1', '-1'), preserved exactly
    label                  VARCHAR,     -- the DfT English label for that code
    is_missing             BOOLEAN      -- TRUE when the code is a missing/unknown sentinel (nulled in silver)
);
```

### `column_manifest`

Static reference table classifying every STATS19 source column — the single source of truth
for how the keep-in-place clean treats each column. Not an audited source.

```sql
CREATE TABLE column_manifest (
    tbl                    VARCHAR,     -- source file: collision / vehicle / casualty
    col                    VARCHAR,     -- source column name
    kind                   VARCHAR,     -- identity / geo / datetime / coded / numeric / text
    dtype                  VARCHAR      -- target type for numeric/coded (INTEGER/DOUBLE), blank otherwise
);
```

## Bronze: raw landing tables

The bronze `*_raw` tables are faithful, append-only copies of each downloaded source, with the
original column names and permissive types ([spec.md §9](spec.md)). Their columns are the upstream
source's and are **not re-listed here** — see the authoritative catalogue for each source:

- **STATS19** (`stats19_collision_raw`, `stats19_vehicle_raw`, `stats19_casualty_raw`) — UK
  Department for Transport *Road Safety Open Dataset*. Columns and code lists are in the
  [DfT Data Guide and reference README](../src/crossroads/reference/README.md); the per-column
  classification also ships as the `column_manifest` table.
- **ONS boundaries** (`ons_lad_raw`, `ons_ctyua_raw`) — ONS Open Geography Portal (Local Authority
  Districts and Counties & Unitary Authorities). The exact per-vintage feature-server URLs and
  code/name columns are pinned in
  [`src/crossroads/reference/ons_boundaries.json`](../src/crossroads/reference/ons_boundaries.json).
- **ERA5-Land weather** (`era5_weather_raw`) — Copernicus Climate Data Store, ERA5-Land reanalysis
  ([cds.climate.copernicus.eu/datasets/reanalysis-era5-land](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land)).
