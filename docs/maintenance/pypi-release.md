# PyPI release — one-time Trusted Publishing setup

Trusted Publishing lets GitHub Actions upload to PyPI with no stored token, by trusting a
specific repo + workflow file + environment. Do this once per index (PyPI and TestPyPI).

## GitHub: create two environments
Repo → Settings → Environments → New environment. Create:
- `pypi`
- `testpypi`
(No secrets needed. Optionally add yourself as a required reviewer on `pypi` for a manual
approval gate before the real upload.)

## PyPI (https://pypi.org) — add a pending publisher
1. Create/log in to a PyPI account (maintainer task — cannot be automated).
2. Account → Publishing → "Add a new pending publisher" (GitHub Actions):
   - PyPI Project Name: `crossroads-uk`
   - Owner: `wpatke`
   - Repository name: `crossroads-uk`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
   "Pending" means the project does not exist yet; the first successful upload creates it and
   binds the trust.

## TestPyPI (https://test.pypi.org) — add a pending publisher
TestPyPI is a completely separate site with its own account. Repeat the same steps there:
   - PyPI Project Name: `crossroads-uk`
   - Owner: `wpatke`
   - Repository name: `crossroads-uk`
   - Workflow name: `publish.yml`
   - Environment name: `testpypi`

The workflow file name (`publish.yml`) and environment names (`pypi` / `testpypi`) MUST match
the workflow exactly, or the OIDC exchange is rejected.
