# Solar Geometry — Per-Collision Sun Elevation & Azimuth ("The Glare Vector")
> Engineer: execute step by step, exactly as written. This is a single-file, self-contained plan.
> Assume no memory of the conversation that produced it. Everything you need is below.

Stamp every STATS19 collision with the sun's **elevation** and **azimuth** angle at the exact
place and moment of the crash — computed purely mathematically in SQL (zero downloads) — so
researchers can isolate low-angle solar glare as a casualty factor that DfT's subjective
"Light Conditions" field misses.

---

## Context & Objective

### What exists
- The STATS19 pipeline lives in [`src/crossroads/transformers/stats19.py`](../../../src/crossroads/transformers/stats19.py).
  `Stats19Transformer.transform_and_load()` builds the `collisions` silver table via
  `_derive_collision_silver()`, then runs a sequence of **stamp** steps that `UPDATE` extra
  columns onto the already-built table:
  - `_spatial_stamp()` — point-in-polygon join to fill `lad_code` / `ctyua_code`.
  - `_weather_stamp()` — reprojects `geom` to lon/lat and fills `temperature_c` / `precipitation_mm`.
- Each stamp column is declared as a **typed-NULL placeholder** in the big `CREATE OR REPLACE
  TABLE collisions AS SELECT ...` inside `_derive_collision_silver()` (see the block that emits
  `CAST(NULL AS DOUBLE) AS temperature_c, CAST(NULL AS DOUBLE) AS precipitation_mm,`), then filled
  later by its stamp method. This is the exact pattern this feature follows.
- `collisions` already carries:
  - `geom GEOMETRY` — a `POINT` in **EPSG:27700** (British National Grid); `NULL` when coordinates
    were missing. `geom_valid BOOLEAN` is `TRUE` only when a real point was derived.
  - `datetime_local TIMESTAMP` — the collision instant in **UK civil time** (`Europe/London`);
    `datetime_valid BOOLEAN` is `TRUE` when the date parsed.
- The **ICU** DuckDB extension is the project's timezone engine. `_weather_stamp()` already
  reprojects `geom` → EPSG:4326 with
  `ST_Transform(geom, 'EPSG:27700', 'EPSG:4326', always_xy := true)` (so `ST_X` = longitude,
  `ST_Y` = latitude). The weather transformer materialises local time via
  `(valid_time_utc AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/London'` after `INSTALL icu; LOAD icu`.
- Schema version lives in [`src/crossroads/__init__.py`](../../../src/crossroads/__init__.py) as
  `SCHEMA_VERSION = 1` (a hand-maintained integer, bumped on ANY schema change).
- The data dictionary is [`docs/schema.md`](../../schema.md). Its `collisions` `CREATE TABLE` block
  lists every column, and an integration test (below) **fails the build** if a real column is
  undocumented.

### What changes
Add two derived columns to the `collisions` silver table:

| Column | Type | Meaning |
|--------|------|---------|
| `solar_elevation_deg` | `DOUBLE` | Apparent solar elevation above the horizon, degrees. **Refraction-corrected**. Negative = sun below the horizon (night/twilight). `NULL` when `geom` or `datetime_local` is missing. |
| `solar_azimuth_deg`   | `DOUBLE` | Solar azimuth, degrees **clockwise from true North** (0 = N, 90 = E, 180 = S, 270 = W). `NULL` under the same condition. |

Both are filled by a new `_solar_stamp()` method, computed with the standard **NOAA solar
position algorithm** implemented as deterministic SQL arithmetic — no new Python dependency, no
per-row Python loop, in-database like every other transform (spec §3A).

### Goal
A researcher can write, with no extra tooling:
```sql
-- Blinding low morning sun on (roughly) east-facing travel, in clear weather.
SELECT count(*) FROM collisions_spatial
WHERE solar_elevation_deg BETWEEN 0 AND 15      -- low sun, above the horizon
  AND solar_azimuth_deg   BETWEEN 45 AND 135     -- sun in the eastern sky (morning)
  AND weather_conditions = 1;                    -- DfT "Fine no high winds" (clear)
```

### Locked design decisions (from planning)
1. **Compute engine:** pure-SQL NOAA algorithm. No `ephem`/`astral`/`pvlib`/`suntime` dependency.
   (`suntime` only yields sunrise/sunset, not per-timestamp angles, so it could not satisfy the goal
   anyway; a Python library would also add a per-row loop that spec §3A avoids and risk numeric
   drift against the reproducibility guarantee.)
2. **Output surface:** the two angle columns only. **No** convenience "glare" view or baked-in
   threshold flag — the worked query above is documented, thresholds stay the researcher's choice.
