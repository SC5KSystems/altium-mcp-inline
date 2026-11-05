"""Design rules from Rules6, resolved per object the way Altium does.

For a rule kind, the enabled rule with the lowest PRIORITY number whose
scope(s) match wins. Binary rules (clearance and friends) match when scope 1
takes one object and scope 2 the other, in either order. A rule whose scope
uses a function scope.py cannot evaluate is listed in `unevaluated` and never
applied, so a check that depends on it says so instead of guessing.
"""

from . import layers as L
from .pcbdoc import parse_bool, parse_float, parse_length
from .scope import ScopeError, compile_scope

BINARY_KINDS = {"Clearance", "ComponentClearance", "HoleToHoleClearance",
                "SilkToSolderMaskClearance", "SilkToSilkClearance", "ShortCircuit",
                "BoardOutlineClearance", "SilkToBoardRegionClearance", "DiffPairsRouting"}

# OBJECTCLEARANCES names -> object classifier used below
CLEARANCE_OBJECT = {"track": "Track", "arc": "Arc", "via": "Via", "fill": "Fill",
                    "text": "Text", "plane": "Poly"}


def clearance_class(obj):
    k = getattr(obj, "KIND", "")
    if k == "pad":
        return "THPad" if obj.hole > 0 else "SMDPad"
    if k == "region":
        return "Poly" if obj.polygon is not None else "Region"
    if k in ("track", "arc") and getattr(obj, "polygon", None) is not None:
        return "Poly"
    return CLEARANCE_OBJECT.get(k, "Region")


class CompiledRule:
    def __init__(self, rule):
        self.rule = rule
        self.error = None
        try:
            self.s1 = compile_scope(rule.scope1)
            self.s2 = compile_scope(rule.scope2)
        except ScopeError as e:
            self.error = str(e)
            self.s1 = self.s2 = None
        self.matrix = {}
        oc = rule.props.get("OBJECTCLEARANCES") or ""
        for part in oc.split(";"):
            if ":" not in part:
                continue
            pair, v = part.rsplit(":", 1)
            a, _, b = pair.partition("-")
            try:
                val = float(v) * 2.54e-6
            except ValueError:
                continue
            if val > 0:  # rules in Simple mode carry all-zero cells: use the generic gap
                a, b = a.replace("ClearanceObj_", ""), b.replace("ClearanceObj_", "")
                self.matrix[(a, b)] = self.matrix[(b, a)] = val

    @property
    def kind(self):
        return self.rule.kind

    @property
    def name(self):
        return self.rule.name

    def length(self, key, default=0.0):
        return parse_length(self.rule.props.get(key), default)


