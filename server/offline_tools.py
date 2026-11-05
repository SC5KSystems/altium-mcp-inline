"""Offline PCB tools for the MCP server: the offline_pcb_* tools.

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


# ---------------------------------------------------------------- MCP

TOOLS = [
    offline_pcb_info, offline_pcb_rules, offline_pcb_connectivity, offline_pcb_net_gaps,
    offline_pcb_render, offline_pcb_drc, offline_pcb_pdn, offline_pcb_review_power,
    offline_pcb_thermal, offline_pcb_via_array, offline_pcb_fuse_check, offline_pcb_fet_screen,
    offline_pcb_notches, offline_pcb_stack_check, offline_pcb_fab_check, offline_pcb_models3d,
    offline_pcb_apply_plan, offline_pcb_live_scripts, offline_pcb_eco_preview,
    offline_pcb_eco_apply, offline_pcb_copper_plan, offline_pcb_repour, offline_pcb_autoroute,
    offline_pcb_blank_board, offline_pcb_assembly_page, offline_pcb_membership_audit_script,
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

