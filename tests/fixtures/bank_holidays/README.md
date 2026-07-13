# Bank-holidays test fixture — `bank-holidays-sample.json`

**This file is a trimmed snapshot.** It is a hand-reduced copy of the live GOV.UK feed at
`https://www.gov.uk/bank-holidays.json` (Open Government Licence v3.0), cut down to a single
year (**2023**) so the offline bank-holidays tests exercise the real JSON parse path with no
network access. Real builds fetch the full live feed; this fixture only exists for tests.

## Provenance / alignment

- All three divisions the real feed carries are kept — `england-and-wales`, `scotland`,
  `northern-ireland` — because the divisions genuinely differ and the code must preserve them.
- The year is **2023** to line up with the committed STATS19 collision fixture
  `tests/fixtures/stats19/dft-road-casualty-statistics-collision-2023.csv`, so Stage 02's
  combined-build stamping test has in-coverage dates to resolve.
- A deliberate **cross-division divergence** is baked in so Stage 02's division-routing test
  has real data to lean on:
  - `Easter Monday` (`2023-04-10`) is **england-and-wales only** (absent from scotland and NI);
  - `2nd January` (`2023-01-03`) is **scotland only**.

The `date`/`title`/`notes`/`bunting` shape mirrors a real feed event. Values are fixed (no
randomness, no wall-clock), so the fixture is fully reproducible.
