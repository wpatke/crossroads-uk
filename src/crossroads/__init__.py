"""Crossroads-UK: reproducible UK road-safety / weather / boundary data pipeline."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from crossroads.client import Client, init_engine

__all__ = ["Client", "init_engine", "SCHEMA_VERSION"]

# The version is derived from git tags (via hatch-vcs) and frozen into the
# installed package's metadata at install/build time. We read it back from that
# metadata here rather than hardcoding it, so the number can never drift and is
# available at runtime whether or not git is present. If the package is not
# installed (e.g. running straight from a source checkout with no install), fall
# back to a clearly-marked placeholder.
try:
    __version__ = _pkg_version("crossroads-uk")
except PackageNotFoundError:  # not installed; no metadata to read
    __version__ = "0.0.0+unknown"

# Monotonic integer describing the physical shape of the built database (tables, columns,
# views). Increment by 1 on ANY schema change (new column, new table, new datasource,
# renamed/removed field). It is a plain literal here (hand-maintained), independent of the
# git-derived package version. A schema change is also a MINOR (or MAJOR, if breaking)
# release — see CHANGELOG.md.
SCHEMA_VERSION = 2
