"""Evaluate Altium rule scope expressions (the query language) offline.

    q = compile_scope("InNetClass('PWR') and not IsVia")
    q.match(doc, obj)        # obj: a primitive, Component, Polygon or plane area

A function this module does not know raises ScopeError when the expression is
compiled, so a rule nobody can evaluate is reported, never silently treated as
matching or not matching. Matching is case-insensitive like Altium's.
"""

import fnmatch
import math
import re

from . import layers as L


class ScopeError(ValueError):
    pass


_TOK = re.compile(r"""\s*(?:
    (?P<str>'(?:[^']|'')*')
  | (?P<num>-?\d+(?:\.\d+)?(?:mil|mm)?)
  | (?P<op><>|<=|>=|=|<|>|\(|\)|,)
  | (?P<id>[A-Za-z_][A-Za-z0-9_.]*)
)""", re.VERBOSE)


def _tokens(text):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _TOK.match(text, pos)
        if not m or m.end() == pos:
            raise ScopeError(f"cannot read scope at {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "str":
            val = val[1:-1].replace("''", "'")
        out.append((kind, val))
    return out


class _Parser:
    def __init__(self, text):
        self.toks = _tokens(text)
        self.i = 0

    def peek(self, kw=None):
        if self.i >= len(self.toks):
            return None
        k, v = self.toks[self.i]
        if kw is None:
            return (k, v)
        return (k, v) if k == "id" and v.upper() == kw else None

    def take(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, op):
        if self.i >= len(self.toks) or self.toks[self.i][1] != op:
            raise ScopeError(f"expected {op!r}")
        self.i += 1

    def parse(self):
        if not self.toks:
            return ("const", True)
        node = self.or_()
        if self.i != len(self.toks):
            raise ScopeError(f"unexpected {self.toks[self.i][1]!r}")
        return node

    def or_(self):
        node = self.xor()
        while self.peek("OR"):
            self.take()
            node = ("or", node, self.xor())
        return node

    def xor(self):
        node = self.and_()
        while self.peek("XOR"):
            self.take()
            node = ("xor", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.peek("AND"):
            self.take()
            node = ("and", node, self.not_())
        return node

    def not_(self):
        if self.peek("NOT"):
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        left = self.primary()
        t = self.peek()
        if t and (t[0] == "op" and t[1] in ("=", "<>", "<", ">", "<=", ">=")
                  or t[0] == "id" and t[1].upper() == "LIKE"):
            op = self.take()[1].upper()
            right = self.primary()
            return ("cmp", op, left, right)
        return left

    def primary(self):
        t = self.peek()
        if t is None:
            raise ScopeError("expression ends early")
        k, v = self.take()
        if k == "op" and v == "(":
            node = self.or_()
            self.expect(")")
            return node
        if k == "str":
            return ("lit", v)
        if k == "num":
            return ("lit", _num(v))
        if k == "id":
            args = []
            if self.peek() == ("op", "("):
                self.take()
                if self.peek() != ("op", ")"):
                    args.append(self.or_())
                    while self.peek() == ("op", ","):
                        self.take()
                        args.append(self.or_())
                self.expect(")")
            return ("call", v, args)
        raise ScopeError(f"unexpected {v!r}")


def _num(v):
    s = str(v).lower()
    if s.endswith("mil"):
        return float(s[:-3]) * 0.0254
    if s.endswith("mm"):
        return float(s[:-2])
    return float(s)


# ---------------------------------------------------------------- object facts

KIND_NAMES = {"track": "Track", "arc": "Arc", "via": "Via", "pad": "Pad", "fill": "Fill",
              "region": "Region", "text": "Text", "component": "Component",
              "polygon": "Polygon", "plane": "Polygon", "body": "ComponentBody"}


def obj_kind(obj):
    k = getattr(obj, "KIND", None)
    if k is None:
        name = type(obj).__name__
        return {"Component": "component", "Polygon": "polygon", "Net": "net"}.get(name, name.lower())
    return k


def obj_net(doc, obj):
    k = obj_kind(obj)
    if k == "polygon":
        return obj.net
    if k in ("component", "text", "body"):
        return None
    return getattr(obj, "effective_net", getattr(obj, "net", None))


def obj_layer(obj):
    k = obj_kind(obj)
    if k == "via":
        return L.MULTI
    if k in ("component", "polygon"):
        return obj.layer
    return getattr(obj, "layer", None)


def obj_designator(doc, obj):
    k = obj_kind(obj)
    if k == "component":
        return obj.designator
    c = getattr(obj, "component", None)
    if c is None or c >= len(doc.components):
        return ""
    return doc.components[c].designator


def obj_component(doc, obj):
    k = obj_kind(obj)
    if k == "component":
        return obj
    c = getattr(obj, "component", None)
    return doc.components[c] if c is not None and c < len(doc.components) else None


def obj_xy(obj):
    k = obj_kind(obj)
    if k == "track":
        return ((obj.x1 + obj.x2) / 2, (obj.y1 + obj.y2) / 2)
    if k == "arc":
        return (obj.cx, obj.cy)
    if k == "fill":
        return ((obj.x1 + obj.x2) / 2, (obj.y1 + obj.y2) / 2)
    if k == "region":
        pts = obj.outline()
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else (0, 0)
    return (getattr(obj, "x", 0.0), getattr(obj, "y", 0.0))


def _point_in(pts, x, y):
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-30) + x1:
            inside = not inside
    return inside


class _Facts:
    """Per-board lookups the functions share (classes, pairs, rooms)."""

    def __init__(self, doc):
        self.doc = doc
        self.classes = {}
        for c in doc.classes:
            self.classes.setdefault((c.kind, c.name.upper()), c)
        self.pairs = {}
        st = doc.storages.get("DifferentialPairs6")
        for p in (st.records if st else []):
            self.pairs[p.text("NAME").upper()] = {p.text("POSITIVENETNAME").upper(),
                                                  p.text("NEGATIVENETNAME").upper()}
        self.signal_classes = {}
        st = doc.storages.get("SignalClasses")
        for p in (st.records if st else []):
            members, i = set(), 0
            while p.get(f"M{i}") is not None:
                members.add(p.text(f"M{i}").upper())
                i += 1
            self.signal_classes[p.text("NAME").upper()] = (members, p.get("SUPERCLASS", "") == "TRUE")
        self.rooms = {}
        from .pcbdoc import props_vertices, vertices_to_points
        for r in doc.rules:
            if r.kind == "RoomDefinition":
                self.rooms[r.name.upper()] = vertices_to_points(props_vertices(r.props))
        self.pad_counts = {}
        for pd in doc.pads:
            if pd.component is not None:
                self.pad_counts[pd.component] = self.pad_counts.get(pd.component, 0) + 1

    def in_class(self, kind, name, key):
        c = self.classes.get((kind, name.upper()))
        if c is None:
            return False
        if c.superclass:
            return True
        return key.upper() in {m.upper() for m in c.members}


def _facts(doc):
    f = doc.__dict__.get("_scope_facts")
    if f is None:
        f = _Facts(doc)
        doc.__dict__["_scope_facts"] = f
    return f


def _s(v):
    return str(v) if v is not None else ""


def _component_kind(doc, comp, wanted):
    pat = (comp.pattern if comp else "").upper()
    return wanted in pat


def _fn_table():
    def net_name(c):
        n = obj_net(c.doc, c.obj)
        return c.doc.nets[n].name if n is not None and n < len(c.doc.nets) else ""

    def layer_is(test):
        return lambda c: (lambda lay: lay is not None and test(lay, c))(obj_layer(c.obj))

    def comp_kind(word):
        return lambda c: _component_kind(c.doc, obj_component(c.doc, c.obj), word)

    def k(*kinds):
        return lambda c: obj_kind(c.obj) in kinds

    def is_poly(c):
        # a polygon, or the copper it poured (Altium tests a pour's copper as the polygon)
        return obj_kind(c.obj) in ("polygon", "plane") or (
            obj_kind(c.obj) in ("region", "track", "arc")
            and getattr(c.obj, "polygon", None) is not None)

    def in_poly(c):
        return getattr(c.obj, "polygon", None) is not None or obj_kind(c.obj) in ("polygon", "plane")

    def poly_of(c):
        if obj_kind(c.obj) == "polygon":
            return c.obj
        p = getattr(c.obj, "polygon", None)
        return c.doc.polygons[p] if p is not None and p < len(c.doc.polygons) else None

    def room(name, within):
        def f(c):
            pts = c.facts.rooms.get(name.upper())
            if not pts:
                return False
            x, y = obj_xy(c.obj)
            return _point_in(pts, x, y)
        return f

    def hole_size(c):
        return getattr(c.obj, "hole", 0.0) or 0.0

    def in_xsignal_class(c, name):
        """xSignal members are 'NETA-NETB' names; a net belongs if it is either end."""
        entry = c.facts.signal_classes.get(name.upper())
        net = net_name(c).upper()
        if entry is None or not net:
            return False
        members, superclass = entry
        return superclass or any(net in m.split("-") for m in members)

    zero = {
        "ALL": lambda c: True,
        "TRUE": lambda c: True,
        "FALSE": lambda c: False,
        "ISPAD": k("pad"), "ISVIA": k("via"), "ISTRACK": k("track"), "ISARC": k("arc"),
        "ISFILL": k("fill"), "ISREGION": k("region"), "ISTEXT": k("text"),
        "ISCOMPONENT": k("component"), "ISPOLYGON": is_poly, "ISPOLY": is_poly,
        "ISCOMPONENTBODY": k("body"),
        "ISDESIGNATOR": lambda c: obj_kind(c.obj) == "text" and c.obj.is_designator,
        "ISCOMMENT": lambda c: obj_kind(c.obj) == "text" and c.obj.is_comment,
        "ISKEEPOUT": lambda c: bool(getattr(c.obj, "is_keepout", False)),
        "ISBOARDCUTOUT": lambda c: obj_kind(c.obj) == "region" and c.obj.is_board_cutout,
        "ISTEARDROP": lambda c: False,
        "INPOLYGON": in_poly, "INPOLY": in_poly,
        "INANYCOMPONENT": lambda c: bool(obj_designator(c.doc, c.obj)),
        "INANYNET": lambda c: obj_net(c.doc, c.obj) is not None,
        "HOLE": lambda c: hole_size(c) > 0,
        "ISHOLE": lambda c: hole_size(c) > 0,
        "ISSMDPAD": lambda c: obj_kind(c.obj) == "pad" and c.obj.is_smd,
        "ISSMTPAD": lambda c: obj_kind(c.obj) == "pad" and c.obj.is_smd,
        "ISTHRUPAD": lambda c: obj_kind(c.obj) == "pad" and c.obj.hole > 0,
        "ONTOP": layer_is(lambda lay, c: lay == L.TOP),
        "ONBOTTOM": layer_is(lambda lay, c: lay == L.BOTTOM),
        "ONMID": layer_is(lambda lay, c: 2 <= lay <= 31),
        "ONSIGNAL": layer_is(lambda lay, c: L.is_signal(lay)),
        "ONPLANE": layer_is(lambda lay, c: L.is_plane(lay)),
        "ONCOPPER": layer_is(lambda lay, c: L.is_copper(lay) or lay == L.MULTI),
        "ONMULTILAYER": layer_is(lambda lay, c: lay == L.MULTI),
        "ONOUTSIDE": layer_is(lambda lay, c: lay in (L.TOP, L.BOTTOM)),
        "ONTOPSILKSCREEN": layer_is(lambda lay, c: lay == L.TOP_OVERLAY),
        "ONBOTTOMSILKSCREEN": layer_is(lambda lay, c: lay == L.BOTTOM_OVERLAY),
        "ONSILKSCREEN": layer_is(lambda lay, c: lay in (L.TOP_OVERLAY, L.BOTTOM_OVERLAY)),
        "ONMECHANICAL": layer_is(lambda lay, c: L.is_mechanical(lay)),
        "ISBGA": comp_kind("BGA"), "ISLCC": comp_kind("LCC"), "ISSOIC": comp_kind("SOIC"),
        "ISQFP": comp_kind("QFP"), "ISDIP": comp_kind("DIP"), "ISPGA": comp_kind("PGA"),
        "ISSIP": comp_kind("SIP"), "ISSMSIP": comp_kind("SMSIP"),
        "ISSMDCOMPONENT": lambda c: (lambda comp: comp is not None and all(
            p.is_smd for p in c.doc.pads if p.component == comp.index))(obj_component(c.doc, c.obj)),
    }
    one = {
        "INNET": lambda c, a: net_name(c).upper() == a.upper(),
        "INNETCLASS": lambda c, a: c.facts.in_class("net", a, net_name(c)) if net_name(c) else False,
        "INCOMPONENT": lambda c, a: obj_designator(c.doc, c.obj).upper() == a.upper(),
        "INCOMPONENTCLASS": lambda c, a: bool(obj_designator(c.doc, c.obj)) and
        c.facts.in_class("component", a, obj_designator(c.doc, c.obj)),
        "INPADCLASS": lambda c, a: obj_kind(c.obj) == "pad" and c.facts.in_class(
            "pad", a, f"{obj_designator(c.doc, c.obj)}-{c.obj.name}"),
        "INPOLYGONCLASS": lambda c, a: (lambda p: p is not None and c.facts.in_class(
            "polygon", a, p.name))(poly_of(c)),
        "INNAMEDPOLYGON": lambda c, a: (lambda p: p is not None and p.name.upper() == a.upper())(poly_of(c)),
        "INDIFFERENTIALPAIR": lambda c, a: net_name(c).upper() in c.facts.pairs.get(a.upper(), set()),
        "INDIFFERENTIALPAIRCLASS": lambda c, a: bool(net_name(c)) and any(
            net_name(c).upper() in nets for pname, nets in c.facts.pairs.items()
            if c.facts.in_class("diff_pair", a, pname)),
        "INXSIGNALCLASS": lambda c, a: in_xsignal_class(c, a),
        "ONLAYER": lambda c, a: obj_layer(c.obj) == L.parse(a),
        "HASFOOTPRINT": lambda c, a: (lambda comp: comp is not None and fnmatch.fnmatch(
            comp.pattern.upper(), a.upper()))(obj_component(c.doc, c.obj)),
        "TOUCHESROOM": lambda c, a: room(a, False)(c),
        "WITHINROOM": lambda c, a: room(a, True)(c),
        "INROOM": lambda c, a: room(a, True)(c),
    }
    two = {
        "HASFOOTPRINTPAD": lambda c, a, b: obj_kind(c.obj) == "pad" and
        obj_designator(c.doc, c.obj).upper() == a.upper() and c.obj.name.upper() == b.upper(),
    }
    values = {
        "OBJECTKIND": lambda c: KIND_NAMES.get(obj_kind(c.obj), obj_kind(c.obj)),
        "NET": net_name, "NETNAME": net_name,
        "LAYER": lambda c: L.name(obj_layer(c.obj)) if obj_layer(c.obj) is not None else "",
        "NAME": lambda c: (getattr(c.obj, "name", None) or obj_designator(c.doc, c.obj)),
        "COMPONENT": lambda c: obj_designator(c.doc, c.obj),
        "DESIGNATOR": lambda c: obj_designator(c.doc, c.obj),
        "FOOTPRINT": lambda c: (lambda comp: comp.pattern if comp else "")(obj_component(c.doc, c.obj)),
        "PATTERN": lambda c: (lambda comp: comp.pattern if comp else "")(obj_component(c.doc, c.obj)),
        "COMPPINCOUNT": lambda c: (lambda comp: c.facts.pad_counts.get(comp.index, 0) if comp else 0)(
            obj_component(c.doc, c.obj)),
        "PADISPLATED": lambda c: "True" if obj_kind(c.obj) == "pad" and c.obj.plated else
        ("False" if obj_kind(c.obj) == "pad" else ""),
        "HOLEDIAMETER": hole_size, "HOLESIZE": hole_size,
        "PADXSIZE": lambda c: c.obj.top_size[0] if obj_kind(c.obj) == "pad" else 0.0,
        "PADYSIZE": lambda c: c.obj.top_size[1] if obj_kind(c.obj) == "pad" else 0.0,
        "VIADIAMETER": lambda c: c.obj.diameter if obj_kind(c.obj) == "via" else 0.0,
        "WIDTH": lambda c: getattr(c.obj, "width", 0.0),
    }
    return zero, one, two, values


_ZERO, _ONE, _TWO, _VALUES = _fn_table()


class _Ctx:
    __slots__ = ("doc", "obj", "facts")

    def __init__(self, doc, obj):
        self.doc, self.obj, self.facts = doc, obj, _facts(doc)


def _check(node):
    """Raise ScopeError for any function this module cannot evaluate."""
    t = node[0]
    if t == "call":
        name, args = node[1].upper(), node[2]
        n = len(args)
        ok = (n == 0 and (name in _ZERO or name in _VALUES)) or (n == 1 and name in _ONE) or \
             (n == 2 and name in _TWO)
        if not ok:
            raise ScopeError(f"unsupported scope function {node[1]}({', '.join('…' for _ in args)})")
        for a in args:
            if a[0] != "lit":
                raise ScopeError(f"{node[1]} expects literal arguments")
    elif t in ("and", "or", "xor"):
        _check(node[1])
        _check(node[2])
    elif t == "not":
        _check(node[1])
    elif t == "cmp":
        _check(node[2])
        _check(node[3])


def _value(node, c):
    t = node[0]
    if t == "lit":
        return node[1]
    if t == "call" and not node[2] and node[1].upper() in _VALUES:
        return _VALUES[node[1].upper()](c)
    return _eval(node, c)


def _eval(node, c):
    t = node[0]
    if t == "const":
        return node[1]
    if t == "and":
        return _eval(node[1], c) and _eval(node[2], c)
    if t == "or":
        return _eval(node[1], c) or _eval(node[2], c)
    if t == "xor":
        return bool(_eval(node[1], c)) != bool(_eval(node[2], c))
    if t == "not":
        return not _eval(node[1], c)
    if t == "lit":
        return bool(node[1])
    if t == "cmp":
        op, a, b = node[1], _value(node[2], c), _value(node[3], c)
        if op == "LIKE":
            return fnmatch.fnmatch(_s(a).upper(), _s(b).upper())
        if isinstance(a, (int, float)) or isinstance(b, (int, float)):
            try:
                a, b = float(a), float(b)
            except (TypeError, ValueError):
                return False
            eq = math.isclose(a, b, abs_tol=1e-9)
        else:
            a, b = _s(a).upper(), _s(b).upper()
            eq = a == b
        return {"=": eq, "<>": not eq, "<": a < b, ">": a > b, "<=": a <= b or eq,
                ">=": a >= b or eq}[op]
    if t == "call":
        name, args = node[1].upper(), [a[1] for a in node[2]]
        if not args:
            if name in _ZERO:
                return bool(_ZERO[name](c))
            return bool(_VALUES[name](c))
        if len(args) == 1:
            return bool(_ONE[name](c, str(args[0])))
        return bool(_TWO[name](c, str(args[0]), str(args[1])))
    raise ScopeError(f"cannot evaluate {t}")


class Scope:
    def __init__(self, text):
        self.text = text
        self.tree = _Parser(text or "All").parse()
        _check(self.tree)

    @property
    def is_all(self):
        return self.tree == ("call", "All", []) or self.tree == ("const", True) or (
            self.tree[0] == "call" and self.tree[1].upper() == "ALL")

    def match(self, doc, obj):
        return bool(_eval(self.tree, _Ctx(doc, obj)))


def compile_scope(text):
    return Scope(text)


def predicates(text):
    """[(function, literal argument)] for every one-argument call in a scope,
    e.g. [("InNetClass", "Power")]. Raises ScopeError when it cannot parse."""
    out = []

    def walk(node):
        t = node[0]
        if t == "call":
            if len(node[2]) == 1 and node[2][0][0] == "lit":
                out.append((node[1], str(node[2][0][1])))
            for a in node[2]:
                walk(a)
        elif t in ("and", "or", "xor"):
            walk(node[1])
            walk(node[2])
        elif t == "not":
            walk(node[1])
        elif t == "cmp":
            walk(node[2])
            walk(node[3])
    walk(_Parser(text or "All").parse())
    return out
