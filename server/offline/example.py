"""Worked example: the offline tools end to end on a synthetic board.

    python -m offline example [-o folder]

Builds a small two-layer 2 oz power board (made up here, not a real design)
and runs the steps a layout session would, through the same functions the
MCP tools (offline_pcb_*) are:

 1. load    offline_pcb_info: nets, parts, layers
 2. ECO     the schematic drops R2, a link between VIN and VBAT, so the two
            nets become one: offline_pcb_eco_preview, offline_pcb_eco_apply
 3. gaps    offline_pcb_net_gaps: VBAT now has a hole where R2 was
 4. copper  offline_pcb_copper_plan: VBAT copper for 30 A from J1.1 to Q1.D,
            then offline_pcb_apply_plan writes it
 5. pour    offline_pcb_repour: the bottom GND pour flows around it
 6. DRC     offline_pcb_drc on the result
 7. render  offline_pcb_render: the board before and after, and a diff
 8. write   final.PcbDoc, to open in Altium, Repour All and run its DRC

Every file goes to the output folder (a new temporary folder by default);
the input board is never written over.
"""

import json
import sys
import tempfile
from pathlib import Path

from .synth import Synth


def board():
    """The synthetic input board: J1 feeds VIN, R2 links VIN to VBAT, Q1
    switches VBAT to VOUT at J2, U1 drives Q1's gate, C1 decouples VBAT. The
    VBAT track to Q1 is far too thin for 30 A; GND is a bottom pour."""
    s = Synth(60, 40, layers=2, copper_mm=0.07)
    for n in ("VIN", "VBAT", "VOUT", "GND", "GATE"):
        s.net(n)
    s.rule("Clearance", "Clearance", GAP=0.3)
    s.rule("Width", "Width", MINLIMIT=0.2, MAXLIMIT=20, PREFEREDWIDTH=0.5)
    s.rule("PolygonConnect", "PolygonConnect", CONNECTSTYLE="Direct")
    tht = dict(layer="MULTILAYER", shape="round", hole=1.5)
    s.component("J1", "CONN2", 6, 21, comment="IN")
    s.pad("J1", "1", 6, 15, 3.0, 3.0, net="VIN", **tht)
    s.pad("J1", "2", 6, 27, 3.0, 3.0, net="GND", **tht)
    s.component("R2", "R2512", 23.2, 15, comment="0R")
    s.pad("R2", "1", 20, 15, 1.6, 3.2, net="VIN")
    s.pad("R2", "2", 26.4, 15, 1.6, 3.2, net="VBAT")
    s.component("Q1", "LFPAK56", 48, 13, comment="FET")
    s.pad("Q1", "D", 48, 15, 5.0, 4.4, net="VBAT")
    s.pad("Q1", "S", 49, 10.2, 3.2, 1.0, net="VOUT")
    s.pad("Q1", "G", 45.6, 10.2, 0.8, 1.0, net="GATE")
    s.component("J2", "CONN2", 55.5, 18, comment="OUT")
    s.pad("J2", "1", 55.5, 10.2, 3.0, 3.0, net="VOUT", **tht)
    s.pad("J2", "2", 55.5, 27, 3.0, 3.0, net="GND", **tht)
    s.component("U1", "SOT23-3", 38, 5, comment="DRIVER")
    s.pad("U1", "1", 36.5, 5, 0.6, 1.1, net="VBAT")
    s.pad("U1", "2", 38, 5, 0.6, 1.1, net="GND")
    s.pad("U1", "3", 39.5, 5, 0.6, 1.1, net="GATE")
    s.component("C1", "C1210", 40, 22, comment="100u")
    s.pad("C1", "1", 40, 20.4, 2.7, 1.2, net="VBAT")
    s.pad("C1", "2", 40, 23.6, 2.7, 1.2, net="GND")
    s.track("Top Layer", 6, 15, 20, 15, 2.5, net="VIN")
    s.track("Top Layer", 26.4, 15, 48, 15, 0.6, net="VBAT")
    s.track("Top Layer", 36.5, 5, 36.5, 15, 0.3, net="VBAT")
    s.track("Top Layer", 40, 20.4, 40, 15, 0.6, net="VBAT")
    s.track("Top Layer", 49, 10.2, 55.5, 10.2, 1.0, net="VOUT")
    s.track("Top Layer", 39.5, 5, 45.6, 5, 0.3, net="GATE")
    s.track("Top Layer", 45.6, 5, 45.6, 10.2, 0.3, net="GATE")
    s.track("Top Layer", 38, 5, 38, 2.6, 0.3, net="GND")
    s.via(38, 2.6, 0.6, 0.3, net="GND")
    s.track("Top Layer", 40, 23.6, 40, 25.6, 0.6, net="GND")
    s.via(40, 25.6, 0.6, 0.3, net="GND")
    s.polygon("Bottom Layer", [(0.5, 0.5), (59.5, 0.5), (59.5, 39.5), (0.5, 39.5)], net="GND",
              name="GND_BOTTOM")
    return s


def schematic(doc):
    """The changed schematic, as a netlist: the board's own parts and nets,
    with R2 gone and its two nets joined as VBAT."""
    from .eco import board_view
    view = board_view(doc)
    parts = {}
    for d, p in view["parts"].items():
        if d == "R2":
            continue
        parts[d] = {"footprint": p["footprint"], "comment": p["comment"],
                    "unique_id": p["source_unique_id"], "pins": []}
    nets = {}
    for (d, pin), net in sorted(view["pins"].items()):
        if d == "R2" or net is None:
            continue
        nets.setdefault("VBAT" if net == "VIN" else net, []).append(f"{d}.{pin}")
    return {"parts": parts, "nets": nets}


