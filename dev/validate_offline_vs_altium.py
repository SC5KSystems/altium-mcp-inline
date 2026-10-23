"""Check the offline PcbDoc reader against what live Altium returned for the same board.

Every tool call saves its last response as temp_*.json in the exchange folder
(C:/Users/Public/altium_mcp). This reads whichever of those exist and compares them with
the saved .PcbDoc. Nothing is hard-coded to one design: nets, pads and polygons to check
come from the responses themselves.

Run it with the board open in Altium *and saved*, after calling the tools you want checked
(get_track_data, get_vias, get_net_geometry_summary, get_polygon_data, get_pad_data,
get_pcb_layer_stackup, get_pcb_rules_parsed, get_all_nets, get_object_classes).
A mismatch is either a reader bug or an unsaved change in Altium; the output says which
response disagreed so you can tell.

Usage:  python dev/validate_offline_vs_altium.py board.PcbDoc [exchange_dir]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from offline.pcbdoc import PcbDoc  # noqa: E402

MIL = 0.0254
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (("  -- " + detail) if detail else ""))


def skipped(name):
    print("skip  " + name + "  (no response saved)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    doc = PcbDoc.load(sys.argv[1])
    ex = Path(sys.argv[2] if len(sys.argv) > 2 else "C:/Users/Public/altium_mcp")

    def load(n):
        p = ex / n
        return json.load(open(p)) if p.exists() else None

    t = load("temp_track_data.json")
    if t:
        for g in t:
            ni = doc.net_index(g["net"])
            trs = [x for x in doc.tracks if x.net == ni]
            check("tracks %s count" % g["net"], len(trs) == g["track_count"], "%d vs %d" % (len(trs), g["track_count"]))
            if trs:
                check("tracks %s width range" % g["net"], round(min(x.width for x in trs) / MIL) == round(g["min_width_mils"])
                      and round(max(x.width for x in trs) / MIL) == round(g["max_width_mils"]))
                check("tracks %s total length" % g["net"],
                      abs(sum(x.length for x in trs) / MIL - g["total_length_mils"]) < 0.5)
    else:
        skipped("get_track_data")

    v = load("temp_vias_data.json")
    if v:
        nets = {x["net"] for x in v}
        for n in nets:
            ni = doc.net_index(n)
            mine = sorted((round(a.x / MIL), round(a.y / MIL)) for a in doc.vias if a.net == ni)
            theirs = sorted((round(x["x"]), round(x["y"])) for x in v if x["net"] == n)
            check("vias %s positions" % n, mine == theirs, "%d vs %d" % (len(mine), len(theirs)))
    else:
        skipped("get_vias")

    g = load("temp_net_geometry_summary.json")
    if g:
        for x in g:
            ni = doc.net_index(x["net"])
            polys = [p for p in doc.polygons if p.net == ni]
            check("summary %s polygon count" % x["net"], len(polys) == x["polygon_count"])
            check("summary %s outline area" % x["net"],
                  abs(sum(p.outline_area for p in polys) / (MIL * MIL) - x["polygon_outline_area_sq_mils"]) < 50)
            check("summary %s track/via/arc counts" % x["net"],
                  (len([a for a in doc.tracks if a.net == ni]), len([a for a in doc.vias if a.net == ni]),
                   len([a for a in doc.arcs if a.net == ni])) == (x["track_count"], x["via_count"], x["arc_count"]))
    else:
        skipped("get_net_geometry_summary")

    pd = load("temp_polygon_data.json")
    if pd:
        for x in pd:
            ni = doc.net_index(x["net"])
            cand = [p for p in doc.polygons if p.net == ni and len(p.vertices) == x["vertex_count"]]
            same = [p for p in cand if abs(p.outline_area / (MIL * MIL) - x["outline_area_sq_mils"]) < 1]
            check("polygon %s outline (vertices + area)" % x["net"], bool(same))
            if same:
                p = same[0]
                # Poly.PourOver is an integer enum equal to the file's POUROVERSTYLE. Older builds of the
                # tool emitted it as a Boolean (always false), so a response with only `pour_over` is
                # reported rather than compared.
                if "pour_over_style" in x:
                    check("polygon %s pour-over style" % x["net"],
                          str(x["pour_over_style"]) == p.props.get("POUROVERSTYLE"),
                          "file %s vs API %s" % (p.props.get("POUROVERSTYLE"), x["pour_over_style"]))
                else:
                    print("      info  %s: response predates pour_over_style (API pour_over=%s; file POUROVERSTYLE=%s)" % (
                        x["net"], x.get("pour_over"), p.props.get("POUROVERSTYLE")))
    else:
        skipped("get_polygon_data")

    s = load("temp_stackup_data.json")
    if s:
        cu = [l for l in doc.board.layers if l.copper_mm]
        check("stackup copper layer count", len(cu) == s["total_layers"])
        for layer in s["layers"]:
            mine = [l for l in cu if l.name == layer["layer_name"]]
            if mine:
                # 1 mil = 25.4 um. A live response that disagrees by ~1.55x is the old unit bug.
                check("stackup %s copper um" % layer["layer_name"],
                      abs(mine[0].copper_mm * 1000 - layer["copper_thickness_um"]) < 0.5,
                      "file %.1f um vs API %.1f um" % (mine[0].copper_mm * 1000, layer["copper_thickness_um"]))
    else:
        skipped("get_pcb_layer_stackup")

    pads = load("temp_pad_data.json")
    if pads:
        for p in pads["pads"]:
            c = doc.component(p["designator"])
            m = [q for q in doc.pads if c is not None and q.comp == c.idx and q.name == p["pad"]]
            check("pad %s.%s" % (p["designator"], p["pad"]), bool(m) and abs(m[0].x / MIL - p["x"]) < 0.05
                  and abs(m[0].y / MIL - p["y"]) < 0.05 and doc.net_name(m[0].net) == p["net"])
    else:
        skipped("get_pad_data")

    r = load("temp_rules_parsed.json")
    if r:
        rm = {x.name: x for x in doc.rules}
        check("rule names", sorted(rm) == sorted(x["name"] for x in r))
        check("rule scopes", all(rm[x["name"]].scope1 == x["scope1"] and rm[x["name"]].scope2 == x["scope2"]
                                 for x in r if x["name"] in rm))
    else:
        skipped("get_pcb_rules_parsed")

    n = load("temp_nets_data.json")
    if n:
        check("net list", sorted(x.name for x in doc.nets) == sorted(n))
    else:
        skipped("get_all_nets")

    oc = load("temp_object_classes.json")
    if oc:
        by = {c["name"]: c for c in oc}
        if "All Pads" in by:
            check("All Pads count", len(doc.pads) == by["All Pads"]["member_count"])
        if "All Components" in by:
            check("All Components count", len(doc.components) == by["All Components"]["member_count"])
    else:
        skipped("get_object_classes")

    print("\n%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
