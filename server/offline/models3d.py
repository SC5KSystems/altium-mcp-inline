"""3D model sanity: does each embedded STEP model sit on its footprint the
way the part sits on the board?

    python -m offline models3d board.PcbDoc|parts.PcbLib [--json] [--all]

A PcbDoc keeps embedded models in Models/N (a PcbLib in Library/Models/N),
zlib-compressed STEP, listed in .../Models/Data. Each 3D body names its
model (MODELID) and places it: MODEL.2D.X/Y on the board, MODEL.3D.ROTX,
ROTY, ROTZ (degrees, applied about X, then Y, then Z) and MODEL.3D.DZ (lift).
For every body with a STEP model the tool takes the model's box from its
points: VERTEX_POINT positions lie on the surface (a lower bound),
every CARTESIAN_POINT includes B-spline control points that stand off the
surface (an upper bound), so both are reported. The body's own projected
outline (what Altium computed when the model was placed) is the cross-check
for the footprint.

Flags: a model that stands on end (its height is its longest side and it is
thinner across than the land pattern, with the 90 degree turn that would lay
it flat and the lift that puts its underside on the board); a model that
does not cover its pads; a height far from what its package class allows,
or from the body's declared OVERALLHEIGHT. Screening only.
"""

import json
import math
import re
import zlib

import numpy as np

from . import geom
from . import layers as L
from .pcbdoc import parse_length, split_props

PACKAGE_HEIGHT = [  # (pattern regex, min mm, max mm): typical seated heights
    (r"0201", 0.2, 0.35), (r"0402", 0.3, 0.6), (r"0603", 0.4, 1.0), (r"0805", 0.5, 1.4),
    (r"1206", 0.5, 1.8), (r"1210|1812|2010|2512", 0.5, 2.8),
    (r"SOT-?23|SOT-?323|SC-?70", 0.75, 1.45), (r"SOT-?223", 1.5, 1.9),
    (r"SOD-?123|SOD-?323", 0.7, 1.35), (r"SMA|SMB|SMC|DO-?214", 1.9, 2.7),
    (r"POWERPAK|SO-?8FL|LFPAK|PQFN|DFN5X6|TDSON|SON5X6", 0.7, 1.3),
    (r"QFN|DFN|SON|LGA|WSON|VQFN", 0.4, 1.1), (r"TSSOP|MSOP|VSSOP", 0.8, 1.25),
    (r"SOIC|SO-?8|SO-?14|SO-?16", 1.2, 1.8), (r"D2PAK|TO-?263", 4.0, 4.9),
    (r"DPAK|TO-?252", 2.0, 2.5), (r"TO-?220", 8.0, 20.0), (r"BGA", 0.5, 2.5),
]


class ModelError(ValueError):
    pass


# ---------------------------------------------------------------- STEP

SI_PREFIX = {"$": 1000.0, ".MILLI.": 1.0, ".CENTI.": 10.0, ".MICRO.": 0.001, ".DECI.": 100.0}
UNIT_NAMES = {"INCH": 25.4, "MIL": 0.0254, "THOU": 0.0254, "FOOT": 304.8, "MILLIMETRE": 1.0,
              "MILLIMETER": 1.0, "CENTIMETRE": 10.0, "METRE": 1000.0, "METER": 1000.0}


def _si(entity):
    m = re.search(r"SI_UNIT\s*\(\s*(\$|\.[A-Z]+\.)\s*,\s*\.METRE\.", entity, re.I)
    return SI_PREFIX.get(m.group(1).upper()) if m else None


def length_scale(text):
    """mm per STEP length unit: the LENGTH_UNIT entity, a conversion-based
    unit by its own measure (times its base SI unit), else the SI prefix."""
    ents = {}
    for m in re.finditer(r"#(\d+)\s*=\s*(.*?);", text, re.S):
        ents[m.group(1)] = m.group(2)
    si_scale = None
    for e in ents.values():
        if "LENGTH_UNIT" not in e:
            continue
        m = re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'([^']*)'\s*,\s*#(\d+)", e, re.S)
        if m:
            meas = ents.get(m.group(2), "")
            v = re.search(r"LENGTH_MEASURE\s*\(\s*([-+\d.Ee]+)\s*\)\s*,\s*#(\d+)", meas)
            if v:
                base = _si(ents.get(v.group(2), ""))
                if base:
                    return float(v.group(1)) * base
            return UNIT_NAMES.get(m.group(1).strip().upper(), 1.0)
        if si_scale is None:
            si_scale = _si(e)
    return si_scale or 1.0