3. **Instant handling & spec §2:** the true instant needed by the math is derived from
   `datetime_local` via ICU (`Europe/London` → instant) **purely to feed the calculation**. Only the
   resulting **angles** are stored — never a `*_utc` timestamp column — so the spec §2 "never
   reconstruct a UTC instant for a local-native source" rule (which forbids storing an invented
   `_utc` column) is respected. The one autumn DST fall-back hour is ambiguous; ICU resolves it
   deterministically and the sun is below the horizon during it anyway. This is the same ICU-based
   determinism the weather source already relies on for `valid_time_local`; document the caveat.
4. **Position source:** reproject `geom` (EPSG:27700 → EPSG:4326) in SQL, exactly as
   `_weather_stamp()` does. `geom` is the single spatial source of truth, so the angles are non-NULL
   **exactly** when `geom_valid AND datetime_valid` — both already audited upstream — meaning **no
   new quality dimension and no new `data_quality_log` rows** are required.

---

## Acceptance Criteria (verifiable "done")
1. After a STATS19 build, `collisions` has `solar_elevation_deg DOUBLE` and
   `solar_azimuth_deg DOUBLE`.
2. For every collision row: the two angles are **non-NULL iff** `geom IS NOT NULL AND
   datetime_local IS NOT NULL`. (When `geom` or `datetime_local` is NULL, both angles are NULL.)
3. Ranges hold for all non-NULL rows: `solar_elevation_deg` ∈ [−90, +90],
   `solar_azimuth_deg` ∈ [0, 360).
4. **Numeric correctness** against an independent hand-check (details in Testing): for a collision
   at London (≈51.50 °N, 0.13 °W) at **2023-12-22 12:00 GMT**, `solar_elevation_deg` ≈ **15.1°**
   (± 1.5°) and `solar_azimuth_deg` ≈ **180°** (± 5°, due south at winter-solstice noon); and at
   **2023-12-22 00:00 GMT** the elevation is **negative** (sun below horizon).
5. `SCHEMA_VERSION` is incremented to `2`; `docs/schema.md` documents both new columns and its
   "Schema version:" line reads `2`.
6. The full offline suite is green: `pytest` passes, **and** the release drift guard
   `pytest -m integration tests/test_schema_doc.py` passes (proves the doc matches the built DB).
7. No new dependency appears in `pyproject.toml`. The conservation and flag/ledger invariants still
   pass unchanged (no rows added/removed, no new audited dimension).

---

## Scope
**In:**
- Two new derived columns on `collisions`, filled by a new `_solar_stamp()` in `stats19.py`.
- `SCHEMA_VERSION` bump, `docs/schema.md` update, `CHANGELOG.md` entry.
- Unit + integration tests for the stamp.
- A short methodology note documenting the algorithm, the worked query, and the DST/ICU caveat.

**Out:**
- Any convenience view/flag (`collisions_glare`, boolean glare markers) — explicitly excluded.
- A `*_utc` column, or any stored instant derived from local time.
- Sunrise/sunset times, solar-noon, day-length, or twilight classification columns.
- Applying solar geometry to any other table (weather, boundaries) — collisions only.
- New CLI flags: the stamp is free and always-on during a STATS19 build (like the weather stamp,
  it self-guards and no-ops when prerequisites are absent).

---

## Constraints
- **Compatibility:** the stamp must not break a **STATS19-only build** (no weather, no boundaries).
  It must `INSTALL icu; LOAD icu` itself (idempotent) because ICU may not be loaded when weather is
  not built. It must no-op cleanly when there are zero valid rows.
- **Determinism / reproducibility (spec §2):** identical version + parameters ⇒ identical angles to
  double precision. All arithmetic is closed-form; the only external input is the ICU timezone
  database (same dependency the weather `valid_time_local` already accepts). No `Date.now()`-style
  or environment-dependent values enter the calc.
- **Performance (spec §3A):** one set-based SQL `UPDATE`, no Python per-row loop. See Performance.
- **Style:** match `stats19.py` conventions — a private `_solar_stamp(self, con)` method with a
  plain-language docstring, called from `transform_and_load()` alongside the other stamps; SQL
  built from code-controlled identifiers only, no row values interpolated.
- **Dependencies:** none added. DuckDB core math (`radians`, `degrees`, `sin`, `cos`, `tan`,
  `asin`, `acos`, `atan2`, `epoch`, `mod`), `ST_Transform`/`ST_X`/`ST_Y` (spatial), and ICU
  (`AT TIME ZONE`) are all already in use.

---

## Approach / Architecture

