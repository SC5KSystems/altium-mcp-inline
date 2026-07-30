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
RAIL_Y = 4600     # input rail
VOUT_Y = 4300     # output rail (LED+)
FB_Y = 2600       # LED- / current-set node
# orient is 0/1/2/3 = 0/90/180/270 deg; mirror flips left-right WITHOUT
# turning the designator and comment text upside down, which a 180 rotation
# would do - so J1 carries the flag to reset its text orientation after rotating.
PARTS = [
    # desig, corp part number,  x,     y,    orient, mirror
    ("U1", "4134-0002", 2400, 3000, 0, 0),  # TPS923611 LED driver, SOT563
    ("L1", "3210-0028", 2900, 4600, 0, 0),  # 10uH 0.84A   pins (3000,4600)-(3400,4600)
    ("C1", "2140-0021", 2100, 4390, 1, 0),  # 4.7uF 16V 0805 - input, top pin on RAIL_Y
    ("C2", "2140-0026", 4500, 4090, 1, 0),  # 2.2uF 50V 0805 - output, top pin on VOUT_Y
    ("J1", "6101-0041", 5600, 4300, 2, 1),  # 2x1 header - backlight string, pins face left
    ("R1", "1112-0082", 4200, 2100, 1, 0),  # 10.0 ohm - LED current set, top pin on FB_Y
    ("R2", "1112-0004", 2000, 2800, 1, 0),  # 100K - ADIM pulldown, top pin at ADIM height
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
    """Orthogonal routes built from the real placed-pin coordinates.

    Parts are positioned so that each part's "top" terminal already sits on
    the rail it belongs to, so the routes here are short and mostly straight.
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
    # Terminal naming on the passives is IN1/IN2; sort by height so "t" is the
    # rail end and "g" the ground end regardless of how rotation ordered them.
    def top_bot(d):
        a, b = p(d, "IN1"), p(d, "IN2")
        return (a, b) if a[1] >= b[1] else (b, a)
    c1t, c1g = top_bot("C1")
    c2t, c2g = top_bot("C2")
    r1t, r1g = top_bot("R1")
    r2t, r2g = top_bot("R2")
    # J1 pin 2 is the upper of the two, so LED+ from the rail above and LED-
    # down to the sense node route without crossing.
    j_hi, j_lo = p("J1", "2"), p("J1", "1")

    rail_x0 = 1400                      # left end of the input rail
    riser_x = vout[0] + 300             # VOUT climbs to the output rail here
    w, j, n, pw = [], [], [], []

    # --- input rail: 5V0 -> C1 -> U1.VIN -> L1 ---------------------------
    w.append([(rail_x0, RAIL_Y), (l_a[0], RAIL_Y)])
    w.append([vin, (vin[0], RAIL_Y)])
    j += [(c1t[0], RAIL_Y), (vin[0], RAIL_Y)]
    pw.append((rail_x0, RAIL_Y, 1, 2, "5V0"))

    # --- L1 -> SW ---------------------------------------------------------
    # Drop down at sw.x+200, NOT at sw.x: a vertical wire in the same column
    # as U1's right-hand pins reads as if SW/VOUT/FB were shorted together.
    w.append([l_b, (sw[0] + 200, l_b[1]), (sw[0] + 200, sw[1]), sw])

    # --- VOUT -> output rail -> C2 -> J1 (LED+) ---------------------------
    w.append([vout, (riser_x, vout[1]), (riser_x, VOUT_Y), j_hi])
    j.append((c2t[0], VOUT_Y))
    n.append((riser_x + 100, VOUT_Y + 100, 0, "LED+"))

    # --- J1 (LED-) -> current-set node -> FB ------------------------------
    w.append([j_lo, (j_lo[0], FB_Y), r1t])
    w.append([fb, (fb[0] + 100, fb[1]), (fb[0] + 100, FB_Y), r1t])
    j.append(r1t)
    n.append((fb[0] + 200, FB_Y + 100, 0, "LED-"))

    # --- ADIM: net label + pulldown ---------------------------------------
    w.append([adim, r2t])
    n.append((r2t[0] + 200, adim[1], 0, "BL_DIM"))

    # --- grounds: a port under each part that returns to ground -----------
    w.append([gnd, (gnd[0] - 300, gnd[1]), (gnd[0] - 300, gnd[1] - 300)])
    pw.append((gnd[0] - 300, gnd[1] - 300, 3, 4, "GND"))
    for term in (c1g, c2g, r1g, r2g):
        w.append([term, (term[0], term[1] - 200)])
        pw.append((term[0], term[1] - 200, 3, 4, "GND"))

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
