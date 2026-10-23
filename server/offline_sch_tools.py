"""MCP tools for schematics, read from saved files without Altium running (or licensed).

Registered by offline_tools.register. Read-only. They accept a .PrjPcb (every sheet in
the project), a single .SchDoc, a .PcbDoc (as a netlist of pad nets) and, for comparisons,
a compiled-netlist text dump (`NET|name|U1.3|...` lines, written by
dev/exp_compiled_netlist.pas). The connectivity rules are in server/offline/schnet.py;
on the project they were built against, the result matched Altium's compiled netlist
net for net and pin for pin.
"""
import asyncio
import json
import logging
from pathlib import Path

from mcp.server.fastmcp import Context

logger = logging.getLogger("AltiumMCPServer")

SCH_SOURCES = (".prjpcb", ".schdoc")
ALL_SOURCES = (".prjpcb", ".schdoc", ".pcbdoc", ".txt")


def _source(path, allowed=ALL_SOURCES):
    p = Path(path)
    if p.suffix.lower() not in allowed:
        raise ValueError("expected one of %s (got %r)" % (", ".join(allowed), p.suffix))
    if not p.is_file():
        raise FileNotFoundError("no such file: %s" % path)
    from offline import schtools
    return schtools.load_any(p)


def _run(fn):
    async def go():
        try:
            return json.dumps(await asyncio.to_thread(fn), indent=2, default=str)
        except Exception as e:  # report, do not crash the server
            logger.error("offline schematic tool failed: %s", e)
            return json.dumps({"error": str(e)})
    return go()