### Data flow
```
collisions (already built, geom + datetime_local present)
   └─ _solar_stamp(con):
        INSTALL icu; LOAD icu
        for rows WHERE geom IS NOT NULL AND datetime_local IS NOT NULL:
            lon,lat  = ST_X/ST_Y( ST_Transform(geom, 27700 -> 4326, always_xy) )
            es       = epoch( datetime_local AT TIME ZONE 'Europe/London' )   -- UTC instant, seconds
            (elev,az) = NOAA(lat, lon, es)                                    -- pure SQL arithmetic
        UPDATE collisions SET solar_elevation_deg=elev, solar_azimuth_deg=az
```
The instant is obtained in one conversion: `epoch(ts AT TIME ZONE 'Europe/London')` interprets the
naive local timestamp as `Europe/London` and returns the absolute instant's epoch seconds (UTC-based).
The Julian date and UTC minutes-of-day both derive from that single number, so no separate `_utc`
value is ever materialised.

### Why a separate stamp method (not inline in `_derive_collision_silver`)
The calc needs `geom` (built in the outer SELECT of `_derive_collision_silver`) and an ICU
conversion of `datetime_local`. SQL cannot reference sibling `SELECT`-list aliases, and `geom` does
not exist until that statement completes — so the calc cannot live in the same SELECT. A post-build
`UPDATE` is exactly how `_spatial_stamp` and `_weather_stamp` already solve the identical ordering
problem. **Rejected alternative:** a third level of CTE inside `_derive_collision_silver` — it would
bloat an already-large statement and diverge from the established stamp pattern.

### Why pure SQL (not a Python library)
Rejected `ephem`/`astral`/`pvlib`: each adds a dependency and a Python row loop (spec §3A avoids
pulling rows into Python), and different library versions can produce slightly different numbers,
weakening the reproducibility guarantee (spec §2). The NOAA algorithm is ~20 closed-form arithmetic
steps — trivial, exact, and stays in-database.

### The NOAA solar position algorithm (reference implementation)
This is the standard NOAA calculation (the same math behind the NOAA Solar Calculator). All angles
are in **degrees** unless a `radians()` wrapper is shown. Inputs per row: `lat`, `lon`
(east-positive degrees) and `es` = epoch seconds of the UTC instant.

```
JD   = es / 86400.0 + 2440587.5                      -- Julian Day from Unix epoch seconds
JC   = (JD - 2451545.0) / 36525.0                    -- Julian Century
utcMin = mod(es, 86400) / 60.0                        -- minutes past UTC midnight (0..1440)

L0   = mod(280.46646 + JC*(36000.76983 + JC*0.0003032), 360)          -- geom mean longitude
M    = 357.52911 + JC*(35999.05029 - 0.0001537*JC)                    -- geom mean anomaly
ecc  = 0.016708634 - JC*(0.000042037 + 0.0000001267*JC)               -- orbital eccentricity
C    = sin(radians(M))    * (1.914602 - JC*(0.004817 + 0.000014*JC))
     + sin(radians(2*M))  * (0.019993 - 0.000101*JC)
     + sin(radians(3*M))  *  0.000289                                  -- equation of centre
trueLong = L0 + C
appLong  = trueLong - 0.00569 - 0.00478*sin(radians(125.04 - 1934.136*JC))   -- apparent longitude
meanObliq= 23 + (26 + (21.448 - JC*(46.815 + JC*(0.00059 - JC*0.001813)))/60)/60
obliq    = meanObliq + 0.00256*cos(radians(125.04 - 1934.136*JC))            -- corrected obliquity
declin   = degrees(asin( sin(radians(obliq)) * sin(radians(appLong)) ))      -- solar declination

vy   = tan(radians(obliq/2)) * tan(radians(obliq/2))
EoT  = 4 * degrees( vy*sin(2*radians(L0))
                    - 2*ecc*sin(radians(M))
                    + 4*ecc*vy*sin(radians(M))*cos(2*radians(L0))
                    - 0.5*vy*vy*sin(4*radians(L0))
                    - 1.25*ecc*ecc*sin(2*radians(M)) )                 -- equation of time, minutes

TST  = mod(utcMin + EoT + 4*lon, 1440)                                 -- true solar time, minutes
                                                                       -- (tz term = 0: we use UTC)
HA   = CASE WHEN TST/4 < 0 THEN TST/4 + 180 ELSE TST/4 - 180 END       -- hour angle, degrees

-- clamp the acos argument to [-1, 1] to absorb floating-point error at the poles/horizon
zenArg = greatest(-1, least(1,
           sin(radians(lat))*sin(radians(declin))
         + cos(radians(lat))*cos(radians(declin))*cos(radians(HA)) ))
zenith = degrees(acos(zenArg))
elev0  = 90 - zenith                                                   -- geometric elevation

-- atmospheric refraction correction (degrees), piecewise on elev0
refr = CASE
    WHEN elev0 > 85      THEN 0
    WHEN elev0 > 5       THEN ( 58.1/tan(radians(elev0))
                              - 0.07/pow(tan(radians(elev0)),3)
                              + 0.000086/pow(tan(radians(elev0)),5) ) / 3600
    WHEN elev0 > -0.575  THEN ( 1735 + elev0*(-518.2 + elev0*(103.4
                              + elev0*(-12.79 + elev0*0.711))) ) / 3600
    ELSE                      ( -20.772/tan(radians(elev0)) ) / 3600
END
elevation = elev0 + refr                                               -- SOLAR_ELEVATION_DEG

-- azimuth (degrees clockwise from true north); clamp acos arg again
azArg = greatest(-1, least(1,
          ( sin(radians(lat))*cos(radians(zenith)) - sin(radians(declin)) )
          / ( cos(radians(lat))*sin(radians(zenith)) ) ))
azimuth = CASE
    WHEN HA > 0 THEN mod( degrees(acos(azArg)) + 180, 360)
    ELSE             mod( 540 - degrees(acos(azArg)), 360)
END                                                                    -- SOLAR_AZIMUTH_DEG
```