def step_points(text):
    """(all CARTESIAN_POINTs, VERTEX_POINT positions) as N x 3 arrays in mm."""
    scale = length_scale(text)
    num = r"([-+]?[\d.]+(?:[Ee][-+]?\d+)?)"
    pts = {}
    for m in re.finditer(r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*" + num +
                         r"\s*,\s*" + num + r"\s*,\s*" + num + r"\s*\)", text):
        pts[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
    if not pts:
        raise ModelError("no CARTESIAN_POINT in the model")
    verts = [pts[m.group(1)] for m in re.finditer(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)",
                                                  text) if m.group(1) in pts]
    a = np.array(list(pts.values()), float) * scale
    v = np.array(verts, float) * scale if verts else a
    return a, v


def rotation(rx, ry, rz):
    """X, then Y, then Z, degrees."""
    def r(axis, deg):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        if axis == 0:
            return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
        if axis == 1:
            return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    return r(2, rz) @ r(1, ry) @ r(0, rx)


def placed_box(points, rx, ry, rz, dz, x0=0.0, y0=0.0, bottom=False):
    """(xmin, ymin, zmin, xmax, ymax, zmax) of points rotated, lifted by dz
    and moved to (x0, y0); a bottom-side part is flipped through the board."""
    p = points @ rotation(rx, ry, rz).T
    p[:, 2] += dz
    if bottom:
        p[:, 0] *= -1
        p[:, 2] *= -1
    p[:, 0] += x0
    p[:, 1] += y0
    return tuple(float(v) for v in np.concatenate([p.min(0), p.max(0)]))


# ---------------------------------------------------------------- reading files

def _model_records(data):
    return list(split_props(data, 0))


def models_of(read, names, prefix):
    """{model ID (upper): {"name", "index", "step"}} from a container."""
    data_key = f"{prefix}Models/Data"
    if data_key not in names:
        return {}
    out = {}
    for k, r in enumerate(_model_records(read(data_key))):
        mid = (r.get("ID") or "").upper()
        key = f"{prefix}Models/{k}"
        if not mid or key not in names:
            continue
        out[mid] = {"name": r.text("NAME") if hasattr(r, "text") else r.get("NAME"), "index": k,
                    "key": key, "rot": (float(r.get("ROTX") or 0), float(r.get("ROTY") or 0),
                                        float(r.get("ROTZ") or 0))}
    return out


def _step_text(read, key):
    raw = read(key)
    try:
        raw = zlib.decompress(raw)
    except zlib.error:
        pass
    return raw.decode("latin-1", errors="replace")


# ---------------------------------------------------------------- checks

def is_assembly(text):
    """A STEP assembly places its parts with transforms, so its raw points
    are in each part's own frame and their box means little."""
    return bool(re.search(r"NEXT_ASSEMBLY_USAGE_OCCURRENCE|ITEM_DEFINED_TRANSFORMATION|"
                          r"REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION", text))


