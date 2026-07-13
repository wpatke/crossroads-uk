"""Interactive data-compilation wizard (spec §6).

A terminal wizard that gathers build parameters, confirms them, and drives
``crossroads.init_engine(...).build(...)``. All input/output is injected
(``reader``/``writer``) so the wizard is driven by scripted input in tests with
no real stdin/stdout and no network. Production wires ``reader = lambda: input()``
and ``writer = print`` (see ``main`` below).
"""

import os
from datetime import date

from crossroads import init_engine  # used by run_build below

# One-line, non-blocking pointer shown before the build. Crossroads does not gate on
# data licences (see docs/data-sources.md for why); it only reminds the user they exist.
LICENCE_NOTICE = (
    "Data licences & required attribution: see docs/data-sources.md "
    "(you must attribute DfT/ONS/Copernicus sources when you publish)."
)


def available_datasets():
    """Discover the user-selectable datasets for the wizard menu.

    Returns a list of ``(source_id, display_name)`` in the registry's stable order.
    Imported lazily so importing the console module stays cheap.
    """
    from crossroads.registry import Registry
    return [(t.source_id, t.display_name) for t in Registry().selectable()]


def _prompt(reader, writer, message, *, parse, default=None):
    """Ask, validate, and re-ask until ``parse`` accepts the input.

    ``parse(raw)`` returns the cleaned value or raises ``ValueError`` with a
    human-readable reason. An empty line falls back to ``default`` when one is set.
    """
    # Show the default in the label when one exists, e.g. "Boundary mode [snapshot]: ".
    label = f"{message} [{default}]: " if default is not None else f"{message}: "
    # Re-ask loop: keep prompting until parse() accepts the input. Only ValueError
    # from parse() triggers a re-ask; anything else propagates as a real error.
    while True:
        writer(label)
        raw = reader().strip()
        if not raw and default is not None:
            raw = str(default)
        try:
            return parse(raw)
        except ValueError as exc:
            writer(f"  Invalid input: {exc}. Please try again.")


def _prompt_secret(secret_reader, writer, message):
    """Show `message` via writer, then read one masked/secret line via secret_reader.

    Same label-then-read shape as _prompt, but the value is read through the
    injected secret_reader (getpass in production, scripted in tests) so it is
    never echoed to the terminal.
    """
    writer(f"{message}: ")
    return secret_reader().strip()


def _parse_database_path(raw):
    if not raw:
        raise ValueError("database path cannot be empty")
    return raw


def prompt_database_path(reader, writer):
    """Where to write the DuckDB file. ':memory:' is allowed for a throwaway build."""
    return _prompt(reader, writer,
                   "Database file path", parse=_parse_database_path,
                   default="crossroads.db")


def _parse_one_index(token, count):
    """Parse a single menu index token to an int and range-check it (1..count)."""
    try:
        i = int(token)
    except ValueError:
        raise ValueError(f"'{token}' is not a whole number")
    if not (1 <= i <= count):
        raise ValueError(f"{i} is not between 1 and {count}")
    return i


def _parse_selection(raw, count):
    """Parse a '1-3, 5' style menu selection into a sorted list of 1-based indices.

    Same comma/space/range grammar as the years prompt: singles and tight-hyphen
    ranges, deduped and sorted. Requires at least one index.
    """
    tokens = [t for t in raw.replace(",", " ").split() if t]
    if not tokens:
        raise ValueError("select at least one dataset, e.g. 1 or 1-3")
    picked = set()
    for t in tokens:
        if "-" in t:
            # A closed range like "1-3" (tight hyphen, no surrounding spaces).
            start, _, end = t.partition("-")
            lo = _parse_one_index(start, count)
            hi = _parse_one_index(end, count)
            if lo > hi:
                raise ValueError(f"range '{t}' is backwards (start after end)")
            picked.update(range(lo, hi + 1))
        else:
            picked.add(_parse_one_index(t, count))
    return sorted(picked)


def prompt_datasets(reader, writer, available):
    """Multi-select which datasets to build.

    ``available`` is a list of ``(source_id, display_name)`` in stable order (from
    ``available_datasets()``). Presents them numbered and returns the selected
    ``source_id`` list, using the same range grammar as the years prompt.
    """
    if not available:
        # Defensive: there is always at least one selectable dataset in practice.
        raise ValueError("no selectable datasets are available")
    writer("Which datasets would you like?")
    for i, (_source_id, label) in enumerate(available, start=1):
        writer(f"  {i}. {label}")

    def parse(raw):
        indices = _parse_selection(raw, len(available))
        # Map 1-based menu indices back to source_ids.
        return [available[i - 1][0] for i in indices]

    return _prompt(reader, writer,
                   "Datasets (e.g. 1 or 1-3, 5)", parse=parse, default=None)


_EARLIEST_STATS19_YEAR = 1979


def _parse_one_year(token, latest):
    """Parse a single year token to an int and range-check it. Shared by singles and range endpoints."""
    try:
        y = int(token)
    except ValueError:
        raise ValueError(f"'{token}' is not a whole number")
    if not (_EARLIEST_STATS19_YEAR <= y <= latest):
        raise ValueError(f"{y} is outside {_EARLIEST_STATS19_YEAR}–{latest}")
    return y


