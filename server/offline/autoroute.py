"""Autorouter bridge: route signal nets with Freerouting, back as an EditPlan.

    python -m offline autoroute board.PcbDoc [--nets S1 'SIG*'] [-o plan.json]
        [--keep-fixed NET ... | --keep-fixed none] [--check] [--json]
        [--passes 20] [--timeout 900] [--freerouting freerouting.jar] [--java java]
        [--dsn work.dsn] [--ses routed.ses] [--include-power] [--pours repour|fixed]
        [--via 0.5 0.25]

Without --nets it routes every net with an open connection, except power
nets: those (pours, power-like names, wide copper; see power_nets) belong to
the power-copper planner and are left out unless named or --include-power.
Existing copper stays as it is (--keep-fixed names the nets whose wiring
must not move; "none" lets the router redo the routed nets' free tracks and
vias, which the plan then removes first).

Freerouting (GPL, github.com/freerouting/freerouting) runs as a separate
Java process; nothing of it is in this repo. Find it with --freerouting,
$FREEROUTING_JAR, or `python -m offline autoroute --fetch` (downloads the
pinned release from Maven Central and checks its SHA-1). Freerouting 2.4 and
later need Java 25: --java, $FREEROUTING_JAVA or $JAVA_HOME. It runs
headless with analytics, its API and MCP servers off, its settings and logs
in the work folder, and HTTP(S) pointed at a closed local port, so board
data stays on the machine. With --ses it only imports a session routed
elsewhere (e.g. Freerouting's GUI).

The plan adds tracks and vias for the routed nets only; anything else the
router changed is ignored and counted. --check applies the plan in memory
and runs the DRC before and after, reporting new violations. Exit status:
0 when every selected connection is routed (and --check finds nothing new),
1 when not, 2 on errors.
"""

import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import layers as L
from . import specctra

FREEROUTING_VERSION = "2.5.0"
FREEROUTING_SHA1 = "a9133705bd3f589ddcaa2acce4dbcfd2cd8ac640"
FREEROUTING_URL = ("https://repo1.maven.org/maven2/app/freerouting/freerouting/{v}/"
                   "freerouting-{v}-executable.jar")
# Java class-file major version -> Java release (for a clear error)
CLASS_JAVA = {61: 17, 65: 21, 66: 22, 67: 23, 68: 24, 69: 25, 70: 26}

POWER_NAME = re.compile(
    r"^(?:[A-Z]*GND[A-Z0-9_]*|GROUND|EARTH|CHASSIS|[A-Z0-9_]*V(?:DD|CC|SS|EE)[A-Z0-9_]*"
    r"|V(?:BAT|BATT|IN|OUT|SYS|BUS|MAIN|REF|PWR|SUP)[A-Z0-9_]*|[+-]?\d+V\d*[A-Z0-9_]*"
    r"|V\d+[VP]\d*[A-Z0-9_]*|P\d+V\d*[A-Z0-9_]*|[A-Z0-9_]*PWR[A-Z0-9_]*"
    r"|[A-Z0-9_]*POWER[A-Z0-9_]*|[A-Z0-9_]*SUPPLY[A-Z0-9_]*)$", re.I)
WIDE_MM = 0.4


class AutorouteError(RuntimeError):
    pass


# ---------------------------------------------------------------- which nets