def check_body(body_props, model_pts, pads_box, pads, side_bottom, pattern="", outline=None,
               assembly=False):
    """Findings for one body. pads_box: (x0, y0, x1, y1) of its pads;
    outline: the body's projected outline (Altium's, from the placed model).
    The outline gives the footprint, the declared OVERALLHEIGHT the height;
    the STEP points cross-check both unless the model is an assembly."""
    p = body_props
    rx = float(p.get("MODEL.3D.ROTX") or 0)
    ry = float(p.get("MODEL.3D.ROTY") or 0)
    rz = float(p.get("MODEL.3D.ROTZ") or 0)
    dz = parse_length(p.get("MODEL.3D.DZ"))
    x0 = parse_length(p.get("MODEL.2D.X"))
    y0 = parse_length(p.get("MODEL.2D.Y"))
    allp, verts = model_pts
    hi = placed_box(allp, rx, ry, rz, dz, x0, y0, side_bottom)
    lo = placed_box(verts, rx, ry, rz, dz, x0, y0, side_bottom)
    step_h = (lo[5] if not side_bottom else -lo[2])
    declared = parse_length(p.get("OVERALLHEIGHT"))
    height = declared if declared > 0.05 else step_h
    out = {"rotation": [rx, ry, rz], "dz_mm": round(dz, 4),
           "box_vertices": [round(v, 3) for v in lo], "box_all_points": [round(v, 3) for v in hi],
           "height_mm": round(height, 3), "step_height_mm": round(step_h, 3),
           "assembly": assembly, "findings": []}
    if outline is not None and not outline.is_empty:
        fb = outline.bounds
        fp_src = "body outline"
    else:
        fb = (lo[0], lo[1], lo[3], lo[4])
        fp_src = "model points"
    out["footprint_from"] = fp_src
    ex, ey = fb[2] - fb[0], fb[3] - fb[1]
    if pads_box:
        px, py = pads_box[2] - pads_box[0], pads_box[3] - pads_box[1]
        inside = sum(1 for (x, y) in pads
                     if fb[0] - 0.5 <= x <= fb[2] + 0.5 and fb[1] - 0.5 <= y <= fb[3] + 0.5)
        if pads and inside < 0.5 * len(pads):
            out["findings"].append(f"covers only {inside} of {len(pads)} pads ({fp_src})")
        thin = min(ex, ey)
        if height > max(ex, ey) * 0.98 and height > 2.0 and thin < 0.6 * min(px, py):
            out["findings"].append(f"stands on end: {height:.2f} mm tall but {thin:.2f} mm across "
                                   f"where the pads span {min(px, py):.2f} mm")
            fix = None if assembly else _lay_flat(allp, verts, rx, ry, rz, pads_box, side_bottom)
            if fix:
                out["suggest"] = fix
    cls = _package_class(pattern)
    if cls:
        lo_h, hi_h = cls[1], cls[2]
        out["package_class"] = {"match": cls[0], "min_mm": lo_h, "max_mm": hi_h}
        if height > hi_h * 1.5 or height < lo_h * 0.5:
            out["findings"].append(f"height {height:.2f} mm is far from its class "
                                   f"({lo_h}-{hi_h} mm for {cls[0]})")
    if not assembly and declared > 0.05 and step_h > 0 and \
            abs(declared - step_h) > max(0.3 * declared, 0.2):
        out["findings"].append(f"the body says {declared:.2f} mm high, the model's points "
                               f"reach {step_h:.2f} mm")
    return out


def _lay_flat(allp, verts, rx, ry, rz, pads_box, bottom):
    """The extra quarter turn about X or Y that best matches the land
    pattern, and the lift that puts the model's underside on the board."""
    px, py = pads_box[2] - pads_box[0], pads_box[3] - pads_box[1]
    best = None
    for dx_, dy_ in ((90, 0), (-90, 0), (0, 90), (0, -90)):
        b = placed_box(verts, rx + dx_, ry + dy_, rz, 0.0)
        ex, ey, ez = b[3] - b[0], b[4] - b[1], b[5] - b[2]
        score = abs(ex - px) + abs(ey - py) + ez
        if best is None or score < best[0]:
            best = (score, dx_, dy_, -b[2], ez)
    if best is None:
        return None
    _s, dx_, dy_, lift, ez = best
    return {"rotx": round((rx + dx_) % 360, 3), "roty": round((ry + dy_) % 360, 3),
            "rotz": rz, "dz_mm": round(lift, 4), "height_after_mm": round(ez, 3),
            "note": "then check the lead tips meet the assembly outline"}


def _package_class(pattern):
    for rx, lo, hi in PACKAGE_HEIGHT:
        m = re.search(rx, pattern or "", re.I)
        if m:
            return (m.group(0), lo, hi)
    return None


# ---------------------------------------------------------------- whole files

