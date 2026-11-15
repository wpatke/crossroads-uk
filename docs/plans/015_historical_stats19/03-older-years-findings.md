# Part 03 — Older years vs the reference data and quality checks

**Findings only. No reference-data change is proposed in this plan.** The modern-year
gaps found here (`junction_detail` 19, `vehicle_type`/`casualty_type` 33, and casualty
`escooter_flag`) have since been fixed by
[plan 016](../016_reference_gaps/00-findings.md).

## How this was checked

On 2026-11-14 all three combined files (`…-{collision,vehicle,casualty}-1979-latest-published-year.csv`)
were streamed once from DfT and scanned with a throwaway script (not committed). The script
used Python's `csv` module and did not save the data. For every row it recorded the year,
the format details listed below, and, for every column the manifest marks `coded`, the
codes in use per year. It also counted, per column and year, the cells that are blank or
`-1`. The results were compared with `src/crossroads/reference/stats19_codebook.csv` and
`stats19_columns.csv`.

| File | Rows | Years |
|---|---|---|
| collision | 9,116,625 | 1979–2025 |
| vehicle | 16,283,094 | 1979–2025 |
| casualty | 12,102,133 | 1979–2025 |

## File format (affects the split in part 01)

- No byte-order mark, `\n` line endings, and every record ends with a newline.
- No quoted fields and no multi-line records. Every row has the same number of fields as
  the header.
- Rows are **heavily interleaved by year**. The year changes from one row to the next
  3,290 times in the collision file, 41,341 times in the vehicle file and about 7.3 million
  times in the casualty file. The single-pass, any-year-to-any-file split in part 01 is
  required.
- The split still handles quotes, CRLF and a missing final newline (part 01) at almost no
  cost, in case DfT's format changes. The real files do not need it today.

## Year column check (decision 4)

`collision_year` agrees with the year in `date` for **every** collision row that has a date.
The only 8 rows where they differ are the 8 rows with a **blank date** (3 in 1990, 2 in 1991,
2 in 1992, 1 in 1997). So routing by `collision_year` places every row in the same year
its date says.

## Columns

- **The combined files have exactly the same columns as the current single-year files**,
  for every year, including 1979. Fields DfT did not collect in early years are present
  but filled with `-1` or left blank. So older years add **no columns** that the
  manifest does not already list.
- The manifest lists `accident_index/accident_year/accident_reference`, but the files use
  `collision_index/collision_year/collision_ref_no`. This is expected and already handled:
  the silver derivations coalesce both naming styles.

## Codes

Every code in use in **1979–2020** that is not in the codebook, plus the
modern-year gaps found along the way:

| Column | Code | Years | Rows | Effect today |
|---|---|---|---|---|
| `collision_injury_based` | `-1` | 1979–2003 | 6.0 M | None. The coded clean always treats a bare `-1` as missing, so it becomes NULL with no warning. |
| `casualty_injury_based` | `-1` | 1979–2003 | 8.0 M | Same as above. |
| `driver_distance_banding` | `-1` | 1979–2025 | 16.0 M | Same as above. |
| `enhanced_severity_collision` | (all) | — | — | Already known: the codebook has no rows for this column, so it is skipped by the undecoded-code warning. |
| `junction_detail` | `19` | **every year, 1979–2025** | 240 k (3–7 k per year) | Kept as the INTEGER code 19 in silver, but `junction_detail_label` is NULL. Because `junction_detail` is a codebook-covered column, `_create_labelled_views` warns about it on every build. **This is not specific to older years.** |
| `vehicle_type` | `33` | 2020–2025 only | 7.4 k | Same as above: kept, unlabelled, warned. Modern years only. |
| `casualty_type` | `33` | 2020–2025 only | 6.1 k | Same as above. |

The old codes that older years *do* use are already in the codebook. For example,
`vehicle_type` / `casualty_type` 103–113 (the "(1979-1998)" and "(1979-2004)" motorcycle,
taxi, car, minibus and goods codes) are present, and 109 (car, 1979–2004) is the most
common `vehicle_type` in 2000.

**Conclusion: older years need no codebook change.** The two real gaps,
`junction_detail` 19 and `vehicle_type`/`casualty_type` 33, affect modern builds too.
Fixing them means first checking that DfT's 2024 data guide defines those codes. That is
a **follow-up**, separate from this fix. (Done in plan 016.)

## Fields not collected in early years

These columns are blank or `-1` in more than half the rows for the years shown. This means
"not collected then", not "bad data":

| Columns | Not collected (>50% missing) |
|---|---|
| `longitude`, `latitude` (geometry uses OSGR, so not affected) | 1979–1998 |
| `lsoa_of_accident_location`, `did_police_officer_attend_scene_of_accident` | 1979–1998 |
| `local_authority_ons_district`, `local_authority_highway`, `trunk_road_flag`, `urban_or_rural_area` | 1979–1993 |
| `local_authority_highway_current` | 1979–2003 |
| `collision_injury_based`, `collision_adjusted_severity_*`, `casualty_injury_based`, `casualty_adjusted_severity_*` | 1979–2003 |
| `enhanced_severity_collision` / `enhanced_casualty_severity` | 1979–2021 / 1979–2020 |
| `second_road_number` | 1979–1981 |
| `engine_capacity_cc`, `generic_make_model`, `propulsion_code` | 1979–1989 |
| `junction_location`, `vehicle_left_hand_drive`, `pedestrian_road_maintenance_worker` | 1979–2004 |
| `vehicle_location_restricted_lane` | 1979–1998 |
| `journey_purpose_of_driver` | 1979–2010 |
| `driver_imd_decile`, `casualty_imd_decile` | 1979–2015 |
| `lsoa_of_driver` / `lsoa_of_casualty` | 1979–2015 / 1979–2000 |