def power_nets(doc):
    """{net name: reason} for nets that look like power: a polygon pour or
    plane, a power-like name, a power net class, or wide copper (median
    track width at least WIDE_MM)."""
    out = {}
    for p in doc.polygons:
        if p.net is not None:
            out.setdefault(doc.nets[p.net].name, "has a polygon pour")
    for lay in doc.copper_layers:
        if L.is_plane(lay):
            for _poly, net, _pts in doc.plane_areas(lay):
                if net is not None:
                    out.setdefault(doc.nets[net].name, "is on a plane layer")
    for c in getattr(doc, "net_classes", []) or []:
        if re.search(r"power|pwr|supply", str(c.name), re.I):
            for m in c.members:
                out.setdefault(m, f"is in net class {c.name}")
    widths = {}
    for t in doc.tracks:
        if t.effective_net is not None and t.polygon is None:
            widths.setdefault(t.effective_net, []).append(t.width)
    for i, n in enumerate(doc.nets):
        if POWER_NAME.match(str(n.name)):
            out.setdefault(n.name, "has a power-like name")
        w = sorted(widths.get(i, []))
        if w and w[len(w) // 2] >= WIDE_MM - 1e-9:
            out.setdefault(n.name, f"its tracks are wide (median {w[len(w) // 2]:.3f} mm)")
    return out


def select_nets(doc, patterns=None, include_power=False, conn=None):
    """(nets to route, {net: why left out}). Patterns are names or globs,
    case-insensitive; a power net named exactly is routed."""
    from .connect import Connectivity
    pads = {}
    for p in doc.pads:
        if p.effective_net is not None:
            pads[p.effective_net] = pads.get(p.effective_net, 0) + 1
    names = [n.name for n in doc.nets]
    power = power_nets(doc)
    skipped = {}
    if patterns:
        chosen, exact = [], set()
        for pat in patterns:
            hits = [n for n in names if fnmatch.fnmatchcase(n.upper(), pat.upper())]
            if not hits:
                raise AutorouteError(f"no net matches {pat!r}")
            if not any(ch in pat for ch in "*?["):
                exact.update(hits)
            chosen.extend(h for h in hits if h not in chosen)
    else:
        conn = conn or Connectivity(doc)
        open_ = {u["net"] for u in conn.unrouted()}
        chosen = [doc.nets[i].name for i in sorted(open_)]
        exact = set()
    out = []
    for n in chosen:
        i = doc.net_index(n)
        if pads.get(i, 0) < 2:
            skipped[n] = "has fewer than two pads"
        elif n in power and not include_power and n not in exact:
            skipped[n] = f"power net ({power[n]}); name it or pass --include-power"
        else:
            out.append(n)
    return out, skipped


# ---------------------------------------------------------------- Freerouting

def tool_dirs():
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    dirs = []
    if base:
        dirs.append(Path(base) / "altium-mcp" / "tools")
    dirs.append(Path.home() / ".cache" / "altium-mcp" / "tools")
    return dirs


def _version_key(path):
    m = re.search(r"freerouting-(\d+)\.(\d+)\.(\d+)", Path(path).name)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_freerouting(path=None):
    if path:
        if not Path(path).is_file():
            raise AutorouteError(f"no Freerouting jar at {path}")
        return str(path)
    env = os.environ.get("FREEROUTING_JAR")
    if env:
        if not Path(env).is_file():
            raise AutorouteError(f"FREEROUTING_JAR points at {env}, which does not exist")
        return env
    found = []
    for d in tool_dirs():
        found += glob.glob(str(d / "freerouting-*.jar"))
    if not found:
        raise AutorouteError("Freerouting not found: pass --freerouting JAR, set FREEROUTING_JAR, "
                             "or run `python -m offline autoroute --fetch`")
    return max(found, key=_version_key)


def jar_java(jar):
    """The Java release a jar's main class was compiled for (None if unknown)."""
    import zipfile
    try:
        with zipfile.ZipFile(jar) as z:
            man = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
            m = re.search(r"^Main-Class:\s*(\S+)", man, re.M)
            if not m:
                return None
            data = z.read(m.group(1).replace(".", "/") + ".class")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    if data[:4] != b"\xca\xfe\xba\xbe":
        return None
    return int.from_bytes(data[6:8], "big") - 44


def java_release(java):
    """The major release of a Java executable, from `java -version`."""
    try:
        p = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=60,
                           env={k: v for k, v in os.environ.items() if k != "JAVA_TOOL_OPTIONS"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'version "(\d+)(?:\.(\d+))?', p.stderr + p.stdout)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


def find_java(path=None, need=None):
    """A Java executable: --java or $FREEROUTING_JAVA as given, else the
    first of $JAVA_HOME, PATH and the usual install folders that is at
    least release `need`."""
    for cand in (path, os.environ.get("FREEROUTING_JAVA")):
        if cand:
            if not (Path(cand).is_file() or shutil.which(cand)):
                raise AutorouteError(f"no Java at {cand}")
            return cand
    exe = "java.exe" if os.name == "nt" else "java"
    cands = []
    if os.environ.get("JAVA_HOME"):
        cands.append(str(Path(os.environ["JAVA_HOME"]) / "bin" / exe))
    if shutil.which("java"):
        cands.append(shutil.which("java"))
    roots = ([os.environ.get("ProgramFiles", r"C:\Program Files")] if os.name == "nt"
             else ["/usr/lib/jvm", "/opt", "/Library/Java/JavaVirtualMachines"])
    for r in roots:
        cands += sorted(glob.glob(str(Path(r) / "*" / "bin" / exe)) +
                        glob.glob(str(Path(r) / "*" / "*" / "bin" / exe)) +
                        glob.glob(str(Path(r) / "*" / "Contents" / "Home" / "bin" / exe)),
                        reverse=True)
    seen = []
    for c in cands:
        if c in seen or not Path(c).is_file():
            continue
        seen.append(c)
        if need is None:
            return c
        v = java_release(c)
        if v is not None and v >= need:
            return c
    if not seen:
        raise AutorouteError(f"Java not found: install Java {need or 25} or pass --java")
    raise AutorouteError(f"Freerouting needs Java {need}; found only older ones ("
                         + ", ".join(seen) + f"). Install Java {need} or pass --java")


def fetch(dest_dir=None, version=FREEROUTING_VERSION, sha1=FREEROUTING_SHA1):
    """Download the pinned Freerouting jar and check its SHA-1."""
    import urllib.request
    d = Path(dest_dir) if dest_dir else tool_dirs()[0]
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"freerouting-{version}-executable.jar"
    if out.is_file() and hashlib.sha1(out.read_bytes()).hexdigest() == sha1:
        return str(out)
    url = FREEROUTING_URL.format(v=version)
    tmp = out.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    got = hashlib.sha1(tmp.read_bytes()).hexdigest()
    if got != sha1:
        tmp.unlink()
        raise AutorouteError(f"{url}: SHA-1 {got} is not the pinned {sha1}; not kept")
    tmp.replace(out)
    return str(out)


def run_freerouting(dsn_path, ses_path, *, jar, java, passes=20, timeout=900, threads=1,
                    ignore_class=specctra.FIXED_CLASS, workdir=None, allow_network=False,
                    neckdown=False):
    """Route dsn_path into ses_path headless. Returns the log text."""
    workdir = Path(workdir or Path(dsn_path).parent)
    home = workdir / "freerouting-home"
    home.mkdir(parents=True, exist_ok=True)
    cmd = [java, f"-Duser.home={home}"]
    env = dict(os.environ)
    if not allow_network:
        # anything that tries the network fails at once on a closed port
        dead = "127.0.0.1"
        cmd += [f"-Dhttp.proxyHost={dead}", "-Dhttp.proxyPort=9", f"-Dhttps.proxyHost={dead}",
                "-Dhttps.proxyPort=9", "-Dhttp.nonProxyHosts=", "-Djava.net.useSystemProxies=false"]
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env[k] = "http://127.0.0.1:9"
        env.pop("JAVA_TOOL_OPTIONS", None)
    settings = {
        "gui.enabled": "false", "usage_and_diagnostic_data.disable_analytics": "true",
        "api_server.enabled": "false", "mcp_server.enabled": "false",
        "feature_flags.save_jobs": "false", "router.fanout.enabled": "false",
        # its neck-down narrows tracks to 0.75x and 0.6x of the class width
        # near pins, whatever the Width rule says: off unless asked for
        "router.automatic_neckdown": "true" if neckdown else "false",
        "router.autorouter.max_passes": str(int(passes)),
        "router.autorouter.max_threads": str(int(threads)),
        "router.autorouter.ignore_net_classes": ignore_class,
    }
    for k, v in settings.items():
        env["FREEROUTING__" + k.replace(".", "__").upper()] = v
    cmd += ["-jar", str(jar), "-de", str(dsn_path), "-do", str(ses_path)]
    cmd += [f"--{k}={v}" for k, v in settings.items()]
    if Path(ses_path).exists():
        Path(ses_path).unlink()
    try:
        p = subprocess.run(cmd, cwd=str(workdir), env=env, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired as e:
        raise AutorouteError(f"Freerouting did not finish in {timeout} s (raise --timeout)") from e
    except OSError as e:
        raise AutorouteError(f"could not start Java ({java}): {e}") from e
    log = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"class file version (\d+)\.\d+\), this version of the Java Runtime only "
                  r"recognizes class file versions up to (\d+)", log)
    if m:
        need, have = (CLASS_JAVA.get(int(x), f"class {x}") for x in m.groups())
        raise AutorouteError(f"{Path(jar).name} needs Java {need}; {java} is Java {have}. "
                             "Install a newer Java or pass --java")
    if p.returncode != 0 or not Path(ses_path).is_file():
        tail = "\n".join(_quiet(log).splitlines()[-15:])
        raise AutorouteError(f"Freerouting failed (exit {p.returncode}); last lines:\n{tail}")
    return log


def _quiet(log):
    """The log without the per-wire noise Freerouting prints for anonymous
    obstacles."""
    return "\n".join(x for x in log.splitlines() if "write_wire_scope: net not found" not in x
                     and "JAVA_TOOL_OPTIONS" not in x)


def _log_facts(log):
    facts = {}
    m = re.findall(r"final score: [\d.]+ \((\d+) unrouted", log)
    if m:
        facts["router_unrouted"] = int(m[-1])
    m = re.search(r"Board has (\d+) pre-existing clearance violation", log)
    if m:
        facts["router_preexisting_conflicts"] = int(m.group(1))
    m = re.search(r"Freerouting v([\d.]+)", log)
    if m:
        facts["freerouting"] = m.group(1)
    return facts


# ---------------------------------------------------------------- the whole job

def autoroute(board, nets=None, *, keep_fixed=None, include_power=False, pours="repour",
              via=None, passes=20, timeout=900, jar=None, java=None, dsn_out=None,
              ses_in=None, check=False, threads=1, allow_network=False, workdir=None,
              neckdown=False):
    """Route; returns (plan, report)."""
    from .connect import Connectivity
    from .pcbdoc import PcbDoc
    t0 = time.time()
    doc = PcbDoc.open(board) if not isinstance(board, PcbDoc) else board
    conn = Connectivity(doc)
    route, skipped = select_nets(doc, nets, include_power, conn=conn)
    report = {"board": str(board) if not isinstance(board, PcbDoc) else None,
              "nets": route, "left_out": skipped}
    if not route:
        report.update({"connections_before": 0, "connections_after": 0, "completion": 1.0,
                       "note": "nothing to route"})
        return {"version": 1, "ops": []}, report
    if keep_fixed is None:
        free = []
    elif [k.lower() for k in keep_fixed] == ["none"]:
        free = list(route)
    else:
        fixed = {n.upper() for n in keep_fixed}
        free = [n for n in route if n.upper() not in fixed]
    keep = [n.name for n in doc.nets if n.name not in free]
    dsn = specctra.export(doc, route, pours=pours, via=via, keep_fixed=keep,
                          name=Path(str(board)).stem if not isinstance(board, PcbDoc) else "board")
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="autoroute-"))
    work.mkdir(parents=True, exist_ok=True)
    dsn_path = Path(dsn_out) if dsn_out else work / "design.dsn"
    if not isinstance(board, PcbDoc) and _same(dsn_path, board):
        raise AutorouteError("refusing to write the DSN over the board")
    dsn_path.write_text(dsn.text, encoding="utf-8", newline="\n")
    report["dsn"] = str(dsn_path)
    report["classes"] = dsn.classes
    if ses_in:
        ses_path = Path(ses_in)
        report["session"] = str(ses_path)
    else:
        ses_path = work / "design.ses"
        jar_ = find_freerouting(jar)
        java_ = find_java(java, need=jar_java(jar_))
        t1 = time.time()
        log = run_freerouting(dsn_path, ses_path, jar=jar_, java=java_, passes=passes,
                              timeout=timeout, threads=threads, workdir=work,
                              allow_network=allow_network, neckdown=neckdown)
        (work / "freerouting.log").write_text(_quiet(log), encoding="utf-8")
        report.update(_log_facts(log))
        report["router_seconds"] = round(time.time() - t1, 1)
        report["log"] = str(work / "freerouting.log")
        report["session"] = str(ses_path)
    session = specctra.read_ses(ses_path.read_text(encoding="utf-8", errors="replace"), dsn)
    wanted = {n.upper() for n in route}
    others = [w for w in session["wires"] if w["net"].upper() not in wanted] + \
        [v for v in session["vias"] if v["net"].upper() not in wanted]
    session["wires"] = [w for w in session["wires"] if w["net"].upper() in wanted]
    session["vias"] = [v for v in session["vias"] if v["net"].upper() in wanted]
    ops, sk = specctra.plan_from_session(doc, session, dsn, replace=free)
    widths = {n.upper(): c["width"] for c in dsn.classes for n in c["nets"]}
    narrow = [o for o in ops if o["op"] == "add_track"
              and o["width"] < widths.get(o["net"].upper(), 0) - 1e-4]
    if narrow:
        report["narrow_tracks"] = {"count": len(narrow), "min_width_mm": min(
            o["width"] for o in narrow), "note": "the router necked these down below the "
            "class width; the DRC will flag them where the Width rule is broken"}
    sk["other_nets"] = sum(1 for x in others if x.get("type") not in ("protect", "fix"))
    report["skipped_from_session"] = sk
    plan = {"version": 1, "ops": ops}
    report["added"] = {"tracks": sum(1 for o in ops if o["op"] == "add_track"),
                       "vias": sum(1 for o in ops if o["op"] == "add_via"),
                       "track_mm": round(sum(((o["x2"] - o["x1"]) ** 2 + (o["y2"] - o["y1"]) ** 2)
                                             ** 0.5 for o in ops if o["op"] == "add_track"), 2)}
    after = _applied(doc, plan)
    idx = {doc.net_index(n) for n in route}
    before_links = [u for u in conn.unrouted(idx)]
    after_links = [u for u in Connectivity(after).unrouted(idx)]
    nb, na = len(before_links), len(after_links)
    report["connections_before"] = nb
    report["connections_after"] = na
    report["completion"] = round(1.0 - na / nb, 4) if nb else 1.0
    report["unrouted_after"] = [{"net": u["net_name"], "from": [round(c, 3) for c in u["from"]],
                                 "to": [round(c, 3) for c in u["to"]],
                                 "gap_mm": round(u["gap_mm"], 3)} for u in after_links]
    if check:
        report["drc"] = _drc_delta(_applied(doc, {"version": 1, "ops": []}), after,
                                   "repour" if pours == "repour" else "saved")
    report["seconds"] = round(time.time() - t0, 1)
    if pours == "repour" and plan["ops"]:
        report["next"] = "apply the plan, then repour (polygons of other nets flow around the " \
                         "new tracks) and run DRC"
    return plan, report


