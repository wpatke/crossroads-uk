# Weather Setup UX: honest 401 errors + missing-extra guidance (v1.0.2)
> Engineer: execute step by step, exactly as written.

Two related weather-setup fixes for the interactive wizard and the weather transformer: (1) translate a rejected CDS API key (HTTP 401) into a clear, actionable message instead of a raw traceback, and (2) when the optional `[weather]` extra is not installed, always show weather in the menu but mark it unavailable and, if selected, explain how to install it and offer to continue without it.

---

## Context & Objective

**What exists.**
- The interactive wizard lives in [`src/crossroads/console.py`](../../../src/crossroads/console.py). It gathers build parameters, then runs two pre-build checks before confirming and building:
  - [`ensure_weather_credentials`](../../../src/crossroads/console.py) (around line 317) — if weather is selected and no CDS key is configured, it prompts for a Personal Access Token and writes `~/.cdsapirc`.
  - The dataset menu is built by [`available_datasets()`](../../../src/crossroads/console.py) (around line 23) → `[(source_id, display_name)]`, rendered by `prompt_datasets` (around line 113).
- The weather transformer lives in [`src/crossroads/transformers/weather.py`](../../../src/crossroads/transformers/weather.py). Heavy deps (`cdsapi`, `xarray`, `netCDF4`) are the optional `[weather]` extra (see `pyproject.toml` `[project.optional-dependencies]`), imported lazily. Download errors are translated to friendly messages by helpers `_missing_key_message`, `_licence_message`, `_too_large_message` and the heuristics `_looks_like_licence_error` / `_looks_like_too_large_error`, wired into `_download_month` (around line 216).

**Two real defects (a user hit defect A post-launch — see the log below).**

- **Defect A — a rejected key produces a raw traceback.** `_cds_key_present()` is existence-only: it returns `True` if `~/.cdsapirc` exists (or both `CDSAPI_*` env vars are set), never checking the key is valid. A user (`C:\Users\Toby\.cdsapirc`) had a `~/.cdsapirc` with a bad/expired key, so the wizard skipped the prompt. `cdsapi.Client()` found the key and succeeded; then `client.retrieve(...)` returned **HTTP 401 Unauthorized / "Authentication failed" / "operation not allowed"**. In `_download_month`, the error heuristics only match *licence* wording (403) and *cost* wording (403); a 401 matches neither, so it re-raises raw — the ugly traceback the user saw. Key *validity* cannot be checked offline (a well-formed key can still be expired/revoked), so the correct, robust fix is to translate the 401 at download time. **Decision: translate the 401 only** — do not add offline validation or change the existence check.

  The user's log (abridged):
  ```
  requests.exceptions.HTTPError: 401 Client Error: Unauthorized for url:
    https://cds.climate.copernicus.eu/api/retrieve/v1/processes/reanalysis-era5-land/execution
  Authentication failed
  operation not allowed
  ```

- **Defect B — the `[weather]` extra being absent is not handled gracefully.** If a user installs the base package (`pip install crossroads-uk`, no extra) and selects weather, the download path hits `import cdsapi` → `ImportError`, surfacing a raw traceback rather than the fix ("install the extra"). And the menu gives no signal that weather needs an extra. **Decisions:** always show weather in the menu; when the extra is absent, **annotate its label** as unavailable with the install command; if the user selects it anyway, **explain and offer continue/abort** (mirroring the existing blank-token "continue without weather" flow). Also translate the programmatic `ImportError` in the transformer so a non-wizard `build()` caller gets the same honest advice.

**The goal.** Ship these two fixes for the **1.0.2** release. The version is derived from git tags via hatch-vcs (`pyproject.toml` `[tool.hatch.version]`, `dynamic = ["version"]`) — there is **no version string to edit in code**; releasing 1.0.2 is a git-tagging step the user performs, out of scope for this plan.

**Package/extra facts (used verbatim below).** Distribution name is `crossroads-uk`; the extra is `weather`. The exact install command is:
```
pip install "crossroads-uk[weather]"
```

---

## Acceptance Criteria

