"""Download public Altium test files into a cache outside the repo.

The repo is public and must never hold real design data, so the offline tools
are tested against boards other projects already publish: KiCad's Altium
importer test data (GPL project) and AltiumSharp's test data (MIT project):
six boards (two of them panels), eight schematics and 89 footprint
libraries, 77 of them generated to hold one kind of primitive each. The
files are only downloaded, never committed or copied into this repo.

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

ALTIUMSHARP_REF = "ce72437f30cd54f549601d4e0ca5846d21272150"
ALTIUMSHARP_DIR = "TestData/"

ALTIUMSHARP_FILES = [
    'DAC.SchDoc',
    'Fanout Buffer.SchDoc',
    'Generated/Individual/PCB/ARCS_TEST.PcbLib',
    'Generated/Individual/PCB/ARC_ANGLES.PcbLib',
    'Generated/Individual/PCB/ARC_FULL_CIRCLES.PcbLib',
    'Generated/Individual/PCB/ARC_WIDTHS.PcbLib',
    'Generated/Individual/PCB/BODY_3D.PcbLib',
    'Generated/Individual/PCB/BODY_3D_EXTENDED.PcbLib',
    'Generated/Individual/PCB/BODY_3D_STEP.PcbLib',
    'Generated/Individual/PCB/BODY_3D_STEP_CHECKSUM0.PcbLib',
    'Generated/Individual/PCB/COMPLEX_EXTENDED.PcbLib',
    'Generated/Individual/PCB/COORDINATE.PcbLib',
    'Generated/Individual/PCB/DIMENSION_ANGULAR.PcbLib',
    'Generated/Individual/PCB/DIMENSION_BASELINE.PcbLib',
    'Generated/Individual/PCB/DIMENSION_CENTER.PcbLib',
    'Generated/Individual/PCB/DIMENSION_DATUM.PcbLib',
    'Generated/Individual/PCB/DIMENSION_EXTENDED.PcbLib',
    'Generated/Individual/PCB/DIMENSION_LINEAR.PcbLib',
    'Generated/Individual/PCB/DIMENSION_RADIAL.PcbLib',
    'Generated/Individual/PCB/FILLS_TEST.PcbLib',
    'Generated/Individual/PCB/KEEPOUT_REGION.PcbLib',
    'Generated/Individual/PCB/MULTIPIN_BGA.PcbLib',
    'Generated/Individual/PCB/MULTIPIN_QFP.PcbLib',
    'Generated/Individual/PCB/MULTIPIN_SOIC.PcbLib',
    'Generated/Individual/PCB/MULTIPIN_SOP.PcbLib',
    'Generated/Individual/PCB/PAD_BACK_DRILL.PcbLib',
    'Generated/Individual/PCB/PAD_COUNTER_HOLE.PcbLib',
    'Generated/Individual/PCB/PAD_CUSTOM_SHAPE.PcbLib',
    'Generated/Individual/PCB/PAD_EXTENDED.PcbLib',
    'Generated/Individual/PCB/PAD_HOLE_TYPES.PcbLib',
    'Generated/Individual/PCB/PAD_ROTATED.PcbLib',
    'Generated/Individual/PCB/PAD_SMD_OBLONG.PcbLib',
    'Generated/Individual/PCB/PAD_SMD_RECTANGULAR.PcbLib',
    'Generated/Individual/PCB/PAD_SMD_ROUND.PcbLib',
    'Generated/Individual/PCB/PAD_SMD_ROUNDED.PcbLib',
    'Generated/Individual/PCB/PAD_STACKED_MODES.PcbLib',
    'Generated/Individual/PCB/PAD_THERMAL_RELIEF.PcbLib',
    'Generated/Individual/PCB/PAD_TH_OBLONG.PcbLib',
    'Generated/Individual/PCB/PAD_TH_OCTAGONAL.PcbLib',
    'Generated/Individual/PCB/PAD_TH_RECTANGULAR.PcbLib',
    'Generated/Individual/PCB/PAD_TH_ROUND.PcbLib',
    'Generated/Individual/PCB/PAD_TH_SLOT.PcbLib',
    'Generated/Individual/PCB/POLYGON_DIRECT_CONNECT.PcbLib',
    'Generated/Individual/PCB/POLYGON_EXTENDED.PcbLib',
    'Generated/Individual/PCB/POLYGON_HATCHED_45.PcbLib',
    'Generated/Individual/PCB/POLYGON_HATCHED_90.PcbLib',
    'Generated/Individual/PCB/POLYGON_ISLANDS.PcbLib',
    'Generated/Individual/PCB/POLYGON_OUTLINE.PcbLib',
    'Generated/Individual/PCB/POLYGON_SOLID.PcbLib',
    'Generated/Individual/PCB/POLYGON_THERMAL.PcbLib',
    'Generated/Individual/PCB/REGIONS_TEST.PcbLib',
    'Generated/Individual/PCB/REGION_BOARD_OUTLINE.PcbLib',
    'Generated/Individual/PCB/REGION_CAVITY.PcbLib',
    'Generated/Individual/PCB/REGION_COMPLEX.PcbLib',
    'Generated/Individual/PCB/REGION_COPPER.PcbLib',
    'Generated/Individual/PCB/REGION_CUTOUT.PcbLib',
    'Generated/Individual/PCB/REGION_KEEPOUT_TRACK.PcbLib',
    'Generated/Individual/PCB/REGION_SLOT.PcbLib',
    'Generated/Individual/PCB/REGION_WITH_HOLE.PcbLib',
    'Generated/Individual/PCB/TEXT_ALL_STROKE_FONTS.PcbLib',
    'Generated/Individual/PCB/TEXT_BARCODE.PcbLib',
    'Generated/Individual/PCB/TEXT_BARCODE_ALL.PcbLib',
    'Generated/Individual/PCB/TEXT_COMMENT.PcbLib',
    'Generated/Individual/PCB/TEXT_DESIGNATOR.PcbLib',
    'Generated/Individual/PCB/TEXT_INVERTED.PcbLib',
    'Generated/Individual/PCB/TEXT_MULTILINE.PcbLib',
    'Generated/Individual/PCB/TEXT_TEST.PcbLib',
    'Generated/Individual/PCB/TEXT_TRUETYPE.PcbLib',
    'Generated/Individual/PCB/TEXT_TRUETYPE_FONTS.PcbLib',
    'Generated/Individual/PCB/TRACKS_MULTILAYER.PcbLib',
    'Generated/Individual/PCB/TRACK_ALL_LAYERS.PcbLib',
    'Generated/Individual/PCB/TRACK_KEEPOUT.PcbLib',
    'Generated/Individual/PCB/TRACK_WIDTHS.PcbLib',
    'Generated/Individual/PCB/VIA_BLIND_BOTTOM.PcbLib',
    'Generated/Individual/PCB/VIA_BLIND_TOP.PcbLib',
    'Generated/Individual/PCB/VIA_BURIED.PcbLib',
    'Generated/Individual/PCB/VIA_MICROVIA.PcbLib',
    'Generated/Individual/PCB/VIA_TENTING.PcbLib',
    'Generated/Individual/PCB/VIA_TYPES.PcbLib',
    'Generated/TestPcbLib.PcbLib',
    'Level Shifter.SchDoc',
    'MAX5719 Breakout.PcbDoc',
    'Overview.SchDoc',
    'PCB - DIODES - DIODES INC SDT10A45P5.PCBLIB',
    'PCB - LEADLESS - BGA - AMD XILINX FCBGA-900 FFG900 31X31.PcbLib',
    'PCB - LEADLESS - BGA - STM WLCSP-56 B0H4.PcbLib',
    'PCB - LEADLESS - BGA - STMICRO UFBGA169 7X7.PcbLib',
    'PCB - LEADLESS - BGA - STMICROELECTRONICS UFBGA176+25 10X10 A0E7.PcbLib',
    'PCB - LEADLESS - BGA - XILINX AMD BGA-676 FFRB676 27x27.PcbLib',
    'PCB - LEADLESS - LFCSP - AD LFCSP-24 4X4MM CP-24-8.PcbLib',
    'PCB - LEADLESS - LFCSP - AD LFCSP-32 CP-32-7.PcbLib',
    'PCB - LEADLESS - QFN - PSEMI QFN-12 3x3x0.5.PcbLib',
    'PCB - LEADLESS - QFN - PSEMI QFN-24 4x4.PcbLib',
    'PCB - LEADLESS - QFN - QORVO RFSW6024.PcbLib',
    'Power Adapter Panel.PcbDoc',
    'Power Supplies.SchDoc',
    'Power Supply.SchDoc',
    'SPI Isolator Panel.PcbDoc',
    'SPI Isolator.PcbDoc',
    'SPI Isolator.SchDoc',
    'USB Power Adapter.PcbDoc',
    'USB Power.SchDoc',
    'VCOCXO Breakout.PcbDoc',
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


def altiumsharp_url(rel):
    path = urllib.parse.quote(ALTIUMSHARP_DIR + rel)
    return f"https://raw.githubusercontent.com/issus/AltiumSharp/{ALTIUMSHARP_REF}/{path}"


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
    jobs = [("kicad", rel, kicad_url(rel)) for rel in KICAD_FILES] + \
        [("altiumsharp", rel, altiumsharp_url(rel)) for rel in ALTIUMSHARP_FILES]
    for source, rel, url in jobs:
        dest = root / source / rel
        if dest.is_file() and not args.force:
            print(f"cached   {source}/{rel}")
            continue
        try:
            size, digest = fetch(url, dest)
            print(f"fetched  {source}/{rel}  {size} bytes  sha256 {digest}")
        except Exception as e:  # report every failure, keep going
            failures += 1
            print(f"FAILED   {source}/{rel}: {e}", file=sys.stderr)
    print(f"cache: {root}")
    if failures:
        print(f"{failures} file(s) failed to download", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
