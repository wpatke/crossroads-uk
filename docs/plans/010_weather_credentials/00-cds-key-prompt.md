# Weather API Key Prompt in the Build Wizard
> Engineer: execute step by step, exactly as written.

Add a wizard step that prompts for the Copernicus CDS API key (masked) and saves it to `~/.cdsapirc` on first run, skips silently when a key is already configured, and only asks when the weather dataset is selected.

---

## Context & Objective

**What exists today.** The interactive build wizard lives in `src/crossroads/console.py`. It gathers parameters via `gather_parameters(reader, writer)` (four prompts: database path, datasets, years, boundary mode), shows a summary, prints a licence notice, asks for confirmation, then builds. All I/O is injected: `reader()` returns a line of input, `writer(line)` emits a line. Production wires `reader = lambda: input()` and `writer = print` in `main()`; tests replay a scripted list of answers (`tests/test_console.py`, `scripted()` helper at line 21).

The weather transformer (`src/crossroads/transformers/weather.py`) downloads ERA5-Land via `cdsapi.Client()` (line 201). `cdsapi.Client()` reads credentials, in order, from: explicit `url=`/`key=` args → `CDSAPI_URL`/`CDSAPI_KEY` environment variables → the `~/.cdsapirc` file. **Today Crossroads uses none of these explicitly** — the user must hand-create `~/.cdsapirc` before running, or the build fails partway through with the friendly `_missing_key_message` (weather.py:49-65). That manual dotfile step is the clunk we are removing.

**What changes.** The wizard gains a credential step, placed in `run_wizard` **after** `gather_parameters` returns and **before** the build summary. When the weather dataset (`era5_weather`) is selected and no CDS key is configured, the wizard prompts for the token (masked, not echoed), writes `~/.cdsapirc`, and continues. When a key is already present (env vars or file), the step does nothing. The weather download path in `weather.py` is **not modified** — `cdsapi.Client()` picks up the file we wrote, exactly as it would a hand-created one.

**The goal.** A first-time user runs the wizard, is asked for their token once, and the build proceeds. Every subsequent run skips the prompt because the file now exists. No secret is threaded through the build layers.

**Reused anchors (do not re-invent):**
- `CDS_HOME_URL = "https://cds.climate.copernicus.eu"` and `ERA5_LAND_URL` — defined in `weather.py:45-46`. The `.cdsapirc` `url:` line is `f"{CDS_HOME_URL}/api"` (matches `_missing_key_message`, weather.py:58).
- `Era5WeatherTransformer.source_id == "era5_weather"` (weather.py:144) — the dataset id to detect/drop.
- `prompt_confirm(reader, writer, *, default=True)` (console.py:218) and its `_parse_yes_no` (console.py:209) — reused for the "continue without weather" question.

---

## Acceptance Criteria

All verifiable by the tests in the Testing section, run offline with no network:

1. **First run, no key, weather selected →** wizard prompts once via the masked reader; `~/.cdsapirc` is created containing exactly `url: https://cds.climate.copernicus.eu/api\nkey: <token>\n`; output contains `Saved ~/.cdsapirc.`; the build then proceeds with `era5_weather` still in the dataset list.
2. **Key already present (file) →** no prompt is issued (the masked reader is never called); the existing `~/.cdsapirc` is left byte-for-byte unchanged; the build proceeds.
3. **Key already present (env vars `CDSAPI_URL`+`CDSAPI_KEY`) →** no prompt; no file written; build proceeds.
4. **Weather not selected →** no prompt regardless of key state; the masked reader is never called.
5. **Blank token → "continue without weather" = yes, other datasets present →** `era5_weather` is removed from `params["datasets"]`; the remaining datasets build; no file written.
6. **Blank token → "continue without weather" = yes, weather was the only dataset →** wizard aborts with `Nothing to build — aborted.`; returns `None`; no build runs.
7. **Blank token → "continue without weather" = no (or blank) →** the token prompt is re-asked; entering a token then saves the file and proceeds.
7b. **`~/.cdsapirc` write fails (e.g. read-only home) →** the wizard prints a friendly message with the exact file to create by hand and aborts cleanly (returns `False`, no traceback).
8. **The token is never echoed** — production input uses `getpass`, verified by the injected `secret_reader` seam (no assertion on terminal masking itself, which is a `getpass` guarantee).
9. The full test suite passes: `python -m pytest tests/test_console.py tests/test_weather.py -q`.

