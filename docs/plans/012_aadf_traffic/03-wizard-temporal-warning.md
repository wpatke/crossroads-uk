# Stage 03 — Wizard Temporal-Mode Warning (temporal + AADF)
> Part of DfT AADF Traffic Counts. You have the overview + this stage; implement ONLY this stage.
> Read the overview's Stage Map first. End State is the contract — if a step doesn't match reality, adapt and note it.

## Prerequisites / Starting State

- Stage 01 is complete: `aadf` is a registered, `user_selectable` source (it appears in
  the wizard dataset menu), and `AadfTransformer._stamp_area_codes` honours
  `self._boundary_mode` (snapshot vs temporal, keyed on `make_date(year, 7, 1)`).
- `python -m pytest` and `python -m pytest -m integration` are green before you start
  (verify first).
- Key files/functions you will touch or model on:
  - `src/crossroads/console.py`:
    - `run_wizard(...)` (around line 378) — the top-level flow: `gather_parameters` →
      `ensure_weather_credentials` → summary → final `prompt_confirm` → `run_build`.
    - `prompt_confirm(reader, writer, *, message=..., default=True)` (around line 269) —
      **reuse this**; it already parses `y/n`, shows a `[y]`/`[n]` default hint, and
      Enter selects the default. Do NOT write a new yes/no prompt.
    - `prompt_boundary_mode(reader, writer)` (around line 189) — its descriptive line is
      currently stats19-specific ("...on each collision date").
  - `tests/test_console.py`:
    - `scripted(answers)` harness and the `_FakeClient` recording fake +
      `factory`/`captured` pattern used by `test_wizard_produces_correct_build_invocation`
      and `test_decline_does_not_build` — **model the new tests on these exactly.**
    - `MENU = [("stats19", "stats19")]` — the fixed injected menu; existing run_wizard
      tests use it, so they never select aadf and are unaffected by this change.

## Objective

When the user selects **temporal** boundary mode **and** has **aadf** in their chosen
datasets, the wizard prints a short warning about the mid-year boundary approximation and
asks `Continue? (y/n)` with **yes as default** (Enter proceeds). Answering `n` aborts the
whole wizard with no build. The warning must NOT appear for any other combination
(snapshot in any case; temporal without aadf).

## Implementation Steps

### Step 1 — Add the temporal+aadf gate in `run_wizard`

In `src/crossroads/console.py`, inside `run_wizard`, immediately AFTER
`params = gather_parameters(reader, writer, available=available)` and BEFORE the
`ensure_weather_credentials(...)` call, insert the gate. Placing it here means an abort
skips the (possibly slow) weather-credential prompt and the summary.

```python
    params = gather_parameters(reader, writer, available=available)

    # Temporal + AADF only: an annual traffic average is attributed to the boundary
    # in force at a mid-year (1 July) date, which is approximate in a year when a
    # boundary changed. Surface that honestly and let the user opt out. (Snapshot,
    # or temporal without traffic counts, has no such approximation — no warning.)
    if params["boundary_mode"] == "temporal" and "aadf" in params["datasets"]:
        writer("")   # spacer
        writer("Note: temporal mode attributes each traffic count to the area "
               "boundaries in force at its mid-year point; this is approximate in a "
               "year when a boundary changed.")
        if not prompt_confirm(reader, writer, message="Continue? (y/n)", default=True):
            writer("Aborted — no database was built.")
            return None

    if not ensure_weather_credentials(params, reader, secret_reader, writer):
        ...
```

Notes:
- `"aadf"` is the source_id set in Stage 01 (`AadfTransformer.source_id = "aadf"`).
- Reuse the existing `"Aborted — no database was built."` message and `return None` — it
  is the same decline/abort path the final confirm already uses, so `main()` closes
  nothing and returns 0 cleanly.
- `default=True` means a bare Enter returns `True` and the build proceeds.
- Do NOT change `gather_parameters` or `prompt_boundary_mode` signatures — the gate reads
  the already-collected `params` dict, which holds both `boundary_mode` and `datasets`.

### Step 2 — Reword the boundary-mode prompt to be source-neutral

`prompt_boundary_mode` now governs BOTH stats19 and aadf, so its stats19-only wording is
misleading. In `src/crossroads/console.py`, change the descriptive line:

```python
    writer("Boundary mode: 1) snapshot = latest ONS boundaries (default); "
           "2) temporal = boundaries as they were at each record's date.")
```

(Only the trailing phrase changes: "on each collision date" → "at each record's date".)
No test asserts this exact sentence, so this is safe; verify with
`python -m pytest tests/test_console.py -k boundary` afterwards.

## Testing & Verification

Add these to `tests/test_console.py`. Define a menu that includes aadf near the other
menu constants:

```python
# Menu with aadf first (matches the real source_id-sorted order: aadf, stats19).
MENU_WITH_AADF = [("aadf", "traffic counts (AADF)"), ("stats19", "stats19")]
WARN_SNIPPET = "approximate in a year when a boundary changed"
```

