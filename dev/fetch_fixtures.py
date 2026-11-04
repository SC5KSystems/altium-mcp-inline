"""Download public Altium test files into a cache outside the repo.

The repo is public and must never hold real design data, so the offline tools
are tested against boards other projects already publish: KiCad's Altium
importer test data (GPL project; the files are only downloaded, never
committed or copied into this repo).

    python dev/fetch_fixtures.py            # fetch anything missing
    python dev/fetch_fixtures.py --list     # show the cache and what is in it
    python dev/fetch_fixtures.py --force    # fetch everything again

The cache is $ALTIUM_FIXTURE_CACHE if set, else %LOCALAPPDATA%\\altium-mcp\\
fixtures on Windows and ~/.cache/altium-mcp/fixtures elsewhere. Tests read it
through server/offline/fixtures.py and skip when it is empty.
"""
import argparse
import hashlib
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

KICAD_REF = "bbe344f935a87778920165cb8e019224dbd476f2"
KICAD_PROJECT = "kicad%2Fcode%2Fkicad"
KICAD_DIR = "qa/data/pcbnew/plugins/altium/"

KICAD_FILES = [
    "HiFive/HiFive1.B01.PcbDoc",
    "eDP_adapter_dvt1_source/eDP_adapter_dvt1.PcbDoc",
    "eDP_adapter_dvt1_source/eDP_adapter_dvt1.PrjPcb",
    "eDP_adapter_dvt1_source/it6251_core.SchDoc",
    "eDP_adapter_dvt1_source/power.SchDoc",
    "issue13750/altium2kicad_region_soldermask_expansion.PcbDoc",
    "issue24456/Fastino_Ground_Isolator.PcbDoc",
    "issue24456/Fastino_Ground_Isolator.PrjPcb",
    "issue24654/PCB1.PcbDoc",
    "issue24847/PCB1.PcbDoc",
    "issue25362/OpenRex_V1I1_PCB_reduced.PcbDoc",
    "issue25374/altium_import_netname.CSPcbDoc",
    "issue25374/altium_import_netname.SchDoc",
    "pcblib/Espressif ESP32-WROOM-32.PcbLib",
    "pcblib/GW_JTLMS1_CM.PcbLib",
    "pcblib/Tracks.v5.PcbLib",
    "pcblib/Tracks.v6.PcbLib",
]

CFB_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
BINARY_SUFFIXES = (".pcbdoc", ".pcblib", ".schdoc", ".cspcbdoc", ".schlib")


def cache_dir():
    env = os.environ.get("ALTIUM_FIXTURE_CACHE")
    if env:
        return Path(env)
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "altium-mcp" / "fixtures"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "altium-mcp" / "fixtures"


def kicad_url(rel):
    path = urllib.parse.quote(KICAD_DIR + rel, safe="")
    return (f"https://gitlab.com/api/v4/projects/{KICAD_PROJECT}/repository/files/"
            f"{path}/raw?ref={KICAD_REF}")


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "altium-mcp-fixtures"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    if dest.suffix.lower() in BINARY_SUFFIXES and not data.startswith(CFB_MAGIC):
        raise RuntimeError(f"{dest.name}: not a compound file (got {data[:16]!r})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return len(data), hashlib.sha256(data).hexdigest()[:16]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="download again even if cached")
    ap.add_argument("--list", action="store_true", help="list the cache and exit")
    args = ap.parse_args(argv)

    root = cache_dir()
    if args.list:
        print(root)
        for p in sorted(root.rglob("*")) if root.is_dir() else []:
            if p.is_file():
                print(f"  {p.relative_to(root)}  {p.stat().st_size} bytes")
        return 0

    failures = 0
    for rel in KICAD_FILES:
        dest = root / "kicad" / rel
        if dest.is_file() and not args.force:
            print(f"cached   {rel}")
            continue
        try:
            size, digest = fetch(kicad_url(rel), dest)
            print(f"fetched  {rel}  {size} bytes  sha256 {digest}")
        except Exception as e:  # report every failure, keep going
            failures += 1
            print(f"FAILED   {rel}: {e}", file=sys.stderr)
    print(f"cache: {root}")
    if failures:
        print(f"{failures} file(s) failed to download", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