---

## Scope

**In:**
- A masked credential prompt in the wizard, gated on weather selection + absent key.
- Detection of an existing key via env vars or `~/.cdsapirc`.
- Atomic write of `~/.cdsapirc` with owner-only permissions.
- Blank-token handling: continue-without-weather (drop dataset, or abort if nothing left) vs. re-ask.
- Threading a new injected `secret_reader` seam through `run_wizard` so the masked prompt stays fully testable.
- Updating the one existing test that selects weather (`test_wizard_builds_weather_offline`) so it stays deterministic under the new gate.

**Out (do not implement):**
- Validating the token against CDS (no live pre-flight request). A wrong token surfaces later via the existing `_missing_key_message`/auth error at download time.
- Automating ERA5-Land **licence acceptance** — that is a manual web click on the Copernicus site. The wizard only *reminds* the user (reuses `ERA5_LAND_URL`). A user who skips it hits the existing `_licence_message` at build time; that is acceptable and out of scope to prevent.
- Any change to `weather.py`'s download/auth path.
- Editing an existing `~/.cdsapirc` (if the file exists we skip entirely, even if malformed — see Failure Modes).
- Reading/prompting for the CDS **url** — it is fixed (`{CDS_HOME_URL}/api`); only the key is prompted.

---

## Constraints

- **License:** MIT. All code written independently; no copying from `../stats19/` (GPL-3).
- **Style:** Match `console.py` conventions — injected `reader`/`writer`, small pure helpers, plain-language comments (the user reads all code before commit). Keep it simple; prefer the explicit small function over abstraction.
- **Testability:** Every new interactive path must be drivable by scripted I/O with no real stdin/stdout and no network. This is why masked input goes through an injected `secret_reader` rather than calling `getpass` inline.
- **Backwards compatibility:** `main()` must keep working with no signature change from its callers; `run_wizard`'s new `secret_reader` parameter must default so existing tests that don't pass it still work (they only exercise non-weather paths, which never reach the prompt).
- **Dependencies:** No new third-party deps. `getpass` is stdlib.
- **Cross-platform:** `os.chmod(..., 0o600)` is best-effort (POSIX); wrap so non-POSIX platforms don't error.

---

## Approach / Architecture

**Chosen design: write `~/.cdsapirc`, do not thread the key through the build.**

The wizard prompts, writes the standard `~/.cdsapirc` file, and the *unchanged* `cdsapi.Client()` in `weather.py` reads it at download time. This keeps the secret out of `gather_parameters → run_build → client.build → transformer.extract → _download`, all of which stay untouched. The alternative — passing `key=` down five layers into `cdsapi.Client(url, key)` — was rejected: it spreads a weather-only secret across the whole build surface for no benefit, since `cdsapi` already has a well-defined file-based credential source.

**Masked input via an injected `secret_reader` seam.** Every existing prompt reads through `reader()`, which tests script. Masked input (`getpass`) reads the terminal directly and would bypass that seam, making the prompt untestable. So `run_wizard` gains a `secret_reader` parameter alongside `reader`: production defaults it to a `getpass`-backed lambda (masked, no echo); tests inject a scripted function returning the token string. This preserves both masking in production and full scriptability in tests. Rejected alternative — route the token through the normal `reader` (visible echo): simpler but prints the credential to the terminal and scrollback.

**Placement in `run_wizard`, before the summary.** The credential step can *mutate* the dataset list (drop `era5_weather` when the user continues without it) or abort the whole build (nothing left). Running it before `format_summary` means the summary reflects the true, final dataset list. It lives in `run_wizard` (the orchestrator), not inside `gather_parameters` (the pure prompt-gatherer), keeping responsibilities clean.

**Reusing `prompt_confirm` for the blank-token branch.** The "Continue without weather data? (y/n)" question reuses the existing y/n machinery by adding an optional `message=` parameter to `prompt_confirm` (default unchanged), so the existing confirm caller is untouched and we don't duplicate `_parse_yes_no`.

