"""Tests for the data-compilation wizard prompts (console.py).

Fast, offline, deterministic. A scripted-I/O harness replaces stdin/stdout so no
real input/print or network is involved: `reader` replays a list of answers and
`writer` collects the emitted lines.
"""
import importlib.metadata as md
import os
import shutil

import pytest

import crossroads
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


def _isolate_cds(monkeypatch, tmp_path):
    """Point ~/.cdsapirc at an empty tmp home and clear CDSAPI_* env vars.

    os.path.expanduser("~") honors $HOME on Linux/macOS (the dev + CI platforms),
    so this keeps the credential tests hermetic — they never touch the real file.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CDSAPI_URL", raising=False)
    monkeypatch.delenv("CDSAPI_KEY", raising=False)


# A fixed one-dataset menu so the full-flow tests don't depend on live discovery.
MENU = [("stats19", "stats19")]


def test_gather_parameters_happy_path():
    # Answers: db path, datasets (menu index), years, boundary mode.
    reader, writer, _ = scripted(["mydb.duckdb", "1", "2021 2022", "temporal"])
    params = console.gather_parameters(reader, writer, available=MENU)
    assert params == {
        "database_path": "mydb.duckdb",
        "datasets": ["stats19"],
        "years": [2021, 2022],
        "boundary_mode": "temporal",
    }


def test_defaults_via_empty_lines():
    # Blank lines fall back to defaults, except datasets/years (no default) which are answered.
    reader, writer, _ = scripted(["", "1", "2023", ""])
    params = console.gather_parameters(reader, writer, available=MENU)
    assert params["database_path"] == "crossroads.db"
    assert params["datasets"] == ["stats19"]
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


# --- Dataset selection prompt ------------------------------------------------


def test_prompt_datasets_single_selection():
    reader, writer, output = scripted(["1"])
    result = console.prompt_datasets(reader, writer, [("stats19", "stats19")])
    assert result == ["stats19"]
    assert any("1. stats19" in line for line in output)   # menu was shown


def test_prompt_datasets_uses_display_name():
    reader, writer, output = scripted(["1"])
    console.prompt_datasets(reader, writer, [("era5_weather", "weather")])
    assert any("1. weather" in line for line in output)   # friendly label, not source_id


def test_prompt_datasets_range_and_list_dedup_sort():
    menu = [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]
    reader, writer, _ = scripted(["1-3, 4"])
    assert console.prompt_datasets(reader, writer, menu) == ["a", "b", "c", "d"]
    # Overlapping selections collapse.
    reader, writer, _ = scripted(["2-3 3-4"])
    assert console.prompt_datasets(reader, writer, menu) == ["b", "c", "d"]


def test_prompt_datasets_rejects_out_of_range_then_accepts():
    reader, writer, output = scripted(["9", "1"])   # 9 > count(1), re-ask, then valid
    result = console.prompt_datasets(reader, writer, [("stats19", "stats19")])
    assert result == ["stats19"]
    assert sum("Invalid input" in line for line in output) == 1


def test_prompt_datasets_rejects_backwards_range():
    menu = [("a", "A"), ("b", "B"), ("c", "C")]
    reader, writer, output = scripted(["3-1", "1-2"])
    assert console.prompt_datasets(reader, writer, menu) == ["a", "b"]
    assert any("backwards" in line for line in output)


def test_prompt_datasets_requires_at_least_one():
    reader, writer, output = scripted(["", "1"])   # empty rejected, then valid
    result = console.prompt_datasets(reader, writer, [("stats19", "stats19")])
    assert result == ["stats19"]
    assert sum("Invalid input" in line for line in output) == 1


def test_format_summary_includes_datasets():
    summary = console.format_summary({
        "database_path": "x.db", "datasets": ["stats19"],
        "years": [2023], "boundary_mode": "snapshot",
    })
    assert "Datasets" in summary and "stats19" in summary


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
    # Answers: db path, datasets (menu index), years, boundary mode, confirm.
    reader, writer, _ = scripted(["mydb.duckdb", "1", "2022 2023", "temporal", "y"])
    captured = {}
    def factory(**kwargs):
        client = _FakeClient(**kwargs)
        captured["client"] = client
        return client
    result = console.run_wizard(reader, writer, engine_factory=factory, available=MENU)
    assert result is captured["client"]
    assert captured["client"].init_kwargs == {"database_path": "mydb.duckdb"}
    assert captured["client"].build_kwargs == {"datasets": ["stats19"],
                                               "years": [2022, 2023],
                                               "boundary_mode": "temporal"}


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


def test_decline_does_not_build():
    reader, writer, output = scripted([":memory:", "1", "2023", "snapshot", "n"])
    calls = []
    def factory(**kwargs):
        calls.append(kwargs); return _FakeClient(**kwargs)
    result = console.run_wizard(reader, writer, engine_factory=factory, available=MENU)
    assert result is None
    assert calls == []            # build path never entered
    assert any("Aborted" in line for line in output)


def test_main_abort_path_returns_zero(monkeypatch, capsys):
    # Exercise the real entry point offline. Declining means no build, no network.
    # main() uses live discovery, so "1" selects the first discovered dataset — which
    # may be the weather source. Make the credential path deterministic so the test
    # exercises the abort path regardless of discovery order or the CI machine's state:
    #   - pretend a CDS key is configured so ensure_weather_credentials never prompts;
    #   - hard-stub getpass so no test can ever read real stdin (which fails under
    #     pytest's output capture with "reading from stdin while output is captured").
    monkeypatch.setenv("CDSAPI_URL", "https://example.invalid/api")
    monkeypatch.setenv("CDSAPI_KEY", "test-key")
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "")
    answers = iter([":memory:", "1", "2023", "snapshot", "n"])
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
    # Scripted answers: db path, datasets (live menu — selectable() is source_id order,
    # so "1"=era5_weather, "2"=stats19; this build seeds only stats19+ONS, so pick "2"),
    # one year (matches the fixture), snapshot, confirm.
    reader, writer, _ = scripted([db_path, "2", "2023", "snapshot", "y"])
    client = console.run_wizard(reader, writer, cache_dir=cache)  # real init_engine
    try:
        assert client is not None
        assert os.path.exists(db_path)                            # file written
        n = client.con.execute("SELECT count(*) FROM collisions").fetchone()[0]
        assert n > 0                                              # silver populated
        # gold view exists and is queryable
        client.con.execute("SELECT count(*) FROM collisions_spatial").fetchone()
        # crossroads_meta stamped by the real build
        row = client.con.execute(
            "SELECT crossroads_version, schema_version FROM crossroads_meta"
        ).fetchone()
        assert row == (crossroads.__version__, crossroads.SCHEMA_VERSION)
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


@pytest.mark.integration
def test_wizard_builds_weather_offline(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    # A configured key bypasses the new credential prompt so this build test stays
    # focused on the offline weather build (the prompt is covered by its own tests).
    monkeypatch.setenv("CDSAPI_URL", "https://example.invalid/api")
    monkeypatch.setenv("CDSAPI_KEY", "test-key")
    cache = str(tmp_path / "cache"); _seed_full_cache(cache)
    shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "weather", "era5_land_sample.nc"),
                os.path.join(cache, "era5_land_2023.nc"))
    db_path = str(tmp_path / "wiz.duckdb")
    # Menu order is source_id: 1=weather (era5_weather), 2=stats19. Pick both with "1-2".
    reader, writer, _ = scripted([db_path, "1-2", "2023", "snapshot", "y"])
    client = console.run_wizard(reader, writer, cache_dir=cache)
    try:
        assert client is not None and os.path.exists(db_path)
        assert client.con.execute("SELECT count(*) FROM weather").fetchone()[0] > 0
        assert client.con.execute(
            "SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL").fetchone()[0] >= 1
    finally:
        client.close()


# --- CDS API key prompt (weather credentials) -------------------------------


def test_weather_key_prompt_saves_file(tmp_path, monkeypatch):
    # First run, no key configured: prompting for a token writes ~/.cdsapirc and proceeds.
    _isolate_cds(monkeypatch, tmp_path)
    reader, writer, output = scripted([])                 # y/n reader unused here
    secret = scripted_secret(["MY-TOKEN-123"])
    params = {"datasets": ["era5_weather", "stats19"], "years": [2023]}
    assert console.ensure_weather_credentials(params, reader, secret, writer) is True
    rc = tmp_path / ".cdsapirc"
    assert rc.read_text() == "url: https://cds.climate.copernicus.eu/api\nkey: MY-TOKEN-123\n"
    assert params["datasets"] == ["era5_weather", "stats19"]     # unchanged
    assert any("Saved ~/.cdsapirc." in line for line in output)


def test_weather_key_skipped_when_file_present(tmp_path, monkeypatch):
    # An existing ~/.cdsapirc means no prompt, and the file is left untouched.
    _isolate_cds(monkeypatch, tmp_path)
    rc = tmp_path / ".cdsapirc"
    rc.write_text("url: x\nkey: y\n")
    before = rc.read_text()
    reader, writer, _ = scripted([])
    params = {"datasets": ["era5_weather"], "years": [2023]}
    assert console.ensure_weather_credentials(params, reader, _boom_secret(), writer) is True
    assert rc.read_text() == before                       # not rewritten


def test_weather_key_skipped_when_env_present(tmp_path, monkeypatch):
    # CDSAPI_* env vars count as configured: no prompt, no file written.
    _isolate_cds(monkeypatch, tmp_path)
    monkeypatch.setenv("CDSAPI_URL", "https://example.invalid/api")
    monkeypatch.setenv("CDSAPI_KEY", "abc")
    reader, writer, _ = scripted([])
    params = {"datasets": ["era5_weather"], "years": [2023]}
    assert console.ensure_weather_credentials(params, reader, _boom_secret(), writer) is True
    assert not (tmp_path / ".cdsapirc").exists()


def test_no_prompt_when_weather_not_selected(tmp_path, monkeypatch):
    # No weather in the build: the credential gate is off entirely.
    _isolate_cds(monkeypatch, tmp_path)
    reader, writer, _ = scripted([])
    params = {"datasets": ["stats19"], "years": [2023]}
    assert console.ensure_weather_credentials(params, reader, _boom_secret(), writer) is True
    assert not (tmp_path / ".cdsapirc").exists()


def test_blank_token_continue_drops_weather(tmp_path, monkeypatch):
    # Blank token + "continue without weather? y" drops weather but builds the rest.
    _isolate_cds(monkeypatch, tmp_path)
    reader, writer, output = scripted(["y"])              # "Continue without weather?" -> yes
    secret = scripted_secret([""])                        # blank token
    params = {"datasets": ["era5_weather", "stats19"], "years": [2023]}
    assert console.ensure_weather_credentials(params, reader, secret, writer) is True
    assert params["datasets"] == ["stats19"]              # weather dropped
    assert not (tmp_path / ".cdsapirc").exists()


def test_blank_token_weather_only_aborts(tmp_path, monkeypatch):
    # Blank token + "continue? y" with weather as the only dataset aborts cleanly.
    _isolate_cds(monkeypatch, tmp_path)
    reader, writer, output = scripted(["y"])
    secret = scripted_secret([""])
    params = {"datasets": ["era5_weather"], "years": [2023]}
    assert console.ensure_weather_credentials(params, reader, secret, writer) is False
    assert any("Nothing to build" in line for line in output)


def test_blank_token_then_reenter_saves(tmp_path, monkeypatch):
    # Blank token, decline continuing, then enter a real token -> saved and proceeds.
    _isolate_cds(monkeypatch, tmp_path)
    reader, writer, _ = scripted(["n"])                   # decline "continue without weather"
    secret = scripted_secret(["", "REAL-TOKEN"])          # blank, then a real token
    params = {"datasets": ["era5_weather"], "years": [2023]}
    assert console.ensure_weather_credentials(params, reader, secret, writer) is True
    assert (tmp_path / ".cdsapirc").read_text().endswith("key: REAL-TOKEN\n")
    assert params["datasets"] == ["era5_weather"]


def test_weather_key_write_failure_is_friendly(tmp_path, monkeypatch):
    # A failed save gives a friendly message and a clean abort, not a traceback.
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


@pytest.mark.integration
def test_run_wizard_prompts_and_builds_weather_offline(tmp_path, monkeypatch):
    # End-to-end: no key configured, the wizard prompts (scripted secret), saves the
    # file, and the real offline build populates weather + stamps collisions.
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
        # Same post-build checks the existing weather test uses:
        assert client.con.execute("SELECT count(*) FROM weather").fetchone()[0] > 0
        assert client.con.execute(
            "SELECT count(*) FROM collisions WHERE temperature_c IS NOT NULL"
        ).fetchone()[0] >= 1
    finally:
        client.close()
