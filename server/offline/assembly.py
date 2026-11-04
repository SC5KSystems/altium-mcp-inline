"""Hand-assembly page: one self-contained HTML file from a PcbDoc (and a BOM).

    python -m offline assembly board.PcbDoc -o assembly.html [--bom bom.csv] [--title NAME]

The page shows the board from the top and from the bottom (mirrored, as you
hold it) next to the BOM grouped by value and footprint. Click a line to
light up its parts; tick it off when they are on. Filter by side, search by
designator or value, hide what is done. Ticks are kept in the browser for
this board. Nothing is fetched at run time.

The BOM is a CSV with a header row; its designator, value/comment,
footprint and quantity columns are found by name (Designator, RefDes,
Reference; Comment, Value; Footprint, Package, Pattern; Quantity, Qty).
Designator cells may list several ("R1, R2" or "R1-R4"). Without a BOM the
parts are grouped by comment and footprint from the board.
"""

import csv
import html
import io
import json
import re

from . import geom
from . import layers as L

DES_COL = ("designator", "designators", "refdes", "reference", "references", "ref", "part")
VAL_COL = ("comment", "value", "description", "val")
FP_COL = ("footprint", "package", "pattern", "case")
QTY_COL = ("quantity", "qty", "count")


def _expand(cell):
    out = []
    for tok in re.split(r"[,\s;]+", cell.strip()):
        if not tok:
            continue
        m = re.match(r"^([A-Za-z_]+)(\d+)-\1?(\d+)$", tok)
        if m and int(m.group(3)) >= int(m.group(2)) and int(m.group(3)) - int(m.group(2)) < 1000:
            out += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(tok)
    return out


