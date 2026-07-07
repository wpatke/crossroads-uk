"""Tests for the data-compilation wizard prompts (console.py).

Fast, offline, deterministic. A scripted-I/O harness replaces stdin/stdout so no
real input/print or network is involved: `reader` replays a list of answers and
`writer` collects the emitted lines.
"""
from crossroads import console


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
