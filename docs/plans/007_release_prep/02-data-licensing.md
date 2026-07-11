# Stage 02 — Data Sources & Attribution
> Part of Release Preparation (v1.0.0). You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

No prior stage required. Verify:
```bash
cd /Users/will/Documents/Code/Crossroads
ls docs/data-sources.md 2>/dev/null || echo "no docs/data-sources.md yet (expected)"
grep -n "def run_wizard\|def format_summary\|def run_build" src/crossroads/console.py
grep -n "def scripted\|run_wizard" tests/test_console.py | head
```
The wizard (`run_wizard`) today gathers params, writes `format_summary(params)`, calls `prompt_confirm`, then builds. It emits **no** licence notice.

## Objective

Give Crossroads a clear, honest data-licensing story **without adding a gate**: a `docs/data-sources.md` that names each source, its licence, and the exact attribution string a researcher must reproduce; plus a single **non-blocking** one-line pointer to it in the wizard output. No per-source Y/N. No new prompt.

**Why no gate (state this reasoning in `docs/data-sources.md` itself):** Copernicus ERA5-Land acceptance happens on the CDS portal — a working API key implies the user has accepted, and the un-accepted case is already handled by `weather.py`'s friendly 403 message. OGL v3.0 (STATS19, ONS) is an *attribution* licence with no click-through; the obligation is to *acknowledge the source when publishing*, which Crossroads cannot discharge on the user's behalf. So the correct action is to **inform**, not block.

## Implementation Steps

**Step 1 — Author `docs/data-sources.md`** (in the `docs/` directory, alongside `spec.md`):

````markdown
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
````
> Confirm the OGL and Copernicus attribution wordings against the current licence pages before release; the strings above are the standard forms. Keep `[year]` as a literal placeholder for the user to fill at publication.

**Step 2 — Add a non-blocking licence pointer to the wizard.**
File: `src/crossroads/console.py`.

2a. Add a module-level constant near the top (after the imports):
```python
# One-line, non-blocking pointer shown before the build. Crossroads does not gate on
# data licences (see docs/data-sources.md for why); it only reminds the user they exist.
LICENCE_NOTICE = (
    "Data licences & required attribution: see docs/data-sources.md "
    "(you must attribute DfT/ONS/Copernicus sources when you publish)."
)
```

2b. Emit it in `run_wizard`, **after** the summary and **before** the confirm prompt, so it is part of what the user reads before deciding — but add **no** `reader()` call (this must not become a prompt). Locate:
```python
    params = gather_parameters(reader, writer, available=available)
    writer(format_summary(params))
    if not prompt_confirm(reader, writer, default=True):
```
and insert the notice between the summary and the confirm:
```python
    params = gather_parameters(reader, writer, available=available)
    writer(format_summary(params))
    writer(LICENCE_NOTICE)          # non-blocking reminder; not a prompt
    if not prompt_confirm(reader, writer, default=True):
```
Expected result: every wizard run prints the pointer once; the number of questions the user answers is unchanged (still db, datasets, years, boundary mode, confirm).

## Testing & Verification

**Integration test (PRIMARY) — the notice shows and does not add a prompt.** Add to `tests/test_console.py` (the file already has the `scripted`/`MENU`/`_FakeClient` harness):
```python
def test_wizard_shows_licence_notice_without_extra_prompt():
    # Exactly the same 5 answers as the happy-path build test. If the notice had become
    # a prompt, these answers would desync and the build kwargs would be wrong.
    reader, writer, output = scripted(["mydb.duckdb", "1", "2022 2023", "temporal", "y"])
    captured = {}
    def factory(**kwargs):
        c = _FakeClient(**kwargs); captured["client"] = c; return c
    result = console.run_wizard(reader, writer, engine_factory=factory, available=MENU)

    # The pointer appeared in the output...
    assert any("docs/data-sources.md" in line for line in output)
    # ...and the build still ran with the correct params (proving no prompt was added).
    assert result is captured["client"]
    assert captured["client"].build_kwargs == {"datasets": ["stats19"],
                                               "years": [2022, 2023],
                                               "boundary_mode": "temporal"}
```

**Docs presence test** — add to `tests/test_release.py` (created in Stage 01; create it if running this stage first):
```python
import os

def test_data_sources_doc_lists_every_source():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "docs", "data-sources.md"), encoding="utf-8") as fh:
        text = fh.read()
    for token in ("Open Government Licence", "STATS19", "ONS", "Copernicus", "ERA5-Land"):
        assert token in text, f"docs/data-sources.md is missing {token!r}"
```

Run:
```bash
cd /Users/will/Documents/Code/Crossroads
source .venv/bin/activate
python -m pytest tests/test_console.py tests/test_release.py -q
```
Expected: all pass.

**Stage ship-readiness checklist:**
- [ ] `docs/data-sources.md` exists (in `docs/`), lists all three sources with licence + attribution + "why no gate"
- [ ] `console.LICENCE_NOTICE` exists and is emitted by `run_wizard` before confirm
- [ ] the wizard adds **no** new prompt (existing 5-answer flow tests still pass)
- [ ] new tests pass; full `python -m pytest` green

## End State / Handoff

The next stage may assume `docs/data-sources.md` exists and the wizard prints a pointer to it. Stage 03's README will link to `docs/data-sources.md` in a "Data & Licences" section.

## Failure Modes & Rollback

- **Existing wizard tests break** because the extra `writer(...)` shifted output-index assertions. Existing tests assert on output *content* (`any(... in line ...)`), not fixed indices, so this is unlikely; if one does, adjust it to content matching and note it.
- **The notice accidentally consumes an answer.** Only if a `reader()` call was added — it must not be. Verify Step 2b added a `writer(...)` line only.
- **Rollback:** remove `LICENCE_NOTICE` and its `writer(...)` line from `console.py`, delete `docs/data-sources.md`, and remove the two new tests. State returns to pre-stage.