Notes for the implementer:
- DuckDB trig functions take **radians**; use `radians()`/`degrees()` for conversion. `pow(x,n)` is
  available (or `x*x*x`). `mod(a,b)` on doubles is fine; the `mod(..., 360)`/`mod(..., 1440)`
  expressions above are always applied to non-negative operands in practice, but if you prefer a
  defensive positive modulo use `((mod(a,b)) + b)` then `mod(..., b)` again.
- `cos(radians(lat))*sin(radians(zenith))` is only zero at the exact pole or exact zenith — outside
  the UK/collision domain — but the `greatest/least` clamp already prevents a NaN; a divide-by-zero
  there would yield `±inf`→ clamp→ a defined azimuth, which is acceptable for those degenerate points.

---

## Implementation Steps

### Step 1 — Declare the two placeholder columns in `collisions` silver
File: [`src/crossroads/transformers/stats19.py`](../../../src/crossroads/transformers/stats19.py),
inside `_derive_collision_silver()`, in the outer `SELECT` list of the
`CREATE OR REPLACE TABLE {self.COLLISION_SILVER} AS ...` statement.

Find the existing placeholder block:
```python
            # Filled by _weather_stamp when a weather table exists; NULL otherwise
            # (mirrors lad_code — collisions always carry these columns). DOUBLE:
            # Celsius and millimetres.
            f"  CAST(NULL AS DOUBLE) AS temperature_c, "
            f"  CAST(NULL AS DOUBLE) AS precipitation_mm, "
```
Immediately **after** the `precipitation_mm` line (still before the `sev_raw` line), add:
```python
            # Filled by _solar_stamp: the sun's apparent elevation and azimuth at the
            # collision's place/time, computed mathematically (NOAA solar position). NULL
            # until stamped, and left NULL where geom or datetime_local is missing. DOUBLE
            # degrees: elevation above horizon (refraction-corrected, negative = night),
            # azimuth clockwise from true north (0=N/90=E/180=S/270=W).
            f"  CAST(NULL AS DOUBLE) AS solar_elevation_deg, "
            f"  CAST(NULL AS DOUBLE) AS solar_azimuth_deg, "
```
Expected result: the built `collisions` table has the two new columns (all NULL) after any build,
even before the stamp runs.

### Step 2 — Add the `_solar_stamp()` method
File: same. Add a new method next to `_weather_stamp()` (place it directly **after**
`_weather_stamp`). Use this exact implementation:

```python
    def _solar_stamp(self, con):
        """Stamp solar_elevation_deg / solar_azimuth_deg onto every collision that has a
        geometry AND a parsed local datetime, computed mathematically (NOAA solar position
        algorithm) — no download, no new dependency, all in-database (spec §3A).

        Position: reproject the EPSG:27700 geom back to lon/lat (same call _weather_stamp
        uses). Instant: epoch(datetime_local AT TIME ZONE 'Europe/London') gives the true
        UTC instant in seconds — ICU resolves the Europe/London offset (GMT/BST), the same
        engine the weather source uses for valid_time_local. Only the resulting ANGLES are
        stored; no *_utc column is ever materialised (spec §2 keeps local-native sources
        free of reconstructed UTC instants). Rows without geom or datetime_local stay NULL —
        they inherit the already-audited geom_valid / datetime_valid flags, so this stamp
        adds no new quality dimension and no ledger rows.

        ICU is loaded here (idempotent) because a STATS19-only build may not have loaded it.
        acos arguments are clamped to [-1, 1] to absorb floating-point error near the horizon.
        The UPDATE is set-based over the whole table (no Python row loop)."""
        con.execute("INSTALL icu"); con.execute("LOAD icu")   # for AT TIME ZONE below
        con.execute(
            f"UPDATE {self.COLLISION_SILVER} AS c "
            f"SET solar_elevation_deg = m.elevation, solar_azimuth_deg = m.azimuth "
            f"FROM ("
            f"  WITH base AS ("
            f"    SELECT source_row_key AS k, "
            f"           ST_X(ll) AS lon, ST_Y(ll) AS lat, "
            f"           epoch(datetime_local AT TIME ZONE 'Europe/London') AS es "
            f"    FROM ("
            f"      SELECT source_row_key, datetime_local, "
            f"             ST_Transform(geom, 'EPSG:27700', 'EPSG:4326', always_xy := true) AS ll "
            f"      FROM {self.COLLISION_SILVER} "
            f"      WHERE geom IS NOT NULL AND datetime_local IS NOT NULL"
            f"    )"
            f"  ), astro AS ("      # date-only astronomical terms (independent of lat/lon)
            f"    SELECT k, lon, lat, mod(es, 86400) / 60.0 AS utc_min, "
            f"           (es / 86400.0 + 2440587.5 - 2451545.0) / 36525.0 AS jc "
            f"    FROM base"
            f"  ), sun AS ("
            f"    SELECT k, lon, lat, utc_min, jc, "
            f"           mod(280.46646 + jc*(36000.76983 + jc*0.0003032), 360) AS l0, "
            f"           357.52911 + jc*(35999.05029 - 0.0001537*jc) AS m, "
            f"           0.016708634 - jc*(0.000042037 + 0.0000001267*jc) AS ecc "
            f"    FROM astro"
            f"  ), sun2 AS ("
            f"    SELECT k, lon, lat, utc_min, jc, l0, m, ecc, "
            f"           sin(radians(m))*(1.914602 - jc*(0.004817 + 0.000014*jc)) "
            f"           + sin(radians(2*m))*(0.019993 - 0.000101*jc) "
            f"           + sin(radians(3*m))*0.000289 AS c "
            f"    FROM sun"
            f"  ), sun3 AS ("
            f"    SELECT k, lon, lat, utc_min, jc, l0, m, ecc, "
            f"           (l0 + c) - 0.00569 - 0.00478*sin(radians(125.04 - 1934.136*jc)) AS app_long, "
            f"           23 + (26 + (21.448 - jc*(46.815 + jc*(0.00059 - jc*0.001813)))/60)/60 "
            f"           + 0.00256*cos(radians(125.04 - 1934.136*jc)) AS obliq "
            f"    FROM sun2"
            f"  ), terms AS ("
            f"    SELECT k, lon, lat, utc_min, l0, m, ecc, "
            f"           degrees(asin(sin(radians(obliq))*sin(radians(app_long)))) AS declin, "
            f"           tan(radians(obliq/2)) * tan(radians(obliq/2)) AS vy "
            f"    FROM sun3"
            f"  ), solartime AS ("
            f"    SELECT k, lon, lat, declin, "
            f"           mod(utc_min "
            f"               + 4*degrees(vy*sin(2*radians(l0)) - 2*ecc*sin(radians(m)) "
            f"                 + 4*ecc*vy*sin(radians(m))*cos(2*radians(l0)) "
            f"                 - 0.5*vy*vy*sin(4*radians(l0)) - 1.25*ecc*ecc*sin(2*radians(m))) "
            f"               + 4*lon, 1440) AS tst "
            f"    FROM terms"
            f"  ), angles AS ("
            f"    SELECT k, lat, declin, "
            f"           CASE WHEN tst/4 < 0 THEN tst/4 + 180 ELSE tst/4 - 180 END AS ha "
            f"    FROM solartime"
            f"  ), zen AS ("
            f"    SELECT k, lat, declin, ha, "
            f"           degrees(acos(greatest(-1, least(1, "
            f"             sin(radians(lat))*sin(radians(declin)) "
            f"             + cos(radians(lat))*cos(radians(declin))*cos(radians(ha)))))) AS zenith "
            f"    FROM angles"
            f"  ) "
            f"  SELECT k, "
            f"    (90 - zenith) + CASE "
            f"       WHEN (90 - zenith) > 85     THEN 0 "
            f"       WHEN (90 - zenith) > 5      THEN (58.1/tan(radians(90 - zenith)) "
            f"           - 0.07/pow(tan(radians(90 - zenith)),3) "
            f"           + 0.000086/pow(tan(radians(90 - zenith)),5))/3600 "
            f"       WHEN (90 - zenith) > -0.575 THEN (1735 + (90 - zenith)*(-518.2 + (90 - zenith)*(103.4 "
            f"           + (90 - zenith)*(-12.79 + (90 - zenith)*0.711))))/3600 "
            f"       ELSE (-20.772/tan(radians(90 - zenith)))/3600 END AS elevation, "
            f"    CASE WHEN ha > 0 "
            f"      THEN mod(degrees(acos(greatest(-1, least(1, "
            f"           (sin(radians(lat))*cos(radians(zenith)) - sin(radians(declin))) "
            f"           / (cos(radians(lat))*sin(radians(zenith))))))) + 180, 360) "
            f"      ELSE mod(540 - degrees(acos(greatest(-1, least(1, "
            f"           (sin(radians(lat))*cos(radians(zenith)) - sin(radians(declin))) "
            f"           / (cos(radians(lat))*sin(radians(zenith))))))), 360) END AS azimuth "
            f"  FROM zen"
            f") m WHERE c.source_row_key = m.k"
        )
```