**Data flow:**
```
run_wizard
  params = gather_parameters(...)                 # unchanged
  proceed = ensure_weather_credentials(params, reader, secret_reader, writer)
      ├─ weather not in params["datasets"]  → return True (no-op)
      ├─ _cds_key_present()                 → return True (no-op)
      └─ prompt loop:
           token entered → _write_cdsapirc(token) ; print "Saved…" ; licence note ; return True
           blank + "continue? y" → drop era5_weather
                                     ├─ datasets now empty → print "Nothing to build" ; return False
                                     └─ else → return True
           blank + "continue? n/blank" → re-ask token
  if not proceed: print "Aborted…" ; return None
  writer(format_summary(params))                  # reflects dropped weather, if any
  … licence notice, prompt_confirm, run_build …   # unchanged
```

---

## Implementation Steps

All edits are in **`src/crossroads/console.py`** unless stated otherwise.

### Step 1 — Add `import os`

At the top of `console.py`, the current imports are:
```python
from datetime import date

from crossroads import init_engine  # used by run_build below
```
Add `import os` above `from datetime import date` (the new helpers need `os.environ`, `os.path`, `os.replace`, `os.chmod`):
```python
import os
from datetime import date

from crossroads import init_engine  # used by run_build below
```
**Expected result:** `os` is available module-wide.

### Step 2 — Add credential detection + file-path helpers

Add these helpers near the other small module-level helpers (e.g. just above `_parse_yes_no` at line 209). Plain-language comments included:

```python
# --- Copernicus CDS credential handling (weather dataset) --------------------

def _cdsapirc_path():
    """Location of the cdsapi credentials file in the user's home directory."""
    return os.path.expanduser("~/.cdsapirc")


def _cds_key_present():
    """True when cdsapi already has credentials, so the wizard should not prompt.

    Matches the sources cdsapi.Client() actually reads: both CDSAPI_* environment
    variables set, OR a ~/.cdsapirc file already on disk.
    """
    if os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY"):
        return True
    return os.path.exists(_cdsapirc_path())


def _write_cdsapirc(token, cds_home_url):
    """Write ~/.cdsapirc with the fixed CDS url and the user's token.

    Written atomically (temp file + os.replace) so a crash never leaves a half
    file, and with owner-only permissions because it holds a credential. The
    format mirrors weather.py's _missing_key_message so cdsapi.Client() reads it
    unchanged.
    """
    path = _cdsapirc_path()
    content = f"url: {cds_home_url}/api\nkey: {token}\n"
    tmp = path + ".part"
    with open(tmp, "w") as fh:
        fh.write(content)
    try:
        os.chmod(tmp, 0o600)   # owner read/write only; POSIX best-effort
    except OSError:
        pass                   # platforms without POSIX perms: skip silently
    os.replace(tmp, path)      # atomic promote
```
**Expected result:** three importable helpers; no behavior change yet (nothing calls them).

### Step 3 — Add a masked-prompt helper

Add near `_prompt` (console.py:32). It mirrors `_prompt`'s "label via writer, then read" shape but reads through `secret_reader` so production can mask and tests can script:

```python
def _prompt_secret(secret_reader, writer, message):
    """Show `message` via writer, then read one masked/secret line via secret_reader.

    Same label-then-read shape as _prompt, but the value is read through the
    injected secret_reader (getpass in production, scripted in tests) so it is
    never echoed to the terminal.
    """
    writer(f"{message}: ")
    return secret_reader().strip()
```
**Expected result:** a testable secret-input seam; nothing calls it yet.

### Step 4 — Parametrize `prompt_confirm`'s message

Change `prompt_confirm` (console.py:218) to accept an optional `message`, defaulting to the current text so the existing caller is unchanged:

```python
def prompt_confirm(reader, writer, *, message="Proceed with the build? (y/n)", default=True):
    """Ask a yes/no question before a (possibly long) action. Reused for both the
    build confirmation and the 'continue without weather' branch."""
    return _prompt(reader, writer, message,
                   parse=_parse_yes_no, default=("y" if default else "n"))
```
**Expected result:** `prompt_confirm(reader, writer)` behaves exactly as before; a caller may now pass `message=...`.

### Step 5 — Add `ensure_weather_credentials`

Add this above `run_wizard` (console.py:263). It is the core of the feature:

