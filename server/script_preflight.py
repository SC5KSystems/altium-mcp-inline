"""Static checks for a DelphiScript sandbox body, run before Altium sees it.

A bad identifier in a script sent to Altium does not fail cleanly: DelphiScript
compiles it, the statement dies at runtime, the debugger parks on it, and every
later script silently does nothing until someone stops the debugger. The two
real examples that motivated this module were `Result := 'x'` (the sandbox's
variable is ResultText) and `Log('x')` (the logger is SandboxLog).

check() tokenizes the body and rejects any identifier that is not known:

  * a bare name must be declared by the sandbox project itself (its globals and
    Run's locals), be a keyword or built-in routine, or be an API name that the
    repo's own .pas files use without declaring (PCBServer, MkSet, eTopLayer..)
    or that was proven on a live board (PROVEN_GLOBALS);
  * a name after a dot must have been used after a dot somewhere in the repo's
    .pas files, or be in PROVEN_MEMBERS.

A name the repo declares as a local variable (Board, Track, ...) is reported as
undeclared here: the body cannot declare variables, and an undeclared name is
exactly what compiles and then wedges. Every error carries the nearest known
name. allow_unknown=True turns identifier errors into warnings; it never
excuses Result, declarations, unbalanced blocks or a list used before Create.
"""

import difflib
import re
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVER_DIR.parent
SERVER_SANDBOX = SERVER_DIR / "SandboxScript" / "Sandbox.pas"
DEV_SANDBOX = REPO_DIR / "dev" / "sandbox" / "Sandbox.pas"
BEGIN_MARK = "// === BEGIN EXPERIMENT"
END_MARK = "// === END EXPERIMENT"

KEYWORDS = {
    "and", "array", "as", "begin", "case", "const", "div", "do", "downto", "else",
    "end", "except", "finally", "for", "function", "goto", "if", "in", "is", "label",
    "mod", "nil", "not", "of", "or", "procedure", "raise", "record", "repeat", "set",
    "shl", "shr", "then", "to", "try", "type", "until", "uses", "var", "while", "with",
    "xor", "true", "false", "out", "string",
}

# Built-in routines, types and constants of the script engine that the repo's
# units may not happen to use yet.
BUILTINS = {
    "Abs", "AnsiCompareText", "AnsiLowerCase", "AnsiPos", "AnsiUpperCase", "ArcTan",
    "Assigned", "BoolToStr", "Boolean", "Break", "Byte", "Cardinal", "ChangeFileExt",
    "Char", "Chr", "CompareStr", "CompareText", "Continue", "Copy", "Cos", "CreateDir",
    "Date", "DateTimeToStr", "DateToStr", "Dec", "Delete", "DeleteFile",
    "DirectoryExists", "Double", "Exit", "Exp", "Extended", "ExtractFileExt",
    "ExtractFileName", "ExtractFilePath", "FileExists", "FloatToStr", "ForceDirectories",
    "Format", "FormatDateTime", "FormatFloat", "Frac", "GetTickCount", "High",
    "IDispatch", "IInterface", "Inc", "IncludeTrailingPathDelimiter", "Insert", "Int",
    "Int64", "IntToHex", "IntToStr", "Integer", "Length", "Ln", "Low", "LowerCase",
    "Now", "Null", "Odd", "Ord", "Pi", "Pos", "QuotedStr", "Random", "Randomize",
    "Real", "RenameFile", "Round", "SameText", "SetLength", "ShowError", "ShowInfo",
    "ShowMessage", "ShowWarning", "Sin", "Single", "Sleep", "Sqr", "Sqrt",
    "StrToFloat", "StrToInt", "StrToIntDef", "StringOfChar", "StringReplace",
    "TInterfaceList", "TList", "TObject", "TStringList", "TStrings", "Time",
    "TimeToStr", "Trim", "TrimLeft", "TrimRight", "Trunc", "Unassigned", "UpperCase",
    "VarIsEmpty", "VarIsNull", "VarToStr", "VarType", "Variant", "WideString", "Word",
    "rfReplaceAll", "rfIgnoreCase",
}