Expected result: after this runs, every collision with a valid geom + datetime has both angles
filled; all others remain NULL.

> **Implementer note:** the CTE chain mirrors the NOAA reference block above, split so each SQL
> `SELECT` only references columns from the previous CTE (SQL cannot see sibling aliases). If a step
> does not match reality (e.g. a DuckDB function name differs in the installed version), adapt it to
> produce the same mathematical result and note the deviation — the End State (correct angles within
> the Acceptance tolerances) is the contract, not the exact SQL text.

### Step 3 — Call `_solar_stamp()` in the build
File: same, in `transform_and_load()`. Find:
```python
        # --- WEATHER STAMP (optional): fill temperature_c/precipitation_mm from the
        # weather grid if it was built this run (the registry orders weather first).
        self._weather_stamp(con)
```
Immediately **after** that call, add:
```python
        # --- SOLAR STAMP: fill solar_elevation_deg/solar_azimuth_deg for every collision
        # with a geom + datetime, computed mathematically (NOAA). Always-on, no download.
        self._solar_stamp(con)
```
Placement rationale: after the geom/datetime columns are final and after `_weather_stamp`, but
**before** the `collisions_spatial` view + RTREE index so those still work unchanged. The stamp is
independent of weather and boundaries.

### Step 4 — Bump the schema version
File: [`src/crossroads/__init__.py`](../../../src/crossroads/__init__.py). Change:
```python
SCHEMA_VERSION = 1
```
to:
```python
SCHEMA_VERSION = 2
```
Expected result: `crossroads.SCHEMA_VERSION == 2`; the fast schema-doc test now requires the doc to
say version 2 (Step 6).

### Step 5 — Document the two columns in `docs/schema.md`
File: [`docs/schema.md`](../../schema.md).

(a) Update the version line near the top from:
```
**Schema version:** 1  ·  See [methodology.md]...
```
to `**Schema version:** 2`.

(b) In the `collisions` `CREATE TABLE` block, immediately after the `precipitation_mm` line
```
    precipitation_mm       DOUBLE,      -- hourly precipitation (mm) from the ERA5-Land cell; NULL if weather not built / no match
```
add:
```
    solar_elevation_deg    DOUBLE,      -- sun's apparent elevation above the horizon (deg, refraction-corrected; negative = below horizon/night); computed mathematically (NOAA) from geom + datetime_local; NULL if geom/datetime invalid
    solar_azimuth_deg      DOUBLE,      -- sun's azimuth (deg clockwise from true north: 0=N/90=E/180=S/270=W) at the collision place/time; NULL if geom/datetime invalid
```
Expected result: both column names appear verbatim in the doc, satisfying the drift guard (Step 6).

