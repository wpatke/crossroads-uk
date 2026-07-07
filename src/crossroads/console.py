"""Interactive data-compilation wizard (spec §6, master-plan Step 5).

A terminal wizard that gathers build parameters, confirms them, and drives
``crossroads.init_engine(...).build(...)``. All input/output is injected
(``reader``/``writer``) so the wizard is driven by scripted input in tests with
no real stdin/stdout and no network. Production wires ``reader = lambda: input()``
and ``writer = print`` (see Stage 02's ``main``).
"""

from datetime import date

from crossroads import init_engine  # used in Stage 02; harmless to import now


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


# --- Stage 02: confirmation, build wiring, and the entry point --------------


def _parse_yes_no(raw):
    value = raw.strip().lower()
    if value in ("y", "yes"):
        return True
    if value in ("n", "no"):
        return False
    raise ValueError("answer 'y' or 'n'")


def prompt_confirm(reader, writer, *, default=True):
    """Ask the user to confirm before the (possibly long) build runs."""
    # Options are shown lowercase; the default is indicated by the "[y]"/"[n]"
    # suffix that _prompt appends from the default value below.
    return _prompt(reader, writer, "Proceed with the build? (y/n)",
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


def run_wizard(reader, writer, *, engine_factory=init_engine, cache_dir=None, available=None):
    """Drive the full wizard. Returns the built Client, or None if the user declined.

    All I/O is injected so this is fully testable with scripted input. ``available``
    overrides the dataset menu for deterministic tests.
    """
    params = gather_parameters(reader, writer, available=available)
    writer(format_summary(params))
    if not prompt_confirm(reader, writer, default=True):
        writer("Aborted — no database was built.")
        return None
    return run_build(params, engine_factory=engine_factory,
                     cache_dir=cache_dir, writer=writer)


def main(argv=None):
    """Console entry point. Wires stdin/stdout, returns a process exit code.

    ``argv`` is accepted for signature stability but currently unused (the wizard
    takes all parameters interactively). Ctrl-C / EOF abort cleanly without a
    traceback.
    """
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
