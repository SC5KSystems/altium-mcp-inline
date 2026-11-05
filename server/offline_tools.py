"""Offline tools for the MCP server: offline_pcb_* (boards), offline_sch_*
(schematics) and calc_* (power-stage calculators).

They read and write Altium .PcbDoc files directly, without Altium: look at a
board, check it, plan edits and write the result to a new file. Altium is
needed only afterwards, to open the result, repour and run its own DRC.

Each tool is a plain function returning a dict, so Python can call them as
well; register(mcp) wraps them as async MCP tools that run in a worker
thread and answer JSON. main.py calls register. Nothing here imports main.py,
pywin32 or mcp, and the offline package is imported only when a tool runs.

Paths must be absolute. Every writer takes an output path and refuses to
write onto one of its inputs, onto the N: drive (the shared libraries,
read-only for this server) or over an existing Altium design file unless
overwrite is true. Every result
has "ok": a tool that fails answers {"ok": false, "error": ...}; a tool that
ran answers ok true and its own verdict ("passed", "met", counts).
"""

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("AltiumMCPServer.offline")
log.addHandler(logging.NullHandler())

DESIGN_SUFFIXES = {".pcbdoc", ".pcblib", ".schdoc", ".schlib", ".prjpcb", ".intlib",
                   ".dblib", ".outjob"}
PROTECTED_DRIVES = ("N:",)
INLINE_IMAGE_BYTES = 900_000
INSTALL_HINT = ("install the offline requirements into the server's Python: "
                "python -m pip install -r server/offline/requirements.txt")


class ToolError(ValueError):
    pass


# ---------------------------------------------------------------- paths

def _absolute(p, what):
    if not p.is_absolute():
        raise ToolError(f"{what} {p}: give an absolute path (a relative one would resolve "
                        "against the server's own working folder)")


def _in(path, what="file"):
    if not path:
        raise ToolError(f"{what}: a path is needed")
    p = Path(os.path.expanduser(str(path)))
    _absolute(p, what)
    if not p.exists():
        raise ToolError(f"{what} not found: {p}")
    return p


def _protected(p):
    raw = str(p).replace("/", "\\").upper()
    if any(raw.startswith(d) for d in PROTECTED_DRIVES):
        return True
    drive = os.path.splitdrive(os.path.abspath(str(p)))[0].upper()
    return drive in PROTECTED_DRIVES


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        norm = lambda p: os.path.normcase(os.path.abspath(str(p)))  # noqa: E731
        return norm(a) == norm(b)


def _out(path, inputs=(), overwrite=False, what="out"):
    """A checked output file path (its folder is created)."""
    if not path:
        raise ToolError(f"{what}: an output path is needed")
    p = Path(os.path.expanduser(str(path)))
    if _protected(p):
        raise ToolError(f"{what} {p}: refusing to write on the N: drive (shared libraries)")
    _absolute(p, what)
    for i in inputs:
        if i and _same(p, i):
            raise ToolError(f"{what} {p}: refusing to write over an input file")
    if p.is_dir():
        raise ToolError(f"{what} {p} is a folder; give a file path")
    if p.exists() and p.suffix.lower() in DESIGN_SUFFIXES and not overwrite:
        raise ToolError(f"{what} {p} already exists; pass overwrite=true to replace it")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _out_dir(path, what="out_dir"):
    if not path:
        raise ToolError(f"{what}: a folder is needed")
    p = Path(os.path.expanduser(str(path)))
    if _protected(p):
        raise ToolError(f"{what} {p}: refusing to write on the N: drive (shared libraries)")
    _absolute(p, what)
    if p.exists() and not p.is_dir():
        raise ToolError(f"{what} {p} is a file; give a folder")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _doc(board, what="board"):
    from offline.pcbdoc import PcbDoc
    return PcbDoc.open(_in(board, what))


