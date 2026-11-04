"""Read .PcbDoc files with KiCad's Altium importer and print object counts as JSON.

An independent check of server/offline's writer: KiCad's parser is strict and
shares no code with ours. Needs KiCad's Python module (pcbnew), which runs in
the interpreter KiCad was built for, e.g. on Ubuntu 24.04:

    sudo apt-get install --no-install-recommends kicad
    python3.12 dev/kicad_read.py a.PcbDoc b.PcbDoc

Prints {path: {tracks, arcs, vias, footprints, pads, zones, nets, ...}} or
{path: {"error": ...}} when KiCad refuses a file.
"""
import collections
import json
import sys

import pcbnew


def counts(path):
    b = pcbnew.LoadBoard(path, pcbnew.IO_MGR.ALTIUM_DESIGNER)
    kinds = collections.Counter(t.GetClass() for t in b.GetTracks())
    fps = list(b.GetFootprints())
    zones = list(b.Zones())
    return {
        "tracks": kinds.get("PCB_TRACK", 0),
        "arcs": kinds.get("PCB_ARC", 0),
        "vias": kinds.get("PCB_VIA", 0),
        "footprints": len(fps),
        "pads": sum(len(list(f.Pads())) for f in fps),
        "zones": len(zones),
        "filled_zones": sum(1 for z in zones if z.IsFilled()),
        "nets": b.GetNetCount(),
        "drawings": len(list(b.GetDrawings())),
        "references": sorted(str(f.GetReference()) for f in fps),
        "net_names": sorted(str(n) for n in b.GetNetsByName().keys() if str(n)),
    }


def main(paths):
    out = {}
    for p in paths:
        try:
            out[p] = counts(p)
        except Exception as e:  # report every file, never stop at one
            out[p] = {"error": str(e)[:400]}
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1:])