def _tools():
    here = str(Path(__file__).resolve().parent.parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import offline_tools
    return offline_tools


def run(folder=None, say=print):
    """Run every step; returns {step: result}. Raises at the first step that
    fails, naming it."""
    T = _tools()
    out = Path(folder) if folder else Path(tempfile.mkdtemp(prefix="offline_example_"))
    out.mkdir(parents=True, exist_ok=True)
    f = {k: str(out / v) for k, v in {
        "input": "input.PcbDoc", "netlist": "schematic_netlist.json", "eco": "eco.PcbDoc",
        "eco_plan": "eco_plan.json", "copper_plan": "copper_plan.json",
        "copper_png": "copper_plan.png", "planned": "planned.PcbDoc", "final": "final.PcbDoc",
        "before": "before.png", "after": "after.png", "diff": "diff.png",
        "markers": "drc_markers.json"}.items()}
    steps = {}

    def step(name, fn, **kw):
        res = T.call(fn, **kw)
        if not res["ok"]:
            raise RuntimeError(f"step {name} ({fn.__name__}) failed: {res['error']}")
        steps[name] = res
        return res

    doc = board().save(f["input"])
    say(f"folder: {out}")
    say(f"0. built {f['input']} (synthetic, two layers, 2 oz)")

    r = step("load", T.offline_pcb_info, board=f["input"])
    say(f"1. load: {len(r['nets'])} nets, {len(r['components'])} parts, "
        f"{r['outline']['width_mm']} x {r['outline']['height_mm']} mm, "
        f"copper {r['copper_mm']}")
    step("render_before", T.offline_pcb_render, board=f["input"], out=f["before"],
         nets=["VBAT", "VIN"], width=1200)

    Path(f["netlist"]).write_text(json.dumps(schematic(doc), indent=1), encoding="utf-8")
    r = step("eco_preview", T.offline_pcb_eco_preview, schematic=f["netlist"],
             board=f["input"])
    say(f"2. ECO preview: remove {[x['designator'] for x in r['parts']['remove']]}, "
        f"merge {[(m['from'], m['into']) for m in r['nets']['merges']]}")
    r = step("eco_apply", T.offline_pcb_eco_apply, schematic=f["netlist"], board=f["input"],
             out=f["eco"], plan_out=f["eco_plan"], overwrite=True)
    say(f"   ECO apply: {r['ops']} op(s) -> {f['eco']}; problems: {r['problems'] or 'none'}")

    r = step("gaps", T.offline_pcb_net_gaps, board=f["eco"], nets=["VBAT"])
    for g in r["gaps"]:
        say(f"3. gap: {g['net']} {g['gap_mm']} mm between {g['from']} and {g['to']} "
            "(GND's pads join only once its pour is poured, in step 5)")

    plan = {"net": "VBAT", "sources": ["J1.1"], "sinks": ["Q1.D"], "current": 30,
            "targets": {"density_a_per_mm2": 40, "drop_mv": 30}, "res": 0.25}
    r = step("copper_plan", T.offline_pcb_copper_plan, board=f["eco"], plan=plan,
             plan_out=f["copper_plan"], png=f["copper_png"])
    fin = r["final"]
    say(f"4. copper plan: {'targets met' if r['met'] else 'targets NOT met'}; drop "
        f"{fin['drop_mv']} mV, worst {fin['max_density_a_per_mm2']} A/mm2, "
        f"{fin['vias']} vias, {r['ops']} op(s)")
    r = step("apply", T.offline_pcb_apply_plan, board=f["eco"], plan=f["copper_plan"],
             out=f["planned"], overwrite=True)
    say(f"   applied -> {f['planned']}; stale pours {r.get('stale_pours')}")

    r = step("pour", T.offline_pcb_repour, board=f["planned"], out=f["final"], store="pour",
             overwrite=True)
    for p in r["polygons"]:
        say(f"5. pour: polygon {p['polygon']} {p['net']} on {p['layer']}: {p['area_mm2']} mm2, "
            f"{p['pieces']} piece(s)")

    r = step("drc", T.offline_pcb_drc, board=f["final"], copper="saved",
             markers_out=f["markers"])
    say(f"6. DRC: {r['summary']['violations']} violation(s) {r['summary']['by_rule'] or ''}")
    r = step("connectivity", T.offline_pcb_connectivity, board=f["final"])
    say(f"   connectivity: {r['summary']}")

    step("render_after", T.offline_pcb_render, board=f["final"], out=f["after"],
         nets=["VBAT"], markers=f["markers"], width=1200)
    step("render_diff", T.offline_pcb_render, board=f["input"], out=f["diff"],
         diff=f["final"], width=1200)
    say(f"7. render: {f['before']}, {f['after']}, {f['diff']}")
    say(f"8. write: {f['final']}. In Altium: open it, Tools > Polygon Pours > Repour All, "
        "then Tools > Design Rule Check.")
    steps["files"] = f
    return steps


def cmd_example(args):
    run(args.out)
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("-o", "--out", help="folder for the files (default: a new temp folder)")
    command("example", cmd_example, "worked example on a synthetic board, end to end", build)
