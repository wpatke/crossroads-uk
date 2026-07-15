"""Release-invariant tests: the version is single-sourced and correct."""
import importlib.metadata
import os

import crossroads


def test_version_single_sourced():
    # The runtime constant and the installed package metadata must agree, proving
    # crossroads.__version__ really reads back the hatch-vcs-derived metadata (no
    # drift). We do NOT pin a literal like "1.0.0": the version is derived from git
    # and legitimately varies with tag distance ("1.1.0", "1.1.1.dev3+g1a2b3c4", or the
    # fallback before the first tag). What must always hold is that the two agree
    # and the value is a real, non-empty version string.
    runtime = crossroads.__version__
    packaged = importlib.metadata.version("crossroads-uk")
    assert runtime == packaged
    assert isinstance(runtime, str) and runtime != ""


def test_cli_version_flag(capsys):
    # A researcher must be able to record the exact version they ran. The flag must
    # print the same string as crossroads.__version__ and exit 0 without prompting.
    from crossroads import console
    rc = console.main(["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert crossroads.__version__ in out


def test_data_sources_doc_lists_every_source():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "docs", "data-sources.md"), encoding="utf-8") as fh:
        text = fh.read()
    for token in ("Open Government Licence", "STATS19", "ONS", "Copernicus", "ERA5-Land", "AADF"):
        assert token in text, f"docs/data-sources.md is missing {token!r}"


def test_citation_cff_is_valid():
    """Prefer cffconvert's validator; fall back to a YAML parse + required-keys check
    so the suite never hard-depends on an optional tool."""
    root = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(root, "CITATION.cff")
    assert os.path.exists(path)
    try:
        from cffconvert.cli.create_citation import create_citation
        from cffconvert.cli.validate_or_write_output import validate_or_write_output
        citation = create_citation(path, None)
        validate_or_write_output(None, "bibtex", False, citation)  # raises on invalid
    except ImportError:
        import pytest
        yaml = pytest.importorskip("yaml")  # PyYAML is a common transitive dep
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        for key in ("cff-version", "message", "title", "authors", "version"):
            assert key in data, f"CITATION.cff missing {key!r}"
        assert data["version"] == "1.0.0"