def read_bom(text):
    """[{"designators", "value", "footprint", "quantity"}] from BOM CSV text."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    head_i = None
    for i, r in enumerate(rows[:20]):
        low = [c.strip().lower() for c in r]
        if any(c in DES_COL for c in low):
            head_i = i
            break
    if head_i is None:
        raise ValueError("no designator column in the BOM (Designator, RefDes, Reference)")
    head = [c.strip().lower() for c in rows[head_i]]

    def col(names):
        for n in names:
            if n in head:
                return head.index(n)
        return None
    di, vi, fi, qi = col(DES_COL), col(VAL_COL), col(FP_COL), col(QTY_COL)
    out = []
    for r in rows[head_i + 1:]:
        if di is None or di >= len(r) or not r[di].strip():
            continue
        des = _expand(r[di])
        out.append({"designators": des,
                    "value": r[vi].strip() if vi is not None and vi < len(r) else "",
                    "footprint": r[fi].strip() if fi is not None and fi < len(r) else "",
                    "quantity": r[qi].strip() if qi is not None and qi < len(r) else str(len(des))})
    return out


def _natural(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", str(s))]


def board_data(doc, bom=None):
    """The JSON the page draws from."""
    sh = geom.require()
    outline = doc.outline()
    if not outline:
        raise ValueError("the board has no outline")
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    box = [min(xs), min(ys), max(xs), max(ys)]

    def pts(g, nd=3):
        return [[round(x, nd), round(y, nd)] for x, y in list(g.exterior.coords)[:-1]]
    parts = {}
    for c in doc.components:
        side = "B" if c.layer == L.BOTTOM else "F"
        pads, shapes = [], []
        for p in doc.pads:
            if p.component != c.index:
                continue
            lay = p.layer if p.layer != L.MULTI else (L.BOTTOM if side == "B" else L.TOP)
            g = geom.pad_shape(p, lay)
            if g.is_empty:
                continue
            g = g.simplify(0.01)
            shapes.append(g)
            pads.append({"p": pts(g), "one": p.name in ("1", "A1"),
                         "th": p.hole > 0})
        if not pads:
            continue
        hull = sh.union_all(shapes).envelope.buffer(0.3, join_style="mitre")
        parts[c.designator or f"(no designator #{c.index})"] = {
                               "side": side, "pads": pads, "box": pts(hull),
                               "value": c.comment or "", "footprint": c.pattern or "",
                               "x": round(c.x, 3), "y": round(c.y, 3)}
    lines, missing = [], []
    if bom:
        seen = set()
        for row in bom:
            des = [d for d in row["designators"]]
            on = [d for d in des if d in parts]
            missing += [d for d in des if d not in parts]
            seen.update(on)
            if on:
                lines.append({"refs": sorted(on, key=_natural), "value": row["value"],
                              "footprint": row["footprint"] or parts[on[0]]["footprint"]})
        extra = sorted((d for d in parts if d not in seen), key=_natural)
        if extra:
            groups = {}
            for d in extra:
                groups.setdefault((parts[d]["value"], parts[d]["footprint"]), []).append(d)
            for (v, f), refs in groups.items():
                lines.append({"refs": refs, "value": v, "footprint": f, "not_in_bom": True})
    else:
        groups = {}
        for d, p in parts.items():
            groups.setdefault((p["value"], p["footprint"]), []).append(d)
        for (v, f), refs in groups.items():
            lines.append({"refs": sorted(refs, key=_natural), "value": v, "footprint": f})
    lines.sort(key=lambda l_: (_natural(re.sub(r"\d.*", "", l_["refs"][0])), -len(l_["refs"]),
                               _natural(l_["value"])))
    return {"outline": [[round(x, 3), round(y, 3)] for x, y in outline], "box": box,
            "parts": parts, "lines": lines, "missing_from_board": sorted(set(missing), key=_natural)}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f5f2;--fg:#1d1d1b;--mute:#6b6a66;--line:#d9d7d1;--board:#2f5d3a;--edge:#c9b26b;
--pad:#c8a24a;--hl:#e8452c;--done:#8a8f86;--row:#ffffff;--rowhl:#fde7e1}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--fg:#ecebe6;--mute:#9a9891;--line:#33322f;
--board:#1f3a26;--edge:#a8935a;--pad:#b08c3c;--hl:#ff6a4d;--done:#5c605a;--row:#1f1f1d;--rowhl:#3d211b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 16px;
align-items:center}h1{font-size:16px;margin:0 8px 0 0}
.ctl{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
button,input{font:inherit;color:inherit;background:var(--row);border:1px solid var(--line);
border-radius:6px;padding:4px 10px}button.on{border-color:var(--hl);color:var(--hl)}
main{display:grid;grid-template-columns:minmax(280px,420px) 1fr;gap:12px;padding:12px 16px}
@media (max-width:820px){main{grid-template-columns:1fr}}
.bom{max-height:calc(100vh - 90px);overflow:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}td,th{padding:6px 8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{position:sticky;top:0;background:var(--bg);font-weight:600}
tr.l{cursor:pointer;background:var(--row)}tr.l.sel{background:var(--rowhl)}tr.l.done td{color:var(--done)}
tr.l.done td.refs{text-decoration:line-through}td.refs{font-variant-numeric:tabular-nums}
.tag{color:var(--mute);font-size:12px}.views{display:grid;gap:12px}
.view{border:1px solid var(--line);border-radius:8px;padding:6px}.view h2{font-size:13px;margin:0 0 4px;
color:var(--mute);font-weight:600}svg{width:100%;height:auto;display:block}
.outline{fill:var(--board);stroke:var(--edge);stroke-width:.3}.pad{fill:var(--pad);opacity:.55}
.box{fill:none;stroke:var(--mute);stroke-width:.12;opacity:.6}.one{stroke:var(--fg);stroke-width:.12}
.sel .pad{fill:var(--hl);opacity:1}.sel .box{stroke:var(--hl);stroke-width:.35;opacity:1}
.dim .pad{opacity:.25}.missing{color:var(--hl);font-size:12px}
</style></head><body>
<header><h1>__TITLE__</h1><span id="prog" class="tag"></span>
<div class="ctl"><button data-s="F" class="side on">Top</button><button data-s="B" class="side on">Bottom</button>
<input id="q" placeholder="search R12, 10k" size="16"><button id="hide">Hide done</button>
<button id="clear">Clear ticks</button></div></header>
<main><div class="bom"><table><thead><tr><th></th><th>Qty</th><th>Parts</th><th>Value</th>
<th>Footprint</th></tr></thead><tbody id="rows"></tbody></table><div id="miss" class="missing"></div></div>
<div class="views" id="views"></div></main>
<script>
const D=__DATA__;const KEY="assembly:"+__KEY__;let done={};
try{done=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){done={}}
const sides={F:true,B:true};let hide=false,sel=-1;
const [x0,y0,x1,y1]=D.box,W=x1-x0,H=y1-y0,NS="http://www.w3.org/2000/svg";
function P(pts,m){return pts.map(p=>((m?x1-p[0]:p[0]-x0)).toFixed(3)+","+(y1-p[1]).toFixed(3)).join(" ")}
function el(n,a){const e=document.createElementNS(NS,n);for(const k in a)e.setAttribute(k,a[k]);return e}
const groups={};
function view(side){const v=document.createElement("div");v.className="view";v.dataset.side=side;
 v.innerHTML="<h2>"+(side=="F"?"Top side":"Bottom side (mirrored, as held)")+"</h2>";
 const s=el("svg",{viewBox:"-2 -2 "+(W+4)+" "+(H+4)});const m=side=="B";
 s.appendChild(el("polygon",{points:P(D.outline,m),class:"outline"}));
 for(const ref in D.parts){const p=D.parts[ref];if(p.side!=side)continue;const g=el("g",{});
  g.appendChild(el("polygon",{points:P(p.box,m),class:"box"}));
  for(const pd of p.pads)g.appendChild(el("polygon",{points:P(pd.p,m),class:"pad"+(pd.one?" one":"")}));
  const t=el("title",{});t.textContent=ref+" "+p.value;g.appendChild(t);
  (groups[ref]=groups[ref]||[]).push(g);s.appendChild(g)}
 v.appendChild(s);return v}
const V=document.getElementById("views");V.appendChild(view("F"));V.appendChild(view("B"));
const rows=document.getElementById("rows");
D.lines.forEach((l,i)=>{const tr=document.createElement("tr");tr.className="l";tr.dataset.i=i;
 const sd=[...new Set(l.refs.map(r=>D.parts[r].side))].join("");tr.dataset.sides=sd;
 tr.innerHTML='<td><input type="checkbox"></td><td>'+l.refs.length+'</td><td class="refs"></td><td></td><td class="tag"></td>';
 tr.children[2].textContent=l.refs.join(", ")+(sd=="B"?"  (bottom)":sd.length>1?"  (both sides)":"");
 tr.children[3].textContent=l.value+(l.not_in_bom?"  (not in BOM)":"");tr.children[4].textContent=l.footprint;
 const cb=tr.querySelector("input");cb.checked=!!done[l.refs.join(",")];
 cb.addEventListener("click",e=>{e.stopPropagation();done[l.refs.join(",")]=cb.checked;save();paint()});
 tr.addEventListener("click",()=>{sel=sel==i?-1:i;paint()});rows.appendChild(tr)});
if(D.missing_from_board.length)document.getElementById("miss").textContent=
 "In the BOM but not on the board: "+D.missing_from_board.join(", ");
function save(){try{localStorage.setItem(KEY,JSON.stringify(done))}catch(e){}}
function paint(){const q=document.getElementById("q").value.trim().toLowerCase();let n=0,k=0;
 for(const tr of rows.children){const l=D.lines[tr.dataset.i];const d=!!done[l.refs.join(",")];
  n+=l.refs.length;if(d)k+=l.refs.length;tr.classList.toggle("done",d);tr.classList.toggle("sel",+tr.dataset.i==sel);
  const sideOk=[...tr.dataset.sides].some(s=>sides[s]);
  const qOk=!q||l.refs.some(r=>r.toLowerCase()==q||r.toLowerCase().startsWith(q))||l.value.toLowerCase().includes(q);
  tr.style.display=sideOk&&qOk&&!(hide&&d)?"":"none"}
 for(const v of V.children)v.style.display=sides[v.dataset.side]?"":"none";
 const on=sel>=0?new Set(D.lines[sel].refs):null;
 for(const r in groups)for(const g of groups[r]){g.classList.toggle("sel",!!on&&on.has(r));g.classList.toggle("dim",!!on&&!on.has(r))}
 document.getElementById("prog").textContent=k+" / "+n+" parts placed"}
document.querySelectorAll("button.side").forEach(b=>b.addEventListener("click",()=>{sides[b.dataset.s]=!sides[b.dataset.s];
 b.classList.toggle("on",sides[b.dataset.s]);paint()}));
document.getElementById("q").addEventListener("input",paint);
document.getElementById("hide").addEventListener("click",e=>{hide=!hide;e.target.classList.toggle("on",hide);paint()});
document.getElementById("clear").addEventListener("click",()=>{if(confirm("Clear every tick?")){done={};save();paint()}});
paint();
</script></body></html>
"""


def page(doc, bom=None, title="Assembly"):
    data = board_data(doc, bom)
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    key = json.dumps(re.sub(r"\W+", "_", title))
    return (PAGE.replace("__TITLE__", html.escape(title)).replace("__KEY__", key)
            .replace("__DATA__", blob)), data


def cmd_assembly(args):
    import os
    from pathlib import Path
    from .pcbdoc import PcbDoc
    if os.path.abspath(args.out) == os.path.abspath(args.board):
        raise ValueError("refusing to write over the board")
    doc = PcbDoc.open(args.board)
    bom = read_bom(Path(args.bom).read_text(encoding="utf-8-sig", errors="replace")) \
        if args.bom else None
    text, data = page(doc, bom, args.title or Path(args.board).stem)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"{args.out}: {len(data['lines'])} BOM line(s), {len(data['parts'])} part(s)"
          + (f"; not on the board: {', '.join(data['missing_from_board'])}"
             if data["missing_from_board"] else ""))
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("-o", "--out", required=True)
        p.add_argument("--bom", help="BOM CSV")
        p.add_argument("--title")
    command("assembly", cmd_assembly, "interactive hand-assembly page (one HTML file)", build)