def _json_arg(value, what):
    """An object given inline, as JSON text, or as the path of a JSON file."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("{", "[")):
            return json.loads(s)
        return json.loads(_in(s, what).read_text(encoding="utf-8"))
    raise ToolError(f"{what}: give an object, JSON text or the path of a JSON file")


def _write_json(path, data):
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return str(path)


def _nets(doc, names):
    if not names:
        return None
    out = set()
    for n in names:
        if not doc.has_net(n):
            raise ToolError(f"no net {n!r} on this board")
        out.add(doc.net_index(n))
    return out


def _flow(spec):
    """'NET:AMPS:SRC>SINK' or {net, current, sources, sinks} -> tuple."""
    if isinstance(spec, dict):
        try:
            return (str(spec["net"]), float(spec["current"]), list(spec["sources"]),
                    list(spec["sinks"]))
        except (KeyError, TypeError, ValueError):
            raise ToolError(f"flow {spec!r}: needs net, current, sources and sinks")
    try:
        net, amps, ends = str(spec).split(":", 2)
        src, snk = ends.split(">", 1)
        return net, float(amps), src.split("+"), snk.split("+")
    except ValueError:
        raise ToolError(f"flow {spec!r}: use NET:AMPS:SRC>SINK, e.g. VBAT:30:J1.1>Q1.D")


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (set, frozenset)):
        return [_clean(v) for v in sorted(o, key=str)]
    if isinstance(o, bool) or o is None or isinstance(o, (str, int)):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, Path):
        return str(o)
    item = getattr(o, "item", None)
    if callable(item) and type(o).__module__ == "numpy":
        return _clean(o.item())
    tolist = getattr(o, "tolist", None)
    if callable(tolist) and type(o).__module__ == "numpy":
        return _clean(o.tolist())
    return str(o)


def _message(e):
    if isinstance(e, KeyError) and e.args:
        return str(e.args[0])
    msg = str(e) or type(e).__name__
    if isinstance(e, ImportError) and "requirements" not in msg:
        msg += f" ({INSTALL_HINT})"
    return msg


def call(fn, **kwargs):
    """Run one tool: its dict with "ok" and "seconds", or ok false and the error."""
    t0 = time.time()
    try:
        res = fn(**kwargs)
    except Exception as e:  # every failure goes back to the caller, named
        if isinstance(e, (ValueError, KeyError, OSError, ImportError)):
            log.warning("%s failed: %s", fn.__name__, _message(e))
        else:
            log.exception("%s failed", fn.__name__)
        return {"ok": False, "tool": fn.__name__, "error": _message(e),
                "error_type": type(e).__name__, "seconds": round(time.time() - t0, 2)}
    res = dict(res)
    res.setdefault("ok", True)
    res["seconds"] = round(time.time() - t0, 2)
    return _clean(res)


# ---------------------------------------------------------------- look

def offline_pcb_info(board: str, limit: int = 500) -> dict:
    """Summarize an Altium .PcbDoc file without Altium: copper layers, object
    counts, board outline size, nets (with pad counts) and components.

    Use it first on any board file: it gives the net names, designators and
    layer names the other offline_pcb_* tools take. Works on the saved file
    only; for the board open in Altium right now (unsaved edits) use the live
    tools (get_all_nets, get_component_data) instead.

    Typical offline workflow: info -> render / connectivity / drc to look ->
    a plan (eco_apply, copper_plan, notches, autoroute, or a hand-written
    EditPlan) -> apply_plan to write a new board -> repour -> drc -> render.
    Then open the new file in Altium, Repour All and run its DRC.
    """
    doc = _doc(board)
    pad_count = {}
    for p in doc.pads:
        if p.net is not None:
            pad_count[p.net] = pad_count.get(p.net, 0) + 1
    pts = doc.outline()
    out = {"summary": doc.summary(), "board": str(board)}
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        out["outline"] = {"box": [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4),
                                  round(max(ys), 4)],
                          "width_mm": round(max(xs) - min(xs), 3),
                          "height_mm": round(max(ys) - min(ys), 3), "vertices": len(pts)}
    else:
        out["outline"] = None
    out["copper_mm"] = {_layer_name(lay): round(doc.copper_thickness(lay), 4)
                        for lay in doc.copper_layers}
    out["copper_oz"] = {k: round(v * 1000 / 35.0, 2) for k, v in out["copper_mm"].items()}
    stack = doc.layer_stack
    out["stackup"] = [{"layer": stack[lay]["name"], "copper_mm": round(stack[lay]["copper_mm"], 4),
                       "dielectric_below_mm": round(stack[lay]["diel_mm"], 4)
                       if lay in stack and stack[lay]["diel_mm"] else None,
                       "dielectric": stack[lay]["diel_material"] or None}
                      for lay in doc.copper_layers if lay in stack]
    out["board_thickness_mm"] = round(doc.thickness, 3)
    poured = {}
    try:
        from offline import geom
        for r in doc.regions:
            if r.polygon is not None and r.kind == "copper":
                k = _layer_name(r.layer)
                poured[k] = poured.get(k, 0.0) + geom.region_shape(r).area
    except ImportError as e:
        poured = {"error": f"{e} ({INSTALL_HINT})"}
    out["poured_copper_mm2"] = {k: round(v, 1) if isinstance(v, float) else v
                                for k, v in poured.items()}
    nets = [{"name": n.name, "pads": pad_count.get(n.index, 0)} for n in doc.nets]
    comps = [{"designator": c.designator, "pattern": c.pattern, "x": round(c.x, 4),
              "y": round(c.y, 4), "rotation": round(c.rotation, 3), "side": c.side}
             for c in doc.components]
    out["nets"] = nets[:limit]
    out["components"] = comps[:limit]
    if len(nets) > limit or len(comps) > limit:
        out["truncated"] = {"nets": len(nets), "components": len(comps), "limit": limit}
    return out


def _layer_name(lay):
    from offline import layers as L
    return L.name(lay)


def offline_pcb_rules(board: str) -> dict:
    """List the design rules stored in a .PcbDoc (kind, name, priority,
    scopes) and any rule scope the offline tools cannot evaluate.

    Use before trusting offline_pcb_drc on a board: rules listed under
    "unevaluated" are not checked offline. For the rules as Altium has them
    loaded now, use the live tools instead.
    """
    from offline.rules import RuleSet
    doc = _doc(board)
    rs = RuleSet(doc)
    return {"rules": [{"kind": c.kind, "name": c.name, "priority": c.rule.priority,
                       "enabled": c.rule.enabled, "scope1": c.rule.scope1,
                       "scope2": c.rule.scope2, "error": c.error} for c in rs.compiled],
            "unevaluated": rs.unevaluated}


def offline_pcb_connectivity(board: str, nets: Optional[List[str]] = None,
                             limit: int = 50) -> dict:
    """Connectivity of a .PcbDoc from its copper: islands per net, unrouted
    connections (with the two nearest points and the gap), shorts between
    nets and dead copper.

    Use to find what is not connected or shorted before and after an edit,
    without Altium's ratsnest. nets limits the unrouted list to those nets.
    For only the open gaps of a few nets, offline_pcb_net_gaps is shorter.
    """
    from offline import connect
    doc = _doc(board)
    rep = connect.report(doc, _nets(doc, nets), limit=limit)
    rep["limit"] = limit
    return rep


def offline_pcb_net_gaps(board: str, nets: Optional[List[str]] = None, limit: int = 200) -> dict:
    """The pieces of each net that are not joined by copper, as gaps: net,
    distance, the two nearest points and what each side is.

    Use after a part is removed or nets are merged (offline_pcb_eco_apply)
    to see where copper must be added, and to check a routing or copper
    plan closed them. Empty gaps means every net's copper is joined.
    """
    from offline import eco
    doc = _doc(board)
    g = eco.gaps(doc, nets)
    return {"gaps": g[:limit], "count": len(g)}


def offline_pcb_render(board: str, out: str, layers: Optional[List[str]] = None,
                       nets: Optional[List[str]] = None, around: Optional[str] = None,
                       margin: float = 10.0, box: Optional[List[float]] = None,
                       ratsnest: bool = False, markers: Any = None, pdn: Optional[dict] = None,
                       diff: Optional[str] = None, width: int = 1600,
                       components: bool = True, show: bool = True):
    """Draw a .PcbDoc to a PNG (or SVG) file and, for a PNG, show it inline.

    Use whenever you need to see a board, a net or a change: layers (names
    like "Top Layer"), nets to highlight, around a designator (with margin
    mm) or box [x0, y0, x1, y1] mm to zoom, ratsnest to draw open
    connections, markers ([{x, y, label}] or the path of a DRC markers file)
    to pin violations, pdn {net, sources, sinks, current} to overlay current
    density, diff another board to colour what changed (red only in board,
    green only in diff). Coordinates are mm, Y up, as the file stores them.
    """
    from offline import render as R
    doc = _doc(board)
    path = _out(out, [board, diff])
    if diff:
        rep = R.diff_png(doc, _doc(diff, "diff"), str(path), layers, width)
    else:
        mk = _markers(markers) if markers else None
        heat = None
        if pdn:
            from offline import pdn as P
            try:
                res = P.solve(doc, pdn["net"], list(pdn["sources"]), list(pdn["sinks"]),
                              float(pdn["current"]), res=float(pdn.get("res", 0.1)))
            except KeyError as e:
                raise ToolError(f"pdn needs net, sources, sinks and current (missing {e})")
            heat = {"maps": res.maps, "grid": res.grid}
        rep = R.render(doc, str(path), layers=layers, nets=nets, around=around, margin=margin,
                       box=tuple(box) if box else None, ratsnest=ratsnest, markers=mk,
                       heat=heat, width=width, components=components)
    rep["image"] = str(path)
    rep["show"] = bool(show)
    return rep


def _markers(markers):
    """[{x, y, label}] from a list, a DRC report or markers file, or JSON text."""
    data = _json_arg(markers, "markers")
    if isinstance(data, dict):
        data = data.get("violations") or data.get("markers") or []
    out = []
    for i, m in enumerate(data):
        if m.get("x") is not None and m.get("y") is not None:
            out.append({"x": float(m["x"]), "y": float(m["y"]),
                        "label": m.get("label") or m.get("rule") or m.get("kind") or i + 1})
    return out


# ---------------------------------------------------------------- check

def offline_pcb_drc(board: str, checks: Optional[List[str]] = None,
                    preset: Optional[str] = None, copper: str = "auto",
                    box: Optional[List[float]] = None, text: bool = False, limit: int = 100,
                    markers_out: Optional[str] = None) -> dict:
    """Design rule check of a .PcbDoc from its own rules, without Altium:
    clearance, short circuit, unrouted net, width, hole size, annular ring,
    hole to hole, board outline, component clearance and more.

    Use after every offline edit and before handing a board to Altium.
    checks limits the kinds (e.g. ["Clearance", "ShortCircuit"]); preset
    adds a fab's 2 oz limits ("jlcpcb-2oz", "pcbway-2oz", "oshpark-2oz");
    copper "auto" pours unpoured polygons first, "saved" uses pours as
    stored, "repour" repours all; box [x0, y0, x1, y1] mm checks one area.
    markers_out writes markers for offline_pcb_render. It approximates some
    rules (marked approximate) and lists rules it cannot evaluate; Altium's
    own DRC stays the final word.
    """
    from offline.drc import DRC
    doc = _doc(board)
    mpath = _out(markers_out, [board], what="markers_out") if markers_out else None
    d = DRC(doc, copper=copper, box=tuple(box) if box else None, checks=checks, preset=preset,
            text=text)
    d.run()
    rep = d.report()
    total = len(rep["violations"])
    rep["violations"] = rep["violations"][:limit]
    rep["violations_listed"] = len(rep["violations"])
    rep["violations_total"] = total
    if mpath:
        rep["markers"] = _write_json(mpath, d.markers())
    return rep


def offline_pcb_pdn(board: str, net: str, sources: List[str], sinks: List[str], current: float,
                    res: float = 0.1, temp_c: float = 20.0, copper_oz: Optional[float] = None,
                    png: Optional[str] = None) -> dict:
    """DC solve of one net's copper: voltage drop, resistance, power and the
    worst current densities, from sources to sinks at a given current.

    Use to check a power path (e.g. VBAT from J1.1 to Q1.D at 30 A) or to
    compare copper before and after an edit. Terminals are pads ("J1.1",
    "U5.3", or a designator for all its pads) or points "x,y[@layer]". res
    is the grid in mm (0.1 fine, 0.2 faster on big boards); copper_oz
    overrides the stack's copper weight; png writes a current-density map.
    """
    from offline import pdn as P
    doc = _doc(board)
    ppath = _out(png, [board], what="png") if png else None
    thick = {lay: copper_oz * 0.0347 for lay in doc.copper_layers} if copper_oz else None
    r = P.solve(doc, net, sources, sinks, current, res=res, temp_c=temp_c, thickness=thick)
    rep = r.report()
    if ppath:
        from offline import render as R
        R.pdn_png(doc, r, str(ppath))
        rep["image"] = str(ppath)
    return rep


def offline_pcb_review_power(board: str, currents: Optional[Dict[str, float]] = None,
                             edge: float = 1.0, rise_c: float = 10.0) -> dict:
    """Power layout review in one pass: thermal-relief joins on power pads,
    via counts per net against the current, the narrowest copper of each
    power net (IPC-2221 current at a rise), parts too near the board edge,
    power nets without a test point, and polarity marks.

    Use as a first screen of a power board. currents {"VBAT": 30, "GND": 30}
    names the power nets and their currents (default: nets that look like
    power by name, with no currents). edge is mm from the outline.
    """
    from offline.review import review
    doc = _doc(board)
    cur = {}
    for n, a in (currents or {}).items():
        if not doc.has_net(n):
            raise ToolError(f"no net {n!r} on this board")
        cur[doc.nets[doc.net_index(n)].name] = float(a)
    return review(doc, cur, edge=edge, rise=rise_c)


def offline_pcb_thermal(board: str, flows: Optional[List[Any]] = None,
                        powers: Optional[Dict[str, float]] = None, ambient_c: float = 25.0,
                        res: float = 0.5, copper_rise_c: float = 20.0,
                        pdn_res: float = 0.2) -> dict:
    """Screening estimate of board temperature rise from current in copper
    and parts' dissipation (2.5-D model, still air).

    Use to find hot spots of a power path, as a screen, not a sign-off.
    flows: ["VBAT:30:J1.1>Q1.D"] or [{net, current, sources, sinks}]; powers
    {"Q1": 1.8} watts per part. Returns the hottest points and per-part
    rises with the model's assumptions.
    """
    from offline import pdn as P
    from offline import thermal as T
    doc = _doc(board)
    fl = [_flow(f) for f in flows or []]
    pw = {str(k): float(v) for k, v in (powers or {}).items()}
    if not fl and not pw:
        raise ToolError("nothing heats the board: give flows and/or powers")
    sols = [P.solve(doc, n, s, k, a, res=pdn_res, temp_c=ambient_c + copper_rise_c)
            for n, a, s, k in fl]
    rep = T.solve(doc, pdn=sols, powers=pw, ambient_c=ambient_c, res=res).report()
    rep["flows"] = [{"net": f[0], "current_a": f[1], "drop_mv": round(s.drop_v * 1e3, 3),
                     "power_w": round(s.power_w, 4)} for f, s in zip(fl, sols)]
    return rep


def offline_pcb_via_array(current: float, hole: float = 0.3, plating: float = 0.025,
                          length: float = 1.6, rise_c: float = 10.0,
                          layers: Optional[List[float]] = None, margin: float = 1.25) -> dict:
    """How many vias of a size carry a current between layers (barrel
    resistance and IPC-2152-style heating), with the copper thickness of the
    layers sharing it.

    Use when sizing a via array for a power layer change; no board needed.
    layers: copper thickness in mm of each layer sharing the current.
    """
    from offline.power import size_vias
    return size_vias(current, hole=hole, plating=plating, length=length, rise_c=rise_c,
                     layers=layers, margin=margin)


def offline_pcb_fuse_check(board: str, pad: str, rating: float, i2t: Optional[float] = None,
                           fault_amps: Optional[float] = None,
                           fault_seconds: Optional[float] = None,
                           sinks: Optional[List[str]] = None, ambient_c: float = 25.0,
                           hold_factor: float = 1.1, max_temp_c: float = 105.0,
                           thermal: bool = True) -> dict:
    """Would the copper after a fuse fail before the fuse does? Checks the
    narrowest copper of the fuse's load net against the fuse's I2t (or one
    fault point) and the sustained hold current.

    Use for each fuse on a power board. pad is the fuse's load-side pad
    ("F1.2"); rating in A; i2t in A2s, or fault_amps with fault_seconds from
    the fuse's curve. "passed" false names the weak copper.
    """
    from offline.power import fuse_check
    doc = _doc(board)
    fault = None
    if fault_amps is not None or fault_seconds is not None:
        if fault_amps is None or fault_seconds is None:
            raise ToolError("give both fault_amps and fault_seconds")
        fault = (fault_amps, fault_seconds)
    rep = fuse_check(doc, pad, rating, i2t=i2t, fault=fault, sinks=sinks, ambient_c=ambient_c,
                     hold_factor=hold_factor, max_temp_c=max_temp_c, thermal=thermal)
    rep["passed"] = rep.pop("ok")
    return rep


def offline_pcb_fet_screen(board: str, currents: Dict[str, float],
                           flows: Optional[List[Any]] = None, fets: Optional[List[str]] = None,
                           warn: float = 50.0, fail: float = 100.0, res: float = 0.1) -> dict:
    """Current density where copper enters each power-FET pad, with a
    pass/warn/fail verdict per pad.

    Use on power boards with FETs carrying large currents. currents
    {"VBAT": 30}; flows (as offline_pcb_thermal) solve the net for a measured
    peak instead of an estimate; fets names the FETs (default: found by
    designator and pattern). warn/fail are A/mm2.
    """
    from offline import pdn as P
    from offline.power import fet_screen
    doc = _doc(board)
    sols = []
    cur = {str(k): float(v) for k, v in currents.items()}
    for f in flows or []:
        n, a, s, k = _flow(f)
        cur.setdefault(n, a)
        sols.append(P.solve(doc, n, s, k, a, res=res, temp_c=45.0))
    return fet_screen(doc, cur, pdn=sols, fets=fets, warn=warn, fail=fail)


def offline_pcb_notches(board: str, gap: float = 0.5, nets: Optional[List[str]] = None,
                        plan_out: Optional[str] = None) -> dict:
    """Same-net pours on one layer that nearly meet (a removed part often
    leaves this): the seam of bare board between them and its V-notch ends.

    Use when two pours of one net should be one, e.g. after an ECO. With
    plan_out it writes an EditPlan that reshapes the first polygon over the
    seam and merges the pair; apply it with offline_pcb_apply_plan, then
    repour.
    """
    from offline import notch
    doc = _doc(board)
    found = notch.find(doc, gap, nets)
    rep = {"gap_mm": gap, "pairs": [{k: v for k, v in f.items() if k not in ("ops", "outline")}
                                    for f in found]}
    if plan_out:
        if not found:
            raise ToolError("no notches found; nothing to plan")
        path = _out(plan_out, [board], what="plan_out")
        ops = [op for f in found for op in f["ops"]]
        rep["plan"] = _write_json(path, {"version": 1, "ops": ops})
        rep["ops"] = len(ops)
    return rep


def offline_pcb_stack_check(lower: str, upper: str, pairs: List[str], facing: bool = False,
                            tol: float = 0.1) -> dict:
    """Check two boards joined by mating headers: pins line up (within tol
    mm), mated pins carry matching nets, mounting holes align, stack height.

    Use for a board stacked on another. pairs: ["J7:J1", ...] as
    LOWER_PART:UPPER_PART; facing true when the upper board is upside down.
    """
    from offline.stack import check
    maps = []
    for m in pairs:
        if isinstance(m, (list, tuple)) and len(m) == 2:
            maps.append((str(m[0]), str(m[1])))
        elif isinstance(m, str) and ":" in m:
            maps.append(tuple(m.split(":", 1)))
        else:
            raise ToolError(f"pair {m!r}: use LOWER_PART:UPPER_PART")
    rep = check(_doc(lower, "lower"), _doc(upper, "upper"), maps, facing=facing, tol=tol)
    rep["passed"] = rep.pop("ok")
    return rep


def offline_pcb_fab_check(board: str, folder: str, tol: float = 0.02) -> dict:
    """Read a folder of Gerber (RS-274X/X2) and Excellon drill files back
    and compare them with the board: layers present, apertures, drill sizes
    and counts, every hole matched to the board within tol mm.

    Use after generating fabrication outputs, before sending them out.
    """
    from offline.gerber import check
    rep = check(_doc(board), _in(folder, "folder"), tol)
    rep["passed"] = rep.pop("ok")
    return rep


def offline_pcb_models3d(path: str, everything: bool = False) -> dict:
    """Check the STEP models embedded in a .PcbDoc or .PcbLib against their
    footprints: units, rotation, height, offset, whether the body covers the
    pads. Suggests a rotation/offset when one fits.

    Use when 3D bodies look wrong, or before a mechanical export.
    everything lists every model, not only the suspicious ones.
    """
    from offline import models3d as M
    p = _in(path, "path")
    rows = M.check_lib(p, everything) if p.suffix.lower() == ".pcblib" else \
        M.check_doc(_doc(p), everything)
    return {"models": rows, "problems": sum(1 for r in rows
                                            if r.get("findings") or r.get("error"))}


# ---------------------------------------------------------------- plan and write

def offline_pcb_apply_plan(board: str, plan: Any, out: Optional[str] = None,
                           overwrite: bool = False) -> dict:
    """Apply an EditPlan to a .PcbDoc offline and write the result to a new
    file (never the input). Without out it is a dry run that reports what
    each op would do.

    Use to make the edits planned by the other tools (eco_apply,
    copper_plan, notches, autoroute) or written by hand: plan is the plan
    object, its JSON text or a .json path, {"version": 1, "ops": [...]}.
    Ops include add_track, add_arc, add_via, add_fill, add_region,
    add_polygon, remove, move_component, rotate_component, flip_component,
    remove_component, place_footprint, swap_footprint, set_pad_net,
    rename_net, merge_nets, merge_polygons, set_polygon_outline, set_rule
    (see server/offline/editplan.py). After writing: repour
    (offline_pcb_repour) and check (offline_pcb_drc), then open in Altium.
    """
    from offline.editplan import apply
    p = _json_arg(plan, "plan")
    path = _out(out, [board], overwrite) if out else None
    doc = _doc(board)
    ed, results = apply(doc, p)
    if path is None:
        return {"dry_run": True, "results": results, **ed.report()}
    rep = ed.save(str(path))
    rep["results"] = results
    rep["written"] = str(path)
    return rep


def offline_pcb_live_scripts(board: str, plan: Any, board_path: str, out_dir: str) -> dict:
    """Turn an EditPlan into sandbox script bodies that make the same edits
    in the board open in Altium, one .pas per op plus a save script and a
    manifest.

    Use only when the edit must happen in the live Altium session instead
    of writing a new file. Run each body in manifest order with
    run_altium_script and stop at the first failure. board_path is the
    board's path as Altium has it open. Bodies marked unverified have not
    been proven against Altium.
    """
    from offline import live
    p = _json_arg(plan, "plan")
    doc = _doc(board)
    folder = _out_dir(out_dir)
    rep = live.write_scripts(p, board_path, str(folder), doc=doc)
    if not rep.get("ok"):
        raise ToolError(rep.get("error") or "live scripts could not be written")
    return rep


def _netlist(schematic):
    from offline import eco
    if isinstance(schematic, dict) or (isinstance(schematic, str) and
                                       schematic.strip().startswith("{")):
        return eco.load_netlist(_json_arg(schematic, "schematic"))
    return eco.load_netlist(str(_in(schematic, "schematic")))


def offline_pcb_eco_preview(schematic: Any, board: Optional[str] = None) -> dict:
    """Compare a schematic with a board, as Altium's Update PCB would:
    parts to add or remove, footprint and comment changes, net renames,
    merges, splits and pin moves.

    Use before offline_pcb_eco_apply, or to see whether a board matches its
    schematic. schematic: a .PrjPcb (its board is used when board is not
    given), a .SchDoc, a netlist .json, or a netlist object
    {"parts": {DES: {footprint, comment, unique_id, pins}}, "nets": {NAME:
    ["DES.PIN", ...]}}.
    """
    from offline import eco
    nl, prj = _netlist(schematic)
    bpath = board or (prj.pcb if prj is not None and prj.pcb else None)
    if not bpath:
        raise ToolError("no board: give board (the .PcbDoc)")
    return eco.preview(nl, _doc(bpath))


def offline_pcb_eco_apply(schematic: Any, board: Optional[str] = None,
                          libraries: Optional[List[str]] = None, out: Optional[str] = None,
                          plan_out: Optional[str] = None, remove_unlinked: bool = False,
                          overwrite: bool = False) -> dict:
    """Bring a board in line with its schematic offline: an EditPlan that
    removes dropped parts, merges and renames nets (tracks and pours follow),
    moves pins, swaps footprints and places new parts beside the board.

    Use after offline_pcb_eco_preview. libraries: .PcbLib/.PcbDoc files to
    take footprints from (a footprint not found is reported, never
    invented). out writes the changed board (and lists the gaps left to
    route); plan_out writes the plan; with neither, the plan is returned.
    Problems it could not handle come back in "problems".
    """
    from offline import eco
    from offline.editplan import apply
    nl, prj = _netlist(schematic)
    bpath = board or (prj.pcb if prj is not None and prj.pcb else None)
    if not bpath:
        raise ToolError("no board: give board (the .PcbDoc)")
    libs = [str(_in(x, "library")) for x in libraries or []]
    inputs = [bpath] + libs + ([schematic] if isinstance(schematic, str) and
                               not schematic.strip().startswith("{") else [])
    path = _out(out, inputs, overwrite) if out else None
    ppath = _out(plan_out, [bpath], what="plan_out") if plan_out else None
    doc = _doc(bpath)
    p, problems, pv = eco.plan(nl, doc, libs, remove_unlinked)
    rep = {"ops": len(p["ops"]), "problems": problems, "summary": pv["summary"]}
    if ppath:
        rep["plan"] = _write_json(ppath, p)
    if path:
        ed, results = apply(doc, p)
        saved = ed.save(str(path))
        rep["written"] = str(path)
        rep["warnings"] = saved.get("warnings", [])
        rep["stale_pours"] = saved.get("stale_pours", [])
        rep["gaps_after"] = eco.gaps(doc)
    if not ppath and not path:
        rep["plan"] = p
    return rep


def offline_pcb_copper_plan(board: str, plan: Any, plan_out: str,
                            png: Optional[str] = None) -> dict:
    """Plan power copper for a current: regions and via arrays on a net,
    grown from the free room around its terminals until drop, density and
    temperature targets are met, as an EditPlan.

    Use when a power path must carry a current (e.g. 30 A) and the copper
    is missing or too narrow. plan: {"net": "VBAT", "sources": ["J1.1"],
    "sinks": ["Q1.D"], "current": 30, "targets": {"drop_mv": 50,
    "density_a_per_mm2": 40, "rise_c": 30}, optional "layers", "area",
    "keepouts", "via": {"diameter", "hole", "in_pad"}, "res"} (full form in
    server/offline/copper.py). Writes the EditPlan to plan_out; apply it
    with offline_pcb_apply_plan, then repour. "met" false names the
    obstacle that limits it.
    """
    from offline.copper import Planner
    doc = _doc(board)
    p = _json_arg(plan, "plan")
    path = _out(plan_out, [board], what="plan_out")
    ipath = _out(png, [board], what="png") if png else None
    pl = Planner(doc, p)
    edit, report, built = pl.run()
    report["plan"] = _write_json(path, edit)
    report["ops"] = len(edit["ops"])
    if ipath:
        from offline.render import render
        render(built, str(ipath), nets=[p["net"]], box=pl._box(pl.corridor()))
        report["image"] = str(ipath)
    return report


def offline_pcb_repour(board: str, out: Optional[str] = None,
                       polygons: Optional[List[Any]] = None, mode: str = "clean",
                       store: str = "pour", compare: bool = False, dents: bool = False,
                       png: Optional[str] = None, overwrite: bool = False) -> dict:
    """Pour polygons offline, as Altium's Repour would, and optionally write
    the board with the new pours.

    Use after any edit that changes copper near a pour (apply_plan reports
    stale pours), and to check pours against the saved ones (compare). mode
    "clean" pours from scratch, "sequential" is Repour All from the saved
    copper, "saved" each polygon against the board as saved; dents lists
    where stale copper makes Repour All differ from a clean pour. store
    "pour" writes the copper as the polygons' own, "solid" as plain regions
    (polygon removed), "unpoured" strips it for Altium to repour. polygons
    are indexes or names (default all).
    """
    from offline.pour import PourEngine
    from offline import layers as L
    doc = _doc(board)
    path = _out(out, [board], overwrite) if out else None
    ipath = _out(png, [board], what="png") if png else None
    eng = PourEngine(doc)
    polys = [eng.polygon(n) for n in polygons] if polygons else None
    found = None
    if dents:
        found, results, _ = eng.dents(polys)
    else:
        results = eng.repour_all(mode, polys)
    rows = []
    for i, res in results.items():
        row = res.summary(doc)
        if compare:
            row["vs_saved"] = eng.compare(res)
        rows.append(row)
    rep = {"mode": "clean" if dents else mode, "polygons": rows}
    if found is not None:
        rep["dents"] = found
    if path or ipath:
        ed = eng.store(results, store)
        if path:
            saved = ed.save(str(path))
            rep["written"] = str(path)
            rep["stored_as"] = store
            rep["warnings"] = saved.get("warnings", [])
        if ipath:
            from offline.render import render
            render(doc, str(ipath), layers=sorted({L.name(results[i].layer) for i in results})
                   or None)
            rep["image"] = str(ipath)
    return rep


def offline_pcb_autoroute(board: str, plan_out: str, nets: Optional[List[str]] = None,
                          keep_fixed: Optional[List[str]] = None, include_power: bool = False,
                          check: bool = True, passes: int = 20, timeout: float = 900,
                          via_diameter: Optional[float] = None, via_hole: Optional[float] = None,
                          java: Optional[str] = None, freerouting: Optional[str] = None,
                          workdir: Optional[str] = None, neckdown: bool = False) -> dict:
    """Route open signal connections with Freerouting (Specctra DSN out,
    session back in) and return the new tracks and vias as an EditPlan.

    Use for signal nets left unrouted; power nets stay out unless named or
    include_power. nets: names or globs ("SIG*"); existing copper is kept
    fixed unless keep_fixed ["none"]. check runs DRC before and after in
    memory. Needs Java 25 and the Freerouting jar (python -m offline
    autoroute --fetch once). Can take minutes; timeout is seconds. Writes the
    plan to plan_out; apply it with offline_pcb_apply_plan.
    """
    from offline import autoroute as A
    from offline import specctra
    _in(board, "board")
    path = _out(plan_out, [board], what="plan_out")
    via = None
    if via_diameter is not None or via_hole is not None:
        if via_diameter is None or via_hole is None:
            raise ToolError("give both via_diameter and via_hole")
        via = (via_diameter, via_hole)
    wd = str(_out_dir(workdir, "workdir")) if workdir else None
    try:
        plan, rep = A.autoroute(str(board), nets, keep_fixed=keep_fixed,
                                include_power=include_power, via=via, passes=passes,
                                timeout=timeout, jar=freerouting, java=java, check=check,
                                workdir=wd, neckdown=neckdown)
    except (A.AutorouteError, specctra.SpecctraError) as e:
        raise ToolError(str(e))
    rep["plan"] = _write_json(path, plan)
    return rep


def offline_pcb_blank_board(template: str, out: str, width: Optional[float] = None,
                            height: Optional[float] = None, at: Optional[List[float]] = None,
                            corner: float = 0.0, outline: Optional[List[List[float]]] = None,
                            holes: Optional[List[List[float]]] = None,
                            plated_holes: Optional[List[List[float]]] = None,
                            overwrite: bool = False) -> dict:
    """A new empty board from a template board: its stack, rules and
    settings kept, its parts, nets and copper removed, a new outline and
    mounting holes added.

    Use to start a board without opening Altium. Give width and height (mm,
    corner = 45 degree cut, at = [x, y] lower-left, default the template's)
    or outline [[x, y], ...]. holes [[x, y, d], ...] unplated;
    plated_holes [[x, y, d, pad], ...].
    """
    from offline import blank as B
    tpath = _in(template, "template")
    path = _out(out, [tpath], overwrite)
    if outline:
        pts = [(float(x), float(y)) for x, y in outline]
    elif width and height:
        if at:
            x0, y0 = float(at[0]), float(at[1])
        else:
            t = _doc(tpath, "template").outline()
            x0, y0 = (min(p[0] for p in t), min(p[1] for p in t)) if t else (0.0, 0.0)
        pts = B.rect(width, height, x0, y0, corner)
    else:
        raise ToolError("give width and height, or outline")
    ed, rep = B.blank(str(tpath), pts, [tuple(h) for h in holes or []],
                      [tuple(h) for h in plated_holes or []])
    saved = ed.save(str(path))
    rep["written"] = str(path)
    rep["integrity"] = saved.get("integrity", saved.get("problems", []))
    return rep


def offline_pcb_assembly_page(board: str, out: str, bom: Optional[str] = None,
                              title: Optional[str] = None) -> dict:
    """Write an interactive hand-assembly page (one HTML file): board
    drawing, BOM lines, click a line to highlight its parts, tick them off.

    Use for hand assembly or rework. bom: a BOM CSV (designators and
    values); without it, lines are grouped by footprint and comment.
    """
    from offline.assembly import page, read_bom
    doc = _doc(board)
    path = _out(out, [board, bom])
    b = read_bom(_in(bom, "bom").read_text(encoding="utf-8-sig", errors="replace")) \
        if bom else None
    text, data = page(doc, b, title or Path(board).stem)
    path.write_text(text, encoding="utf-8")
    return {"written": str(path), "lines": len(data["lines"]), "parts": len(data["parts"]),
            "missing_from_board": data["missing_from_board"]}


def offline_pcb_membership_audit_script(board_path: str, fix: bool = False,
                                        limit: int = 200) -> dict:
    """A sandbox script body that lists, in the board open in Altium,
    primitives whose net disagrees with their net's member list (stale
    group membership), and with fix repairs them.

    Use when Altium's live tools see copper on the wrong net or nets that
    will not highlight. Run the returned script with run_altium_script with
    the board open. The script itself is unverified against Altium.
    """
    from offline.audit import membership_script
    return {"script": membership_script(board_path, fix=fix, limit=limit), "fix": fix,
            "proven": False, "run": "run_altium_script with this body; the board must be open"}


# ---------------------------------------------------------------- mechanical, ties, neck

def offline_pcb_holes(board: str, min_diameter: float = 0.0, mounting_only: bool = False) -> dict:
    """Drilled holes (pad barrels and vias) larger than min_diameter mm, with
    each one's distance to the board edge (cutouts count as edges).
    mounting_only lists only holes that belong to no component (board
    fasteners).

    Use when checking mounting holes, edge distances or drill sizes.
    """
    from offline import mech
    doc = _doc(board)
    rows = mech.mounting_holes(doc, min_diameter) if mounting_only else \
        mech.holes(doc, min_diameter)
    return {"holes": rows, "count": len(rows)}


def offline_pcb_fasteners(board: str, screw: str = "M4-pan", washer: bool = False,
                          head_diameter: Optional[float] = None,
                          edge_margin: float = 1.0) -> dict:
    """For each mounting hole (a hole of 3 mm or more in no component): how far
    the screw head or washer is from the board edge, how far the hole must
    move inward for edge_margin mm, and which other nets' copper (per outer
    layer) and which parts lie under the head.

    Use before fixing a board with screws. screw: M3-pan, M3-cap, M4-pan,
    M4-cap, M5-pan, M5-cap or M6-pan; head_diameter (mm) overrides it.
    """
    from offline import mech
    return {"holes": mech.fastener_report(_doc(board), screw, washer, head_diameter,
                                          edge_margin)}


def offline_pcb_edges(board: str, within: float = 3.0, silk_gap: float = 0.2) -> dict:
    """Parts whose pads come within `within` mm of the board edge, lie
    outside it or whose 3D body hangs over it, and silkscreen that crosses
    the edge, lies outside it or is closer than silk_gap mm (what Altium's
    Board Outline Clearance check flags for silkscreen).

    Use for an edge and outline review of a board file.
    """
    from offline import mech
    doc = _doc(board)
    return {"near_or_over_edge": mech.edge_items(doc, within),
            "silkscreen_outline_clearance": mech.outline_clearance(doc, silk_gap)}


def offline_pcb_rules_check(board: str) -> dict:
    """Rules whose scope names something that does not exist or is empty: a
    net class, pad class, component class, net, component or room. Such a
    branch never matches, so "(InNetClass('Power')) or IsVia" silently
    becomes "IsVia" when there is no class Power.

    Use when a rule seems not to apply, and as part of any rule review.
    """
    from offline.rules import dead_scopes
    rows = dead_scopes(_doc(board))
    return {"dead_scopes": rows, "count": len(rows)}


def offline_pcb_pad_ties(board: str, nets: List[str]) -> dict:
    """How each pad on the given nets is actually tied into its pour, measured
    from the saved copper: direct, direct_partial (merged, part of the edge
    cleared round another net), relief (spoke count and widths) or none,
    beside what the PolygonConnect rule says.

    Use when a pad's current path matters (power pads, thermal pads) or to
    find pours whose saved copper disagrees with the rules. Repour first
    (offline_pcb_repour) if the saved pours are stale.
    """
    from offline.padties import pad_connections
    return pad_connections(_doc(board), nets)


def offline_pcb_neck(board: str, net: str, sources: List[str], sinks: List[str],
                     res: float = 0.1, ignore: float = 3.0) -> dict:
    """The narrowest point of a net's copper on the widest route between two
    pad groups, per copper layer, with the IPC-2221 current that width
    carries (external layer, +10 and +20 C).

    Use to find the bottleneck of a power path quickly. sources/sinks are
    pads ("J1" = every pad of J1 on the net, or "J1.1"); copper within
    `ignore` mm of them is not judged. Layers are judged one at a time; for
    the whole net with vias use offline_pcb_pdn.
    """
    from offline.neck import neck_between
    return {"net": net, "layers": neck_between(_doc(board), net, sources, sinks, res, ignore)}


# ---------------------------------------------------------------- schematics

def _sch(path, kinds=(".prjpcb", ".schdoc")):
    from offline import schtools
    p = _in(path, "schematic")
    if p.suffix.lower() not in kinds:
        raise ToolError(f"{p.name}: expected {', '.join(kinds)}")
    return schtools.load_any(p)


def offline_sch_netlist(schematic: str, net: str = "", designator: str = "") -> dict:
    """The netlist of a saved schematic project (.PrjPcb, every sheet) or one
    .SchDoc: every net with its pins, and the components. Needs no Altium;
    unsaved edits are not in the files.

    Use to read a schematic's connectivity. net limits to nets whose name
    contains it; designator to nets touching that part.
    """
    from offline import schtools
    src = _sch(schematic)
    nl = src.netlist
    nets = []
    for n in nl.nets:
        if net and net.lower() not in n.name.lower():
            continue
        if designator and not any(p.designator == designator for p in n.pins):
            continue
        nets.append({"name": n.name, "auto_named": n.auto_named, "sheets": n.sheets,
                     "pins": ["%s.%s %s" % (p.designator, p.pin, p.pin_name) for p in n.pins]})
    comps = [{"designator": d, "comment": c["comment"], "sheet": c["sheet"]}
             for d, c in sorted(src.components.items(), key=lambda kv: schtools.natural_key(kv[0]))]
    return {"summary": nl.summary(), "nets": nets,
            "components": comps if not (net or designator) else None}


def offline_sch_check(schematic: str, include_info: bool = False) -> dict:
    """Schematic problems from the saved files: unconnected pins, single-pin
    nets, floating or empty net labels, floating power ports, nets with two
    names, duplicate or unannotated designators, nets driven by two outputs,
    wire ends inside another wire with no junction.

    Use to review a schematic before Update PCB. include_info adds info-level
    findings (stub wires, hierarchy not followed).
    """
    nl = _sch(schematic).netlist
    return {"summary": nl.summary(),
            "findings": [f.as_dict() for f in nl.findings
                         if include_info or f.severity != "info"]}


def offline_sch_compare(before: str, after: str, ignore_single_pin: bool = True) -> dict:
    """What changed between two netlists: components added, removed or changed,
    nets added, removed, renamed, split or merged, pins that joined or left a
    net. Nets are matched by the pins they share.

    Use to review a schematic edit (an old saved copy against the new one),
    to check a board matches its schematic (.PrjPcb against .PcbDoc), or to
    compare with Altium's compiled netlist (a NET| text dump). Each side is a
    .PrjPcb, .SchDoc, .PcbDoc or that .txt.
    """
    from offline import schtools
    kinds = (".prjpcb", ".schdoc", ".pcbdoc", ".txt")
    res = schtools.compare(_sch(before, kinds), _sch(after, kinds), ignore_single_pin)
    res["report"] = schtools.format_compare(res)
    return res


def offline_sch_bom(schematic: str) -> dict:
    """Bill of materials from a schematic: one row per part type with
    designators, quantity, comment, description, manufacturer and MPN where
    the parts carry them, and search_terms (MPN spellings worth trying in a
    parts search: packing suffix dropped, then the family stem).

    Use to build or check a BOM, or to look parts up.
    """
    from offline import schtools
    return {"lines": schtools.bom(_sch(schematic))}


def offline_sch_part(schematic: str, designator: str) -> dict:
    """One component's pins, the net on each and the other parts on that net
    (crowded nets such as ground summarised by count).

    Use to see how a part is wired, or which of its pins are unconnected.
    """
    from offline import schtools
    return schtools.part_view(_sch(schematic), designator)


def offline_sch_rules(schematic: str, rules: List[Any]) -> dict:
    """Check every instance of a circuit block against rules, e.g. that each
    high-side switch's sense pin has a resistor to ground and reaches an ADC.

    Use for design-rule reviews of repeated circuit blocks. Each rule:
    parts (regex on designator, comment, library reference, design item,
    description, MPN), pin (regex on pin names, or "#4" for pin number 4),
    expect ("connected", "net_name", "same_net", "resistor_to",
    "capacitor_to", "diode_to", "two_pin_to" or "connects_to"), net (regex
    for net_name or the far end), prefix (designator prefix for two_pin_to),
    part (regex for connects_to), via (designator prefix of one series part
    connects_to may pass), name. Example: [{"parts": "HSW", "pin": "IS",
    "expect": "resistor_to", "net": "GND"}, {"parts": "HSW", "pin": "IS",
    "expect": "connects_to", "part": "ADC", "via": "R"}].
    """
    from offline import schtools
    results = schtools.check_rules(_sch(schematic), _json_arg(rules, "rules"))
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"counts": counts, "results": results}


def offline_sch_footprints(project: str, board: Optional[str] = None,
                           extra_libs: Optional[List[str]] = None) -> dict:
    """Before Design > Update PCB Document: for every part, a current PCB
    footprint, found in the project's PcbLibs (or the board, or extra_libs),
    and a pad for every pin. With a board (the project's own by default),
    previews the update matching parts by unique ID as Altium does: parts
    added and removed, footprint swaps, renames, board parts with no link.

    Use before every Update PCB. extra_libs: .PcbLib files or folders, e.g.
    the copies Altium keeps of released workspace footprints.
    """
    from offline import fpcheck
    prj = _in(project, "project")
    if prj.suffix.lower() != ".prjpcb":
        raise ToolError(f"{prj.name}: expected a .PrjPcb")
    libs = [str(_in(x, "extra_libs")) for x in extra_libs or []]
    res = fpcheck.check(prj, str(_in(board, "board")) if board else None, libs)
    res["report"] = fpcheck.format_check(res)
    return res


# ---------------------------------------------------------------- calculators

def calc_pwm_ripple(volts: float, resistance_ohm: float, inductance_h: float,
                    frequency_hz: float, duty: float = 0.5) -> dict:
    """Steady-state current in an R-L load (a pump or a solenoid) chopped by
    PWM with a freewheel path: average, minimum, maximum, peak-to-peak and
    the period against L/R.

    Use to choose a PWM frequency: near the load's L/R pole the current
    collapses every cycle. inductance in henries (550 uH = 0.00055).
    """
    from offline import calc
    return calc.rl_ripple(volts, resistance_ohm, inductance_h, frequency_hz, duty)


def calc_profet(part: str, amps: float, ambient_c: float = 85.0,
                rth_ja_k_per_w: Optional[float] = None,
                pwm_hz: Optional[float] = None) -> dict:
    """Junction temperature, continuous-current limit and PWM cost of an
    Infineon PROFET+2 channel (BTS7008-1EPP or BTS7008-2EPA), from datasheet
    values.

    Use when sizing a high-side switch channel. The default junction-to-
    ambient is the datasheet's 4-layer JEDEC figure, which a 2-layer board
    will not reach: pass your own rth_ja_k_per_w.
    """
    from offline import calc
    if part not in calc.PROFETS:
        raise ToolError(f"unknown part {part!r}; known: {sorted(calc.PROFETS)}")
    return {"steady": calc.profet_operating_point(part, amps, ambient_c, rth_ja_k_per_w),
            "max_continuous_a": calc.max_current_for_tj(part, ambient_c, rth_ja_k_per_w),
            "pwm": calc.profet_pwm(part, pwm_hz, amps, t_amb=ambient_c, rth_ja=rth_ja_k_per_w)
            if pwm_hz else None, "datasheet": calc.PROFETS[part]}


def calc_profet_network(part: str, sense_full_scale_a: Optional[float] = None,
                        adc_full_scale_v: Optional[float] = None,
                        input_high_v: Optional[float] = None,
                        input_series_ohm: Optional[float] = None,
                        pulse_peak_a: Optional[float] = None, pulse_s: Optional[float] = None,
                        load_inductance_h: Optional[float] = None) -> dict:
    """Size the parts around a PROFET+2 channel from datasheet values: the
    sense resistor for an ADC full scale, the current into the IN pin's 7 V
    clamp from a higher drive signal, and whether a short high-current pulse
    stays under the overload threshold (with its flyback energy against the
    clamp rating where the datasheet gives one).

    Use when designing a channel's sense, input and pulse handling. Give only
    the groups you need.
    """
    from offline import calc
    if part not in calc.PROFETS:
        raise ToolError(f"unknown part {part!r}; known: {sorted(calc.PROFETS)}")
    out = {"part": part}
    if sense_full_scale_a and adc_full_scale_v:
        out["sense_resistor"] = calc.sense_resistor(part, sense_full_scale_a, adc_full_scale_v)
    if input_high_v is not None and input_series_ohm:
        out["input"] = calc.input_network(input_high_v, input_series_ohm)
    if pulse_peak_a and pulse_s:
        out["pulse"] = calc.solenoid_pulse(part, pulse_peak_a, pulse_s, load_inductance_h)
    if len(out) == 1:
        raise ToolError("give at least one group: sense, input or pulse")
    return out


def calc_mosfet_stage(rds_hot_mohm: float, qg_nc: float, qgd_nc: float, v_plateau: float,
                      v_drive: float, r_gate_on_ohm: float, r_gate_off_ohm: float,
                      load_a: float, bus_v: float, pwm_hz: float, duty: float,
                      qgs2_nc: float = 0.0, diode_vf: Optional[float] = None,
                      diode_qrr_nc: float = 0.0, rth_ja_k_per_w: Optional[float] = None,
                      ambient_c: Optional[float] = None) -> dict:
    """Loss budget of a discrete PWM switch driving an inductive load:
    switching (Miller-plateau model), conduction, gate drive and, with
    diode_vf, the freewheel diode.

    Use to size a FET and its gate drive for a PWM load. Switching loss leaves
    out Coss and reverse recovery, so it is a floor. Give Rds(on) at the
    junction temperature you expect.
    """
    from offline import calc
    out = {"fet": calc.mosfet_stage(rds_hot_mohm, qg_nc, qgd_nc, v_plateau, v_drive,
                                    r_gate_on_ohm, r_gate_off_ohm, load_a, bus_v, pwm_hz, duty,
                                    qgs2_nc, rth_ja_k_per_w, ambient_c)}
    if diode_vf is not None:
        out["diode"] = calc.freewheel_diode(diode_vf, load_a, duty, diode_qrr_nc, bus_v, pwm_hz)
        out["stage_total_w"] = round(out["fet"]["p_fet_w"] + out["diode"]["p_diode_w"], 3)
    return out


# ---------------------------------------------------------------- MCP

TOOLS = [
    offline_pcb_info, offline_pcb_rules, offline_pcb_connectivity, offline_pcb_net_gaps,
    offline_pcb_render, offline_pcb_drc, offline_pcb_pdn, offline_pcb_review_power,
    offline_pcb_thermal, offline_pcb_via_array, offline_pcb_fuse_check, offline_pcb_fet_screen,
    offline_pcb_notches, offline_pcb_stack_check, offline_pcb_fab_check, offline_pcb_models3d,
    offline_pcb_apply_plan, offline_pcb_live_scripts, offline_pcb_eco_preview,
    offline_pcb_eco_apply, offline_pcb_copper_plan, offline_pcb_repour, offline_pcb_autoroute,
    offline_pcb_blank_board, offline_pcb_assembly_page, offline_pcb_membership_audit_script,
    offline_pcb_holes, offline_pcb_fasteners, offline_pcb_edges, offline_pcb_rules_check,
    offline_pcb_pad_ties, offline_pcb_neck,
    offline_sch_netlist, offline_sch_check, offline_sch_compare, offline_sch_bom, offline_sch_part,
    offline_sch_rules, offline_sch_footprints,
    calc_pwm_ripple, calc_profet, calc_profet_network, calc_mosfet_stage,
]


def _respond(result, image_types=None):
    """JSON text, plus the image when the result asks to show a small PNG."""
    text = json.dumps(result, indent=1)
    img = result.get("image") if result.get("ok") and result.get("show") else None
    if not img or not str(img).lower().endswith(".png") or image_types is None:
        return text
    p = Path(img)
    if not p.exists() or p.stat().st_size > INLINE_IMAGE_BYTES:
        return text
    return [text, image_types(path=str(p))]


def _as_tool(fn, image_types):
    import asyncio
    import inspect
    sig = inspect.signature(fn)
    shows = "show" in sig.parameters

    async def tool(**kwargs):
        result = await asyncio.to_thread(call, fn, **kwargs)
        return _respond(result, image_types if shows else None)

    tool.__name__ = fn.__name__
    tool.__qualname__ = fn.__name__
    tool.__doc__ = fn.__doc__
    tool.__module__ = fn.__module__
    # a tool that may answer with an image has no return type, so newer
    # FastMCP versions do not wrap its answer as structured text
    tool.__signature__ = sig.replace(
        return_annotation=inspect.Signature.empty if shows else str)
    return tool


def register(mcp):
    """Add every offline_pcb_* tool to a FastMCP server; returns their names.
    Set ALTIUM_MCP_OFFLINE_TOOLS=0 to leave them out."""
    if os.environ.get("ALTIUM_MCP_OFFLINE_TOOLS", "1").strip() == "0":
        log.info("offline PCB tools disabled by ALTIUM_MCP_OFFLINE_TOOLS=0")
        return []
    try:
        from mcp.server.fastmcp.utilities.types import Image
    except ImportError:
        Image = None
    import inspect
    names = []
    for fn in TOOLS:
        mcp.add_tool(_as_tool(fn, Image), name=fn.__name__,
                     description=inspect.cleandoc(fn.__doc__))
        names.append(fn.__name__)
    return names

