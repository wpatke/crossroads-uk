"""Shared HTTP download helper (spec §2 -- a build must fail fast, never hang).

Every source that fetches a file uses download_to_file so the four downloaders
behave identically:
  * a socket-INACTIVITY timeout -- urlopen(timeout=...) applies to the connect and
    to each blocking read, so the deadline is "no bytes for `timeout` seconds",
    NOT a total-download budget. A large but healthy download keeps resetting the
    clock as chunks arrive; only a genuinely stalled endpoint trips it.
  * chunked streaming (shutil.copyfileobj) -- a 150 MB CSV is never read fully
    into memory.
  * an atomic temp-then-rename promote -- an interrupted download never leaves a
    half-file that the cache check would trust.
  * an optional validator run BEFORE the promote -- for content checks a status
    code alone would miss (an HTML error page served as 200 OK).
"""

import os
import shutil
import urllib.request

# Socket INACTIVITY timeout in seconds (not a wall-clock budget): fires only when
# no bytes arrive for this long. 120s is generous enough for a healthy large
# download yet fails fast on a stalled DfT/ONS endpoint.
HTTP_TIMEOUT_SECONDS = 120


def download_to_file(url, dest, *, timeout=HTTP_TIMEOUT_SECONDS, validator=None):
    """Download `url` to `dest`, streamed and atomic.

    Streams the response in chunks to `dest + '.part'`, optionally validates that
    finished temp file, then atomically renames it onto `dest`. On ANY error the
    temp file is removed, so a bad or interrupted response never poisons the cache.

    validator: optional callable(tmp_path) -> None, run BEFORE the atomic promote.
    Raise from it (e.g. ValueError) to reject the download; the temp file is then
    removed and the exception propagates.
    """
    tmp = dest + ".part"
    try:
        # urlopen's timeout covers connect AND each socket read; copyfileobj issues
        # repeated reads, so a stall between chunks raises rather than hanging.
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        if validator is not None:
            validator(tmp)                 # may raise to reject the download
        os.replace(tmp, dest)              # atomic within the same directory
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
