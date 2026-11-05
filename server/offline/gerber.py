"""Gerber and drill read-back: parse fabrication outputs, compare them with
the PcbDoc, and say whether the set is ready to send.

    python -m offline fab-check board.PcbDoc outputs/ [--json] [--tol 0.02]

Reads RS-274X Gerber (X2 attributes when present) and Excellon drill
files from a folder, identifies each layer by its X2 FileFunction or, for
Altium's names, by extension (.GTL .GBL .G1.. .GTO .GBO .GTS .GBS .GTP .GBP
.GKO .GM1.., .TXT/.DRL drill), and checks:

- layers the board needs but the set lacks: each copper layer, solder
  mask and silkscreen where the board has objects there, paste where it
  has SMD pads, an outline, and drill when it has holes;
- drill: hole sizes and counts per size against the board's pads and vias
  (plated and unplated apart when the files say), and the hole positions
  once the files' origin is found (offset, and a mirror if any);
- copper: flashes per copper layer against the pads there, and every image
  inside the board outline;
- format notes: units, coordinate format, zero-size apertures, empty files.

Exit status 1 when anything is missing or does not match.
"""

import json
import math
import re
from pathlib import Path

from . import layers as L

EXT = {".gtl": ("copper", "top"), ".gbl": ("copper", "bottom"), ".gto": ("silk", "top"),
       ".gbo": ("silk", "bottom"), ".gts": ("mask", "top"), ".gbs": ("mask", "bottom"),
       ".gtp": ("paste", "top"), ".gbp": ("paste", "bottom"), ".gko": ("outline", None),
       ".gm1": ("mechanical", 1), ".gml": ("outline", None), ".gpt": ("pad master", "top"),
       ".gpb": ("pad master", "bottom"), ".gd1": ("drill drawing", None),
       ".gg1": ("drill guide", None)}
DRILL_EXT = (".txt", ".drl", ".xln", ".exc", ".drd", ".tap")


class GerberError(ValueError):
    pass


# ---------------------------------------------------------------- Gerber

def _num(tok, fmt, unit_mm):
    """A coordinate token to mm, by the FS format (leading zeros omitted)."""
    if "." in tok:
        v = float(tok)
    else:
        neg = tok.startswith("-")
        digits = tok.lstrip("+-")
        _i, d = fmt
        v = int(digits or "0") / (10 ** d)
        v = -v if neg else v
    return v * unit_mm