# Calls proven on a live board (2026-11-02 and 2026-11-04) that the repo's own
# units do not all use yet. Globals are bare names; members follow a dot.
PROVEN_GLOBALS = {
    "PCBServer", "SchServer", "Client", "MkSet", "AllLayers", "eProcessAll",
    "eTrackObject", "eFillObject", "eViaObject", "eArcObject", "ePadObject",
    "eRegionObject", "ePolyObject", "eTextObject", "eComponentObject",
    "eComponentBodyObject", "eRuleObject", "eNetObject", "eNoDimension",
    "eCreate_Default", "c_Broadcast", "c_NoEventData", "PCBM_BeginModify",
    "PCBM_EndModify", "PCBM_BoardRegisteration", "MMsToCoord", "CoordToMMs",
    "MilsToCoord", "CoordToMils", "eTopLayer", "eBottomLayer", "eMultiLayer",
    "eImplementation", "TInterfaceList", "TPolySegment", "ePolySegmentLine",
    "ePolySegmentArc",
}
PROVEN_MEMBERS = {
    "GetPCBBoardByPath", "BoardIterator_Create", "AddFilter_ObjectSet",
    "AddFilter_LayerSet", "AddFilter_Method", "FirstPCBObject", "NextPCBObject",
    "BoardIterator_Destroy", "Name", "Net", "Text", "SourceUniqueId", "InComponent",
    "InPolygon", "PreProcess", "PostProcess", "SendMessageToRobots", "I_ObjectAddress",
    "Add", "Count", "Items", "RemovePCBObject", "GetDocumentByPath", "DoFileSave",
    "PCBObjectFactory", "x1", "y1", "x2", "y2", "Width", "Layer", "x1Location",
    "y1Location", "x2Location", "y2Location", "Rotation", "x", "y", "Size",
    "HoleSize", "LowLayer", "HighLayer", "AddPCBObject", "Rebuild", "MoveToXY",
    "Scope2Expression", "Scope1Expression", "GroupIterator_Create",
    "GroupIterator_Destroy", "PointCount", "Segments", "vx", "vy", "cx", "cy",
    "Radius", "Angle1", "Angle2", "Kind", "MoveByXY", "BoundingRectangle",
    "XLocation", "YLocation", "IsDesignator", "IsComment", "Component", "Model",
    "SetState", "SetState_FromModel", "OverallHeight", "UniqueId",
    "SchIterator_Create", "SchIterator_Destroy", "ModelName", "IsCurrent",
    "RemoveSchImplementation", "Create", "Free", "Clear", "Delete", "IndexOf",
    "Strings", "SaveToFile", "LoadFromFile", "ObjectId", "FileName", "Pattern",
    "PolygonType", "Left", "Right", "Top", "Bottom", "Descriptor", "Identifier",
}

# Names that exist but are dangerous in the sandbox.
HAZARDS = {
    "getcurrentpcbboard": "GetCurrentPCBBoard returned nil in the sandbox and using it "
                          "wedged Altium; use PCBServer.GetPCBBoardByPath(path)",
}

# Hand-written answers for mistakes that edit distance alone answers badly.
HINTS = {
    "log": ("SandboxLog", "the sandbox logger is SandboxLog"),
    "writeln": ("SandboxLog", "there is no console; log with SandboxLog"),
    "rotation3dx": ("SetState", "set 3D rotation with M := Body.Model; "
                                "M.SetState(RotX, RotY, RotZ, Dz); Body.Model := M; "
                                "Body.SetState_FromModel"),
    "rotation3dy": ("SetState", "see Rotation3DX: use Model.SetState"),
    "rotation3dz": ("SetState", "see Rotation3DX: use Model.SetState"),
}

