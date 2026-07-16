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