def register(mcp):
    from offline import power, schtools

    @mcp.tool()
    async def offline_sch_netlist(ctx: Context, path: str, net: str = "", designator: str = "") -> str:
        """
        Netlist of a schematic project or sheet, read from the saved files: every net with its
        pins, and every component. Needs no running Altium. Save in Altium first; unsaved edits
        are not in the files.

        Args:
            path (str): Absolute path to a .PrjPcb (all its sheets) or one .SchDoc.
            net (str): Optional. Only nets whose name contains this text.
            designator (str): Optional. Only nets that touch this component.
        """
        def go():
            src = _source(path, SCH_SOURCES)
            nl = src.netlist
            nets = []
            for n in nl.nets:
                if net and net.lower() not in n.name.lower():
                    continue
                if designator and not any(p.designator == designator for p in n.pins):
                    continue
                nets.append({"name": n.name, "auto_named": n.auto_named, "sheets": n.sheets,
                             "pins": ["%s.%s %s" % (p.designator, p.pin, p.pin_name) for p in n.pins]})
            comps = [{"designator": d, "comment": c["comment"], "sheet": c["sheet"]}
                     for d, c in sorted(src.components.items(), key=lambda kv: schtools.natural_key(kv[0]))]
            return {"summary": nl.summary(), "nets": nets,
                    "components": comps if not (net or designator) else None}
        return await _run(go)

    @mcp.tool()
    async def offline_sch_check(ctx: Context, path: str, include_info: bool = False) -> str:
        """
        Schematic problems found from the saved files: unconnected pins, single-pin nets,
        floating or empty net labels, floating power ports, nets with two names, duplicate or
        unannotated designators, nets driven by two outputs, and wire ends that land inside
        another wire with no junction. Info-level items (stub wires whose far end is free,
        hierarchy not followed) are left out unless include_info is true.

        Args:
            path (str): Absolute path to a .PrjPcb or .SchDoc.
            include_info (bool): Also list info-level findings.
        """
        def go():
            nl = _source(path, SCH_SOURCES).netlist
            items = [f.as_dict() for f in nl.findings if include_info or f.severity != "info"]
            return {"summary": nl.summary(), "findings": items}
        return await _run(go)

    @mcp.tool()
    async def offline_sch_compare(ctx: Context, path_a: str, path_b: str, ignore_single_pin: bool = True) -> str:
        """
        What changed between two netlists: components added, removed or changed (library
        reference, comment, design item), nets added, removed, renamed, split or merged, and
        pins that joined or left a net. Nets are matched by the pins they share.

        Use it to review a schematic edit (old saved copy against the new one), to check that
        a PCB matches its schematic, or to compare against Altium's compiled netlist.

        Args:
            path_a (str): The "before": .PrjPcb, .SchDoc, .PcbDoc or compiled-netlist .txt.
            path_b (str): The "after", same choices.
            ignore_single_pin (bool): Treat a pin alone on its net as unconnected on both sides.
        """
        def go():
            res = schtools.compare(_source(path_a), _source(path_b), ignore_single_pin)
            res["report"] = schtools.format_compare(res)
            return res
        return await _run(go)

    @mcp.tool()
    async def offline_sch_bom(ctx: Context, path: str) -> str:
        """
        Bill of materials from a schematic: one row per part, with designators, quantity,
        comment, description, manufacturer and MPN where the part carries them, and
        `search_terms`, the MPN spellings worth trying in a parts search (packing suffixes
        such as Infineon's XUMA1 removed, then the family stem).

        Args:
            path (str): Absolute path to a .PrjPcb or .SchDoc.
        """
        return await _run(lambda: schtools.bom(_source(path, SCH_SOURCES)))

    @mcp.tool()
    async def offline_sch_part(ctx: Context, path: str, designator: str) -> str:
        """
        One component's pins, the net on each, and the other parts on that net (crowded nets
        such as ground are summarised by count). Shows unconnected pins, e.g. spare ADC inputs.

        Args:
            path (str): Absolute path to a .PrjPcb or .SchDoc.
            designator (str): The component, e.g. "U3".
        """
        return await _run(lambda: schtools.part_view(_source(path, SCH_SOURCES), designator))

    @mcp.tool()
    async def offline_sch_rules(ctx: Context, path: str, rules: list) -> str:
        """
        Check every instance of a circuit block against rules, e.g. that each high-side
        switch's sense pin has a resistor to ground and reaches the ADC.

        Each rule is an object:
            parts   regex searched in designator, comment, library reference, design item ID,
                    description and MPN, e.g. "BTS7008"
            pin     regex for pin names (whole name, any case), or "#4" for pin number 4
            expect  "connected" | "net_name" | "same_net" | "resistor_to" | "capacitor_to" |
                    "diode_to" | "two_pin_to" | "connects_to"
            net     regex for the net name (net_name) or the far end (resistor_to etc.)
            prefix  designator prefix for two_pin_to (resistor_to etc. default to R, C, D)
            part    regex for the part that connects_to must reach
            via     designator prefix of one series part connects_to may pass through, e.g. "R"
            name    optional label

        Example: [{"parts": "BTS7008", "pin": "IS", "expect": "resistor_to", "net": "GND"},
                  {"parts": "BTS7008", "pin": "OUT", "expect": "same_net"},
                  {"parts": "BTS7008", "pin": "IS", "expect": "connects_to", "part": "ADS8668", "via": "R"}]

        Args:
            path (str): Absolute path to a .PrjPcb or .SchDoc.
            rules (list): The rules.
        """
        def go():
            results = schtools.check_rules(_source(path, SCH_SOURCES), rules)
            counts = {}
            for r in results:
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            return {"counts": counts, "results": results}
        return await _run(go)

    @mcp.tool()
    async def offline_sch_footprints(ctx: Context, path: str, board: str = "", extra_libs: list = None) -> str:
        """
        Run before Design > Update PCB Document. For every part: does it have a current PCB
        footprint, is a library footprint in one of the project's PcbLibs, and does every pin
        have a pad of the same designator (pads with no pin are warned: they get no net). With
        a board (the project's own by default), also previews the update by matching parts on
        their unique IDs as Altium does: parts added and removed, footprint swaps, renames, and
        board parts with no schematic link.

        Server (workspace) footprints are checked against copies found on the board or in
        `extra_libs`; one found nowhere is listed as not checked. Altium leaves copies of
        released workspace footprints under %LOCALAPPDATA%\\Temp\\EDMS_Release.

        Args:
            path (str): Absolute path to a .PrjPcb.
            board (str): Optional .PcbDoc to preview against instead of the project's.
            extra_libs (list): Optional .PcbLib files or folders searched for server footprints.
        """
        from offline import footprints
        p = Path(path)
        if p.suffix.lower() != ".prjpcb" or not p.is_file():
            return json.dumps({"error": "expected an existing .PrjPcb: %s" % path})

        def go():
            res = footprints.check(p, board or None, extra_libs or [])
            res["report"] = footprints.format_check(res)
            return res
        return await _run(go)

    @mcp.tool()
    async def calc_profet_network(ctx: Context, part: str, sense_full_scale_a: float = None,
                                  adc_full_scale_v: float = None, input_high_v: float = None,
                                  input_series_ohm: float = None, pulse_peak_a: float = None,
                                  pulse_s: float = None, load_inductance_h: float = None) -> str:
        """
        Size the parts around an Infineon PROFET+2 channel from datasheet values: the sense
        resistor (R_IS) for a given ADC full scale, the current into the IN pin's 7 V clamp
        when the driving signal swings higher, and whether a short high-current pulse (e.g. a
        shift solenoid) stays under the overload threshold, with its flyback energy against
        the part's clamp rating where the datasheet gives one. Give only the groups you need.

        Args:
            part (str): "BTS7008-1EPP" or "BTS7008-2EPA".
            sense_full_scale_a (float): Load current that should read as ADC full scale.
            adc_full_scale_v (float): ADC input full scale at that current.
            input_high_v (float): High level of the signal driving IN (e.g. 12 for an ECU output).
            input_series_ohm (float): Series resistor between that signal and IN.
            pulse_peak_a (float): Peak current of a short pulse.
            pulse_s (float): Pulse length in seconds.
            load_inductance_h (float): Load inductance, for flyback energy.
        """
        def go():
            if part not in power.PROFETS:
                raise ValueError("unknown part %r; known: %s" % (part, sorted(power.PROFETS)))
            out = {"part": part}
            if sense_full_scale_a and adc_full_scale_v:
                out["sense_resistor"] = power.sense_resistor(part, sense_full_scale_a, adc_full_scale_v)
            if input_high_v is not None and input_series_ohm:
                out["input"] = power.input_network(input_high_v, input_series_ohm)
            if pulse_peak_a and pulse_s:
                out["pulse"] = power.solenoid_pulse(part, pulse_peak_a, pulse_s, load_inductance_h)
            if len(out) == 1:
                raise ValueError("give at least one group: sense, input or pulse")
            return out
        return await _run(go)

    @mcp.tool()
    async def calc_mosfet_stage(ctx: Context, rds_hot_mohm: float, qg_nc: float, qgd_nc: float,
                                v_plateau: float, v_drive: float, r_gate_on_ohm: float, r_gate_off_ohm: float,
                                load_a: float, bus_v: float, pwm_hz: float, duty: float, qgs2_nc: float = 0.0,
                                diode_vf: float = None, diode_qrr_nc: float = 0.0,
                                rth_ja_k_per_w: float = None, ambient_c: float = None) -> str:
        """
        Loss budget for a discrete PWM switch driving an inductive load (e.g. a fuel pump at
        2.5 kHz): switching (Miller-plateau model), conduction, gate drive and, with diode_vf,
        the freewheel diode. Switching loss ignores Coss and reverse recovery of the FET, so it
        is a floor. Use Rds(on) at the junction temperature you expect, not the 25 C value.

        Args:
            rds_hot_mohm (float): On-resistance at operating junction temperature, mOhm.
            qg_nc (float): Total gate charge at v_drive, nC.
            qgd_nc (float): Gate-drain (Miller) charge, nC.
            v_plateau (float): Miller plateau voltage, V.
            v_drive (float): Gate drive voltage, V.
            r_gate_on_ohm (float): Total resistance in the turn-on path (driver + series + internal).
            r_gate_off_ohm (float): Total resistance in the turn-off path.
            load_a (float): Load current while on, A.
            bus_v (float): Supply voltage switched, V.
            pwm_hz (float): PWM frequency, Hz.
            duty (float): Duty cycle, 0 to 1.
            qgs2_nc (float): Gate charge from threshold to plateau, nC, if known.
            diode_vf (float): Freewheel diode forward voltage at load_a, V.
            diode_qrr_nc (float): Diode reverse-recovery charge, nC (0 for a Schottky).
            rth_ja_k_per_w (float): FET junction-to-ambient on your board, for a temperature.
            ambient_c (float): Ambient temperature, C.
        """
        def go():
            out = {"fet": power.mosfet_stage(rds_hot_mohm, qg_nc, qgd_nc, v_plateau, v_drive, r_gate_on_ohm,
                                             r_gate_off_ohm, load_a, bus_v, pwm_hz, duty, qgs2_nc,
                                             rth_ja_k_per_w, ambient_c)}
            if diode_vf is not None:
                out["diode"] = power.freewheel_diode(diode_vf, load_a, duty, diode_qrr_nc, bus_v, pwm_hz)
                out["stage_total_w"] = round(out["fet"]["p_fet_w"] + out["diode"]["p_diode_w"], 3)
            return out
        return await _run(go)