def check_doc(doc, everything=False):
    """[{component, model, ...}] for a PcbDoc."""
    names = set(doc.streams)
    models = models_of(lambda k: doc.streams[k], names, "")
    cache = {}
    out = []
    for b in doc.bodies:
        mid = (b.props.get("MODELID") or "").upper()
        if mid not in models:
            continue
        comp = doc.components[b.component] if b.component is not None else None
        pads = [p for p in doc.pads if comp is not None and p.component == comp.index]
        if mid not in cache:
            try:
                text = _step_text(lambda k: doc.streams[k], models[mid]["key"])
                cache[mid] = (step_points(text), is_assembly(text))
            except ModelError as e:
                cache[mid] = e
        pts, asm = cache[mid] if not isinstance(cache[mid], Exception) else (cache[mid], False)
        row = {"component": comp.designator if comp else None,
               "pattern": comp.pattern if comp else None, "model": models[mid]["name"]}
        if isinstance(pts, Exception):
            row["error"] = str(pts)
            out.append(row)
            continue
        box = _pads_box(pads)
        try:
            outline = geom.region_shape(b)
        except Exception:     # noqa: BLE001  (a body outline we cannot read is not fatal)
            outline = None
        row.update(check_body(b.props, pts, box, [(p.x, p.y) for p in pads],
                              comp is not None and comp.layer == L.BOTTOM,
                              comp.pattern if comp else "", outline, asm))
        if everything or row["findings"]:
            out.append(row)
    return out


def check_lib(path, everything=False):
    from . import footprints as F
    lib = F.PcbLib.open(path)
    names = set(lib.cf.streams())
    models = models_of(lib.cf.read, names, "Library/")
    cache = {}
    out = []
    for name in lib.names():
        fp = lib.footprint(name)
        pads = [r for r in fp.records if r.KIND == "pad"]
        for b in (r for r in fp.records if r.KIND == "body"):
            mid = (b.props.get("MODELID") or "").upper()
            if mid not in models:
                continue
            if mid not in cache:
                try:
                    text = _step_text(lib.cf.read, models[mid]["key"])
                    cache[mid] = (step_points(text), is_assembly(text))
                except ModelError as e:
                    cache[mid] = e
            row = {"footprint": name, "model": models[mid]["name"]}
            if isinstance(cache[mid], Exception):
                row["error"] = str(cache[mid])
                out.append(row)
                continue
            try:
                outline = geom.region_shape(b)
            except Exception:     # noqa: BLE001
                outline = None
            row.update(check_body(b.props, cache[mid][0], _pads_box(pads),
                                  [(p.x, p.y) for p in pads], False, name, outline,
                                  cache[mid][1]))
            if everything or row["findings"]:
                out.append(row)
    return out


def _pads_box(pads):
    if not pads:
        return None
    shapes = [geom.pad_shape(p, p.layer if p.layer != L.MULTI else L.TOP) for p in pads]
    xs0 = [s.bounds for s in shapes if not s.is_empty]
    if not xs0:
        return None
    return (min(b[0] for b in xs0), min(b[1] for b in xs0), max(b[2] for b in xs0),
            max(b[3] for b in xs0))


def cmd_models3d(args):
    from pathlib import Path
    from .pcbdoc import PcbDoc
    p = Path(args.file)
    rows = check_lib(p, args.all) if p.suffix.lower() == ".pcblib" else \
        check_doc(PcbDoc.open(p), args.all)
    if args.json:
        print(json.dumps(rows, indent=1))
    else:
        if not rows:
            print("no embedded STEP model looks wrong" if not args.all else "no embedded models")
        for r in rows:
            who = r.get("component") or r.get("footprint")
            if r.get("error"):
                print(f"{who}: {r['model']}: {r['error']}")
                continue
            print(f"{who} ({r.get('pattern') or ''}) {r['model']}: {r['height_mm']} mm high, "
                  f"rot {r['rotation']}")
            for f in r["findings"]:
                print(f"  - {f}")
            if r.get("suggest"):
                s = r["suggest"]
                print(f"  try RotX {s['rotx']}, RotY {s['roty']}, RotZ {s['rotz']}, DZ "
                      f"{s['dz_mm']} mm ({s['height_after_mm']} mm high); {s['note']}")
    return 1 if any(r.get("findings") or r.get("error") for r in rows) else 0


def register_commands(command):
    def build(p):
        p.add_argument("file", help="a PcbDoc or PcbLib")
        p.add_argument("--all", action="store_true", help="list every model, not only problems")
        p.add_argument("--json", action="store_true")
    command("models3d", cmd_models3d, "check embedded STEP models against their footprints",
            build)
