# Offline PCB tools

Read, check, edit and write Altium `.PcbDoc` files without Altium. Run from the
`server/` directory:

    python -m offline info board.PcbDoc
    python -m offline connect board.PcbDoc            # islands, gaps, shorts
    python -m offline pdn board.PcbDoc --net VBAT --source J1.1 --sink U5.3 --current 30
    python -m offline rules board.PcbDoc

All lengths are millimetres. Coordinates are absolute (database) coordinates,
Y up, as the file stores them; `PcbDoc.origin` gives the user origin.

Install: `pip install -r server/offline/requirements.txt` (numpy, shapely,
scipy, Pillow; olefile for tests).

## Modules

| Module | Does |
|---|---|
| `cfb.py`, `cfb_write.py` | compound-file container: read; write with FAT, mini FAT, DIFAT, directory |
| `pcbdoc.py` | the reader: every storage parsed losslessly, mm API |
| `layers.py` | layer numbers, names, copper stack order |
| `records.py`, `templates.py` | construct new tracks, arcs, vias, pads, fills, regions, texts |
| `pcbwrite.py` | save (never onto the input), add records, integrity check |
| `geom.py` | shapely shapes of primitives |
| `connect.py` | islands per net, unrouted gaps, shorts, dead copper |
| `scope.py`, `rules.py` | rule scope language and rule resolution |
| `raster.py` | exact pixel-centre rasterizing per net |
| `pdn.py` | DC voltage drop, current density, power, via currents |
| `synth.py` | small synthetic boards for tests |
| `fixtures.py` | where tests find real boards |

## Facts the tools rely on

The binary layout and every cross-reference between streams are written down
in [`PCBDOC_FORMAT.md`](PCBDOC_FORMAT.md). Tests:
`python -m unittest discover -s server/tests -p "test_offline*.py"`.