def parse_gerber(text, name=""):
    """{units, format, function, side, apertures, flashes, draws, arcs,
    regions, box, zero_apertures, x2} from RS-274X text."""
    out = {"file": name, "units": None, "format": None, "function": None, "side": None,
           "apertures": {}, "flashes": 0, "flash_by_aperture": {}, "draws": 0, "arcs": 0,
           "regions": 0, "box": None, "zero_apertures": [], "x2": False, "min_draw_mm": None}
    fmt, unit = (2, 6), 1.0
    trailing = False
    x = y = 0.0
    ap = None
    interp = "G01"
    in_region = False
    box = [math.inf, math.inf, -math.inf, -math.inf]

    def grow(px, py, r=0.0, ry=None):
        ry = r if ry is None else ry
        box[0] = min(box[0], px - r)
        box[1] = min(box[1], py - ry)
        box[2] = max(box[2], px + r)
        box[3] = max(box[3], py + ry)

    # split into extended (%...%) blocks and plain '*' commands
    for m in re.finditer(r"%([^%]*)%|([^%*]*)\*", text):
        ext, cmd = m.group(1), m.group(2)
        if ext is not None:
            for part in ext.split("*"):
                part = part.strip()
                if not part:
                    continue
                if part.startswith("FS"):
                    mm_ = re.match(r"FS([LT])([AI])X(\d)(\d)Y(\d)(\d)", part)
                    if mm_:
                        trailing = mm_.group(1) == "T"
                        fmt = (int(mm_.group(3)), int(mm_.group(4)))
                        out["format"] = f"{mm_.group(3)}.{mm_.group(4)}" + \
                            (" trailing zeros" if trailing else "")
                elif part.startswith("MO"):
                    out["units"] = "mm" if "MM" in part else "inch"
                    unit = 1.0 if "MM" in part else 25.4
                elif part.startswith("AD"):
                    mm_ = re.match(r"ADD(\d+)([A-Za-z_][\w.$]*),?(.*)", part)
                    if mm_:
                        code, shape, params = int(mm_.group(1)), mm_.group(2), mm_.group(3)
                        vals = [float(v) for v in re.findall(r"-?[\d.]+", params)]
                        size = max(vals[:2]) * unit if vals else 0.0
                        sx = vals[0] * unit if vals else 0.0
                        sy = (vals[1] * unit if len(vals) > 1 and shape in ("R", "O") else sx)
                        out["apertures"][code] = {"shape": shape, "size_mm": round(size, 5),
                                                  "sx": sx, "sy": sy}
                        if shape in ("C", "R", "O", "P") and vals and max(vals[:2]) == 0:
                            out["zero_apertures"].append(code)
                elif part.startswith("TF.FileFunction"):
                    out["x2"] = True
                    f = part.split(",")[1:]
                    out["function"] = f[0].lower() if f else None
                    if f and f[0].lower() == "copper":
                        out["side"] = (f[2].lower() if len(f) > 2 else None)
                        out["copper_index"] = f[1] if len(f) > 1 else None
                    elif len(f) > 1:
                        out["side"] = f[1].lower()
                    if out["function"] == "profile":
                        out["function"] = "outline"
                    if out["function"] == "soldermask":
                        out["function"] = "mask"
                    if out["function"] == "legend":
                        out["function"] = "silk"
                    if out["function"] == "paste":
                        out["function"] = "paste"
                elif part.startswith("TF"):
                    out["x2"] = True
            continue
        cmd = (cmd or "").strip()
        if not cmd or cmd.startswith("G04"):
            continue
        if cmd in ("M02", "M00"):
            break
        for g in re.findall(r"G0?(\d+)", cmd):
            g = int(g)
            if g in (1, 2, 3):
                interp = f"G0{g}"
            elif g == 36:
                in_region = True
            elif g == 37:
                in_region = False
                out["regions"] += 1
            elif g == 70:
                unit, out["units"] = 25.4, out["units"] or "inch"
            elif g == 71:
                unit, out["units"] = 1.0, out["units"] or "mm"
        sel = re.fullmatch(r"(?:G54)?D(\d+)", cmd)
        if sel and int(sel.group(1)) >= 10:
            ap = int(sel.group(1))
            continue
        cm = re.findall(r"([XYIJ])([+-]?[\d.]+)", cmd)
        dm = re.search(r"D0?([123])$", cmd)
        if not cm and not dm:
            continue
        nx, ny = x, y
        for k, v in cm:
            if trailing and "." not in v:
                v = v.ljust(sum(fmt) + (1 if v.startswith(("-", "+")) else 0), "0")
            if k == "X":
                nx = _num(v, fmt, unit)
            elif k == "Y":
                ny = _num(v, fmt, unit)
        op = int(dm.group(1)) if dm else 1
        apd = out["apertures"].get(ap) or {}
        size = apd.get("size_mm", 0.0)
        if op == 3:
            out["flashes"] += 1
            out["flash_by_aperture"][ap] = out["flash_by_aperture"].get(ap, 0) + 1
            if apd.get("shape") in ("R", "O"):
                grow(nx, ny, apd["sx"] / 2, apd["sy"] / 2)      # flashed unrotated
            else:
                grow(nx, ny, size / 2)
        elif op == 1:
            if in_region:
                grow(nx, ny)
            else:
                if interp == "G01":
                    out["draws"] += 1
                else:
                    out["arcs"] += 1
                grow(x, y, size / 2)
                grow(nx, ny, size / 2)
                if size > 0 and (out["min_draw_mm"] is None or size < out["min_draw_mm"]):
                    out["min_draw_mm"] = size
        x, y = nx, ny
    if box[0] < math.inf:
        out["box"] = [round(v, 4) for v in box]
    return out


# ---------------------------------------------------------------- Excellon

