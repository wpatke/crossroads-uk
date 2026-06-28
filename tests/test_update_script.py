"""Tests for scripts/update_ons_boundaries.py.

All tests here are offline (no network) except the one marked `integration`,
which is deselected by default and run deliberately with:  pytest -m integration
"""

import importlib.util
import os

import pytest

# Load the maintenance script as a module without installing it.
_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "update_ons_boundaries.py",
)
_spec = importlib.util.spec_from_file_location("update_ons_boundaries", _SCRIPT)
uob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uob)


# ---------------------------------------------------------------------------
# Pure-logic unit tests (no network)
# ---------------------------------------------------------------------------

def test_parse_title_december():
    assert uob.parse_title(
        "Local Authority Districts (December 2025) Boundaries UK BGC"
    ) == ("2025-12", "2025-12-01")


def test_parse_title_may():
    assert uob.parse_title(
        "Counties and Unitary Authorities (May 2023) Boundaries UK BGC"
    ) == ("2023-05", "2023-05-01")


def test_parse_title_april():
    assert uob.parse_title(
        "Local Authority Districts (April 2019) Boundaries UK BGC"
    ) == ("2019-04", "2019-04-01")


def test_parse_title_unparseable():
    assert uob.parse_title("Some Lookup Table 2024") == (None, None)


def test_manifest_path_points_at_package():
    assert uob.MANIFEST_PATH.endswith(
        os.path.join("crossroads", "transformers", "ons_boundaries.json")
    )


def test_manifest_loads():
    manifest = uob._load_manifest()
    assert "ons_lad" in manifest and "ons_ctyua" in manifest
    assert len(manifest["ons_lad"]) == 15
    assert len(manifest["ons_ctyua"]) == 11


# ---------------------------------------------------------------------------
# Opt-in integration test (deselected by default; reaches the live portal)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_validate_against_live_portal():
    manifest = uob._load_manifest()
    # Validate just the newest LAD vintage to keep the call light.
    newest = {"ons_lad": [manifest["ons_lad"][-1]]}
    assert uob.validate(newest) is True