## How the quality checks treat older years

The build fails if any formal dimension's reject rate is above **5%** (`DEFAULT_REJECT_CEILING`).

| Dimension | Older-year result | Verdict |
|---|---|---|
| Collision `geom` (blank or `-1` OSGR) | Worst year **1987: 1.36%**. Next worst: 1999 0.58%, 1979 0.57%. Every other year is below 0.4%. | Well under 5%. (The scan did not count the `0` sentinel separately. The real-build check below confirms the true rate.) |
| Collision `datetime` | 8 blank dates in 47 years | Effectively 0%. |
| Collision / casualty `severity` | No missing values in any year | 0%. |
| Vehicle / casualty `link` | Not measured by the scan, which did not join tables | **Checked during implementation** by a real build (below). |
| Conservation / quarantine | Every row has the right number of fields, and there are no quotes | Expected to match exactly. |

## How the completeness report treats older years

`stats19_completeness` counts a cleaned coded/numeric value as missing when it is NULL, so
a "not collected" column reports a high `missing_rate`. That is accurate, but:

- The report is **per build, across all its years**. A 1979–2025 build blends a column
  that was not collected for 25 years with years where it is nearly complete. For
  example, `collision_injury_based` would show about 66% missing. That number is right,
  but easy to misread. `docs/data-sources.md` will get a one-line note pointing readers
  to the "not collected" table above (part 04).
- **Text** columns (`lsoa_*`, `local_authority_ons_district`, `local_authority_highway*`,
  `generic_make_model`) are carried verbatim and are not in the completeness report, so an
  older year's `-1` shows as the string `'-1'` in silver, not NULL. This is existing
  behaviour that also applies to modern years. It is more visible in old years. It is not
  changed here.

## Side finding (fixed since by plan 016)

- **Casualty `escooter_flag` is not in the column manifest.** It is in every current DfT
  casualty file, single-year and combined, but `stats19_columns.csv` lists
  `escooter_flag` for `vehicle` only. So the casualty `escooter_flag` reaches bronze but
  never reaches the `casualties` silver table. The committed 2023 casualty fixture has no
  `escooter_flag` column, which is why no test noticed. This affects every build, not just
  older years.

## Real-data check during implementation

The committed tests are offline, so after implementing, run one real build from the
combined files: `years=[1979, 2000, 2010]`, `datasets=["stats19"]`. This downloads about
4.3 GB into a scratch cache that is **not** committed. Record in the implementation notes:

- that the build completes, so conservation, flag/ledger agreement and the reject ceilings
  all pass;
- the real geom, datetime and link reject rates for each year;
- that the per-year row counts equal the scan's counts above (1979: 254,967 collisions;
  2000: 233,729; 2010: 154,414).

### Results (run 2026-11-15)

The build of `years=[1979, 2000, 2010]`, `datasets=["stats19"]` (boundaries included,
snapshot mode) **completed in about 6 minutes, including the download**. Every invariant
passed and nothing was quarantined. Row counts match the scan exactly:

| Year | Collisions | Vehicles | Casualties | geom rejects | datetime rejects | severity rejects | Orphan vehicles / casualties |
|---|---|---|---|---|---|---|---|
| 1979 | 254,967 | 422,983 | 334,513 | 0.57% | 0% | 0% | 0 / 0 |
| 2000 | 233,729 | 429,943 | 320,283 | 0.38% | 0% | 0% | 0 / 0 |
| 2010 | 154,414 | 281,401 | 208,648 | 0% | 0% | 0% | 0 / 0 |

### New finding: unreliable coordinates in 1979–1988

In 1979, **19.9% of collisions got no `lad_code`**, against 0.5% in 2000 and 0.08% in 2010.
The cause is DfT's coordinates, not the split. Some police forces' early grid references are
simply wrong. For example, about 45% of 1979 Metropolitan Police collisions (force 1) have
points far from London, such as (206980, 476000). All of Strathclyde's (force 97) 1979
points miss every boundary. Some eastings are as large as 999,980, beyond Great Britain's
grid (maximum 700,000).

These points pass the `geom` check because they are real numbers, not the `-1`/`0`/blank
sentinels. So they count as valid geometry, but they fall in the sea or outside Great
Britain, and therefore get no `lad_code` (and no weather, solar or bank-holiday value that
depends on it). A wrong point that happens to land inside some *other* local authority would
be stamped with the wrong area, and this cannot be detected here.

Share of collisions **with usable coordinates** that fall outside every LAD (2025-12 LAD
boundaries, all years from the combined file):

| Years | Outside every LAD | Beyond the grid (E > 700,000 or N > 1,300,000) |
|---|---|---|
| 1979 | 19.4% | 1.92% |
| 1980–1981 | 13.5–13.8% | 0.2–0.3% |
| 1982–1985, 1987–1988 | 3.7–4.6% | ≤ 0.2% |
| 1986 | 11.3% | 0.01% |
| 1989–1998 | 0.2–0.3% | ≤ 0.02% |
| 1999–2025 | 0.08–0.16% | 0% |

This is documented in `docs/data-sources.md` ("Older years"). **Not changed here:** treating
off-grid points as invalid geometry would change ingestion behaviour, so it is a separate
decision. If adopted, the existing `BNG_MIN_*`/`BNG_MAX_*` constants in `stats19.py` give
the bounds, and the 1979 geom reject rate would rise to about 2.5%, still under the 5%
ceiling.