```python
def ensure_weather_credentials(params, reader, secret_reader, writer):
    """Make sure a CDS API key exists before a weather build; prompt and save one if not.

    Returns True to proceed with the build, False to abort ('nothing to build').
    May remove 'era5_weather' from params['datasets'] if the user chooses to
    continue without weather. A no-op when weather is not selected or a key is
    already configured — so a returning user never sees a prompt.
    """
    # Imported lazily so importing console stays cheap and to reuse the exact
    # CDS constants/source id from the weather transformer (DRY).
    from crossroads.transformers.weather import (
        Era5WeatherTransformer, CDS_HOME_URL, ERA5_LAND_URL)
    weather_id = Era5WeatherTransformer.source_id

    if weather_id not in params["datasets"]:
        return True                      # weather not selected: nothing to do
    if _cds_key_present():
        return True                      # already configured: no prompt

    writer("")                           # spacer before the credential block
    writer("Weather data needs a free Copernicus CDS API key, and none was found")
    writer("on this machine (no ~/.cdsapirc and no CDSAPI_* environment variables).")
    writer(f"Get your token from {CDS_HOME_URL} (log in -> your profile ->")
    writer("'Personal Access Token').")

    while True:
        token = _prompt_secret(secret_reader, writer,
                               "Personal Access Token (leave blank to skip)")
        if token:
            try:
                _write_cdsapirc(token, CDS_HOME_URL)
            except OSError as exc:
                # Rare (e.g. a read-only home directory). Don't crash with a raw
                # traceback: explain the problem, show the file to create by hand,
                # then abort cleanly.
                writer("")
                writer(f"Could not write {_cdsapirc_path()}: {exc}")
                writer("Create it by hand with these two lines, then re-run:")
                writer(f"    url: {CDS_HOME_URL}/api")
                writer("    key: <your token>")
                return False
            writer("Saved ~/.cdsapirc.")
            writer("")
            writer("Note: you must also accept the ERA5-Land licence once (free) at")
            writer(f"  {ERA5_LAND_URL}")
            writer("  (Download tab -> Terms of use -> Accept)")
            return True

        # Blank token: offer to continue without weather. Default N re-asks for a key.
        if prompt_confirm(reader, writer,
                          message="Continue without weather data? (y/n)",
                          default=False):
            params["datasets"] = [d for d in params["datasets"] if d != weather_id]
            if not params["datasets"]:
                writer("Nothing to build — aborted.")
                return False
            writer("Continuing without weather data.")
            return True
        # 'n' or blank: loop back and re-ask for the token.
```
**Expected result:** the full credential decision logic exists as one testable function.

### Step 6 — Wire it into `run_wizard`

Modify `run_wizard` (console.py:263-276) to add the `secret_reader` parameter (defaulted for back-compat) and call `ensure_weather_credentials` before the summary:

```python
def run_wizard(reader, writer, *, secret_reader=None, engine_factory=init_engine,
               cache_dir=None, available=None):
    """Drive the full wizard. Returns the built Client, or None if the user declined.

    All I/O is injected so this is fully testable with scripted input.
    ``secret_reader`` reads the (masked) CDS token; it defaults to a getpass-backed
    reader in production and is injected by tests. ``available`` overrides the
    dataset menu for deterministic tests.
    """
    if secret_reader is None:
        import getpass
        # Label is printed via writer; pass an empty getpass prompt so nothing
        # extra is shown, and the typed token is not echoed.
        secret_reader = lambda: getpass.getpass("")

    params = gather_parameters(reader, writer, available=available)
    if not ensure_weather_credentials(params, reader, secret_reader, writer):
        writer("Aborted — no database was built.")
        return None
    writer(format_summary(params))
    writer(LICENCE_NOTICE)          # non-blocking reminder; not a prompt
    if not prompt_confirm(reader, writer, default=True):
        writer("Aborted — no database was built.")
        return None
    return run_build(params, engine_factory=engine_factory,
                     cache_dir=cache_dir, writer=writer)
```
**Expected result:** `main()` is unchanged and still calls `run_wizard(reader, writer)`; production gets masked input automatically; the summary reflects any dropped weather dataset.

### Step 7 — Update the existing weather wizard test so the gate is deterministic

`tests/test_console.py::test_wizard_builds_weather_offline` (line 294) selects weather (`"1-2"`) and calls `run_wizard` with no credential provision. Under the new gate it would either prompt (desyncing its scripted answers) or skip based on the developer's real `~/.cdsapirc` — both non-deterministic. Fix it by giving the test a configured key via env vars so `_cds_key_present()` returns True and the prompt is bypassed, leaving its scripted answers unchanged.

