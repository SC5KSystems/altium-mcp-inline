"""Evaluate Altium rule-scope expressions against pads, vias and polygons.

Scopes look like:  (InNetClass('HighCurrent')) or IsVia or WithinRoom('PowerRoom')
They come from the design file, so they are parsed with a small recursive-descent
parser rather than eval(). Unknown predicates evaluate False and are recorded in
`Scope.unknown`, so a rule that depends on something this code cannot see (a room,
for instance) is reported instead of silently matching.
"""
import re

_TOKEN = re.compile(r"\s*(?:(\()|(\))|'([^']*)'|(&&|\|\|)|(!)|([A-Za-z_][A-Za-z0-9_.]*)|(<=|>=|<>|<|>|=)|(-?[0-9]+(?:\.[0-9]+)?))")


class ScopeError(ValueError):
    pass


class Ctx:
    """What a predicate can see about the object being tested."""

    def __init__(self, doc, kind, net="", comp="", pad="", layer="", polygon=""):
        self.doc, self.kind = doc, kind          # kind: pad | via | polygon | track
        self.net, self.comp, self.pad, self.layer, self.polygon = net, comp, pad, layer, polygon


class Scope:
    def __init__(self, doc):
        self.doc = doc
        self.unknown = set()
        self._classes = {}
        for c in doc.classes:
            self._classes.setdefault((c.kind, c.name), set()).update(c.members)

    def matches(self, expr, ctx):
        toks = self._tokenize(expr or "All")
        self._t, self._i, self._ctx = toks, 0, ctx
        val = self._or()
        if self._i != len(toks):
            raise ScopeError("trailing tokens in scope: %r" % expr)
        return val

    # -- tokenizer / parser
    @staticmethod
    def _tokenize(expr):
        out, pos = [], 0
        while pos < len(expr):
            if expr[pos:].strip() == "":
                break
            m = _TOKEN.match(expr, pos)
            if not m:
                raise ScopeError("cannot parse scope near %r" % expr[pos:pos + 20])
            pos = m.end()
            if m.group(1):
                out.append(("(", None))
            elif m.group(2):
                out.append((")", None))
            elif m.group(3) is not None:
                out.append(("str", m.group(3)))
            elif m.group(4):
                out.append(("or" if m.group(4) == "||" else "and", None))
            elif m.group(5):
                out.append(("not", None))
            elif m.group(7):
                out.append(("cmp", m.group(7)))
            elif m.group(8):
                out.append(("num", m.group(8)))
            else:
                w = m.group(6)
                lw = w.lower()
                out.append((lw, None) if lw in ("and", "or", "not") else ("id", w))
        return out

    def _peek(self):
        return self._t[self._i][0] if self._i < len(self._t) else None

    def _or(self):
        v = self._and()
        while self._peek() == "or":
            self._i += 1
            r = self._and()
            v = v or r
        return v

    def _and(self):
        v = self._not()
        while self._peek() == "and":
            self._i += 1
            r = self._not()
            v = v and r
        return v

    def _not(self):
        if self._peek() == "not":
            self._i += 1
            return not self._not()
        return self._atom()

    def _atom(self):
        k = self._peek()
        if k == "(":
            self._i += 1
            v = self._or()
            if self._peek() != ")":
                raise ScopeError("missing )")
            self._i += 1
            return v
        if k != "id":
            raise ScopeError("expected a predicate")
        name = self._t[self._i][1]
        self._i += 1
        arg = None
        if self._peek() == "cmp":                 # e.g. CompPinCount < 5: a numeric test we do not evaluate
            self._i += 1
            if self._peek() == "num":
                self._i += 1
            self.unknown.add(name + " (numeric comparison)")
            return False
        if self._peek() == "(":
            self._i += 1
            if self._peek() == "str":
                arg = self._t[self._i][1]
                self._i += 1
            if self._peek() != ")":
                raise ScopeError("missing ) after argument")
            self._i += 1
        return self._call(name, arg)

    # -- predicates
    def _call(self, name, arg):
        c = self._ctx
        n = name.lower()
        if n == "all":
            return True
        if n == "isvia":
            return c.kind == "via"
        if n == "ispad":
            return c.kind == "pad"
        if n == "innet":
            return c.net == arg
        if n == "innetclass":
            return c.net in self._classes.get((0, arg), ())
        if n == "inpadclass":
            return ("%s.%s" % (c.comp, c.pad)) in self._classes.get((3, arg), ())
        if n == "incomponent":
            return c.comp == arg
        if n == "incomponentclass":
            return c.comp in self._classes.get((1, arg), ())
        if n == "onlayer":
            return c.layer == arg
        if n == "inpolygon":
            return c.polygon == arg
        self.unknown.add("%s(%s)" % (name, arg if arg is not None else ""))
        return False


def predicates(expr):
    """(name, argument) for every predicate mentioned in a scope expression."""
    toks = Scope._tokenize(expr or "All")
    out, i = [], 0
    while i < len(toks):
        if toks[i][0] == "id":
            name, arg = toks[i][1], None
            if i + 2 < len(toks) and toks[i + 1][0] == "(" and toks[i + 2][0] == "str":
                arg = toks[i + 2][1]
            out.append((name, arg))
        i += 1
    return out