def _parse_years(raw):
    # Split on commas and/or whitespace; ignore empty tokens from double separators.
    tokens = [t for t in raw.replace(",", " ").split() if t]
    if not tokens:
        raise ValueError("enter at least one year or range, e.g. 2015-2018 2021")
    latest = date.today().year
    years = set()
    for t in tokens:
        if "-" in t:
            # A closed range like "1990-2000" (tight hyphen, no surrounding spaces).
            start, _, end = t.partition("-")
            lo = _parse_one_year(start, latest)
            hi = _parse_one_year(end, latest)
            if lo > hi:
                raise ValueError(f"range '{t}' is backwards (start after end)")
            years.update(range(lo, hi + 1))
        else:
            years.add(_parse_one_year(t, latest))
    return sorted(years)


def prompt_years(reader, writer):
    """One or more collision years/ranges to ingest (STATS19 is inactive without years)."""
    return _prompt(reader, writer,
                   "Years to ingest (e.g. 2015-2018 2021, space or comma separated)",
                   parse=_parse_years, default=None)


_BOUNDARY_MODES = {"snapshot", "temporal"}
_BOUNDARY_ALIASES = {"1": "snapshot", "2": "temporal"}


def _parse_boundary_mode(raw):
    value = _BOUNDARY_ALIASES.get(raw, raw.lower())
    if value not in _BOUNDARY_MODES:
        raise ValueError("choose 'snapshot' (1) or 'temporal' (2)")
    return value


def prompt_boundary_mode(reader, writer):
    """Retrospective snapshot (latest ONS vintage) vs temporally-sliced boundaries."""
    writer("Boundary mode: 1) snapshot = latest ONS boundaries (default); "
           "2) temporal = boundaries as they were on each collision date.")
    return _prompt(reader, writer,
                   "Boundary mode", parse=_parse_boundary_mode, default="snapshot")


def gather_parameters(reader, writer, *, available=None):
    """Run the parameter prompts and return the build-parameter dict.

    Keys map onto the build surface: ``database_path`` feeds
    ``init_engine(database_path=...)``; ``datasets``, ``years`` and
    ``boundary_mode`` feed ``client.build(...)``. ``available`` is the dataset menu
    (list of ``(source_id, display_name)``); defaults to live discovery and is
    injectable so tests supply a fixed menu.
    """
    if available is None:
        available = available_datasets()
    writer("Crossroads-UK — data compilation wizard")
    writer("")  # blank spacer line
    return {
        "database_path": prompt_database_path(reader, writer),
        "datasets": prompt_datasets(reader, writer, available),
        "years": prompt_years(reader, writer),
        "boundary_mode": prompt_boundary_mode(reader, writer),
    }


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


# --- Confirmation, build wiring, and the entry point ------------------------


def _parse_yes_no(raw):
    value = raw.strip().lower()
    if value in ("y", "yes"):
        return True
    if value in ("n", "no"):
        return False
    raise ValueError("answer 'y' or 'n'")


def prompt_confirm(reader, writer, *, message="Proceed with the build? (y/n)", default=True):
    """Ask a yes/no question before a (possibly long) action.

    Reused for both the build confirmation and the 'continue without weather' branch.
    """
    # Options are shown lowercase; the default is indicated by the "[y]"/"[n]"
    # suffix that _prompt appends from the default value below.
    return _prompt(reader, writer, message,
                   parse=_parse_yes_no, default=("y" if default else "n"))


def format_summary(params):
    """Human-readable recap of the gathered parameters, shown before confirmation."""
    years = ", ".join(str(y) for y in params["years"])
    datasets = ", ".join(params["datasets"])
    return (
        "\nBuild summary:\n"
        f"  Database file : {params['database_path']}\n"
        f"  Datasets      : {datasets}\n"
        f"  Years         : {years}\n"
        f"  Boundary mode : {params['boundary_mode']}\n"
    )


def run_build(params, *, engine_factory=init_engine, cache_dir=None, writer=None):
    """Open the engine and run the build for the gathered parameters.

    ``engine_factory`` defaults to ``crossroads.init_engine`` and is injectable so
    tests can (a) record the exact call without doing work, or (b) point a real
    build at a fixture-seeded ``cache_dir`` and run offline. Returns the Client.
    """
    # No-op writer when the caller doesn't supply one (e.g. a test recording calls).
    say = writer or (lambda _line: None)
    # cache_dir is threaded through only when the caller overrides it (tests);
    # production leaves it None so init_engine uses its default cache location.
    engine_kwargs = {"database_path": params["database_path"]}
    if cache_dir is not None:
        engine_kwargs["cache_dir"] = cache_dir

    client = engine_factory(**engine_kwargs)
    say("\nBuilding database — this may take a while for large year ranges...")
    client.build(datasets=params["datasets"],
                 years=params["years"],
                 boundary_mode=params["boundary_mode"])
    say(f"Done. Database written to {params['database_path']}")
    return client


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


def main(argv=None):
    """Console entry point. Wires stdin/stdout, returns a process exit code.

    Handles ``--version``/``-V`` and ``--help``/``-h`` before starting the wizard,
    so a researcher can check the version without triggering the interactive flow.
    Ctrl-C / EOF abort cleanly without a traceback.
    """
    import sys
    from crossroads import __version__
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] in ("--version", "-V"):
        print(f"crossroads {__version__}")
        return 0
    if args and args[0] in ("--help", "-h"):
        print("Usage: crossroads            # run the interactive build wizard\n"
              "       crossroads --version  # print the version and exit")
        return 0
    reader = lambda: input()   # prompt text is emitted via writer, so input() gets no prompt
    writer = print
    try:
        client = run_wizard(reader, writer)
    except (KeyboardInterrupt, EOFError):
        writer("\nCancelled.")
        return 130   # conventional exit code for SIGINT
    # Close the client so DuckDB releases the file handle (build already wrote it).
    # On the decline/abort path client is None, so there is nothing to close.
    if client is not None:
        client.close()
    return 0