def _applied(doc, plan):
    """A copy of the board with the plan applied, in memory."""
    from .cfb import CompoundFile
    from .editplan import apply as apply_plan
    from .pcbdoc import PcbDoc
    from .pcbwrite import to_document_bytes
    copy = PcbDoc(CompoundFile(to_document_bytes(doc)))
    apply_plan(copy, plan)
    return copy


DRC_CHECKS = ["Clearance", "ShortCircuit", "Width", "HoleToHoleClearance", "BoardOutlineClearance"]


def _drc_delta(before, after, copper):
    """DRC violations the plan adds (same rule and message counted out).
    copper "repour" pours both boards afresh, as Altium's Repour All would
    after the new tracks go in; "saved" keeps the pours as stored."""
    from collections import Counter
    from .drc import DRC
    a = DRC(before, checks=DRC_CHECKS, copper=copper)
    a.run()
    b = DRC(after, checks=DRC_CHECKS, copper=copper)
    b.run()
    key = lambda v: (v.rule, v.message)   # noqa: E731
    new = Counter(key(v) for v in b.violations) - Counter(key(v) for v in a.violations)
    return {"before": len(a.violations), "after": len(b.violations),
            "new": sum(new.values()),
            "new_list": [{"rule": r, "message": m, "count": c} for (r, m), c in
                         sorted(new.items())][:50],
            "checks": DRC_CHECKS, "pours": copper}