Change its signature to take `monkeypatch` and set the env vars at the top:
```python
@pytest.mark.integration
def test_wizard_builds_weather_offline(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    # A configured key bypasses the new credential prompt so this build test stays
    # focused on the offline weather build (the prompt is covered by its own tests).
    monkeypatch.setenv("CDSAPI_URL", "https://example.invalid/api")
    monkeypatch.setenv("CDSAPI_KEY", "test-key")
    cache = str(tmp_path / "cache"); _seed_full_cache(cache)
    # ... rest unchanged ...
```
**Expected result:** the existing offline weather build test passes unchanged in behavior, now independent of the developer's real home directory.

---

## Testing & Verification

Add tests to **`tests/test_console.py`**. They are fast, offline, deterministic — no network, no real stdin/stdout. Reuse the existing `scripted()` helper for `reader`, and add a tiny secret-reader helper.

### Test harness additions

Add near `scripted()` (line 21):
```python
def scripted_secret(tokens):
    """Return a secret_reader that replays `tokens` in order (masked-input stand-in)."""
    tokens = list(tokens)
    def secret_reader():
        return tokens.pop(0)
    return secret_reader


def _boom_secret():
    """A secret_reader that fails if called — proves the prompt was NOT shown."""
    def secret_reader():
        raise AssertionError("secret_reader was called but no prompt was expected")
    return secret_reader
```

All key-handling tests must isolate the home directory and env so they never touch the developer's real `~/.cdsapirc`. Use a fixture pattern in each test:
```python
def _isolate_cds(monkeypatch, tmp_path):
    """Point ~/.cdsapirc at an empty tmp home and clear CDSAPI_* env vars."""
    monkeypatch.setenv("HOME", str(tmp_path))          # os.path.expanduser("~") uses $HOME on POSIX
    monkeypatch.delenv("CDSAPI_URL", raising=False)
    monkeypatch.delenv("CDSAPI_KEY", raising=False)
```
> Note: `os.path.expanduser("~")` honors `$HOME` on Linux/macOS (the dev + CI platforms). This keeps the tests hermetic.

### Integration tests (PRIMARY — drive the whole credential step)

Menu note: these call `ensure_weather_credentials` directly with an explicit `params` dict, so they do not depend on menu ordering. `weather_id` is `"era5_weather"`.

1. **`test_weather_key_prompt_saves_file`** — first run, saves file, proceeds:
   ```python
   def test_weather_key_prompt_saves_file(tmp_path, monkeypatch):
       _isolate_cds(monkeypatch, tmp_path)
       reader, writer, output = scripted([])                 # y/n reader unused
       secret = scripted_secret(["MY-TOKEN-123"])
       params = {"datasets": ["era5_weather", "stats19"], "years": [2023]}
       assert console.ensure_weather_credentials(params, reader, secret, writer) is True
       rc = tmp_path / ".cdsapirc"
       assert rc.read_text() == "url: https://cds.climate.copernicus.eu/api\nkey: MY-TOKEN-123\n"
       assert params["datasets"] == ["era5_weather", "stats19"]     # unchanged
       assert any("Saved ~/.cdsapirc." in line for line in output)
   ```

2. **`test_weather_key_skipped_when_file_present`** — existing file → no prompt, untouched:
   ```python
   def test_weather_key_skipped_when_file_present(tmp_path, monkeypatch):
       _isolate_cds(monkeypatch, tmp_path)
       rc = tmp_path / ".cdsapirc"
       rc.write_text("url: x\nkey: y\n")
       before = rc.read_text()
       reader, writer, _ = scripted([])
       params = {"datasets": ["era5_weather"], "years": [2023]}
       assert console.ensure_weather_credentials(params, reader, _boom_secret(), writer) is True
       assert rc.read_text() == before                       # not rewritten
   ```

3. **`test_weather_key_skipped_when_env_present`** — env vars → no prompt, no file:
   ```python
   def test_weather_key_skipped_when_env_present(tmp_path, monkeypatch):
       _isolate_cds(monkeypatch, tmp_path)
       monkeypatch.setenv("CDSAPI_URL", "https://example.invalid/api")
       monkeypatch.setenv("CDSAPI_KEY", "abc")
       reader, writer, _ = scripted([])
       params = {"datasets": ["era5_weather"], "years": [2023]}
       assert console.ensure_weather_credentials(params, reader, _boom_secret(), writer) is True
       assert not (tmp_path / ".cdsapirc").exists()
   ```