1. **401 is friendly.** When `client.retrieve(...)` raises a 401/authentication error, `_download_month` raises a `RuntimeError` whose message explains the key was *found but rejected* (expired/revoked/mistyped), shows the `~/.cdsapirc` format, points to the CDS profile to get a fresh token, and says to re-run — **no bare `HTTPError` traceback**. Verified by an automated test that drives `_download_month` with a fake client raising a 401-like error.
2. **Missing extra, menu.** When the `[weather]` extra is not installed, `available_datasets()` still includes weather, with a label that marks it as not-installed (so the menu doesn't look broken). When the extra is installed, the label is the plain `"weather"`. Verified by tests that monkeypatch the availability check both ways.
3. **Missing extra, selection gate.** A new `ensure_weather_installed(...)` gate: if weather is selected but the extra is absent, it prints a friendly, plain-language explanation (this is a setup step, not a bug), the reason (weather is an optional extra), and the exact install command, then asks "Build the other datasets now, without weather? (y/n)". "y" drops weather from `params["datasets"]` and proceeds (or aborts cleanly if weather was the only dataset); "n" aborts with an install-and-re-run message. It is a no-op when weather is not selected or the extra is present (no prompt). Verified by unit tests for each branch.
4. **Missing extra, programmatic path.** In the transformer's `_download`, a failed `import cdsapi` raises a `RuntimeError` with the install command (not a raw `ImportError`). Verified by a test that forces the import to fail.
5. **Ordering.** In `run_wizard`, `ensure_weather_installed` runs **before** `ensure_weather_credentials` (no key prompt when the code to use it isn't installed).
6. **No regressions.** The full test suite passes: `pytest -q`. Existing offline weather tests (which run with `xarray` present and a seeded cache) are unaffected because the availability check keys on `xarray` (see Approach).

---

## Scope

**In:**
- `src/crossroads/transformers/weather.py`: add `weather_extra_available()`, `WEATHER_EXTRA_HINT`, `_missing_extra_message`, `_auth_error_message`, `_looks_like_auth_error`; wire auth translation into `_download_month`; translate `ImportError` in `_download`.
- `src/crossroads/console.py`: annotate the weather menu label when the extra is absent; add `ensure_weather_installed(...)`; call it in `run_wizard` before `ensure_weather_credentials`.
- Tests: new `tests/test_weather_errors.py` (dep-free helper + `_download_month` tests) and additions to `tests/test_console.py`.

**Out:**
- No change to `_cds_key_present()` or the credential-prompt flow (decision: translate the 401 only, no offline key validation).
- No live/network validation of the key in the wizard.
- No version-string edit (git-tag-driven release).
- No change to the actual download/merge/transform logic beyond error translation.

---

## Constraints

- **Compatibility:** the weather module must still import cleanly **without** the `[weather]` extra (deps stay lazy — do not add a top-level `import cdsapi`/`import xarray`). `weather_extra_available()` must use `importlib.util.find_spec` (no heavy import).
- **DRY:** the install command lives in exactly one place, `WEATHER_EXTRA_HINT` in `weather.py`; the console imports it. The console imports weather symbols lazily inside functions (matching how `ensure_weather_credentials` already imports weather constants) so importing `console` stays cheap.
- **Style:** match the surrounding code — plain-language comments explaining *why*, the same message-helper shape (`def _x_message(exc): return "...\n\n(underlying ... error: {exc})"`), injected `reader`/`writer` for testability.
- **Tests:** prefer real behavior over mocks where cheap; the 401 path is driven through the real `_download_month` with a fake client (no network, no `xarray`).
- **Time:** small, focused change across two source files.

---

## Approach / Architecture

### Why key the availability check on `xarray` (critical — do not "fix" to check cdsapi)

The `[weather]` extra installs `cdsapi` + `xarray` + `netCDF4` together, so any one is a valid sentinel for "the extra is installed." **Use `xarray`** as the sentinel, because the existing **offline** weather tests build from a *seeded* NetCDF cache: they need `xarray` (to parse the `.nc`) but **not** `cdsapi` (no download happens). Every such test starts with `pytest.importorskip("xarray")`, so it runs only where `xarray` is present. Keying the gate on `xarray` therefore makes the gate a **no-op in exactly the environments those tests run in**, so they pass unchanged. Keying it on `cdsapi` instead could falsely block an offline build in an `xarray`-only environment. The narrow "`xarray` present but `cdsapi` absent" case (essentially only a dev box) is still handled: the download path's `import cdsapi` failure is translated to the same install message (Acceptance Criteria 4).

```python
def weather_extra_available():
    # xarray is the sentinel: it is required to parse ERA5-Land NetCDF on every
    # weather build path (download AND offline seeded-cache), and the [weather]
    # extra installs it alongside cdsapi/netCDF4. find_spec avoids a heavy import.
    import importlib.util
    return importlib.util.find_spec("xarray") is not None
```

### Wizard flow after the change

`run_wizard`: `gather_parameters` → **`ensure_weather_installed`** (new) → temporal/aadf note → `ensure_weather_credentials` → summary → confirm → build. If `ensure_weather_installed` returns `False`, return `None` early (before prompting for a key or showing the summary). If it drops weather (user continues without it), the later credential gate sees no weather and is a no-op.

### Error-translation ordering in `_download_month`

Check auth (401) **first**, then licence (403), then too-large (403), then re-raise. 401 is distinct from the 403 cases, so ordering only matters for clarity; putting auth first keeps the most-common setup failure obvious.

### Alternatives rejected

- **Validate the key live in the wizard** (a network round-trip): adds latency and complexity, and still can't cover every revocation race. Rejected — translate at download instead (chosen: "translate the 401 only").
- **Tighten `_cds_key_present()` to treat empty/keyless `~/.cdsapirc` as absent**: helps only the empty-file subset and duplicates coverage the 401 translation already gives for *all* bad keys. Rejected per decision.
- **Key the availability gate on `cdsapi`**: breaks offline weather tests in `xarray`-only environments (see above). Rejected.
- **Hard-abort when the extra is missing**: forces a full re-run even when the user would happily build the other datasets. Rejected in favor of continue/abort.

---

## Implementation Steps

### Step 1 — `weather.py`: add constants, availability check, and new message helpers

File: `src/crossroads/transformers/weather.py`. Add the following **after** the existing `_too_large_message` / heuristic helpers and **before** `_normalize_to_netcdf` (i.e. in the module-level helpers block, around line 112). Keep the existing helpers unchanged.

```python
# Exact install command for the optional [weather] extra. Distribution name is
# 'crossroads-uk'; the extra is 'weather'. Single source of truth — the wizard
# imports this so its advice matches the transformer's exactly.
WEATHER_EXTRA_HINT = 'pip install "crossroads-uk[weather]"'


def weather_extra_available():
    """True when the optional [weather] extra is importable.

    Keyed on xarray: it parses ERA5-Land NetCDF on every weather build path
    (real download AND offline seeded-cache), and the [weather] extra installs it
    alongside cdsapi/netCDF4. Uses importlib.util.find_spec so it does NOT import
    the heavy module — cheap enough to call while drawing the wizard menu.
    """
    import importlib.util
    return importlib.util.find_spec("xarray") is not None


def _missing_extra_message(exc):
    """Friendly, actionable steps when the [weather] extra is not installed.

    Reached by a programmatic build() caller who selected weather without the extra;
    the wizard blocks this earlier with the same command. Framed as a setup step the
    user still needs to do, NOT as a program error.
    """
    return (
        "Weather data isn't available yet because the optional weather add-on isn't\n"
        "installed. This isn't a bug: weather ships as an optional extra so the base\n"
        "install stays small and fast.\n\n"
        "To enable weather, install the add-on and then re-run:\n\n"
        f"    {WEATHER_EXTRA_HINT}\n\n"
        f"(technical detail: {exc})"
    )


def _auth_error_message(exc):
    """Actionable steps when CDS rejects the key (HTTP 401 / authentication failed).

    The key was FOUND but not accepted — expired, revoked, or mistyped. cdsapi does
    not validate offline, so this only surfaces at download time; we translate the raw
    401 into a check-your-key message instead of a traceback.
    """
    return (
        "Copernicus rejected your CDS API key (authentication failed).\n\n"
        "A key was found in ~/.cdsapirc (or your CDSAPI_* environment variables) but\n"
        "not accepted — it may be expired, revoked, or mistyped.\n\n"
        f"Get a fresh token from your CDS profile ({CDS_HOME_URL} -> log in -> your\n"
        "profile -> 'Personal Access Token'), then set ~/.cdsapirc to:\n\n"
        f"    url: {CDS_HOME_URL}/api\n"
        "    key: <your-personal-access-token>\n\n"
        "and re-run the build.\n\n"
        f"(underlying cdsapi error: {exc})"
    )


def _looks_like_auth_error(exc):
    """Heuristic: is this CDS failure an authentication/authorization rejection (401)?

    Matches 401 / auth wording only, kept separate from the licence (403) heuristic so a
    bad key is not mis-reported as an unaccepted licence and vice versa.
    """
    text = str(exc).lower()
    return any(word in text for word in (
        "401", "unauthorized", "authentication failed", "operation not allowed",
        "invalid api key", "invalid token"))
```

Expected result: module still imports without the extra (no heavy imports added).

### Step 2 — `weather.py`: wire auth translation into `_download_month`

File: `src/crossroads/transformers/weather.py`, in `_download_month` (around line 224). Add the auth check as the **first** translation in the `except` block. Change:

```python
        except Exception as exc:
            if os.path.exists(tmp):
                os.remove(tmp)                       # never leave a partial temp behind
            # Give targeted help for the two common post-auth failures; otherwise let the
            # real error through unchanged (network, disk, etc. are self-explanatory).
            if _looks_like_licence_error(exc):
                raise RuntimeError(_licence_message(exc)) from exc
            if _looks_like_too_large_error(exc):
                raise RuntimeError(_too_large_message(exc)) from exc
            raise
```

to:

```python
        except Exception as exc:
            if os.path.exists(tmp):
                os.remove(tmp)                       # never leave a partial temp behind
            # Translate the common CDS setup failures into actionable steps; otherwise let
            # the real error through unchanged (network, disk, etc. are self-explanatory).
            if _looks_like_auth_error(exc):          # 401: a key was found but rejected
                raise RuntimeError(_auth_error_message(exc)) from exc
            if _looks_like_licence_error(exc):
                raise RuntimeError(_licence_message(exc)) from exc
            if _looks_like_too_large_error(exc):
                raise RuntimeError(_too_large_message(exc)) from exc
            raise
```

Expected result: a 401 at retrieve time becomes a friendly `RuntimeError`.

### Step 3 — `weather.py`: translate a missing extra in `_download`

File: `src/crossroads/transformers/weather.py`, in `_download` (around line 195). The lazy `import cdsapi` currently sits *outside* the try that wraps `cdsapi.Client()`. Wrap the import so a missing extra becomes the friendly install message. Change:

```python
        import cdsapi                                   # lazy: real download only
        cache_dir = os.path.dirname(dest)

        # cdsapi raises a bare, cryptic exception when it can't find the API key
        # (no ~/.cdsapirc and no CDSAPI_* env vars). Translate it into setup steps.
        try:
            client = cdsapi.Client()
        except Exception as exc:
            raise RuntimeError(_missing_key_message(exc)) from exc
```

to:

```python
        try:
            import cdsapi                               # lazy: real download only
        except ImportError as exc:
            # The [weather] extra is not installed. Give the exact install command
            # rather than a raw ImportError. The wizard blocks this earlier, but a
            # programmatic build() caller reaches here directly.
            raise RuntimeError(_missing_extra_message(exc)) from exc
        cache_dir = os.path.dirname(dest)

        # cdsapi raises a bare, cryptic exception when it can't find the API key
        # (no ~/.cdsapirc and no CDSAPI_* env vars). Translate it into setup steps.
        try:
            client = cdsapi.Client()
        except Exception as exc:
            raise RuntimeError(_missing_key_message(exc)) from exc
```

Expected result: `build()` without the extra gives the install command, not an `ImportError` traceback.

### Step 4 — `console.py`: annotate the weather menu label when the extra is absent

File: `src/crossroads/console.py`, `available_datasets()` (around line 23). Replace its body so the weather entry is annotated when the extra is missing. The `source_id` returned is unchanged, so selection still maps to weather; only the display label changes.

```python
def available_datasets():
    """Discover the user-selectable datasets for the wizard menu.

    Returns a list of ``(source_id, display_name)`` in the registry's stable order.
    Weather is always listed, but when the optional [weather] extra is not installed
    its label is marked unavailable with the install command, so the menu shows the
    user it is possible and gives them a concrete action (rather than looking broken).
    Imported lazily so importing the console module stays cheap.
    """
    from crossroads.registry import Registry
    from crossroads.transformers.weather import (
        Era5WeatherTransformer, weather_extra_available)
    weather_id = Era5WeatherTransformer.source_id
    extra_ok = weather_extra_available()
    result = []
    for t in Registry().selectable():
        label = t.display_name
        if t.source_id == weather_id and not extra_ok:
            # Keep the menu line short; the full explanation is given if they select it.
            label = f"{label} (add-on not installed - see below if you pick it)"
        result.append((t.source_id, label))
    return result
```

Expected result: with the extra absent, the menu shows
`3. weather (add-on not installed - see below if you pick it)`; with it present, `3. weather`.

> Note: the detailed, friendly explanation and the exact install command live in the
> selection gate (Step 5), so the menu stays readable and the user only sees the full
> instructions when weather is actually chosen. `available_datasets()` no longer needs
> `WEATHER_EXTRA_HINT` in this step — keep the import of `Era5WeatherTransformer` and
> `weather_extra_available`; drop `WEATHER_EXTRA_HINT` from *this* function's import line
> (it is still imported by `ensure_weather_installed`).

### Step 5 — `console.py`: add the `ensure_weather_installed` gate

File: `src/crossroads/console.py`. Add this function **immediately before** `ensure_weather_credentials` (around line 317). It mirrors that function's shape (lazy weather import, no-op guards, `prompt_confirm`, dataset drop/abort).

```python
def ensure_weather_installed(params, reader, writer):
    """Make sure the optional [weather] extra is installed before a weather build.

    Returns True to proceed with the build, False to abort. May remove 'era5_weather'
    from params['datasets'] if the user chooses to continue without weather. A no-op
    when weather is not selected or the extra is already installed — so it never
    prompts unnecessarily. Runs BEFORE the credential gate: no point asking for a key
    when the code to use it is not installed.
    """
    from crossroads.transformers.weather import (
        Era5WeatherTransformer, weather_extra_available, WEATHER_EXTRA_HINT)
    weather_id = Era5WeatherTransformer.source_id

    if weather_id not in params["datasets"]:
        return True                          # weather not selected: nothing to do
    if weather_extra_available():
        return True                          # extra installed: nothing to do

    # Friendly, plain-language explanation: this is a one-time setup step the user
    # still needs to do, NOT a bug. Say what happened, why, and exactly how to fix it.
    writer("")                               # spacer before the block
    writer("You picked weather, but the optional weather add-on isn't installed yet,")
    writer("so Crossroads can't download or build weather data. This isn't an error -")
    writer("weather ships as an optional extra to keep the base install small and fast.")
    writer("")
    writer("To enable weather, install the add-on and then run Crossroads again:")
    writer("")
    writer(f"    {WEATHER_EXTRA_HINT}")
    writer("")

    # Offer to continue without weather for this run. Default N: the user most likely
    # wants to install and re-run, so a bare Enter does not silently drop weather.
    if prompt_confirm(reader, writer,
                      message="Build the other datasets now, without weather? (y/n)",
                      default=False):
        params["datasets"] = [d for d in params["datasets"] if d != weather_id]
        if not params["datasets"]:
            writer("Weather was the only dataset selected, so there's nothing to build.")
            writer("Install the add-on with the command above, then re-run.")
            return False
        writer("Continuing without weather data for this build.")
        return True

    writer("No problem - install the weather add-on with the command above, then re-run.")
    return False
```

Expected result: a new, independently testable gate.

### Step 6 — `console.py`: call the gate in `run_wizard`

File: `src/crossroads/console.py`, `run_wizard` (around line 393). Insert the install gate **immediately after** `gather_parameters(...)` and **before** the temporal/aadf note. Change:

```python
    params = gather_parameters(reader, writer, available=available)

    # Temporal + AADF only: an annual traffic average ...
```

to:

```python
    params = gather_parameters(reader, writer, available=available)

    # Block early if weather was selected without its optional extra installed: no
    # point asking about boundaries or a key when weather cannot be built.
    if not ensure_weather_installed(params, reader, writer):
        writer("Aborted - no database was built.")
        return None

    # Temporal + AADF only: an annual traffic average ...
```

(The existing `ensure_weather_credentials` call a few lines below is unchanged; it now runs after the install gate.)

Expected result: correct ordering; a missing-extra build is caught before the key prompt and summary.

---

## Testing & Verification

Run everything from the repo root: `/Users/will/Documents/Code/Crossroads`.

### Integration / behavior tests (PRIMARY)

#### New file `tests/test_weather_errors.py` — dep-free (must run without the `[weather]` extra)

This file must **not** have a module-level `importorskip`, because its whole point is behavior when deps are absent or a download fails. The 401 path is exercised through the **real** `_download_month` with a fake client, so no network and no `xarray` are needed (the `xarray` import in `_merge_months` is never reached on the raise path).

```python
"""Weather error-translation and extra-availability, exercised WITHOUT the [weather]
extra. No module-level importorskip: these paths must work when deps are absent."""
import sys
import importlib.util

import pytest

from crossroads.transformers import weather as W
from crossroads.transformers.weather import Era5WeatherTransformer


class _FakeClient:
    """Stand-in cdsapi client whose retrieve() raises a chosen error."""
    def __init__(self, exc):
        self._exc = exc
    def retrieve(self, name, request, target):
        raise self._exc


def test_auth_error_is_translated(tmp_path):
    # A 401 from retrieve() becomes a friendly RuntimeError, not a raw HTTPError.
    err = Exception("401 Client Error: Unauthorized ... Authentication failed operation not allowed")
    t = Era5WeatherTransformer()
    dest = str(tmp_path / "era5_land_2021_01.nc")
    with pytest.raises(RuntimeError) as ei:
        t._download_month(_FakeClient(err), 2021, 1, dest)
    msg = str(ei.value)
    assert "authentication failed" in msg.lower()
    assert "Personal Access Token" in msg
    # And no partial temp file is left behind.
    assert not (tmp_path / "era5_land_2021_01.nc.part").exists()


def test_licence_error_still_translated(tmp_path):
    # Regression: the pre-existing licence translation is unchanged by the new auth branch.
    err = Exception("403 Forbidden: required licence not accepted; see terms of use")
    t = Era5WeatherTransformer()
    dest = str(tmp_path / "era5_land_2021_02.nc")
    with pytest.raises(RuntimeError) as ei:
        t._download_month(_FakeClient(err), 2021, 2, dest)
    assert "licence" in str(ei.value).lower()


def test_auth_heuristic_matches_and_excludes():
    assert W._looks_like_auth_error(Exception("401 Unauthorized"))
    assert W._looks_like_auth_error(Exception("Authentication failed"))
    assert not W._looks_like_auth_error(Exception("request is too large; cost limit"))
    assert not W._looks_like_auth_error(Exception("licence not accepted"))


def test_missing_extra_message_has_command():
    m = W._missing_extra_message(ImportError("No module named 'cdsapi'"))
    assert 'pip install "crossroads-uk[weather]"' in m


def test_weather_extra_available_reflects_xarray(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: None if name == "xarray" else real(name))
    assert W.weather_extra_available() is False
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert W.weather_extra_available() is True


def test_download_translates_missing_cdsapi(tmp_path, monkeypatch):
    # Force `import cdsapi` inside _download to fail, even if cdsapi is installed.
    monkeypatch.setitem(sys.modules, "cdsapi", None)   # `import cdsapi` -> ImportError
    t = Era5WeatherTransformer()
    with pytest.raises(RuntimeError) as ei:
        t._download(2021, str(tmp_path / "era5_land_2021.nc"))
    assert 'pip install "crossroads-uk[weather]"' in str(ei.value)
```

Run: `pytest tests/test_weather_errors.py -q` — expect all green, including on a machine **without** the `[weather]` extra.

#### Additions to `tests/test_console.py`

Add these near the existing credential tests (after `test_weather_key_write_failure_is_friendly`, ~line 527). They reuse the existing `scripted(...)` helper. `weather_extra_available` is imported lazily inside the console functions, so patch it on the weather module.

```python
# --- [weather] extra install gate + menu annotation -------------------------

def _set_extra(monkeypatch, present):
    monkeypatch.setattr("crossroads.transformers.weather.weather_extra_available",
                        lambda: present)


def test_menu_annotates_weather_when_extra_absent(monkeypatch):
    _set_extra(monkeypatch, False)
    labels = dict(console.available_datasets())
    assert labels["era5_weather"] != "weather"          # marked, not the plain label
    assert "not installed" in labels["era5_weather"].lower()


def test_menu_plain_weather_when_extra_present(monkeypatch):
    _set_extra(monkeypatch, True)
    labels = dict(console.available_datasets())
    assert labels["era5_weather"] == "weather"


def test_install_gate_noop_when_extra_present(monkeypatch):
    _set_extra(monkeypatch, True)
    reader, writer, output = scripted([])              # reader must not be needed
    params = {"datasets": ["era5_weather", "stats19"], "years": [2021]}
    assert console.ensure_weather_installed(params, reader, writer) is True
    assert params["datasets"] == ["era5_weather", "stats19"]
    assert not any("not installed" in line for line in output)


def test_install_gate_noop_when_weather_not_selected(monkeypatch):
    _set_extra(monkeypatch, False)
    reader, writer, _ = scripted([])
    params = {"datasets": ["stats19"], "years": [2021]}
    assert console.ensure_weather_installed(params, reader, writer) is True
    assert params["datasets"] == ["stats19"]


def test_install_gate_continue_drops_weather(monkeypatch):
    _set_extra(monkeypatch, False)
    reader, writer, output = scripted(["y"])           # continue without weather -> yes
    params = {"datasets": ["era5_weather", "stats19"], "years": [2021]}
    assert console.ensure_weather_installed(params, reader, writer) is True
    assert params["datasets"] == ["stats19"]
    assert any('pip install "crossroads-uk[weather]"' in line for line in output)


def test_install_gate_weather_only_abort_on_continue(monkeypatch):
    _set_extra(monkeypatch, False)
    reader, writer, output = scripted(["y"])           # continue, but nothing else left
    params = {"datasets": ["era5_weather"], "years": [2021]}
    assert console.ensure_weather_installed(params, reader, writer) is False
    assert any("nothing to build" in line.lower() for line in output)


def test_install_gate_decline_aborts(monkeypatch):
    _set_extra(monkeypatch, False)
    reader, writer, output = scripted(["n"])           # do not continue without weather
    params = {"datasets": ["era5_weather", "stats19"], "years": [2021]}
    assert console.ensure_weather_installed(params, reader, writer) is False
    assert any("install the weather add-on" in line.lower() for line in output)
```

Run: `pytest tests/test_console.py -q`.

### Ship-readiness checklist

- [ ] `pytest -q` is fully green (run once with the `[weather]` extra installed, and `tests/test_weather_errors.py -q` once **without** it, to prove both paths).
- [ ] Existing offline weather tests (`test_wizard_builds_weather_offline`, `test_run_wizard_prompts_and_builds_weather_offline`) still pass — the `xarray`-keyed availability check makes the gate a no-op where they run.
- [ ] Manual smoke of the 401 message wording: it names the key as *rejected*, shows the `~/.cdsapirc` format, and points to the CDS profile.
- [ ] `README.md` install instructions already reference `pip install "crossroads-uk[weather]"` (line ~91) — confirm the plan's `WEATHER_EXTRA_HINT` matches it verbatim.

### Manual verification (optional, quick)

With the extra **not** installed, drive the wizard and select weather; confirm the menu shows the "(add-on not installed ...)" label, the gate prints the friendly explanation plus the exact install command, and it asks "Build the other datasets now, without weather? (y/n)". This mirrors the automated gate tests, which are the source of truth.

---

## Performance

`weather_extra_available()` uses `importlib.util.find_spec` (no heavy import) and is called a small, constant number of times per wizard run (menu build + install gate). Negligible. No hot paths, data volumes, or I/O are touched.

---

## Failure Modes

- **`xarray` present but `cdsapi` absent (dev box).** The menu shows weather un-annotated and the gate passes; if the user builds weather, the download's `import cdsapi` fails and is translated to the install message (Step 3). Friendly, just later than selection. Documented, acceptable — this combination does not occur via the published extra.
- **A future non-401 auth phrasing from CDS.** `_looks_like_auth_error` matches several phrasings ("401", "unauthorized", "authentication failed", "operation not allowed", ...). If CDS invents new wording, it falls through to the raw error — the same behavior as today, no regression. Extend the heuristic list if a new phrasing is observed.
- **Heuristic overlap.** Auth (401) is checked before licence/too-large (403). The auth list deliberately excludes cost/licence wording, so a "too large" or "licence" 403 is not misrouted to the auth branch.
- **`params["datasets"]` shape.** The gate assumes `params["datasets"]` is a list of `source_id`s (as produced by `gather_parameters`). It edits it in place, exactly like `ensure_weather_credentials` already does.

---

## Rollback

Revert the two source files and delete the new test file:
```
git checkout -- src/crossroads/transformers/weather.py src/crossroads/console.py
rm tests/test_weather_errors.py
git checkout -- tests/test_console.py
```
No data, schema, or migration is involved — the change is pure control-flow and messaging, so rollback is clean.