def _same(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(str(a)) == os.path.abspath(str(b))


# ---------------------------------------------------------------- CLI

def cmd_autoroute(args):
    if args.fetch:
        path = fetch(args.fetch_dir)
        print(f"Freerouting {FREEROUTING_VERSION}: {path} (SHA-1 checked)")
        return 0
    if not args.board:
        raise ValueError("a board is needed (or --fetch)")
    if args.out and _same(args.out, args.board):
        raise ValueError("refusing to write the plan over the board")
    try:
        plan, rep = autoroute(args.board, args.nets, keep_fixed=args.keep_fixed,
                              include_power=args.include_power, pours=args.pours,
                              via=tuple(args.via) if args.via else None, passes=args.passes,
                              timeout=args.timeout, jar=args.freerouting, java=args.java,
                              dsn_out=args.dsn, ses_in=args.ses, check=args.check,
                              threads=args.threads, allow_network=args.allow_network,
                              workdir=args.workdir, neckdown=args.neckdown)
    except (AutorouteError, specctra.SpecctraError) as e:
        raise ValueError(str(e))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=1)
    if args.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        if not rep["nets"]:
            print("nothing to route")
        else:
            print(f"routed {len(rep['nets'])} net(s): {rep['connections_before']} open "
                  f"connection(s) before, {rep['connections_after']} after "
                  f"({rep['completion'] * 100:.1f} % complete) in {rep['seconds']} s")
            a = rep["added"]
            print(f"  plan: {a['tracks']} track(s), {a['vias']} via(s), {a['track_mm']} mm"
                  + (f" -> {args.out}" if args.out else ""))
            for u in rep.get("unrouted_after", [])[:20]:
                print(f"  still open: {u['net']} {u['from']} -> {u['to']} ({u['gap_mm']} mm)")
            if rep.get("drc"):
                d = rep["drc"]
                print(f"  DRC: {d['new']} new violation(s) ({d['before']} before)")
                for v in d["new_list"][:10]:
                    print(f"    {v['message']}")
        for n, why in sorted(rep.get("left_out", {}).items()):
            print(f"  left out: {n}: {why}")
        if rep.get("next"):
            print(f"  next: {rep['next']}")
    bad = rep.get("connections_after", 0) > 0 or (rep.get("drc") or {}).get("new", 0) > 0
    return 1 if bad else 0