class RuleSet:
    def __init__(self, doc):
        self.doc = doc
        self.compiled = [CompiledRule(r) for r in doc.rules]
        self.unevaluated = [{"rule": c.name, "kind": c.kind, "error": c.error}
                            for c in self.compiled if c.error and c.rule.enabled]
        self._cache = {}
        self._kinds = {}

    def of_kind(self, kind):
        got = self._kinds.get(kind)
        if got is None:
            got = self._kinds[kind] = sorted(
                (c for c in self.compiled if c.kind == kind and c.rule.enabled and c.error is None),
                key=lambda c: c.rule.priority)
        return got

    def has_unevaluated(self, kind):
        return [u for u in self.unevaluated if u["kind"] == kind]

    def _match(self, c, which, obj):
        key = (c.rule.index, which, getattr(obj, "storage", type(obj).__name__),
               getattr(obj, "index", id(obj)))
        v = self._cache.get(key)
        if v is None:
            scope = c.s1 if which == 1 else c.s2
            v = scope.is_all or scope.match(self.doc, obj)
            self._cache[key] = v
        return v

    def best(self, kind, a, b=None):
        for c in self.of_kind(kind):
            if b is None:
                if self._match(c, 1, a):
                    return c
            elif (self._match(c, 1, a) and self._match(c, 2, b)) or \
                    (self._match(c, 1, b) and self._match(c, 2, a)):
                return c
        return None

    # ------------------------------------------------------------ values
    def clearance(self, a, b, kind="Clearance", classes=None):
        """(gap mm, rule) for two objects, or (None, None) if no rule applies.

        classes overrides the (a, b) object classes used for the rule's
        per-object matrix, e.g. ("Poly", "Hole") for a drill hole.
        """
        c = self.best(kind, a, b)
        if c is None:
            return None, None
        pair = tuple(classes) if classes else (clearance_class(a), clearance_class(b))
        if pair in c.matrix:
            return c.matrix[pair], c
        return c.length("GAP", c.length("GENERICCLEARANCE")), c

    def max_clearance(self, kind="Clearance"):
        vals = []
        for c in self.of_kind(kind):
            vals.append(c.length("GAP", c.length("GENERICCLEARANCE")))
            vals.extend(c.matrix.values())
        return max(vals) if vals else 0.0

    def width(self, obj):
        """Width limits for a track or arc on its layer. Rules may carry
        per-layer values (TOPLAYER_MINWIDTH, MIDLAYERn_MAXWIDTH, ...) that
        override MINLIMIT / PREFEREDWIDTH / MAXLIMIT."""
        c = self.best("Width", obj)
        if c is None:
            return None
        lay = getattr(obj, "layer", None)
        prefix = None
        if lay == L.TOP:
            prefix = "TOPLAYER_"
        elif lay == L.BOTTOM:
            prefix = "BOTTOMLAYER_"
        elif lay is not None and 2 <= lay <= 31:
            prefix = f"MIDLAYER{lay - 1}_"

        def pick(key, generic):
            if prefix and c.rule.props.get(prefix + key) is not None:
                return c.length(prefix + key)
            return c.length(generic)

        return {"min": pick("MINWIDTH", "MINLIMIT"), "preferred": pick("PREFWIDTH", "PREFEREDWIDTH"),
                "max": pick("MAXWIDTH", "MAXLIMIT"), "rule": c.name}

    def hole_size(self, obj):
        c = self.best("HoleSize", obj)
        if c is None:
            return None
        if not parse_bool(c.rule.props.get("ABSOLUTEVALUES"), True):
            return {"rule": c.name, "relative": True}
        return {"min": c.length("MINLIMIT"), "max": c.length("MAXLIMIT"), "rule": c.name}

    def annular_ring(self, obj):
        c = self.best("MinimumAnnularRing", obj)
        return (c.length("MINIMUMRING"), c) if c else (None, None)

    def hole_to_hole(self, a, b):
        c = self.best("HoleToHoleClearance", a, b)
        return (c.length("GAP"), c) if c else (None, None)

    def board_outline(self, obj):
        c = self.best("BoardOutlineClearance", obj, obj)
        if c is None:
            return None, None
        cls = clearance_class(obj)
        for (x, _y), v in c.matrix.items():
            if x == cls:
                return v, c
        return c.length("GAP", c.length("GENERICCLEARANCE")), c

    def component_clearance(self, a, b):
        c = self.best("ComponentClearance", a, b)
        return (c.length("GAP"), c) if c else (None, None)

    def short_allowed(self, a, b):
        c = self.best("ShortCircuit", a, b)
        return bool(c and parse_bool(c.rule.props.get("ALLOWED"))), c

    def silk_to_mask(self, a, b):
        c = self.best("SilkToSolderMaskClearance", a, b)
        return (c.length("MINSILKSCREENTOMASKGAP"), c) if c else (None, None)

    def solder_mask_expansion(self, obj):
        c = self.best("SolderMaskExpansion", obj)
        return c.length("EXPANSION") if c else 0.1016

    def tented(self, obj, side):
        """True when a via's mask opening on `side` ("top"/"bottom") is
        closed by a SolderMaskExpansion rule's tenting flag. Without a rule
        that says either way, vias count as tented."""
        c = self.best("SolderMaskExpansion", obj)
        if c is None:
            return True
        p = c.rule.props
        v = p.get("ISTENTINGTOP") if side == "top" else \
            (p.get("ISTENTINGBOTTOM") if p.get("ISTENTINGBOTTOM") is not None else p.get("ISTENTINGTOP"))
        if v is None:
            return True
        return parse_bool(v)

    def polygon_connect(self, a, b):
        """How pad or via `a` joins polygon copper `b`: style (relief, direct
        or noconnect), spoke conductor_width, entries, air_gap and angle.

        Newer rules keep separate settings per object (THPAD., SMDPAD.,
        VIA. keys); older ones one set for all.
        """
        c = self.best("PolygonConnect", a, b)
        if c is None:
            return None
        p = c.rule.props
        k = getattr(a, "KIND", "")
        prefix = "VIA." if k == "via" else "SMDPAD." if k == "pad" and a.is_smd else "THPAD."

        def get(key, default=None):
            v = p.get(prefix + key)
            return v if v is not None else p.get(key, default)

        return {"style": (get("CONNECTSTYLE") or "Relief").lower(),
                "conductor_width": parse_length(get("RELIEFCONDUCTORWIDTH"), 0.254),
                "entries": int(parse_float(get("RELIEFENTRIES", "4"))),
                "air_gap": parse_length(get("AIRGAPWIDTH"), 0.254),
                "angle": 45 if "45" in (get("POLYGONRELIEFANGLE") or "") else 90,
                "rule": c.name}

    def via_style(self, obj):
        c = self.best("RoutingVias", obj)
        if c is None:
            return None
        return {"min_diameter": c.length("MINWIDTH"), "max_diameter": c.length("MAXWIDTH"),
                "min_hole": c.length("MINHOLEWIDTH"), "max_hole": c.length("MAXHOLEWIDTH"),
                "diameter": c.length("WIDTH"), "hole": c.length("HOLEWIDTH"), "rule": c.name}


