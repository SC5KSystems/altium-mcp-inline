"""Command-line front end:  python -m offline <command> <file.PcbDoc> [options]

Run from the server/ directory. Everything prints JSON so it can be piped or saved.
Commands needing numpy (neck, solve) say so if it is missing.
"""
import argparse
import json
import sys

from . import connect, power, review
from .pcbdoc import PcbDoc


def _out(obj):
    print(json.dumps(obj, indent=1, default=float))


def _csv(s):
    return [x for x in (s or "").split(",") if x]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="offline", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def pcb(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("file")
        return p

    pcb("summary", "board size, stackup, counts, poured copper")
    p = pcb("holes", "drilled holes and their distance to the board edge")
    p.add_argument("--min", type=float, default=3.5, help="minimum hole diameter, mm")
    p.add_argument("--mounting", action="store_true", help="only free (non-component) holes")
    p = pcb("fasteners", "screw head / washer versus board edge and live copper")
    p.add_argument("--screw", default="M4-pan", choices=sorted(review.SCREWS))
    p.add_argument("--washer", action="store_true")
    p.add_argument("--head", type=float, help="override head or washer diameter, mm")
    p.add_argument("--margin", type=float, default=1.0, help="wanted head-to-edge margin, mm")
    p = pcb("edges", "parts near or over the board edge, and outline-clearance violations")
    p.add_argument("--within", type=float, default=3.0)
    pcb("rules", "rule scopes that reference something missing or empty")
    p = pcb("connect", "how pads tie into pours, measured from the saved copper")
    p.add_argument("--nets", required=True, help="comma-separated net names")
    p = pcb("neck", "narrowest point between two pad groups on one net (numpy)")
    p.add_argument("--net", required=True)
    p.add_argument("--src", required=True, help="REF or REF.PAD, comma-separated")
    p.add_argument("--dst", required=True)
    p.add_argument("--res", type=float, default=0.1)
    p.add_argument("--ignore", type=float, default=3.0, help="ignore copper this close to the pads, mm")
    p = pcb("solve", "DC resistance, drop, power and hot spots between two pad groups (numpy)")
    p.add_argument("--net", required=True)
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True)
    p.add_argument("--amps", type=float, required=True)
    p.add_argument("--res", type=float, default=0.1)
    p.add_argument("--temp", type=float, default=25.0, help="copper temperature, C")

    p = sub.add_parser("ripple", help="R-L load current ripple under PWM")
    p.add_argument("--v", type=float, default=13.5)
    p.add_argument("--r", type=float, required=True, help="ohms")
    p.add_argument("--l", type=float, required=True, help="henries")
    p.add_argument("--f", type=float, required=True, help="Hz")
    p.add_argument("--duty", type=float, default=0.5)
    p = sub.add_parser("profet", help="PROFET operating point and PWM cost from datasheet values")
    p.add_argument("--part", default="BTS7008-1EPP", choices=sorted(power.PROFETS))
    p.add_argument("--amps", type=float, required=True)
    p.add_argument("--amb", type=float, default=85.0)
    p.add_argument("--rth", type=float, help="junction-to-ambient K/W on YOUR board (default: datasheet 4-layer)")
    p.add_argument("--pwm", type=float, help="PWM frequency, Hz")

    # schematics: a .PrjPcb (all its sheets) or one .SchDoc; compare also takes .PcbDoc or a
    # compiled-netlist .txt (dev/exp_compiled_netlist.pas)
    p = sub.add_parser("sch-netlist", help="schematic netlist: nets and their pins")
    p.add_argument("file")
    p.add_argument("--net", default="", help="only nets whose name contains this")
    p = sub.add_parser("sch-check", help="unconnected pins, floating labels, two-named nets ...")
    p.add_argument("file")
    p.add_argument("--info", action="store_true", help="include info-level findings")
    p = sub.add_parser("sch-compare", help="what changed between two netlists")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--json", action="store_true", help="full result instead of the text report")
    p = sub.add_parser("sch-part", help="one part's pins, nets and neighbours")
    p.add_argument("file")
    p.add_argument("designator")
    p = sub.add_parser("sch-bom", help="bill of materials with MPN search terms")
    p.add_argument("file")
    p = sub.add_parser("sch-rules", help="check circuit blocks against a JSON list of rules")
    p.add_argument("file")
    p.add_argument("rules", help="JSON file holding a list of rules (see schtools.check_rules)")
    p = sub.add_parser("sch-footprints", help="footprints resolve, pins match pads; Update PCB preview")
    p.add_argument("file", help=".PrjPcb")
    p.add_argument("--board", default=None, help="board to preview against (default: the project's)")
    p.add_argument("--libs", type=_csv, default=[], help="extra .PcbLib files or folders, comma-separated")
    p.add_argument("--json", action="store_true", help="full result instead of the text report")
    a = ap.parse_args(argv)

    if a.cmd == "sch-footprints":
        from . import footprints
        res = footprints.check(a.file, a.board, a.libs)
        if a.json:
            _out(res)
        else:
            for line in footprints.format_check(res):
                print(line)
        return 0
    if a.cmd.startswith("sch-"):
        from . import schtools
        if a.cmd == "sch-compare":
            res = schtools.compare(schtools.load_any(a.before), schtools.load_any(a.after))
            if a.json:
                _out(res)
            else:
                for line in schtools.format_compare(res):
                    print(line)
            return 0
        src = schtools.load_any(a.file)
        if a.cmd == "sch-netlist":
            _out({"summary": src.netlist.summary(),
                  "nets": {n.name: ["%s.%s" % (q.designator, q.pin) for q in n.pins]
                           for n in src.netlist.nets if a.net.lower() in n.name.lower()}})
        elif a.cmd == "sch-check":
            _out({"summary": src.netlist.summary(),
                  "findings": [f.as_dict() for f in src.netlist.findings if a.info or f.severity != "info"]})
        elif a.cmd == "sch-part":
            _out(schtools.part_view(src, a.designator))
        elif a.cmd == "sch-bom":
            _out(schtools.bom(src))
        elif a.cmd == "sch-rules":
            with open(a.rules, encoding="utf-8") as f:
                _out(schtools.check_rules(src, json.load(f)))
        return 0

    if a.cmd == "ripple":
        _out(power.rl_ripple(a.v, a.r, a.l, a.f, a.duty))
        return 0
    if a.cmd == "profet":
        _out({"steady": power.profet_operating_point(a.part, a.amps, a.amb, a.rth),
              "max_continuous_a": power.max_current_for_tj(a.part, a.amb, a.rth),
              "pwm": power.profet_pwm(a.part, a.pwm, a.amps, t_amb=a.amb, rth_ja=a.rth) if a.pwm else None})
        return 0

    doc = PcbDoc.load(a.file)
    if a.cmd == "summary":
        _out(review.summary(doc))
    elif a.cmd == "holes":
        rows = review.mounting_holes(doc, a.min) if a.mounting else review.holes(doc, a.min)
        _out(rows)
    elif a.cmd == "fasteners":
        _out(review.fastener_report(doc, a.screw, a.washer, a.head, a.margin))
    elif a.cmd == "edges":
        _out({"near_or_over_edge": review.edge_items(doc, a.within),
              "outline_clearance": review.outline_clearance_violations(doc)})
    elif a.cmd == "rules":
        _out(review.dead_scopes(doc))
    elif a.cmd == "connect":
        rows, unknown = connect.pad_connections(doc, _csv(a.nets))
        _out({"unevaluated_predicates": unknown, "pads": rows})
    elif a.cmd in ("neck", "solve"):
        try:
            from . import pdn
        except ImportError as e:
            print("this command needs numpy: %s" % e, file=sys.stderr)
            return 2
        if a.cmd == "neck":
            r = pdn.neck_between(doc, a.net, _csv(a.src), _csv(a.dst), a.res, ignore_mm=a.ignore)
            th = r.pop("thickness_mm")
            _out({"thickness_mm": th, "neck": r, "ipc2221_note": "A at dT=20/30 C"})
        else:
            _out(pdn.solve_net(doc, a.net, _csv(a.src), _csv(a.dst), a.amps, a.res, a.temp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