1. **Warning fires on temporal+aadf; Enter proceeds.** Answers: db, datasets `"1"`
   (aadf), years, `"temporal"`, `""` (Enter = yes to the warning), `""` (Enter = yes to
   the final build confirm).
   ```python
   def test_temporal_aadf_warns_then_enter_proceeds():
       reader, writer, output = scripted(
           ["mydb.duckdb", "1", "2023", "temporal", "", ""])
       captured = {}
       def factory(**kwargs):
           c = _FakeClient(**kwargs); captured["client"] = c; return c
       result = console.run_wizard(reader, writer, engine_factory=factory,
                                   available=MENU_WITH_AADF)
       assert any(WARN_SNIPPET in line for line in output)   # warning shown
       assert result is captured["client"]                   # build proceeded
       assert captured["client"].build_kwargs["boundary_mode"] == "temporal"
       assert captured["client"].build_kwargs["datasets"] == ["aadf"]
   ```

2. **`n` at the warning aborts the wizard.** Answers end at the warning with `"n"`; the
   build must never run.
   ```python
   def test_temporal_aadf_decline_aborts():
       reader, writer, output = scripted(
           ["mydb.duckdb", "1", "2023", "temporal", "n"])
       calls = []
       def factory(**kwargs):
           calls.append(kwargs); return _FakeClient(**kwargs)
       result = console.run_wizard(reader, writer, engine_factory=factory,
                                   available=MENU_WITH_AADF)
       assert result is None
       assert calls == []                                    # build never entered
       assert any(WARN_SNIPPET in line for line in output)   # warning was shown
       assert any("Aborted" in line for line in output)
   ```

3. **No warning when temporal but aadf NOT selected.** Pick stats19 only (`"2"`), temporal,
   then a single `""`/`"y"` for the final confirm. If a warning prompt had fired, the
   single confirm answer would be consumed by it and the build would desync — so a clean
   build with no warning line proves the gate stayed shut.
   ```python
   def test_temporal_without_aadf_no_warning():
       reader, writer, output = scripted(
           ["mydb.duckdb", "2", "2023", "temporal", ""])
       captured = {}
       def factory(**kwargs):
           c = _FakeClient(**kwargs); captured["client"] = c; return c
       result = console.run_wizard(reader, writer, engine_factory=factory,
                                   available=MENU_WITH_AADF)
       assert not any(WARN_SNIPPET in line for line in output)
       assert result is captured["client"]
       assert captured["client"].build_kwargs["datasets"] == ["stats19"]
       assert captured["client"].build_kwargs["boundary_mode"] == "temporal"
   ```

4. **No warning when aadf selected but snapshot.** Pick aadf (`"1"`), snapshot, one final
   confirm `""`. Warning absent, build proceeds.
   ```python
   def test_snapshot_aadf_no_warning():
       reader, writer, output = scripted(
           ["mydb.duckdb", "1", "2023", "snapshot", ""])
       captured = {}
       def factory(**kwargs):
           c = _FakeClient(**kwargs); captured["client"] = c; return c
       result = console.run_wizard(reader, writer, engine_factory=factory,
                                   available=MENU_WITH_AADF)
       assert not any(WARN_SNIPPET in line for line in output)
       assert result is captured["client"]
   ```

Run:
```bash
python -m pytest tests/test_console.py     # all console tests, including the 4 new ones
python -m pytest                           # full fast suite must stay green
```

Stage ship-readiness checklist:
- [ ] Warning appears ONLY on temporal+aadf (tests 1–2 assert present; 3–4 assert absent).
- [ ] Bare Enter at the warning proceeds; `n` aborts with "Aborted — no database was built."
- [ ] `prompt_boundary_mode` wording no longer says "collision date".
- [ ] Full fast suite green; no existing console test regressed (they use `MENU`, which
      has no aadf, so none should see the new prompt).

## End State / Handoff

- Running the real wizard (`crossroads`), selecting traffic counts + temporal, shows the
  mid-year warning and a `Continue? (y/n)` prompt defaulting to yes; `n` aborts.
- No warning for snapshot, or for temporal without traffic counts.
- Stage 04 (docs) may state that the wizard warns on temporal+aadf and document the
  mid-year (1 July) attribution convention as the metric's one caveat.

## Failure Modes & Rollback

- **Warning fires for the wrong combination** → re-check the gate condition is
  `boundary_mode == "temporal" AND "aadf" in datasets` (both required).
- **Existing run_wizard tests desync/fail** → they must keep using the aadf-free `MENU`;
  the new prompt only appears with `MENU_WITH_AADF`. If one breaks, it selected aadf +
  temporal unintentionally — fix the test's menu, not the gate.
- **Rollback:** delete the gate block (Step 1), revert the one-line wording change
  (Step 2), and remove the four new tests. Stage 01/02 remain valid and green.