def parse_drill(text, name=""):
    """{units, tools {T: mm}, holes [(x, y, d)], slots, plated} from Excellon."""
    out = {"file": name, "units": None, "tools": {}, "holes": [], "slots": 0, "plated": None,
           "format": None}
    unit = 25.4
    int_d, dec_d = 2, 4
    zeros = "LZ"
    head = True
    tool = None
    rout_start = rout_end = None
    x = y = 0.0
    if re.search(r"non.?plated|npth", name, re.I):
        out["plated"] = False
    elif re.search(r"(?<!non)plated|pth", name, re.I):
        out["plated"] = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        up = line.upper()
        if line.startswith(";"):
            m = re.search(r"FILE_FORMAT\s*=\s*(\d+):(\d+)", line, re.I)
            if m:
                int_d, dec_d = int(m.group(1)), int(m.group(2))
                out["format"] = f"{int_d}:{dec_d}"
            m = re.search(r"TF\.FileFunction,(Plated|NonPlated)", line, re.I)
            if m:
                out["plated"] = m.group(1).lower() == "plated"
            if re.search(r"TYPE\s*=\s*NON_PLATED", line, re.I):
                out["plated"] = False
            elif re.search(r"TYPE\s*=\s*PLATED", line, re.I):
                out["plated"] = True
            continue
        if up.startswith(("METRIC", "INCH")):
            unit = 1.0 if up.startswith("METRIC") else 25.4
            out["units"] = "mm" if unit == 1.0 else "inch"
            if ",TZ" in up:
                zeros = "TZ"
            elif ",LZ" in up:
                zeros = "LZ"
            m = re.search(r",(0+)\.(0+)", up)
            if m:
                int_d, dec_d = len(m.group(1)), len(m.group(2))
            if out["format"] is None and unit == 1.0 and not re.search(r"FILE_FORMAT", text):
                int_d, dec_d = (3, 3) if not m else (int_d, dec_d)
            continue
        if up in ("M71",):
            unit, out["units"] = 1.0, "mm"
            continue
        if up in ("M72",):
            unit, out["units"] = 25.4, "inch"
            continue
        if up in ("%", "M95"):
            head = False
            continue
        m = re.match(r"T(\d+)(?:.*?C([\d.]+))?", up)
        if m and not re.search(r"[XY]", up):
            t = int(m.group(1))
            if m.group(2) is not None and (head or t not in out["tools"]):
                out["tools"][t] = round(float(m.group(2)) * unit, 4)
            elif t != 0:
                tool = t
            continue
        if up.startswith("M30"):
            break
        if up.startswith("M15"):
            continue
        if up.startswith("M16"):
            if rout_start is not None and tool is not None:
                # a slot is one hole, at its middle
                ex, ey = rout_end or rout_start
                out["holes"].append((round((rout_start[0] + ex) / 2, 4),
                                     round((rout_start[1] + ey) / 2, 4),
                                     out["tools"].get(tool, 0.0)))
                out["slots"] += 1
            rout_start = rout_end = None
            continue
        if up.startswith("G05") or up.startswith("G90"):
            continue
        if re.search(r"[XY]", up):
            def num(v):
                if "." in v:
                    return float(v) * unit
                neg = v.startswith("-")
                d = v.lstrip("+-")
                if zeros == "TZ":       # trailing zeros kept: leading may be gone
                    d = d.rjust(int_d + dec_d, "0")
                else:                   # leading zeros kept: trailing may be gone
                    d = d.ljust(int_d + dec_d, "0")
                val = int(d) / 10 ** dec_d
                return (-val if neg else val) * unit
            parts = up.split("G85")
            m1 = re.search(r"X([+-]?[\d.]+)", parts[0])
            m2 = re.search(r"Y([+-]?[\d.]+)", parts[0])
            if m1:
                x = num(m1.group(1))
            if m2:
                y = num(m2.group(1))
            if up.startswith("G00"):
                rout_start = (x, y)        # a routed slot starts here
                continue
            if up.startswith(("G01", "G02", "G03")):
                rout_end = (x, y)          # and ends here; one hole, counted at M16
                continue
            hx, hy = x, y
            if len(parts) > 1:
                out["slots"] += 1
                m3 = re.search(r"X([+-]?[\d.]+)", parts[1])
                m4 = re.search(r"Y([+-]?[\d.]+)", parts[1])
                x2 = num(m3.group(1)) if m3 else x
                y2 = num(m4.group(1)) if m4 else y
                hx, hy = (x + x2) / 2, (y + y2) / 2
                x, y = x2, y2
            if tool is not None:
                out["holes"].append((round(hx, 4), round(hy, 4), out["tools"].get(tool, 0.0)))
    return out