4. **`test_no_prompt_when_weather_not_selected`** — gate off entirely:
   ```python
   def test_no_prompt_when_weather_not_selected(tmp_path, monkeypatch):
       _isolate_cds(monkeypatch, tmp_path)
       reader, writer, _ = scripted([])
       params = {"datasets": ["stats19"], "years": [2023]}
       assert console.ensure_weather_credentials(params, reader, _boom_secret(), writer) is True
       assert not (tmp_path / ".cdsapirc").exists()
   ```

5. **`test_blank_token_continue_drops_weather`** — blank + "y", other datasets remain:
   ```python
   def test_blank_token_continue_drops_weather(tmp_path, monkeypatch):
       _isolate_cds(monkeypatch, tmp_path)
       reader, writer, output = scripted(["y"])              # "Continue without weather?" -> yes
       secret = scripted_secret([""])                        # blank token
       params = {"datasets": ["era5_weather", "stats19"], "years": [2023]}
       assert console.ensure_weather_credentials(params, reader, secret, writer) is True
       assert params["datasets"] == ["stats19"]              # weather dropped
       assert not (tmp_path / ".cdsapirc").exists()
   ```

6. **`test_blank_token_weather_only_aborts`** — blank + "y", nothing left → abort:
   ```python
   def test_blank_token_weather_only_aborts(tmp_path, monkeypatch):
       _isolate_cds(monkeypatch, tmp_path)
       reader, writer, output = scripted(["y"])
       secret = scripted_secret([""])
       params = {"datasets": ["era5_weather"], "years": [2023]}
       assert console.ensure_weather_credentials(params, reader, secret, writer) is False
       assert any("Nothing to build" in line for line in output)
   ```

7. **`test_blank_token_then_reenter_saves`** — blank + "n" re-asks, then a token saves:
   ```python
   def test_blank_token_then_reenter_saves(tmp_path, monkeypatch):
       _isolate_cds(monkeypatch, tmp_path)
       reader, writer, _ = scripted(["n"])                   # decline "continue without weather"
       secret = scripted_secret(["", "REAL-TOKEN"])          # blank, then a real token
       params = {"datasets": ["era5_weather"], "years": [2023]}
       assert console.ensure_weather_credentials(params, reader, secret, writer) is True
       assert (tmp_path / ".cdsapirc").read_text().endswith("key: REAL-TOKEN\n")
       assert params["datasets"] == ["era5_weather"]
   ```

8b. **`test_weather_key_write_failure_is_friendly`** — a failed save gives a friendly message and a clean abort, not a traceback:
   ```python
   def test_weather_key_write_failure_is_friendly(tmp_path, monkeypatch):
       _isolate_cds(monkeypatch, tmp_path)
       def boom(*a, **k):
           raise OSError("read-only file system")
       monkeypatch.setattr(console, "_write_cdsapirc", boom)
       reader, writer, output = scripted([])
       secret = scripted_secret(["TOKEN"])
       params = {"datasets": ["era5_weather"], "years": [2023]}
       assert console.ensure_weather_credentials(params, reader, secret, writer) is False
       assert any("Could not write" in line for line in output)
       assert any("url: https://cds.climate.copernicus.eu/api" in line for line in output)
   ```

### End-to-end wizard test (through `run_wizard`)

