"""python -m offline <command> ...   (run from the server/ directory)

Commands print a short human summary, or JSON with --json. Lengths are mm.
"""

import argparse
import json
import os
import sys


def _doc(path):
    from .pcbdoc import PcbDoc
    return PcbDoc.open(path)


def _out(args, data, text=None):
    if getattr(args, "json", False) or text is None:
        print(json.dumps(data, indent=2, default=_jsonable))
    else:
        print(text)


def _jsonable(o):
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
    except ImportError:
        pass
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def cmd_info(args):
    doc = _doc(args.board)
    s = doc.summary()
    lines = [f"{k}: {v}" for k, v in s.items()]
    _out(args, s, "\n".join(lines))


def cmd_connect(args):
    from . import connect
    doc = _doc(args.board)
    nets = {doc.net_index(n) for n in args.nets} if args.nets else None
    rep = connect.report(doc, nets)
    s = rep["summary"]
    text = [f"WARNING: {w}" for w in rep["warnings"]]
    text += [f"{s['nets']} nets, {s['islands']} islands, {s['unrouted_connections']} unrouted, "
             f"{s['shorts']} shorts, {s['dead_copper_islands']} dead copper islands"]
    for u in rep["unrouted"][:args.limit]:
        text.append(f"  gap {u['net']}: {u['gap_mm']:.3f} mm  {u['from']} -> {u['to']}")
    for x in rep["shorts"][:args.limit]:
        text.append(f"  SHORT {x['nets']} on {x['layer']} at {x['at']}")
    _out(args, rep, "\n".join(text))
    return 1 if s["shorts"] else 0


def cmd_pdn(args):
    from . import pdn, layers as L
    doc = _doc(args.board)
    thick = None
    if args.copper_oz:
        thick = {lay: args.copper_oz * 0.0347 for lay in doc.copper_layers}
    res = pdn.solve(doc, args.net, args.source, args.sink, args.current, res=args.res,
                    temp_c=args.temp, thickness=thick)
    rep = res.report()
    if args.png:
        from . import render
        render.pdn_png(doc, res, args.png)
        rep["png"] = args.png
    text = (f"{rep['net']}: {args.current} A, drop {rep['drop_mv']} mV "
            f"({rep['resistance_mohm']} mOhm), {rep['power_w']} W\n" +
            "\n".join(f"  J {h['j_a_per_mm2']} A/mm2 at ({h['x']}, {h['y']}) on {h['layer']}"
                      for h in rep["max_current_density_a_per_mm2"]) +
            ("\n  warnings: " + "; ".join(rep["warnings"]) if rep["warnings"] else ""))
    _out(args, rep, text)


def cmd_rules(args):
    from .rules import RuleSet
    doc = _doc(args.board)
    rs = RuleSet(doc)
    data = {"rules": [{"kind": c.kind, "name": c.name, "priority": c.rule.priority,
                       "enabled": c.rule.enabled, "scope1": c.rule.scope1,
                       "scope2": c.rule.scope2, "error": c.error} for c in rs.compiled],
            "unevaluated": rs.unevaluated}
    text = "\n".join(f"{c.kind:28s} p{c.rule.priority:<3d} {c.name:30s} {c.rule.scope1} | "
                     f"{c.rule.scope2}" + (f"  !! {c.error}" if c.error else "")
                     for c in rs.compiled)
    _out(args, data, text)


COMMANDS = {}


def command(name, fn, help_, build):
    COMMANDS[name] = (fn, help_, build)


def _board(p):
    p.add_argument("board", help=".PcbDoc file")
    p.add_argument("--json", action="store_true", help="print JSON")


command("info", cmd_info, "summarize a board", _board)
command("connect", cmd_connect, "islands, unrouted gaps and shorts per net",
        lambda p: (_board(p), p.add_argument("--nets", nargs="*"),
                   p.add_argument("--limit", type=int, default=50)))
command("pdn", cmd_pdn, "DC drop, current density and power for one net",
        lambda p: (_board(p), p.add_argument("--net", required=True),
                   p.add_argument("--source", nargs="+", required=True, help="U1.3 or x,y[@layer]"),
                   p.add_argument("--sink", nargs="+", required=True),
                   p.add_argument("--current", type=float, required=True, help="amps"),
                   p.add_argument("--res", type=float, default=0.1, help="grid mm"),
                   p.add_argument("--temp", type=float, default=20.0, help="copper temperature C"),
                   p.add_argument("--copper-oz", type=float, help="override copper weight"),
                   p.add_argument("--png", help="write a current-density map")))
command("rules", cmd_rules, "list rules and any scope this tool cannot evaluate", _board)


def _load_plugins():
    """Commands defined in other modules register themselves here."""
    import importlib
    for mod in ("render", "editplan", "drc", "pour", "eco", "copper", "thermal", "power",
                "specctra", "autoroute", "audit", "notch", "review", "blank", "stack",
                "assembly", "gerber", "models3d", "example", "mech", "padties", "neck",
                "fpcheck", "schtools", "calc", "rules"):
        try:
            m = importlib.import_module(f".{mod}", __package__)
        except ImportError as e:
            if f"offline.{mod}" in str(e) or f"'{mod}'" in str(e):
                continue
            raise
        reg = getattr(m, "register_commands", None)
        if reg:
            reg(command)


def main(argv=None):
    _load_plugins()
    ap = argparse.ArgumentParser(prog="python -m offline", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, (fn, help_, build) in sorted(COMMANDS.items()):
        p = sub.add_parser(name, help=help_)
        build(p)
        p.set_defaults(func=fn)
    args = ap.parse_args(argv)
    try:
        rc = args.func(args)
    except BrokenPipeError:
        # the reader went away (| head): stop quietly, as the Python docs advise
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1
    except (ValueError, KeyError, ImportError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