# ---------------------------------------------------------------- the board's side

def expected(doc):
    """{name: why} for the layers a fab set of this board needs."""
    need = {}
    for lay in doc.copper_layers:
        need[f"copper:{L.name(lay)}"] = "a copper layer of the stack"
    smd = {L.TOP: 0, L.BOTTOM: 0}
    for p in doc.pads:
        if p.hole <= 0 and p.layer in smd:
            smd[p.layer] += 1
    for side, lay in (("top", L.TOP), ("bottom", L.BOTTOM)):
        if smd[lay]:
            need[f"paste:{side}"] = f"{smd[lay]} SMD pads on the {side}"
        if any(p.layer in (lay, L.MULTI) for p in doc.pads):
            need[f"mask:{side}"] = f"pads on the {side}"
        over = L.TOP_OVERLAY if lay == L.TOP else L.BOTTOM_OVERLAY
        if any(o.layer == over for o in list(doc.tracks) + list(doc.arcs) + list(doc.texts)):
            need[f"silk:{side}"] = f"silkscreen on the {side}"
    need["outline"] = "the board outline"
    if any(p.hole > 0 for p in doc.pads) or len(doc.vias):
        need["drill"] = "the board has holes"
    return need


def board_holes(doc):
    out = [(p.x, p.y, round(p.hole, 4), bool(p.plated)) for p in doc.pads if p.hole > 0]
    out += [(v.x, v.y, round(v.hole, 4), True) for v in doc.vias if v.hole > 0]
    return out


