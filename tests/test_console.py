"""Tests for the data-compilation wizard prompts (console.py).

Fast, offline, deterministic. A scripted-I/O harness replaces stdin/stdout so no
real input/print or network is involved: `reader` replays a list of answers and
`writer` collects the emitted lines.
"""
import importlib.metadata as md
import os
import shutil

import pytest

from crossroads import console
from crossroads.transformers.spatial import LADBoundaryTransformer, CTYUABoundaryTransformer

STATS19_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "stats19")
ONS_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ons")


def scripted(answers):
    """Return (reader, writer, output) where reader replays `answers` in order."""
    answers = list(answers)
    output = []
    def reader():
        return answers.pop(0)
    def writer(line):
        output.append(line)
    return reader, writer, output


def test_gather_parameters_happy_path():
    reader, writer, _ = scripted(["mydb.duckdb", "2021 2022", "temporal"])
    params = console.gather_parameters(reader, writer)
    assert params == {
        "database_path": "mydb.duckdb",
        "years": [2021, 2022],
        "boundary_mode": "temporal",
    }


def test_defaults_via_empty_lines():
    # Blank lines fall back to defaults, except years (no default) which is answered.
    reader, writer, _ = scripted(["", "2023", ""])
    params = console.gather_parameters(reader, writer)
    assert params["database_path"] == "crossroads.db"
    assert params["years"] == [2023]
    assert params["boundary_mode"] == "snapshot"


def test_years_comma_and_space_dedup_sort():
    reader, writer, _ = scripted(["2023, 2021 2021,2022"])
    assert console.prompt_years(reader, writer) == [2021, 2022, 2023]


def test_years_ranges_and_singles():
    # Mixed ranges and singles expand and sort.
    reader, writer, _ = scripted(["1990-1992, 2010, 2022-2024"])
    assert console.prompt_years(reader, writer) == [1990, 1991, 1992, 2010, 2022, 2023, 2024]
    # Overlapping ranges collapse via the set.
    reader, writer, _ = scripted(["2010-2012 2011-2013"])
    assert console.prompt_years(reader, writer) == [2010, 2011, 2012, 2013]


def test_years_rejects_bad_range():
    # A backwards range is rejected, then a valid one is accepted.
    reader, writer, output = scripted(["2024-2020", "2020-2024"])
    assert console.prompt_years(reader, writer) == [2020, 2021, 2022, 2023, 2024]
    invalid = [line for line in output if "Invalid input" in line]
    assert len(invalid) == 1
    assert "backwards" in invalid[0]
    # A spaced hyphen is not a valid range (tight-hyphen rule): rejected, then valid retry.
    reader, writer, output = scripted(["2020 - 2024", "2023"])
    assert console.prompt_years(reader, writer) == [2023]
    assert any("Invalid input" in line for line in output)


def test_years_rejects_then_accepts():
    # Non-numeric then out-of-range, then a valid year — re-asks twice.
    reader, writer, output = scripted(["abc", "1500", "2023"])
    assert console.prompt_years(reader, writer) == [2023]
    assert len([line for line in output if "Invalid input" in line]) == 2


def test_years_requires_at_least_one():
    # prompt_years has no default, so an empty line is rejected, then 2023 accepted.
    reader, writer, output = scripted(["", "2023"])
    assert console.prompt_years(reader, writer) == [2023]
    assert len([line for line in output if "Invalid input" in line]) == 1


def test_boundary_mode_numeric_and_case():
    reader, writer, _ = scripted(["2"])
    assert console.prompt_boundary_mode(reader, writer) == "temporal"
    reader, writer, _ = scripted(["SNAPSHOT"])
    assert console.prompt_boundary_mode(reader, writer) == "snapshot"
    reader, writer, output = scripted(["bogus", "1"])
    assert console.prompt_boundary_mode(reader, writer) == "snapshot"
    assert any("Invalid input" in line for line in output)


def test_database_path_allows_memory():
    reader, writer, _ = scripted([":memory:"])
    assert console.prompt_database_path(reader, writer) == ":memory:"


