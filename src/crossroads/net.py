"""Shared HTTP download helper (spec §2 -- a build must fail fast, never hang).

Every source that fetches a file uses download_to_file so the four downloaders
behave identically:
  * a socket-INACTIVITY timeout -- urlopen(timeout=...) applies to the connect and
    to each blocking read, so the deadline is "no bytes for `timeout` seconds",
    NOT a total-download budget. A large but healthy download keeps resetting the
    clock as chunks arrive; only a genuinely stalled endpoint trips it.
  * chunked streaming -- a 150 MB CSV is never read fully into memory.
  * an atomic temp-then-rename promote -- an interrupted download never leaves a
    half-file that the cache check would trust.
  * an optional validator run BEFORE the promote -- for content checks a status
    code alone would miss (an HTML error page served as 200 OK).
  * a fingerprint (SHA-256) of the bytes, taken WHILE downloading, written with the
    URL and time to a "<dest>.source.json" sidecar (see crossroads.provenance).
"""

import hashlib
import os
import urllib.error
import urllib.request

from crossroads import provenance

# Socket INACTIVITY timeout in seconds (not a wall-clock budget): fires only when
# no bytes arrive for this long. 120s is generous enough for a healthy large
# download yet fails fast on a stalled DfT/ONS endpoint.
HTTP_TIMEOUT_SECONDS = 120

# Download in 1 MB pieces.
_CHUNK = 1024 * 1024


def download_to_file(url, dest, *, timeout=HTTP_TIMEOUT_SECONDS, validator=None):
    """Download `url` to `dest`, streamed and atomic. Returns the file's SHA-256.

    Streams the response in chunks to `dest + '.part'`, fingerprinting the bytes as
    they arrive, optionally validates that finished temp file, then atomically
    renames it onto `dest`. On ANY error the temp file is removed, so a bad or
    interrupted response never poisons the cache. After a successful download, a
    "<dest>.source.json" sidecar records the URL, the fingerprint and the time.

    validator: optional callable(tmp_path) -> None, run BEFORE the atomic promote.
    Raise from it (e.g. ValueError) to reject the download; the temp file is then
    removed and the exception propagates (and no sidecar is written).
    """
    tmp = dest + ".part"
    digest = hashlib.sha256()
    try:
        # urlopen's timeout covers connect AND each socket read; we issue repeated
        # reads, so a stall between chunks raises rather than hanging.
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)        # fingerprint as we go: no second read needed
        if validator is not None:
            validator(tmp)                 # may raise to reject the download
        os.replace(tmp, dest)              # atomic within the same directory
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    # Outside the try: if this fails, the downloaded file is still complete and simply
    # has no sidecar, which later builds treat as "origin unknown".
    provenance.write_sidecar(dest, origin_url=url, origin_sha256=digest.hexdigest())
    return digest.hexdigest()


def url_exists(url, *, timeout=HTTP_TIMEOUT_SECONDS):
    """True if `url` answers 200, False if it answers 404. Any other error is raised.

    Uses a normal GET because some servers (DfT's included) refuse HEAD requests. The
    response is closed straight away without reading the body, so even a large file
    costs almost nothing to check.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
