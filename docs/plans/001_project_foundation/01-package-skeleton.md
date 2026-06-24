# Stage 01 — Package skeleton & install

## Objective

Create the installable `crossroads` package skeleton (src-layout) with a Hatchling `pyproject.toml`, a minimal package, a README stub, and updated `.gitignore`; install it editable into a fresh virtual environment and prove `import crossroads` works and `pytest` is green.

## Implementation Steps

### 1. Create `pyproject.toml` (repo root)

Exact contents:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "crossroads-uk"
version = "0.0.1"
description = "A reproducible pipeline for unifying UK road-safety, weather, and boundary data into a single local DuckDB database."
readme = "README.md"
requires-python = ">=3.11"
license = { file = "LICENSE" }
authors = [{ name = "wpatke" }]
keywords = ["uk", "road-safety", "stats19", "duckdb", "etl", "reproducible-research"]
classifiers = [
  "Development Status :: 2 - Pre-Alpha",
  "Intended Audience :: Science/Research",
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
  "Operating System :: OS Independent",
  "Topic :: Scientific/Engineering",
]
dependencies = [
  "duckdb>=1.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=7.0",
]

[tool.hatch.build.targets.wheel]
packages = ["src/crossroads"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Notes:
- `license = { file = "LICENSE" }` is used (not an SPDX `license` string and not a `License ::` classifier) to avoid PEP 639 conflicts across Hatchling versions. Do not also add a `License :: OSI Approved` classifier.
- `name` (the distribution) is `crossroads-uk`; the **import** name is `crossroads` (set by the `packages` path below). These intentionally differ.

### 2. Create the package directory and `src/crossroads/__init__.py`

```bash
mkdir -p src/crossroads
```

Create `src/crossroads/__init__.py` with **exactly**:

```python
"""Crossroads-UK: reproducible UK road-safety / weather / boundary data pipeline."""

__version__ = "0.0.1"
```

> Do not import `client`/`registry` here yet — those modules are created in later stages. Stage 03 updates this file to also export `init_engine` and `Client`.

### 3. Create the README stub (repo root `README.md`)

A minimal stub so the Hatchling build (`readme = "README.md"`) succeeds. Exact contents:

```markdown
# Crossroads-UK

A reproducible Python pipeline that downloads, cleanses, and unifies UK road-safety
(DfT Stats19), meteorological (ERA5-Land), and ONS boundary data into a single local
DuckDB database — built on the fly from version-controlled code.

See [`docs/spec.md`](docs/spec.md) for the full product definition.

> **Status:** early foundation. Installation and usage documentation will be expanded
> as the pipeline's data sources come online.

## Development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```
```

### 4. Update `.gitignore` (repo root)

The file currently contains:
```
/CLAUDE.md
.claude/
```
**Append** the following Python / project entries (keep the existing two lines):

```
# Python
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
build/
dist/

# Virtual environments
.venv/
venv/

# Crossroads build artifacts
*.db
*.duckdb
.crossroads_cache/
```

### 5. Create the test directory and a smoke test

```bash
mkdir -p tests
```

Create `tests/test_package.py` with **exactly**:

```python
import crossroads


def test_package_imports():
    assert crossroads is not None


def test_version_is_a_string():
    assert isinstance(crossroads.__version__, str)
    assert crossroads.__version__ != ""
```

### 6. Create the virtual environment and install editable

From the repo root:

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Expected: a successful build via Hatchling, `duckdb` and `pytest` installed, and `crossroads-uk` shown as an editable install. If the build complains that `README.md` is missing, you skipped step 3 — create it.

## Testing & Verification

**Integration test (PRIMARY) — the package installs and imports for real.** With the venv active, from the repo root:

```bash
python -c "import crossroads; print(crossroads.__version__)"
```
Expected output: `0.0.1`.

**Automated test suite:**
```bash
python -m pytest -q
```
Expected: `2 passed`.

**Editable-install sanity:**
```bash
pip show crossroads-uk | grep -E "Name|Version|Location"
```
Expected: `Name: crossroads-uk`, `Version: 0.0.1`, and a `Location` pointing at this repo's `src` (editable).

## Known Pitfalls

- **Hatchling build error: "metadata file 'README.md' does not exist".** Step 3 was skipped — create the README stub.
- **`pip install -e .` fails on `requires-python`.** The active interpreter is older than 3.11 — you used the system `python3` instead of `/opt/homebrew/bin/python3.12` when creating the venv. Recreate the venv with 3.12.
- **`import crossroads` fails after install (ModuleNotFoundError).** The `packages = ["src/crossroads"]` line is missing/incorrect in `pyproject.toml`, or `src/crossroads/__init__.py` was not created. Verify both.
- **`pytest` collects 0 tests.** `tests/test_package.py` is missing or `testpaths` is wrong; confirm the file exists and `[tool.pytest.ini_options] testpaths = ["tests"]` is present.
