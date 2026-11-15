"""Tests for the shared download helper (net.py).

Reviewer point 4: a downloader must fail fast on a stalled endpoint, never hang.
All offline -- urlopen is monkeypatched, so no network access occurs.
"""
import io
import os
import socket

import pytest

from crossroads import net


def test_download_to_file_streams_to_dest(tmp_path, monkeypatch):
    """A healthy response is streamed to dest and promoted atomically (no .part left)."""
    monkeypatch.setattr(net.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(b"hello world"))
    dest = str(tmp_path / "out.txt")
    net.download_to_file("http://example/x", dest)
    assert open(dest, "rb").read() == b"hello world"
    assert not os.path.exists(dest + ".part")


def test_download_to_file_times_out_and_cleans_up(tmp_path, monkeypatch):
    """A stalled socket (no bytes) raises and leaves no file -- the 'never hang' guard."""
    class _StallingResponse(io.RawIOBase):
        def read(self, *a):
            raise socket.timeout("timed out")     # simulate a socket that goes silent
        def readable(self):
            return True
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(net.urllib.request, "urlopen",
                        lambda url, timeout=None: _StallingResponse())
    dest = str(tmp_path / "out.bin")
    with pytest.raises(socket.timeout):
        net.download_to_file("http://example/x", dest)
    assert not os.path.exists(dest)               # nothing promoted
    assert not os.path.exists(dest + ".part")     # temp cleaned up


def test_download_to_file_validator_rejects_and_cleans_up(tmp_path, monkeypatch):
    """A validator that raises rejects the download: temp removed, nothing promoted."""
    monkeypatch.setattr(net.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(b"nope"))

    def _reject(tmp):
        raise ValueError("bad content")

    dest = str(tmp_path / "y.txt")
    with pytest.raises(ValueError, match="bad content"):
        net.download_to_file("http://example/x", dest, validator=_reject)
    assert not os.path.exists(dest)
    assert not os.path.exists(dest + ".part")


def test_download_to_file_validator_passes_and_promotes(tmp_path, monkeypatch):
    """A validator that returns cleanly lets the file promote to dest."""
    monkeypatch.setattr(net.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(b"good data"))
    seen = {}

    def _ok(tmp):
        seen["path"] = tmp                       # validator runs on the temp, before promote
        assert tmp.endswith(".part")

    dest = str(tmp_path / "z.txt")
    net.download_to_file("http://example/x", dest, validator=_ok)
    assert open(dest, "rb").read() == b"good data"
    assert seen["path"] == dest + ".part"


# --- url_exists: a cheap "is this file published?" check --------------------

class _Response(io.BytesIO):
    """A fake response whose body must never be read by url_exists."""
    def read(self, *a):
        raise AssertionError("url_exists must not read the response body")


def _raise_http(code):
    def fake(url, timeout=None):
        raise net.urllib.error.HTTPError(url, code, "error", {}, None)
    return fake


def test_url_exists_true_on_200_without_reading_body(monkeypatch):
    monkeypatch.setattr(net.urllib.request, "urlopen", lambda url, timeout=None: _Response())
    assert net.url_exists("http://example/x") is True


def test_url_exists_false_on_404(monkeypatch):
    monkeypatch.setattr(net.urllib.request, "urlopen", _raise_http(404))
    assert net.url_exists("http://example/x") is False


def test_url_exists_raises_other_errors(monkeypatch):
    monkeypatch.setattr(net.urllib.request, "urlopen", _raise_http(500))
    with pytest.raises(net.urllib.error.HTTPError):
        net.url_exists("http://example/x")
