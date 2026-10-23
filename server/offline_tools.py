"""MCP tools that work on a saved .PcbDoc without Altium running (or licensed).

Registered from main.py with register(mcp). Everything here is read-only, reads only files
ending in .PcbDoc, and is stdlib-only except offline_pcb_neck, which needs numpy (not in
the server venv; it says so, and the same analysis runs from the command line:
python -m offline neck ...).
"""
import asyncio
import json
import logging
from pathlib import Path

from mcp.server.fastmcp import Context

logger = logging.getLogger("AltiumMCPServer")

_cache = {}


def _load(path):
    """Parse a .PcbDoc, reusing the last parse while the file is unchanged."""
    from offline.pcbdoc import PcbDoc
    p = Path(path)
    if p.suffix.lower() != ".pcbdoc":
        raise ValueError("only .PcbDoc files can be read (got %r)" % p.suffix)
    if not p.is_file():
        raise FileNotFoundError("no such file: %s" % path)
    key = (str(p.resolve()), p.stat().st_mtime_ns)
    if key not in _cache:
        _cache.clear()
        _cache[key] = PcbDoc.load(str(p))
    return _cache[key]


def _run(fn, *a, **kw):
    """Run a blocking analysis off the event loop and return JSON (errors as JSON too)."""
    async def go():
        try:
            return json.dumps(await asyncio.to_thread(fn, *a, **kw), indent=2, default=float)
        except Exception as e:  # report, do not crash the server
            logger.error("offline tool failed: %s", e)
            return json.dumps({"error": str(e)})
    return go()