def dead_scopes(doc):
    """Rules whose scope names something that does not exist or is empty: a net
    class, pad class, component class, net, component or room. Such a branch
    can never match, so "(InNetClass('Power')) or IsVia" quietly becomes
    "IsVia" when there is no class Power. One row per dead predicate."""
    from .scope import ScopeError, predicates
    classes = {}
    for c in doc.classes:
        classes.setdefault((c.kind, c.name.upper()), []).extend(c.members)
    nets = {n.name.upper() for n in doc.nets}
    comps = {c.designator.upper() for c in doc.components}
    rooms = {r.name.upper() for r in doc.rules if r.kind == "RoomDefinition"}
    kinds = {"INNETCLASS": "net", "INPADCLASS": "pad", "INCOMPONENTCLASS": "component",
             "INPOLYGONCLASS": "polygon", "INDIFFERENTIALPAIRCLASS": "diff_pair"}
    out = []
    for r in doc.rules:
        for which, expr in (("scope1", r.scope1), ("scope2", r.scope2)):
            try:
                preds = predicates(expr)
            except ScopeError as e:
                out.append({"rule": r.name, "kind": r.kind, "priority": r.priority,
                            "where": which, "predicate": None, "scope": expr,
                            "problem": f"scope does not parse: {e}"})
                continue
            for name, arg in preds:
                n, a = name.upper(), arg.upper()
                why = None
                if n in kinds:
                    k = kinds[n]
                    if (k, a) not in classes:
                        why = f"no {k.replace('_', ' ')} class named {arg!r}"
                    elif not classes[(k, a)] and not a.startswith("ALL "):
                        # Altium's built-in "All ..." classes are empty when the board
                        # simply has none of that kind
                        why = f"{k.replace('_', ' ')} class {arg!r} has no members"
                elif n == "INNET" and a not in nets:
                    why = f"no net named {arg!r}"
                elif n == "INCOMPONENT" and a not in comps:
                    why = f"no component {arg!r}"
                elif n in ("WITHINROOM", "TOUCHESROOM", "INROOM") and a not in rooms:
                    why = f"no room named {arg!r}"
                if why:
                    out.append({"rule": r.name, "kind": r.kind, "priority": r.priority,
                                "where": which, "predicate": f"{name}('{arg}')", "scope": expr,
                                "problem": why})
    return out


def cmd_rule_scopes(args):
    import json
    from .pcbdoc import PcbDoc
    rows = dead_scopes(PcbDoc.open(args.board))
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        print(f"  {r['rule']} ({r['kind']}, {r['where']}): {r['predicate']}: {r['problem']}")
    print(f"{len(rows)} dead scope predicate(s)")
    return 0


def register_commands(command):
    def build(p):
        p.add_argument("board")
        p.add_argument("--json", action="store_true")
    command("rule-scopes", cmd_rule_scopes,
            "rule scopes that name a missing or empty class, net, part or room", build)
