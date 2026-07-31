"""Generate a circuit spec for build_circuit.pas from database part numbers.

Every part's parameters come from the component database, exactly as
make_part_spec.py does for a single part - no hardcoded parameter sets, so
each part carries whatever its own table defines.

Two-stage flow, because a rotated symbol's absolute pin coordinates are not
worth predicting:

    stage 1: python dev/make_circuit.py place   -> spec with parts only
             run build_circuit.pas              -> writes pin_map.txt
    stage 2: python dev/make_circuit.py wire    -> spec with parts + wires,
                                                   routed from real pin coords
             run build_circuit.pas              -> the finished sheet

Spec format (pipe-delimited, one record per line):
    PART|<designator>|<symlib>|<symbol>|<designitemid>|<x>|<y>|<orientation>
    PARAM|<name>|<value>            (applies to the most recent PART)
    FOOTPRINT|<name>                (       "        )
    DESCRIPTION|<text>              (       "        )
    COMMENT|<text>                  (       "        )
    WIRE|x1|y1|x2|y2[|x3|y3...]
    JUNCTION|x|y
    NETLABEL|x|y|<orientation>|<text>
    POWER|x|y|<orientation>|<style>|<text>
    NOTE|x|y|<text>
All coordinates are in mils.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_part_spec as mps

SPEC_OUT = Path("C:/Users/Public/altium_mcp/circuit_spec.txt")
PIN_MAP = Path("C:/Users/Public/altium_mcp/pin_map.txt")

# --- the circuit -----------------------------------------------------------
# TPS923611 synchronous-boost white-LED backlight driver.
#   5V0 -> L1 -> SW ; VOUT -> LED string (in the display module) -> FB
#   FB -> R1 -> GND sets the LED current. ADIM is the analog dimming input.
#
# The LED string leaves through J1 rather than sitting on this sheet: in a
# real backlight the LEDs are inside the display module. That also keeps every
# part inside the database's approved *_Query views - the only white LED in
# the database (5120-0017) is in the base table but not in the approved view.
# Locations are the symbol ORIGIN, not a pin. The offsets below were measured
# from a placement run's pin_map.txt rather than predicted - a rotated symbol's
# pin offsets are not obvious (a rotated CAP-NP puts its pins 100 mil left of
# the origin and only 110 mil apart).
GRID = 100        # schematic snap grid; all spacing is a multiple of this
TAP_GAP = GRID    # wire between a shunt pin and the node it taps
GND_DROP = GRID   # wire from a pin down to its ground port

# Rails are packed just clear of the pin rows they serve rather than floated
# high above the part - tall empty risers are wasted sheet, not clarity.
#   U1 pin rows:  FB 3000, VOUT 3300, SW 3600
RAIL_Y = 4100     # 5V0 input rail. Not tighter: C1 hangs below it and its
                  # text block needs room before R2's block starts.
LEDP_Y = 3300     # LED+ runs straight out of VOUT at its own height
FB_Y = 3000       # LED- runs straight out of FB at its own height

# Locations are the symbol ORIGIN. Hot-end offsets were MEASURED, not guessed:
#   CAP-NP   rot90: (-100, 0) and (-100, +300)
#   RES-DISC rot90: (-100, 0) and (-100, +600)
#   INDUCTOR rot0 : (0, 0) and (+600, 0)
#   HEADER-2X1    : pin2 (0, 0), pin1 (0, +100)
#   TPS92361      : left pins (0, 0/+300/+600), right (+1500, 0/+300/+600)
#
# Shunt parts sit TAP_GAP below their rail, not on it, so there is a short wire
# between the pin and the junction instead of a node sitting on the pin.
PARTS = [
    # desig, corp part number,  x,     y,    orient, mirror
    ("U1", "4134-0002", 2400, 3000, 0, 0),  # VIN(2400,3600) SW(3900,3600) FB(3900,3000)
    ("L1", "3210-0028", 2900, 4100, 0, 0),  # pins (2900,4100)-(3500,4100) on the rail
    ("C1", "2140-0021", 2100, 3700, 1, 0),  # top hot (2000,4000) = RAIL_Y - TAP_GAP
    ("C2", "2140-0026", 5300, 2900, 1, 0),  # top hot (5200,3200); right of R1's text
    ("J1", "6101-0041", 5600, 3200, 0, 0),  # pin2 (5600,3200) LED-, pin1 (5600,3300) LED+
    ("R1", "1112-0082", 4200, 2300, 1, 0),  # top hot (4100,2900); left of C2's text
    ("R2", "1112-0004", 2000, 2700, 1, 0),  # top hot (1900,3300) at ADIM height
]


def part_records():
    """Resolve every part against the database and emit its spec lines."""
    out, summary = [], []
    for desig, pn, x, y, orient, mirror in PARTS:
        table, row = mps.find_part(pn)
        if not row:
            raise SystemExit(f"{desig}: part {pn} not found in any enabled *_Query view")
        excluded, _ = mps.field_mappings(table)

        symbol = footprint = description = None
        params = []
        for col, val in row.items():
            key = col.lower()
            if key in excluded or val == "":
                continue
            role = mps.SYSTEM_COLUMNS.get(key)
            if role == "symbol":
                symbol = val
            elif role == "footprint":
                footprint = val
            elif role == "description":
                description = val
            elif role == "model3d":
                continue
            else:
                params.append((col, val))
        if not symbol:
            raise SystemExit(f"{desig}: no symbol for {pn}")

        _, _, search = mps.dblib_config()
        symlib = mps.symbol_library_for(symbol, search.split(";"))
        if not symlib:
            raise SystemExit(f"{desig}: no .SchLib contains symbol {symbol}")

        out.append(f"PART|{desig}|{symlib}|{symbol}|{pn}|{x}|{y}|{orient}|{mirror}")
        # A database-placed part carries its LibReference as the Comment
        out.append(f"COMMENT|{symbol}")
        if footprint:
            out.append(f"FOOTPRINT|{footprint}")
        if description:
            out.append(f"DESCRIPTION|{description}")
        for name, val in params:
            out.append(f"PARAM|{name}|{val}")
        summary.append((desig, pn, symbol, footprint, len(params)))
    return out, summary


def read_pin_map():
    """pin_map.txt lines: PIN|<designator>|<pinname>|<x>|<y> (mils)."""
    pins = {}
    if not PIN_MAP.is_file():
        raise SystemExit(f"{PIN_MAP} not found - run the 'place' stage first")
    for line in PIN_MAP.read_text(errors="replace").splitlines():
        f = line.strip().split("|")
        if len(f) == 5 and f[0] == "PIN":
            pins[(f[1], f[2].upper())] = (int(f[3]), int(f[4]))
    return pins


def wiring(pin):
    """Orthogonal routes from the pins' true electrical connection points.

    Drafting rules (see dev/SCHEMATIC_CONVENTIONS.md):
      - every wire ends ON a hot end, never part-way along a pin
      - a shunt part taps its rail through a TAP_GAP wire, so the junction
        never sits directly on a pin
      - ground is one grid across, one grid down - no long detours
      - risers never share a column with a pin's connection points
      - net labels sit ON the wire they name
    """
    def p(desig, name):
        try:
            return pin[(desig, name.upper())]
        except KeyError:
            raise SystemExit(f"pin {desig}.{name} not in pin map; "
                             f"have {sorted(k for k in pin if k[0] == desig)}")

    vin, adim, gnd = p("U1", "VIN"), p("U1", "ADIM"), p("U1", "GND")
    sw, vout, fb = p("U1", "SW"), p("U1", "VOUT"), p("U1", "FB")
    l_a, l_b = p("L1", "1"), p("L1", "2")

    def top_bot(d):
        a_, b_ = p(d, "IN1"), p(d, "IN2")
        return (a_, b_) if a_[1] >= b_[1] else (b_, a_)
    c1t, c1g = top_bot("C1")
    c2t, c2g = top_bot("C2")
    r1t, r1g = top_bot("R1")
    r2t, r2g = top_bot("R2")
    j_a, j_b = p("J1", "1"), p("J1", "2")
    j_hi, j_lo = (j_a, j_b) if j_a[1] >= j_b[1] else (j_b, j_a)

    # A shunt terminal must sit exactly TAP_GAP below its rail. Too close and
    # the junction lands on the pin; misaligned and the tap wire is diagonal.
    for what, got, want in (("C1 top", c1t[1], RAIL_Y - TAP_GAP),
                            ("L1", l_a[1], RAIL_Y),
                            ("C2 top", c2t[1], LEDP_Y - TAP_GAP),
                            ("R1 top", r1t[1], FB_Y - TAP_GAP),
                            ("J1 LED+", j_hi[1], LEDP_Y)):
        if got != want:
            raise SystemExit(f"{what} sits at y={got}, expected y={want}; "
                             f"move the part rather than bending the wire to it")

    w, j, n, pw = [], [], [], []

    def tap(term, rail_y):
        """Short wire from a shunt pin up to its rail, plus the junction."""
        w.append([term, (term[0], rail_y)])
        j.append((term[0], rail_y))

    def to_ground(term):
        w.append([term, (term[0], term[1] - GND_DROP)])
        pw.append((term[0], term[1] - GND_DROP, 3, 4, "GND"))

    # --- input rail: 5V0 -> C1 -> U1.VIN -> L1 ---------------------------
    w.append([(1400, RAIL_Y), l_a])
    w.append([vin, (vin[0], RAIL_Y)])
    j.append((vin[0], RAIL_Y))
    tap(c1t, RAIL_Y)
    pw.append((1400, RAIL_Y, 1, 2, "5V0"))

    # --- L1 -> SW: drop clear of the pin column, then in --------------------
    w.append([l_b, (sw[0] + 2 * GRID, RAIL_Y), (sw[0] + 2 * GRID, sw[1]), sw])

    # --- VOUT -> LED+ -> J1, straight out at VOUT's own height -------------
    w.append([vout, j_hi])
    tap(c2t, LEDP_Y)
    n.append(((vout[0] + j_hi[0]) // 2 + 200, LEDP_Y, 0, "LED+"))

    # --- FB -> LED- -> J1, straight out at FB's own height ------------------
    # riser sits clear of C2's body, which reaches to its own Location.x
    w.append([fb, (j_lo[0] - 2 * GRID, FB_Y), (j_lo[0] - 2 * GRID, j_lo[1]), j_lo])
    tap(r1t, FB_Y)
    n.append((r1t[0] + 500, FB_Y, 0, "LED-"))

    # --- ADIM: net label + pulldown ----------------------------------------
    w.append([adim, r2t])
    n.append((r2t[0] + 250, adim[1], 0, "BL_DIM"))

    # --- grounds: one grid across, one grid down ---------------------------
    w.append([gnd, (gnd[0] - GRID, gnd[1]), (gnd[0] - GRID, gnd[1] - GND_DROP)])
    pw.append((gnd[0] - GRID, gnd[1] - GND_DROP, 3, 4, "GND"))
    for term in (c1g, c2g, r1g, r2g):
        to_ground(term)

    return w, j, n, pw


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "place"
    lines, summary = part_records()

    if stage == "wire":
        pin = read_pin_map()
        w, j, n, pw = wiring(pin)
        for route in w:
            lines.append("WIRE|" + "|".join(f"{x}|{y}" for x, y in route))
        for x, y in j:
            lines.append(f"JUNCTION|{x}|{y}")
        for x, y, o, t in n:
            lines.append(f"NETLABEL|{x}|{y}|{o}|{t}")
        for x, y, o, style, t in pw:
            lines.append(f"POWER|{x}|{y}|{o}|{style}|{t}")
        lines.append("NOTE|1400|5600|LED Backlight Driver - TPS923611 synchronous boost")
        lines.append("NOTE|1400|5450|LED string is in the display module, connected via J1")
        lines.append("NOTE|1400|5300|I_LED set by R1 (I = V_FB / R1) - confirm V_FB against the datasheet")
        print(f"wires={len(w)} junctions={len(j)} netlabels={len(n)} powerports={len(pw)}")

    SPEC_OUT.write_text("\n".join(lines) + "\n", encoding="cp1252", errors="replace")
    print(f"\n{'ref':<4} {'part':<12} {'symbol':<20} {'footprint':<14} params")
    for desig, pn, sym, fp, np_ in summary:
        print(f"{desig:<4} {pn:<12} {sym:<20} {str(fp):<14} {np_}")
    print(f"\nstage: {stage}\nspec : {SPEC_OUT}")


if __name__ == "__main__":
    main()