def register(mcp):
    # Schematic tools and design calculators live in offline_sch_tools.py
    import offline_sch_tools
    offline_sch_tools.register(mcp)

    from offline import connect, power, review

    @mcp.tool()
    async def offline_pcb_summary(ctx: Context, path: str) -> str:
        """
        Board size, stackup, copper weight, object counts and poured copper area, read from a
        saved .PcbDoc. Needs no running Altium and no license.

        Args:
            path (str): Absolute path to a .PcbDoc file.
        """
        return await _run(lambda: review.summary(_load(path)))

    @mcp.tool()
    async def offline_pcb_holes(ctx: Context, path: str, min_diameter_mm: float = 3.5, mounting_only: bool = True) -> str:
        """
        Drilled holes with their distance to the board edge. By default only board mounting
        holes: large holes that belong to no component.

        Args:
            path (str): Absolute path to a .PcbDoc file.
            min_diameter_mm (float): Smallest hole to list.
            mounting_only (bool): Skip holes that are part of a connector, terminal or other footprint.
        """
        def go():
            d = _load(path)
            return review.mounting_holes(d, min_diameter_mm) if mounting_only else review.holes(d, min_diameter_mm)
        return await _run(go)

    @mcp.tool()
    async def offline_pcb_fasteners(ctx: Context, path: str, screw: str = "M4-pan", washer: bool = False,
                                    head_diameter_mm: float = None, edge_margin_mm: float = 1.0) -> str:
        """
        For each board mounting hole: how far the screw head (or washer) is from the board edge,
        how far the hole must move inward for a wanted margin, and which live copper (by net, on
        each layer) and which components lie within the head's footprint.

        Args:
            path (str): Absolute path to a .PcbDoc file.
            screw (str): One of M3-pan, M3-cap, M4-pan, M4-cap, M5-pan, M5-cap, M6-pan.
            washer (bool): Use the washer outside diameter instead of the head.
            head_diameter_mm (float): Override the bearing diameter.
            edge_margin_mm (float): Wanted clearance from head edge to board edge.
        """
        return await _run(lambda: review.fastener_report(_load(path), screw, washer, head_diameter_mm, edge_margin_mm))

    @mcp.tool()
    async def offline_pcb_edges(ctx: Context, path: str, within_mm: float = 3.0) -> str:
        """
        Components whose pads come within `within_mm` of the board edge or hang over it, plus
        silkscreen tracks that touch, cross or lie outside the outline (the same ones Altium's
        Board Outline Clearance check reports).

        Args:
            path (str): Absolute path to a .PcbDoc file.
            within_mm (float): Edge distance below which a component is listed.
        """
        def go():
            d = _load(path)
            return {"near_or_over_edge": review.edge_items(d, within_mm),
                    "outline_clearance": review.outline_clearance_violations(d)}
        return await _run(go)

    @mcp.tool()
    async def offline_pcb_rules_check(ctx: Context, path: str) -> str:
        """
        Find rules whose scope names something that does not exist or is empty: a net class, pad
        class, component class, net, component, or room. A rule like
        "(InNetClass('Power')) or IsVia" silently matches only vias if no 'Power' class exists.

        Args:
            path (str): Absolute path to a .PcbDoc file.
        """
        return await _run(lambda: review.dead_scopes(_load(path)))

    @mcp.tool()
    async def offline_pcb_pad_ties(ctx: Context, path: str, net_names: list) -> str:
        """
        How each pad on the given nets is actually tied into its copper pour, measured from the
        saved copper: merged directly, joined by thermal-relief spokes (count and width in mm), or
        not joined on that layer. Also names the rule Altium should have applied.

        Args:
            path (str): Absolute path to a .PcbDoc file.
            net_names (list): Net names, e.g. ["VBAT", "GND"].
        """
        def go():
            rows, unknown = connect.pad_connections(_load(path), net_names)
            return {"unevaluated_predicates": unknown, "pads": rows}
        return await _run(go)

    @mcp.tool()
    async def offline_pcb_neck(ctx: Context, path: str, net: str, src: list, dst: list, resolution_mm: float = 0.1,
                               ignore_mm: float = 3.0) -> str:
        """
        Narrowest point of the copper between two groups of pads on one net, per layer, with the
        IPC-2221 current that width carries. Needs numpy, which the server venv does not include;
        without it this says so and the same analysis runs as `python -m offline neck`.

        Args:
            path (str): Absolute path to a .PcbDoc file.
            net (str): Net name.
            src (list): Start pads, like ["J1"] (every pad of J1 on the net) or ["U1.2"].
            dst (list): End pads, same form.
            resolution_mm (float): Raster step; keep at or below a quarter of the narrowest feature.
            ignore_mm (float): Ignore copper this close to the pads (local width there is set by the pad).
        """
        def go():
            try:
                from offline import pdn
            except ImportError as e:
                return {"error": "numpy is not installed in this environment (%s). Run: python -m offline neck "
                                 "<file> --net %s --src %s --dst %s" % (e, net, ",".join(src), ",".join(dst))}
            r = pdn.neck_between(_load(path), net, src, dst, resolution_mm, ignore_mm=ignore_mm)
            th = r.pop("thickness_mm")
            for layer in r.values():
                if layer.get("width_mm"):
                    layer["ipc2221_amps_dT20_external"] = round(pdn.ipc2221_current(layer["width_mm"], th, 20), 1)
                    layer["ipc2221_amps_dT30_external"] = round(pdn.ipc2221_current(layer["width_mm"], th, 30), 1)
            return {"copper_thickness_mm": th, "neck": r}
        return await _run(go)

    @mcp.tool()
    async def calc_pwm_ripple(ctx: Context, volts: float, resistance_ohm: float, inductance_h: float,
                              frequency_hz: float, duty: float = 0.5) -> str:
        """
        Steady-state current in an R-L load (a pump or solenoid) driven by PWM with a freewheel
        path: average, minimum, maximum and peak-to-peak current, and the period relative to L/R.
        Shows why a PWM frequency near the load's L/R pole makes the current collapse every cycle.

        Args:
            volts (float): Supply voltage.
            resistance_ohm (float): Load resistance.
            inductance_h (float): Load inductance in henries (551 uH = 0.000551).
            frequency_hz (float): PWM frequency.
            duty (float): Duty cycle, 0 to 1.
        """
        return await _run(lambda: power.rl_ripple(volts, resistance_ohm, inductance_h, frequency_hz, duty))

    @mcp.tool()
    async def calc_profet(ctx: Context, part: str, amps: float, ambient_c: float = 85.0, rth_ja_k_per_w: float = None,
                          pwm_hz: float = None) -> str:
        """
        Junction temperature, continuous-current limit and PWM cost for an Infineon PROFET+2 channel,
        using datasheet values (BTS7008-1EPP Rev 1.11, BTS7008-2EPA Rev 1.21). Junction-to-ambient
        defaults to the datasheet's 4-layer JEDEC figure, which a 2-layer board will not match:
        pass your own rth_ja_k_per_w.

        Args:
            part (str): "BTS7008-1EPP" or "BTS7008-2EPA".
            amps (float): Load current per channel.
            ambient_c (float): Ambient temperature.
            rth_ja_k_per_w (float): Junction-to-ambient thermal resistance on your board.
            pwm_hz (float): If given, also report what PWM at this frequency costs.
        """
        def go():
            if part not in power.PROFETS:
                raise ValueError("unknown part %r; known: %s" % (part, sorted(power.PROFETS)))
            return {"steady": power.profet_operating_point(part, amps, ambient_c, rth_ja_k_per_w),
                    "max_continuous_a": power.max_current_for_tj(part, ambient_c, rth_ja_k_per_w),
                    "pwm": power.profet_pwm(part, pwm_hz, amps, t_amb=ambient_c, rth_ja=rth_ja_k_per_w)
                    if pwm_hz else None, "datasheet": power.PROFETS[part]}
        return await _run(go)
