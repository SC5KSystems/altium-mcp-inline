"""Where tests find real Altium files: the dev/fetch_fixtures.py cache and
$ALTIUM_TEST_PCBDOC (one path, or several separated by os.pathsep). Nothing
here downloads; tests skip when this finds nothing."""

import os
from pathlib import Path


def cache_dir():
    env = os.environ.get("ALTIUM_FIXTURE_CACHE")
    if env:
        return Path(env)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "altium-mcp" / "fixtures"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "altium-mcp" / "fixtures"


def find(suffix=".PcbDoc"):
    out = []
    env = os.environ.get("ALTIUM_TEST_PCBDOC", "")
    for part in env.split(os.pathsep):
        if part.strip() and Path(part).is_file() and part.lower().endswith(suffix.lower()):
            out.append(Path(part))
    root = cache_dir()
    if root.is_dir():
        out += sorted(p for p in root.rglob("*") if p.is_file() and p.name.lower().endswith(suffix.lower()))
    seen, uniq = set(), []
    for p in out:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def boards():
    """PcbDoc fixtures that carry a board (the reduced OpenRex file has no primitives)."""
    return [p for p in find(".PcbDoc") if "reduced" not in p.name.lower()]


def by_name(fragment, suffix=".PcbDoc"):
    for p in find(suffix):
        if fragment.lower() in str(p).lower():
            return p
    return None