def _align(file_pts, board_pts, tol):
    """The (flip, dx, dy) that maps file points onto board points with the
    most hits within tol; flip is (sx, sy), each 1 or -1 (a mirrored or
    turned output). Candidate shifts pair a few file holes of the rarest
    size with every board hole of that size."""
    if not file_pts or not board_pts:
        return (False, 0.0, 0.0), 0
    from collections import defaultdict
    by = defaultdict(list)
    for x, y, d in board_pts:
        by[round(d, 2)].append((x, y))
    grid = defaultdict(list)
    cell = max(tol * 4, 0.05)
    for x, y, d in board_pts:
        grid[(int(x // cell), int(y // cell))].append((x, y))

    def hits(fl, dx, dy):
        n = 0
        for x, y, _d in file_pts:
            X, Y = fl[0] * x + dx, fl[1] * y + dy
            cx, cy = int(X // cell), int(Y // cell)
            ok = False
            for i in (-1, 0, 1):
                for j in (-1, 0, 1):
                    for bx, by_ in grid.get((cx + i, cy + j), ()):
                        if abs(bx - X) <= tol and abs(by_ - Y) <= tol:
                            ok = True
                            break
                    if ok:
                        break
                if ok:
                    break
            n += ok
        return n
    # the rarest hole size gives the fewest candidate shifts
    sizes = sorted({round(d, 2) for _x, _y, d in file_pts},
                   key=lambda s: len(by.get(s, [])) or math.inf)
    best = (((1, 1), 0.0, 0.0), hits((1, 1), 0.0, 0.0))
    for s in sizes[:2]:
        fpts = [(x, y) for x, y, d in file_pts if round(d, 2) == s][:3]
        for fl in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            for fx, fy in fpts:
                for bx, by_ in by.get(s, [])[:400]:
                    dx, dy = bx - fl[0] * fx, by_ - fl[1] * fy
                    n = hits(fl, dx, dy)
                    if n > best[1]:
                        best = ((fl, dx, dy), n)
        if best[1] == len(file_pts):
            break
    return best


FLIPS = {(1, 1): "as is", (1, -1): "mirrored top to bottom", (-1, 1): "mirrored left to right",
         (-1, -1): "turned 180 degrees"}


def check(doc, folder, tol=0.02):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"{folder} is not a folder")
    # Altium's output jobs often put Gerbers and drill files in subfolders
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    gerbers, drills, skipped, zips = [], [], [], []
    for f in files:
        ext = f.suffix.lower()
        name = str(f.relative_to(folder))
        if ext == ".zip":
            zips.append(name)
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        head = text[:4000]
        if ext in DRILL_EXT or re.search(r"^M48", head, re.M):
            if re.search(r"^M48", head, re.M) or re.search(r"^T\d+C", head, re.M):
                drills.append(parse_drill(text, name))
                continue
        if "%FS" in head or "%MO" in head or re.search(r"%ADD\d+", text[:20000]):
            g = parse_gerber(text, name)
            if g["function"] is None and ext in EXT:
                g["function"], g["side"] = EXT[ext]
            if g["function"] is None:
                m = re.match(r"\.g(\d+)$", ext)
                if m:
                    g["function"], g["side"] = "copper", f"inner {m.group(1)}"
                m = re.match(r"\.gm(\d+)$", ext)
                if m:
                    g["function"], g["side"] = "mechanical", int(m.group(1))
            gerbers.append(g)
        else:
            skipped.append(name)
    rep = {"folder": str(folder), "gerbers": [], "drills": [], "skipped": skipped,
           "missing": [], "problems": [], "notes": []}
    if zips:
        rep["notes"].append(f"zip archives are not opened (unzip them first): {zips[:5]}")
    if not gerbers and not drills:
        rep["problems"].append(f"no Gerber or drill file found in {folder} or its subfolders "
                               f"({len(files)} file(s) looked at)")
    have = set()
    claims = {}
    copper_files = [g for g in gerbers if g["function"] == "copper"]
    order = doc.copper_layers
    for g in gerbers:
        key = g["function"]
        if key == "copper":
            side = str(g["side"] or "")
            if side == "top":
                lay = order[0]
            elif side in ("bot", "bottom"):
                lay = order[-1]
            else:
                m = re.search(r"(\d+)", side) or re.search(r"(\d+)", str(g.get("copper_index") or ""))
                lay = order[min(len(order) - 1, int(m.group(1)))] if m else None
            g["layer"] = L.name(lay) if lay else None
            if lay:
                have.add(f"copper:{L.name(lay)}")
                claims.setdefault(f"copper:{L.name(lay)}", []).append(g["file"])
        elif key in ("mask", "silk", "paste"):
            side = "top" if str(g["side"]).startswith("top") else "bottom"
            have.add(f"{key}:{side}")
            claims.setdefault(f"{key}:{side}", []).append(g["file"])
        elif key in ("outline", "mechanical"):
            if key == "outline" or g["draws"] + g["arcs"] > 0:
                have.add("outline" if key == "outline" else f"mech:{g['side']}")
        if not (g["flashes"] or g["draws"] or g["arcs"] or g["regions"]):
            rep["notes"].append(f"{g['file']}: an empty image")
        if g["zero_apertures"]:
            rep["notes"].append(f"{g['file']}: zero-size apertures {g['zero_apertures']}")
        rep["gerbers"].append({k: v for k, v in g.items() if k not in ("apertures",
                                                                         "flash_by_aperture")})
    for k, fs in sorted(claims.items()):
        if len(fs) > 1:
            rep["notes"].append(f"{len(fs)} files for {k} ({', '.join(fs[:3])}): more than one "
                                "output set in the folder? Give the folder of one set")
    if "outline" not in have and any(k.startswith("mech:") for k in have):
        have.add("outline")
        rep["notes"].append("no outline file as such; a mechanical layer is taken as the outline")
    if drills:
        have.add("drill")
    need = expected(doc)
    for k, why in need.items():
        if k not in have:
            rep["missing"].append({"layer": k, "why": why})
    units = {g["units"] for g in gerbers if g["units"]}
    if len(units) > 1:
        rep["notes"].append(f"mixed units across Gerber files: {sorted(units)}")

    # drill
    bh = board_holes(doc)
    fh = [h for d in drills for h in d["holes"]]
    rep["drills"] = [{k: v for k, v in d.items() if k != "holes"} | {"holes": len(d["holes"])}
                     for d in drills]
    if drills:
        from collections import Counter
        bc = Counter(round(d, 2) for _x, _y, d, _p in bh)
        fc = Counter(round(d, 2) for _x, _y, d in fh)
        sizes = []
        for s in sorted(set(bc) | set(fc)):
            row = {"size_mm": s, "board": bc.get(s, 0), "files": fc.get(s, 0)}
            if row["board"] != row["files"]:
                rep["problems"].append(f"{s} mm holes: {row['board']} on the board, "
                                       f"{row['files']} in the drill files")
            sizes.append(row)
        rep["drill_sizes"] = sizes
        (fl, dx, dy), n = _align(fh, [(x, y, d) for x, y, d, _p in bh], max(tol, 0.01))
        rep["drill_alignment"] = {"orientation": FLIPS[fl], "offset_mm": [round(dx, 4), round(dy, 4)],
                                  "matched": n, "file_holes": len(fh), "board_holes": len(bh)}
        if fl != (1, 1):
            rep["notes"].append(f"the drill files line up with the board only {FLIPS[fl]}")
        if n < len(fh):
            rep["problems"].append(f"{len(fh) - n} drill hit(s) match no hole on the board "
                                   f"(after the best shift {round(dx, 3)}, {round(dy, 3)} mm, "
                                   f"{FLIPS[fl]})")
        for d in drills:
            if d["plated"] is not None:
                want = [h for h in bh if h[3] == d["plated"]]
                if len(want) != len(d["holes"]):
                    rep["notes"].append(f"{d['file']}: {len(d['holes'])} "
                                        f"{'plated' if d['plated'] else 'unplated'} hits, the "
                                        f"board has {len(want)}")
    # copper flashes vs pads, inside the outline
    if copper_files:
        for g in copper_files:
            if not g.get("layer"):
                continue
            lay = L.parse(g["layer"])
            pads = sum(1 for p in doc.pads if p.on_layer(lay)) + \
                sum(1 for v in doc.vias if v.spans(lay, order))
            g2 = [x for x in rep["gerbers"] if x["file"] == g["file"]][0]
            g2["board_pads_and_vias"] = pads
            if g["flashes"] and abs(g["flashes"] - pads) > max(2, 0.02 * pads):
                rep["notes"].append(f"{g['file']}: {g['flashes']} flashes, the board has {pads} "
                                    "pads and vias there (pads drawn as regions or merged "
                                    "copper can explain a difference)")
    rep["ok"] = not rep["missing"] and not rep["problems"]
    return rep


def cmd_fab_check(args):
    from .pcbdoc import PcbDoc
    doc = PcbDoc.open(args.board)
    rep = check(doc, args.folder, args.tol)
    if args.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        for g in rep["gerbers"]:
            print(f"  {g['file']:<36} {g['function'] or '?':<10} {str(g.get('layer') or g['side'] or ''):<14}"
                  f" {g['units'] or '?':<4} {g['format'] or '':<8} flashes {g['flashes']:>5}, "
                  f"draws {g['draws'] + g['arcs']:>5}, regions {g['regions']:>4}")
        for d in rep["drills"]:
            pl = {True: "plated", False: "unplated", None: "?"}[d["plated"]]
            print(f"  {d['file']:<36} drill {pl:<9} {d['units'] or '?':<4} {d['holes']} hits, "
                  f"{len(d['tools'])} tools")
        for m in rep["missing"]:
            print(f"  MISSING {m['layer']}: {m['why']}")
        for p in rep["problems"]:
            print(f"  PROBLEM {p}")
        for n in rep["notes"]:
            print(f"  note: {n}")
        if "drill_alignment" in rep:
            a = rep["drill_alignment"]
            print(f"  drill: {a['matched']}/{a['file_holes']} hits on board holes "
                  f"({a['board_holes']} on the board); offset {a['offset_mm']} mm, "
                  f"{a['orientation']}")
        print("ready" if rep["ok"] else "not ready")
    return 0 if rep["ok"] else 1


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("folder", help="the Gerber and drill files")
        p.add_argument("--tol", type=float, default=0.02, help="hole position tolerance, mm")
        p.add_argument("--json", action="store_true")
    command("fab-check", cmd_fab_check, "read Gerber/drill outputs back and compare with the board",
            build)