def register_commands(command):
    def build(p):
        p.add_argument("board", nargs="?")
        p.add_argument("--nets", nargs="+", help="names or globs (default: every open signal net)")
        p.add_argument("--keep-fixed", nargs="+", metavar="NET",
                       help="nets whose existing wiring stays (default: all; 'none': the router "
                            "may redo the routed nets)")
        p.add_argument("-o", "--out", help="write the EditPlan here")
        p.add_argument("--check", action="store_true", help="DRC before and after, in memory")
        p.add_argument("--include-power", action="store_true")
        p.add_argument("--pours", choices=("repour", "fixed"), default="repour",
                       help="other nets' poured copper: left out to repour (default) or fixed")
        p.add_argument("--via", nargs=2, type=float, metavar=("DIAMETER", "HOLE"))
        p.add_argument("--passes", type=int, default=20)
        p.add_argument("--threads", type=int, default=1)
        p.add_argument("--timeout", type=float, default=900)
        p.add_argument("--freerouting", help="Freerouting executable jar")
        p.add_argument("--java", help="Java 25 executable")
        p.add_argument("--dsn", help="also keep the DSN here")
        p.add_argument("--ses", help="import this session instead of running the router")
        p.add_argument("--workdir", help="folder for the DSN, session and log")
        p.add_argument("--neckdown", action="store_true",
                       help="let Freerouting narrow tracks at pins (may break the Width rule)")
        p.add_argument("--allow-network", action="store_true",
                       help="do not block Freerouting's network access")
        p.add_argument("--fetch", action="store_true",
                       help=f"download Freerouting {FREEROUTING_VERSION} (SHA-1 checked)")
        p.add_argument("--fetch-dir")
        p.add_argument("--json", action="store_true")
    command("autoroute", cmd_autoroute, "route signal nets with Freerouting, as an EditPlan",
            build)
