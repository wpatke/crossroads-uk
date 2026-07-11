"""Fail the build if a committed doc points at something that does not exist.

Scans first-party markdown (README, docs/, but NOT docs/plans/** or vendored trees) and checks:
  (a) relative file links resolve to a real file;
  (b) `file.md#anchor` fragments match a GitHub-style heading slug in that file;
  (c) `§N` spec-section citations name a real section number in docs/spec.md.
External (http/https/mailto) links and bare #anchors are not file-checked. Offline, deterministic.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
SPEC = os.path.join(REPO_ROOT, "docs", "spec.md")

# Never scan: implementation plans (may reference future files), venv, coverage, VCS internals.
SKIP_DIRS = {".venv", ".git", "htmlcov", "node_modules", "__pycache__", ".pytest_cache"}
SKIP_PATH_PARTS = (os.path.join("docs", "plans"),)

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.M)
# Section citations like "§9", "§3A", "§5 Phase 4" -> capture the leading number (+ optional letter).
SECTION_CITE_RE = re.compile(r"§\s*(\d+)([A-Z]?)")


def _iter_markdown_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if any(part in dirpath for part in SKIP_PATH_PARTS):
            continue
        for name in filenames:
            if name.endswith(".md"):
                yield os.path.join(dirpath, name)


def _slug(heading):
    """GitHub-style anchor slug: lower-case, drop punctuation, spaces -> hyphens."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s+", "-", s)


def _anchors_of(path):
    if not path.endswith(".md") or not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {_slug(h) for h in HEADING_RE.findall(fh.read())}


def _spec_section_numbers():
    """Section numbers present in spec.md, e.g. {'1','2','3','3A','3B','5','9'}.
    Spec headings look like '## 3. Ingestion' and '### A. Spatial ...' nested under a number."""
    numbers, current = set(), None
    with open(SPEC, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^##\s+(\d+)\.", line)      # top-level "## N. Title"
            if m:
                current = m.group(1); numbers.add(current); continue
            m = re.match(r"^###\s+([A-Z])\.", line)   # sub "### A. Title" -> "NA"
            if m and current:
                numbers.add(current + m.group(1))
    return numbers


def test_relative_markdown_links_resolve():
    broken = []
    for md_path in _iter_markdown_files():
        with open(md_path, encoding="utf-8") as fh:
            text = fh.read()
        for target in LINK_RE.findall(text):
            target = target.strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            raw = target.strip("<>").strip('"').strip("'")
            local, _, frag = raw.partition("#")
            local = local.strip()
            if not local:
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(md_path), local))
            rel = os.path.relpath(md_path, REPO_ROOT)
            if not os.path.exists(resolved):
                broken.append(f"{rel} -> {target} (missing file)")
            elif frag and _slug(frag) not in _anchors_of(resolved):
                broken.append(f"{rel} -> {target} (no such anchor #{frag})")
    assert not broken, "Broken markdown references:\n" + "\n".join(broken)


def test_spec_section_citations_exist():
    valid = _spec_section_numbers()
    assert valid, "could not parse any section numbers from docs/spec.md"
    bad = []
    for md_path in _iter_markdown_files():
        if os.path.abspath(md_path) == os.path.abspath(SPEC):
            continue  # the spec citing itself internally is fine to skip
        with open(md_path, encoding="utf-8") as fh:
            text = fh.read()
        for num, letter in SECTION_CITE_RE.findall(text):
            cite = num + letter
            if cite not in valid and num not in valid:
                bad.append(f"{os.path.relpath(md_path, REPO_ROOT)} cites spec §{cite} (no such section)")
    assert not bad, "Stale spec-section citations:\n" + "\n".join(bad)