### Step 6 — Update the changelog
File: [`CHANGELOG.md`](../../../CHANGELOG.md). Add an entry under the current unreleased/next
section (match the file's existing format) noting: *"Added `solar_elevation_deg` and
`solar_azimuth_deg` to the `collisions` table — the sun's apparent elevation and azimuth at each
collision's place and time, computed mathematically (NOAA solar position, no external data). Schema
version 1 → 2."* Read the file first and mirror its exact heading/bullet style.

### Step 7 — Add a methodology note (documentation)
File: [`docs/methodology.md`](../../methodology.md). Read it first to match tone/heading depth. Add a
short subsection titled e.g. **"Solar geometry (the glare vector)"** covering:
- What the two columns are and their units/reference frames.
- That they are computed with the NOAA solar position algorithm, in SQL, from `geom` + the
  `datetime_local` instant (ICU `Europe/London` → UTC for the math only) — zero downloads.
- The spec §2 note: only angles are stored, never a reconstructed `*_utc` column; the single DST
  fall-back hour is resolved deterministically by ICU (sun below the horizon then anyway).
- The worked query from the Goal section above.

---

## Testing & Verification

Add tests to [`tests/test_stats19.py`](../../../tests/test_stats19.py) (reuse its
`_stats19_client` / `_seed_cache` helpers, which seed the committed sample CSVs and build offline).
All commands are run from the repo root with the project venv active.

### PRIMARY — integration test on the real fixture build
Add:
```python
def test_solar_angles_present_and_ranged(tmp_path):
    """Every valid collision is stamped with in-range solar angles; invalid rows stay NULL."""
    client = _stats19_client(tmp_path)
    client.build(years=YEARS)
    con = client.con
    # Columns exist.
    cols = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='collisions'").fetchall()]
    assert "solar_elevation_deg" in cols and "solar_azimuth_deg" in cols
    # Non-NULL iff geom AND datetime present; ranges hold.
    mism = con.execute(
        "SELECT count(*) FROM collisions "
        "WHERE (solar_elevation_deg IS NULL) <> "
        "      (geom IS NULL OR datetime_local IS NULL)").fetchone()[0]
    assert mism == 0, "angles must be non-NULL exactly when geom AND datetime_local exist"
    bad = con.execute(
        "SELECT count(*) FROM collisions WHERE solar_elevation_deg IS NOT NULL AND ("
        " solar_elevation_deg < -90 OR solar_elevation_deg > 90 "
        " OR solar_azimuth_deg < 0 OR solar_azimuth_deg >= 360)").fetchone()[0]
    assert bad == 0, "elevation must be in [-90,90] and azimuth in [0,360)"
    client.close()
```

### PRIMARY — numeric correctness vs an independent hand-check
Build a tiny synthetic `collisions`-shaped table and run the stamp directly, so the expected values
are computed by hand (independent of the implementation). Use **winter (GMT)** so `datetime_local`
== UTC and no BST offset complicates the hand-check.
```python
def test_solar_stamp_matches_known_noaa_values(tmp_path):
    """NOAA anchor: London at winter-solstice GMT noon -> elevation ~15.1 deg, azimuth ~180 (due
    south); midnight -> sun below the horizon. Uses GMT so local time == UTC (no BST offset)."""
    import crossroads
    from crossroads.transformers.stats19 import Stats19Transformer
    client = crossroads.init_engine(cache_dir=str(tmp_path / "cache"))
    con = client.con
    con.execute("INSTALL spatial"); con.execute("LOAD spatial")
    # Minimal table with the columns _solar_stamp reads + writes. geom is EPSG:27700, built
    # from London lon/lat (0.13 W, 51.50 N) so the stamp's reprojection round-trips it back.
    con.execute(
        "CREATE TABLE collisions AS "
        "SELECT * FROM (VALUES "
        "  ('noon', TIMESTAMP '2023-12-22 12:00:00'), "
        "  ('midnight', TIMESTAMP '2023-12-22 00:00:00') "
        ") AS t(source_row_key, datetime_local)")
    con.execute(
        "ALTER TABLE collisions ADD COLUMN geom GEOMETRY")
    con.execute(
        "UPDATE collisions SET geom = ST_Transform("
        "  ST_Point(-0.13, 51.50), 'EPSG:4326', 'EPSG:27700', always_xy := true)")
    con.execute("ALTER TABLE collisions ADD COLUMN solar_elevation_deg DOUBLE")
    con.execute("ALTER TABLE collisions ADD COLUMN solar_azimuth_deg DOUBLE")
    Stats19Transformer()._solar_stamp(con)
    noon = con.execute("SELECT solar_elevation_deg, solar_azimuth_deg "
                       "FROM collisions WHERE source_row_key='noon'").fetchone()
    midnight = con.execute("SELECT solar_elevation_deg "
                           "FROM collisions WHERE source_row_key='midnight'").fetchone()
    assert abs(noon[0] - 15.1) < 1.5, f"winter-noon elevation was {noon[0]}"
    assert abs(noon[1] - 180) < 5,   f"winter-noon azimuth was {noon[1]}"
    assert midnight[0] < 0,          f"midnight elevation should be < 0, was {midnight[0]}"
    client.close()
```
> If `ST_Point(-0.13, 51.50)` argument order differs in the installed DuckDB spatial build, adapt so
> the point is lon=-0.13, lat=51.50 (with `always_xy`, X=lon, Y=lat). The physics of the anchor
> (≈15° / ≈180° / negative) is the contract.

### Regression — invariants and full suite
- `pytest tests/test_stats19.py -q` — new + existing STATS19 tests pass; conservation and
  flag/ledger invariants (run inside `client.build`) still pass (no new audited dimension).
- `pytest -q` — the whole offline suite is green.
- `pytest -m integration tests/test_schema_doc.py -q` — the drift guard builds the full offline DB
  and confirms `solar_elevation_deg` / `solar_azimuth_deg` are documented. **This is the ship gate
  for the schema-doc update; it fails if Step 5 was missed.**

### Ship-readiness checklist
- [ ] `solar_elevation_deg` + `solar_azimuth_deg` exist on `collisions` after a build.
- [ ] Angles non-NULL iff `geom` AND `datetime_local` present; ranges hold.
- [ ] NOAA anchor test within tolerance (≈15.1° / ≈180° / negative at midnight).
- [ ] `crossroads.SCHEMA_VERSION == 2`; `docs/schema.md` says version 2 and lists both columns.
- [ ] `pytest -q` green; `pytest -m integration tests/test_schema_doc.py -q` green.
- [ ] `pyproject.toml` dependencies unchanged (no astronomy library).
- [ ] STATS19-only build (no weather/boundaries) still succeeds — the anchor test proves the stamp
      runs without either.

---

## Performance
- **Hot path:** one set-based `UPDATE ... FROM (SELECT ...)` over `collisions`. DuckDB evaluates the
  CTE chain as vectorised columnar arithmetic — a single pass over the valid-row subset. No Python
  row loop (spec §3A). For a full year (~100k collisions) this is milliseconds-to-sub-second; for
  the multi-year 2022 DB the cost is linear in row count and dwarfed by the STATS19 CSV load and the
  spatial join that already run.
- **Complexity:** O(rows) time, O(1) extra memory beyond the two `DOUBLE` columns.
- **I/O:** none — zero downloads; the calc is pure arithmetic on columns already in the table.
- The `ST_Transform` per row is the same call `_weather_stamp` already performs and has proven cheap.

## Failure Modes
| Failure | Symptom | Guardrail |
|---------|---------|-----------|
| ICU not loaded (STATS19-only build) | `AT TIME ZONE` errors | `_solar_stamp` runs `INSTALL icu; LOAD icu` itself (idempotent) before the UPDATE. |
| Zero valid rows (all geom/datetime NULL) | — | The `WHERE geom IS NOT NULL AND datetime_local IS NOT NULL` subquery is empty; the UPDATE matches nothing and no-ops. Columns stay NULL. |
| Floating-point pushes an `acos` argument just past ±1 | `NaN` angle | `greatest(-1, least(1, ...))` clamps both `acos` arguments. |
| DST fall-back ambiguous hour | one instant is ambiguous | ICU resolves it deterministically; sun is below the horizon then, so elevation is negative either way. Documented (Step 7). |
| Schema doc not updated | drift guard fails | `pytest -m integration tests/test_schema_doc.py` fails loudly — Step 5 is enforced, not optional. |
| Undocumented DuckDB function-name difference | SQL error at build | The reference NOAA block + CTE chain use only core functions already used elsewhere; adapt names if needed (End State is the contract). |

## Rollback
Fully self-contained and reversible:
1. Remove the `self._solar_stamp(con)` call (Step 3) and the `_solar_stamp` method (Step 2).
2. Remove the two placeholder columns (Step 1).
3. Revert `SCHEMA_VERSION` to `1` (Step 4), the `docs/schema.md` version line + two column lines
   (Step 5), the `CHANGELOG.md` entry (Step 6), the methodology note (Step 7), and the new tests.
No data migration is involved — the columns are rebuilt from scratch on every `build()`, so reverting
the code and rebuilding yields the previous database shape exactly.

## Open Questions
- **Refraction near/below the horizon:** the NOAA refraction model is only meaningful for elevations
  ≳ −1°; well below the horizon the "apparent" elevation is physically irrelevant. This is fine for
  the glare use-case (which filters `elevation BETWEEN 0 AND 15`), but if a future consumer needs
  precise deep-twilight geometry, note that sub-horizon `solar_elevation_deg` values are geometric +
  a best-effort refraction term, not a rigorous below-horizon apparent position. Not blocking.