# --- Stage 02: build wiring, orchestration, and the entry point -------------


class _FakeClient:
    """Recording fake so functional tests can prove the exact build invocation
    without doing any real work (no engine, no network)."""
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.build_kwargs = None
        self.closed = False

    def build(self, **kwargs):
        self.build_kwargs = kwargs
        return self

    def close(self):
        self.closed = True


def test_wizard_produces_correct_build_invocation():
    reader, writer, _ = scripted(["mydb.duckdb", "2022 2023", "temporal", "y"])
    captured = {}
    def factory(**kwargs):
        client = _FakeClient(**kwargs)
        captured["client"] = client
        return client
    result = console.run_wizard(reader, writer, engine_factory=factory)
    assert result is captured["client"]
    assert captured["client"].init_kwargs == {"database_path": "mydb.duckdb"}
    assert captured["client"].build_kwargs == {"years": [2022, 2023],
                                               "boundary_mode": "temporal"}


def test_decline_does_not_build():
    reader, writer, output = scripted([":memory:", "2023", "snapshot", "n"])
    calls = []
    def factory(**kwargs):
        calls.append(kwargs); return _FakeClient(**kwargs)
    result = console.run_wizard(reader, writer, engine_factory=factory)
    assert result is None
    assert calls == []            # build path never entered
    assert any("Aborted" in line for line in output)


def test_main_abort_path_returns_zero(monkeypatch, capsys):
    # Exercise the real entry point offline. Declining means no build, no network.
    answers = iter([":memory:", "2023", "snapshot", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert console.main() == 0
    assert "Aborted" in capsys.readouterr().out


def _seed_full_cache(cache_dir):
    """Seed the cache with committed STATS19 + ONS fixtures so a real build runs
    offline. Mirrors tests/test_stats19.py's _seed_cache / _seed_ons_cache."""
    os.makedirs(cache_dir, exist_ok=True)
    # STATS19 CSVs (2023 sample).
    for ftype in ("collision", "vehicle", "casualty"):
        name = f"dft-road-casualty-statistics-{ftype}-2023.csv"
        shutil.copy(os.path.join(STATS19_FIXTURES, name), os.path.join(cache_dir, name))
    # ONS boundary GeoJSON, copied to the filename each newest vintage expects.
    for prefix, cls in (("lad", LADBoundaryTransformer), ("ctyua", CTYUABoundaryTransformer)):
        newest = cls().vintages[-1]
        year = newest.valid_from[:4]
        src = os.path.join(ONS_FIXTURES, f"{prefix}_{year}", f"{prefix}_sample.geojson")
        shutil.copy(src, os.path.join(cache_dir, newest.source_file))


@pytest.mark.integration
def test_wizard_builds_populated_database_offline(tmp_path):
    cache = str(tmp_path / "cache")
    _seed_full_cache(cache)
    db_path = str(tmp_path / "wizard.duckdb")
    # Scripted answers: db path, one year (matches the fixture), snapshot, confirm.
    reader, writer, _ = scripted([db_path, "2023", "snapshot", "y"])
    client = console.run_wizard(reader, writer, cache_dir=cache)  # real init_engine
    try:
        assert client is not None
        assert os.path.exists(db_path)                            # file written
        n = client.con.execute("SELECT count(*) FROM collisions").fetchone()[0]
        assert n > 0                                              # silver populated
        # gold view exists and is queryable
        client.con.execute("SELECT count(*) FROM collisions_spatial").fetchone()
    finally:
        client.close()


def test_console_script_registered():
    # Tolerate a not-yet-reinstalled package: skip rather than fail so a fresh
    # checkout stays green until `pip install -e .` registers the script.
    scripts = md.entry_points(group="console_scripts")
    match = [e for e in scripts if e.name == "crossroads"]
    if not match:
        pytest.skip("run `pip install -e .` to register the console script")
    assert match[0].value == "crossroads.console:main"