8. **`test_run_wizard_prompts_and_builds_weather_offline`** — full flow with a scripted secret_reader, seeded cache, real offline build. Model it on `test_wizard_builds_weather_offline` (line 294) but instead of setting env vars, isolate home and pass a `secret_reader`, then assert the file was written and the build populated:
   ```python
   @pytest.mark.integration
   def test_run_wizard_prompts_and_builds_weather_offline(tmp_path, monkeypatch):
       pytest.importorskip("xarray")
       _isolate_cds(monkeypatch, tmp_path)
       cache = str(tmp_path / "cache"); _seed_full_cache(cache)
       shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "weather", "era5_land_sample.nc"),
                   os.path.join(cache, "era5_land_2023.nc"))
       db_path = str(tmp_path / "wiz.duckdb")
       # Menu order is source_id: 1=weather (era5_weather), 2=stats19. Pick both with "1-2".
       reader, writer, _ = scripted([db_path, "1-2", "2023", "snapshot", "y"])
       secret = scripted_secret(["TOKEN-XYZ"])
       client = console.run_wizard(reader, writer, secret_reader=secret, cache_dir=cache)
       try:
           assert client is not None and os.path.exists(db_path)
           assert (tmp_path / ".cdsapirc").read_text().endswith("key: TOKEN-XYZ\n")
           # Same post-build checks the existing weather test uses (test_console.py:305-307):
           # the weather table is populated and collisions got weather-stamped.
           assert client.con.execute("SELECT count(*) FROM weather").fetchone()[0] > 0
           assert client.con.execute(
               "SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL"
           ).fetchone()[0] >= 1
       finally:
           client.close()
   ```

### Update existing test

9. Apply **Step 7** to `test_wizard_builds_weather_offline` (add `monkeypatch`, set `CDSAPI_URL`/`CDSAPI_KEY`).

### Ship-readiness checklist / runnable commands

Run from repo root:
```bash
# Focused: the console + weather suites (fast, offline)
python -m pytest tests/test_console.py tests/test_weather.py -q

# The new credential tests by name
python -m pytest tests/test_console.py -q -k "weather_key or blank_token or credential or prompts_and_builds"

# Full suite (integration marker included)
python -m pytest -q
```
**Pass condition:** all green. Acceptance criteria 1-9 map onto tests 1-8 + the updated test 9 + the full-suite run.

---

## Performance

Not a hot path. The credential step runs once per wizard invocation, does at most a few string writes and one small file write (< 100 bytes). `_cds_key_present()` is two env lookups + one `os.path.exists`. No measurable cost. The build itself is unchanged.

---

## Failure Modes

- **Token typed incorrectly.** We save whatever is entered (no live validation — out of scope). The wrong key surfaces at download time via the existing `_missing_key_message`/auth error in `weather.py`. Recovery: user deletes `~/.cdsapirc` (or re-runs after fixing it) and runs again. Guardrail: the saved-file format is fixed and correct, so only the key value can be wrong.
- **Existing but malformed `~/.cdsapirc`.** `_cds_key_present()` returns True on file existence, so we skip and do not overwrite the user's file. The build then fails with the existing friendly message. This is deliberate — we never clobber a file the user may have hand-tuned. (Documented in Scope as out.)
- **Home directory not writable / write fails.** `ensure_weather_credentials` catches the `OSError` from `_write_cdsapirc`, prints a friendly message showing the exact two lines to create by hand, and returns `False` — so `run_wizard` aborts cleanly with `Aborted — no database was built.` and no traceback. Covered by test 8b.
- **`os.chmod` unsupported (non-POSIX).** Wrapped in `try/except OSError` → silently skipped; the file is still written.
- **User selects weather, has no token, and no other dataset, answers "continue" = yes.** Handled: aborts cleanly with `Nothing to build — aborted.` and returns `None` (no empty build).
- **Licence not accepted.** Out of scope by decision — build fails later with the existing `_licence_message`; the wizard already printed the accept-here reminder. Non-destructive; re-run after accepting resumes via per-month caching.

---

## Rollback

The change is additive and localized to two files. To undo:
1. Revert `src/crossroads/console.py` (remove `import os`, the four helpers, `_prompt_secret`, the `prompt_confirm` `message=` param, `ensure_weather_credentials`, and the `secret_reader` wiring + call in `run_wizard`).
2. Revert `tests/test_console.py` (remove new tests/helpers; restore `test_wizard_builds_weather_offline` to its original signature).

No data migrations, no schema changes, no persisted state beyond the user's own `~/.cdsapirc` (which was the manual setup step before this feature). Reverting leaves behavior identical to today.

---

## Open Questions

None outstanding. Both prior questions are resolved: a failed `~/.cdsapirc` write is caught and reported as a friendly message with a clean abort (Step 5 + test 8b), and the end-to-end build assertions use the confirmed `weather` table plus the `collisions.temperature_c` stamp, mirroring the existing `test_wizard_builds_weather_offline` (test_console.py:305-307).