CLASS_TYPES = {"tstringlist", "tinterfacelist", "tlist", "tstrings"}
INTERNAL = {"loglines", "logpath", "outpath", "outlines", "run", "sandboxlog"}

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<line_comment>//[^\n]*)
  | (?P<brace_comment>\{[^}]*\})
  | (?P<paren_comment>\(\*.*?\*\))
  | (?P<string>'(?:[^'\n]|'')*')
  | (?P<char>\#(?:\d+|\$[0-9A-Fa-f]+))
  | (?P<number>\$[0-9A-Fa-f]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>:=|<=|>=|<>|\.\.|[-+*/=<>()\[\],;:.^@])
""", re.VERBOSE | re.DOTALL)


class PreflightError(ValueError):
    pass


class Token:
    __slots__ = ("kind", "text", "line", "col")

    def __init__(self, kind, text, line, col):
        self.kind, self.text, self.line, self.col = kind, text, line, col

    @property
    def low(self):
        return self.text.lower()

    def __repr__(self):
        return f"Token({self.kind},{self.text!r},{self.line}:{self.col})"


def tokenize(src):
    """Split Pascal source into tokens, dropping whitespace and comments."""
    out = []
    pos, line, line_start = 0, 1, 0
    n = len(src)
    while pos < n:
        m = _TOKEN_RE.match(src, pos)
        if not m:
            ch = src[pos]
            col = pos - line_start + 1
            if ch == "'":
                raise PreflightError(f"line {line} col {col}: unterminated string")
            if ch == "{" or src.startswith("(*", pos):
                raise PreflightError(f"line {line} col {col}: unterminated comment")
            raise PreflightError(f"line {line} col {col}: unexpected character {ch!r}")
        kind = m.lastgroup
        text = m.group()
        if kind in ("ident", "number", "string", "char", "op"):
            out.append(Token(kind, text, line, pos - line_start + 1))
        nl = text.count("\n")
        if nl:
            line += nl
            line_start = pos + text.rfind("\n") + 1
        pos = m.end()
    return out


def split_template(src):
    """(text before the experiment body, text after it) of a sandbox template."""
    if BEGIN_MARK not in src or END_MARK not in src:
        return src, ""
    pre, rest = src.split(BEGIN_MARK, 1)
    _, post = rest.split(END_MARK, 1)
    return pre, post


def strip_body(src):
    pre, post = split_template(src)
    return pre + "\n" + post


def _skip_to_semicolon(toks, i):
    depth = 0
    while i < len(toks):
        t = toks[i]
        if t.text in ("(", "["):
            depth += 1
        elif t.text in (")", "]"):
            depth -= 1
        elif t.low == "record":
            depth += 1
        elif t.low == "end" and depth > 0:
            depth -= 1
        elif t.text == ";" and depth <= 0:
            return i + 1
        i += 1
    return i


def declarations(toks):
    """Names a unit declares: routines, parameters, variables, constants, types.

    Returns {lowercase name: (spelling, kind, type)}: kind is routine, param,
    var, const or type; type is the lowercased declared type of a var or param
    ('' when none). Enumeration members in a type section count as constants.
    """
    found = {}

    def add(tok, kind, typ=""):
        found.setdefault(tok.low, (tok.text, kind, typ))

    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        low = t.low if t.kind == "ident" else None
        if low in ("procedure", "function"):
            i += 1
            if i < n and toks[i].kind == "ident":
                add(toks[i], "routine")
                i += 1
            if i < n and toks[i].text == "(":
                i += 1
                group = []
                while i < n and toks[i].text != ")":
                    tk = toks[i]
                    if tk.kind == "ident" and tk.low in ("var", "const", "out") and not group:
                        i += 1
                        continue
                    if tk.kind == "ident":
                        group.append(tk)
                    elif tk.text == ":":
                        typ = toks[i + 1].low if i + 1 < n else ""
                        for g in group:
                            add(g, "param", typ)
                        group = []
                        while i < n and toks[i].text not in (";", ")"):
                            i += 1
                        continue
                    elif tk.text == ";":
                        group = []
                    i += 1
            i = _skip_to_semicolon(toks, i)
            continue
        if low == "var":
            i += 1
            while i < n and toks[i].kind == "ident" and toks[i].low not in KEYWORDS:
                names = [toks[i]]
                j = i + 1
                while j + 1 < n and toks[j].text == "," and toks[j + 1].kind == "ident":
                    names.append(toks[j + 1])
                    j += 2
                if j < n and toks[j].text == ":":
                    typ = toks[j + 1].low if j + 1 < n else ""
                    for nm in names:
                        add(nm, "var", typ)
                    i = _skip_to_semicolon(toks, j)
                elif j < n and toks[j].text == ";":
                    for nm in names:
                        add(nm, "var")
                    i = j + 1
                else:
                    break
            continue
        if low == "const":
            i += 1
            while i + 1 < n and toks[i].kind == "ident" and toks[i].low not in KEYWORDS \
                    and toks[i + 1].text in ("=", ":"):
                add(toks[i], "const")
                i = _skip_to_semicolon(toks, i + 1)
            continue
        if low == "type":
            i += 1
            while i + 1 < n and toks[i].kind == "ident" and toks[i].low not in KEYWORDS \
                    and toks[i + 1].text == "=":
                add(toks[i], "type")
                j = i + 2
                if j < n and toks[j].text == "(":
                    j += 1
                    while j < n and toks[j].text != ")":
                        if toks[j].kind == "ident":
                            add(toks[j], "const")
                        j += 1
                i = _skip_to_semicolon(toks, j)
            continue
        i += 1
    return found


def _uses(toks):
    """(bare names, member names) used in a token stream, lowercased -> spelling."""
    bare, members = {}, {}
    for k, t in enumerate(toks):
        if t.kind != "ident":
            continue
        if k and toks[k - 1].text == ".":
            members.setdefault(t.low, t.text)
        else:
            bare.setdefault(t.low, t.text)
    return bare, members


class Vocabulary:
    """What a body checked against one sandbox may use."""

    def __init__(self, label, sandbox, api_globals, members, elsewhere, class_vars, ready):
        self.label = label
        self.sandbox = sandbox          # lower -> (spelling, kind, type) declared by the sandbox
        self.api_globals = api_globals  # lower -> spelling
        self.members = members          # lower -> spelling
        self.elsewhere = elsewhere      # lower -> (spelling, kind, file) declared only elsewhere
        self.class_vars = class_vars    # sandbox variables of a list type
        self.ready = ready              # of those, the ones the template creates before the body

    def bare_known(self, low):
        return low in KEYWORDS or low in self.sandbox or low in self.api_globals

    def bare_candidates(self):
        out = dict(self.api_globals)
        out.update({k: v[0] for k, v in self.sandbox.items()})
        return out

    def scratch(self):
        names = [v[0] for k, v in self.sandbox.items()
                 if v[1] == "var" and k not in INTERNAL and k != "resulttext"]
        return ", ".join(names)


def corpus_files(repo=REPO_DIR):
    files = sorted((repo / "server" / "AltiumScript").glob("*.pas"))
    files += sorted((repo / "dev").glob("*.pas"))
    for sb in (repo / "server" / "SandboxScript" / "Sandbox.pas",
               repo / "dev" / "sandbox" / "Sandbox.pas"):
        if sb.is_file():
            files.append(sb)
    return files


def _label(path):
    try:
        return Path(path).resolve().relative_to(REPO_DIR).as_posix()
    except ValueError:
        return Path(path).name


_CACHE = {}
_CREATE_RE = re.compile(r"\b(\w+)\s*:=\s*(?:TStringList|TInterfaceList|TList)\.Create", re.I)


def build_vocabulary(sandbox_pas=SERVER_SANDBOX, corpus=None):
    """Allow-lists for bodies run in sandbox_pas, built from the repo's .pas files."""
    sandbox_pas = Path(sandbox_pas)
    corpus = [Path(p) for p in (corpus if corpus is not None else corpus_files())]
    key = (str(sandbox_pas.resolve()), sandbox_pas.stat().st_mtime_ns,
           tuple((str(p), p.stat().st_mtime_ns) for p in corpus if p.is_file()))
    if key in _CACHE:
        return _CACHE[key]

    template = sandbox_pas.read_text(encoding="utf-8", errors="replace")
    pre, _post = split_template(template)
    sandbox = declarations(tokenize(strip_body(template)))
    class_vars = {k for k, v in sandbox.items() if v[1] == "var" and v[2] in CLASS_TYPES}
    ready = {m.group(1).lower() for m in _CREATE_RE.finditer(pre)} & class_vars

    declared, api_bare, members = {}, {}, {}
    for p in corpus:
        if not p.is_file():
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if p.name == "Sandbox.pas":
            src = strip_body(src)
        try:
            toks = tokenize(src)
        except PreflightError:
            continue
        for low, (spelling, kind, _typ) in declarations(toks).items():
            declared.setdefault(low, (spelling, kind, _label(p)))
        bare, mem = _uses(toks)
        for low, sp in bare.items():
            api_bare.setdefault(low, sp)
        for low, sp in mem.items():
            members.setdefault(low, sp)

    api_globals = {low: sp for low, sp in api_bare.items()
                   if low not in declared and low not in KEYWORDS and low != "result"}
    for name in BUILTINS | PROVEN_GLOBALS:
        api_globals.setdefault(name.lower(), name)
    for name in PROVEN_MEMBERS:
        members.setdefault(name.lower(), name)
    elsewhere = {low: v for low, v in declared.items()
                 if low not in sandbox and low not in api_globals}
    vocab = Vocabulary(_label(sandbox_pas), sandbox, api_globals,
                       members, elsewhere, class_vars, ready)
    _CACHE.clear()
    _CACHE[key] = vocab
    return vocab


def _nearest(low, candidates, n=3):
    hits = difflib.get_close_matches(low, list(candidates), n=n, cutoff=0.6)
    if not hits and len(low) >= 3:
        # containment catches renamed helpers (Log -> SandboxLog)
        hits = sorted((c for c in candidates if low in c), key=len)[:n]
    return [candidates[h] for h in hits]


def _with_scopes(toks):
    """Token indices inside `with X do <statement>` bodies."""
    inside = set()
    i, n = 0, len(toks)
    while i < n:
        if toks[i].kind == "ident" and toks[i].low == "with":
            j = i + 1
            while j < n and not (toks[j].kind == "ident" and toks[j].low == "do"):
                j += 1
            j += 1
            k = j
            if j < n and toks[j].low == "begin":
                depth = 0
                while k < n:
                    if toks[k].kind == "ident" and toks[k].low in ("begin", "case", "try"):
                        depth += 1
                    elif toks[k].kind == "ident" and toks[k].low == "end":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
            else:
                while k < n and toks[k].text != ";" and toks[k].low not in ("end", "else"):
                    k += 1
            inside.update(range(j, k + 1))
            i = j
        i += 1
    return inside


def _problem(tok, message, suggestion=None, name=None):
    p = {"line": tok.line, "col": tok.col, "message": message}
    if name is not None:
        p["name"] = name
    if suggestion:
        p["suggestion"] = suggestion
    return p


def check(body, sandbox_pas=SERVER_SANDBOX, corpus=None, allow_unknown=False):
    """Check a sandbox body. Returns {"ok", "errors", "warnings", "sandbox"}.

    ok is False when any error remains; a caller must not send the body to
    Altium then. Line numbers are 1-based within the body.
    """
    errors, warnings = [], []
    vocab = build_vocabulary(sandbox_pas, corpus)
    try:
        toks = tokenize(body)
    except PreflightError as e:
        return {"ok": False, "errors": [{"line": 0, "col": 0, "message": str(e)}],
                "warnings": [], "sandbox": vocab.label}
    soft = warnings if allow_unknown else errors

    depth = 0
    for t in toks:
        if t.kind != "ident":
            continue
        if t.low in ("var", "const", "type", "procedure", "function", "label"):
            errors.append(_problem(t, f"'{t.text}' declarations are not allowed: the body is "
                                      "pasted inside procedure Run. Reuse the sandbox's scratch "
                                      f"variables ({vocab.scratch()})", name=t.text))
        elif t.low in ("begin", "case", "try"):
            depth += 1
        elif t.low == "end":
            depth -= 1
            if depth < 0:
                errors.append(_problem(t, "'end' without a matching begin, case or try"))
                depth = 0
    if depth > 0:
        errors.append({"line": toks[-1].line, "col": 0,
                       "message": f"{depth} begin/case/try block(s) never closed with end"})

    in_with = _with_scopes(toks)
    reported = set()
    for k, t in enumerate(toks):
        if t.kind != "ident":
            continue
        is_member = k > 0 and toks[k - 1].text == "."
        key = ("m" if is_member else "b", t.low)
        if key in reported:
            continue
        if not is_member and t.low == "result":
            reported.add(key)
            errors.append(_problem(t, "the body runs inside procedure Run, which has no Result; "
                                      "assign the string to ResultText", "ResultText", t.text))
            continue
        if t.low in HAZARDS:
            reported.add(key)
            soft.append(_problem(t, HAZARDS[t.low], name=t.text))
            continue
        if is_member:
            if t.low in vocab.members:
                continue
            reported.add(key)
            if t.low in HINTS:
                sugg, why = HINTS[t.low]
                soft.append(_problem(t, f"unknown member '.{t.text}': {why}", sugg, t.text))
            else:
                near = _nearest(t.low, vocab.members)
                soft.append(_problem(t, f"unknown member '.{t.text}': no .pas file in this repo "
                                        "uses it and it is not on the proven list",
                                     ", ".join(near) or None, t.text))
            continue
        if vocab.bare_known(t.low) or (k in in_with and t.low in vocab.members):
            continue
        reported.add(key)
        if t.low in HINTS:
            sugg, why = HINTS[t.low]
            soft.append(_problem(t, f"unknown name '{t.text}': {why}", sugg, t.text))
        elif t.low in vocab.elsewhere:
            spelling, kind, where = vocab.elsewhere[t.low]
            near = ", ".join(_nearest(t.low, {k2: v[0] for k2, v in vocab.sandbox.items()})) or None
            if kind == "routine":
                why = f"'{t.text}' is a routine in {where}, which {vocab.label} does not include"
            elif kind == "const":
                why = (f"'{t.text}' is a constant in {where}, which {vocab.label} does not "
                       "include; an undefined constant compiles and then kills the script")
            else:
                why = (f"'{t.text}' is declared in {where}, not in {vocab.label}; an undeclared "
                       f"name compiles and then wedges the engine. Use a scratch variable "
                       f"({vocab.scratch()})")
            soft.append(_problem(t, why, near, t.text))
        else:
            near = _nearest(t.low, vocab.bare_candidates())
            soft.append(_problem(t, f"unknown name '{t.text}'", ", ".join(near) or None, t.text))

    for t in toks:
        if t.kind == "ident" and t.low == "exit":
            msg = ("Exit leaves procedure Run before ResultText is written, so a healthy run "
                   "looks wedged; use if/else instead")
            (errors if Path(sandbox_pas).resolve() == SERVER_SANDBOX.resolve() else warnings
             ).append(_problem(t, msg, name="Exit"))
            break
    _flow_checks(toks, vocab, errors, warnings)
    return {"ok": not errors, "errors": errors, "warnings": warnings, "sandbox": vocab.label}


def _flow_checks(toks, vocab, errors, warnings):
    lows = [t.low if t.kind == "ident" else t.text for t in toks]
    member = [k > 0 and toks[k - 1].text == "." for k in range(len(toks))]

    # A list variable used before it is created wedges the engine.
    ready = set(vocab.ready)
    for k, t in enumerate(toks):
        if t.kind != "ident" or member[k] or t.low not in vocab.class_vars or t.low in ready:
            continue
        if k + 1 < len(toks) and lows[k + 1] == ":=":
            ready.add(t.low)
            continue
        errors.append(_problem(t, f"{t.text} is used before '{t.text} := TStringList.Create' "
                                  "(or another assignment); that wedges the engine", name=t.text))
        ready.add(t.low)

    used = {lows[k] for k in range(len(toks)) if member[k]}
    if "component" in used and "incomponent" not in used:
        k = next(i for i in range(len(toks)) if member[i] and lows[i] == "component")
        warnings.append(_problem(toks[k], "reads .Component without checking .InComponent; "
                                          "pads without a component (mounting holes) wedge the "
                                          "engine there"))
    for k in range(len(toks) - 1):
        if member[k] and lows[k] == "net" and lows[k + 1] == ":=" and "addpcbobject" not in used:
            warnings.append(_problem(toks[k], "assigning .Net does not register the primitive in "
                                              "the net's member group, which DRC reads; also call "
                                              "NetObj.AddPCBObject(Prim) inside Begin/EndModify"))
            break
    if "rebuild" in used and ({"segments", "pointcount"} & used) and "movebyxy" not in used:
        k = next(i for i in range(len(toks)) if member[i] and lows[i] == "rebuild")
        warnings.append(_problem(toks[k], "Rebuild after an outline edit without "
                                          "Poly.MoveByXY(1, 0); Poly.MoveByXY(-1, 0): a stale "
                                          "BoundingRectangle makes the pour ignore obstacles"))
    open_iter = False
    for k, low in enumerate(lows):
        if low == "boarditerator_create":
            open_iter = True
        elif low == "boarditerator_destroy":
            open_iter = False
        elif low == "removepcbobject" and open_iter:
            warnings.append(_problem(toks[k], "RemovePCBObject while a board iterator is open; "
                                              "collect into a TInterfaceList and remove after "
                                              "BoardIterator_Destroy"))
            break
    if "showmessage" in lows:
        k = lows.index("showmessage")
        warnings.append(_problem(toks[k], "ShowMessage opens a modal dialog that blocks an "
                                          "unattended run; log with SandboxLog instead"))


def format_report(report):
    lines = []
    for kind in ("errors", "warnings"):
        for p in report[kind]:
            s = f"{kind[:-1]}: line {p['line']}: {p['message']}"
            if p.get("suggestion"):
                s += f" (nearest known: {p['suggestion']})"
            lines.append(s)
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Preflight a DelphiScript sandbox body")
    ap.add_argument("body", help="file holding the body, or - for stdin")
    ap.add_argument("--sandbox", default=str(SERVER_SANDBOX))
    ap.add_argument("--allow-unknown", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    src = sys.stdin.read() if a.body == "-" else Path(a.body).read_text(encoding="utf-8")
    rep = check(src, a.sandbox, allow_unknown=a.allow_unknown)
    print(json.dumps(rep, indent=2) if a.json else (format_report(rep) or "ok"))
    sys.exit(0 if rep["ok"] else 1)
