"""Power-stage calculators for a switched-load power distribution board (stdlib only).

PROFET figures below are quoted from Infineon's datasheets (BTS7008-2EPA Rev 1.21 and
BTS7008-1EPP Rev 1.11, both 2024-07-29). Each carries the table it came from. Anything a
datasheet does not give is None, never a guess; pass your own numbers for other parts.

Nothing here replaces a thermal measurement. The junction-to-ambient figures are JEDEC
4-layer (2s2p) values; a 2-layer board does worse, so rth_ja is always an argument.
"""
import math

# mohm, A, V, K/W, us, V/us, mJ
PROFETS = {
    "BTS7008-1EPP": {
        "channels": 1, "package": "PG-TSDSO-14",
        "rds25_typ_mohm": 8.8, "rds150_max_mohm": 16.0,          # Table 1
        "i_nom_a": 11.0,                                          # Table 1, TA=85C
        "i_ovl0_min_m40_a": 69.0,                                 # Table 1
        "i_ovl0_min_150_a": None,
        "k_ilis": 14800.0,                                        # Table 1, typ at IL(NOM)
        "clamp_v": (35.0, 38.0, 44.0),                            # Table 11, TJ>=25C
        "rth_ja": 33.7, "rth_jc_typ": 2.6, "rth_jc_max": 4.4,     # Table 6, JEDEC 2s2p
        "t_on_delay_us": (10, 70, 130), "t_off_delay_us": (10, 50, 160),   # Table 12, VS=13.5 V
        "t_on_us": (50, 130, 210), "t_off_us": (30, 100, 220),             # to 90% / 10% of VS
        "slew_v_per_us": None, "e_as_mj": None, "e_ar_mj": None,
    },
    "BTS7008-2EPA": {
        "channels": 2, "package": "PG-TSDSO-14 (2 ch)",
        "rds25_typ_mohm": 9.0, "rds150_max_mohm": 16.0,           # Tables 13 (typ by design), 1
        "i_nom_a": 7.5,                                           # per channel, all channels on
        "i_ovl0_min_m40_a": 77.0, "i_ovl0_min_150_a": 65.0,       # Table 17
        "k_ilis": 5450.0,
        "clamp_v": (35.0, 38.0, 44.0),
        "rth_ja": 30.9, "rth_jc_typ": 0.8, "rth_jc_max": 1.4,
        "t_on_delay_us": (10, 35, 60), "t_off_delay_us": (10, 25, 50),
        "t_on_us": (30, 60, 110), "t_off_us": (15, 50, 100),
        "slew_v_per_us": (0.3, 0.6, 0.9),                         # 30-70% of VS
        "e_as_mj": 75.0, "e_ar_mj": 25.0,                         # Table 4 (15 A / 7.5 A)
        "parallel_channels_ok": False,                            # section 7.3.2
    },
}
INPUT_CLAMP_V = 7.0            # VDI(CLAMP1) typ at 1 mA, both parts' logic pins
INPUT_THRESHOLD_V = (0.8, 1.3, 2.0)


def rds_hot_mohm(part, tj):
    """On-resistance at junction temperature `tj`, linear from the 25 C typical to the
    150 C maximum (so it is conservative between the two points)."""
    p = PROFETS[part]
    return p["rds25_typ_mohm"] + (p["rds150_max_mohm"] - p["rds25_typ_mohm"]) * (tj - 25.0) / 125.0


def profet_operating_point(part, amps, t_amb, rth_ja=None, channels_on=1, iterate=40):
    """Self-consistent junction temperature with R_DS(on) rising with temperature.
    `rth_ja` defaults to the datasheet value (a 4-layer test board)."""
    p = PROFETS[part]
    rth = rth_ja if rth_ja is not None else p["rth_ja"]
    tj = t_amb
    for _ in range(iterate):
        pw = channels_on * amps ** 2 * rds_hot_mohm(part, tj) * 1e-3
        new = t_amb + pw * rth
        if abs(new - tj) < 1e-4:
            break
        tj = new
        if tj > 400:
            break
    return {"part": part, "amps": amps, "t_amb_c": t_amb, "rth_ja": rth, "power_w": round(pw, 3),
            "tj_c": round(tj, 1), "ok_to_150c": tj <= 150.0, "runaway": tj > 400}


def max_current_for_tj(part, t_amb, rth_ja=None, tj_max=150.0, channels_on=1):
    """Largest continuous current per channel that keeps TJ <= tj_max (bisection)."""
    lo, hi = 0.0, 60.0
    for _ in range(60):
        mid = (lo + hi) / 2
        r = profet_operating_point(part, mid, t_amb, rth_ja, channels_on)
        if r["tj_c"] <= tj_max and not r["runaway"]:
            lo = mid
        else:
            hi = mid
    return round(lo, 2)


def profet_pwm(part, f_hz, amps, vbat=13.5, duty=0.5, which=1, t_amb=85.0, rth_ja=None):
    """How much of a PWM period the datasheet timings consume, and what that costs.

    `which` picks the min (0), typical (1) or max (2) timing column. Edge energy assumes a
    hard-switched inductive load (V_DS swings fully while the current is already flowing,
    over the 10-90% time / 0.8), which is the worst case; a resistive load loses roughly a
    third of that. The verdict is judged on junction temperature, not on which loss is bigger.
    """
    p = PROFETS[part]
    rth = rth_ja if rth_ja is not None else p["rth_ja"]
    T = 1e6 / f_hz
    ton, toff = p["t_on_us"][which], p["t_off_us"][which]
    dly_on, dly_off = p["t_on_delay_us"][which], p["t_off_delay_us"][which]
    t_ramp_on = (ton - dly_on) / 0.8
    t_ramp_off = (toff - dly_off) / 0.8
    e_sw_mj = 0.5 * vbat * amps * (t_ramp_on + t_ramp_off) * 1e-6 * 1e3
    p_sw = e_sw_mj * 1e-3 * f_hz
    p_cond = amps ** 2 * p["rds150_max_mohm"] * 1e-3 * duty
    total = p_sw + p_cond
    tj = t_amb + total * rth
    if (t_ramp_on + t_ramp_off) > 0.5 * T:
        verdict = "edges take more than half the period"
    elif tj > 150.0:
        verdict = "too hot: TJ %.0f C at %.0f C ambient" % (tj, t_amb)
    else:
        verdict = "usable"
    return {"period_us": round(T, 1), "ramp_on_us": round(t_ramp_on, 1), "ramp_off_us": round(t_ramp_off, 1),
            "ramps_fraction_of_period": round((t_ramp_on + t_ramp_off) / T, 3),
            "on_plus_off_time_fraction": round((ton + toff) / T, 3),
            "switching_loss_w": round(p_sw, 2), "conduction_loss_w_150c": round(p_cond, 2),
            "total_w": round(total, 2), "tj_c": round(tj, 1), "verdict": verdict}


def solenoid_pulse(part, i_peak, t_pulse_s, l_h=None, vbat=14.4):
    """Can a channel carry a short, large solenoid pulse, and what happens at turn-off?"""
    p = PROFETS[part]
    ovl = p["i_ovl0_min_150_a"] or p["i_ovl0_min_m40_a"]
    e_cond_j = i_peak ** 2 * p["rds150_max_mohm"] * 1e-3 * t_pulse_s
    out = {"i_peak": i_peak, "overload_threshold_min_a": ovl, "margin": round(ovl / i_peak, 2),
           "within_overload_limit": ovl > i_peak, "conduction_energy_mj": round(e_cond_j * 1e3, 1),
           "peak_power_w": round(i_peak ** 2 * p["rds150_max_mohm"] * 1e-3, 1)}
    if l_h is not None:
        ef = 0.5 * l_h * i_peak ** 2
        out["flyback_energy_mj"] = round(ef * 1e3, 1)
        if p["e_as_mj"]:
            out["flyback_vs_EAS"] = round(ef * 1e3 / p["e_as_mj"], 2)
        out["needs_external_freewheel"] = bool(p["e_as_mj"]) and ef * 1e3 > 0.5 * p["e_as_mj"]
    return out


def sense_resistor(part, i_full_scale, v_adc_full):
    """R_IS so that i_full_scale maps to v_adc_full (V_IS = I_L * R_IS / k_ILIS)."""
    k = PROFETS[part]["k_ilis"]
    r = v_adc_full * k / i_full_scale
    return {"r_is_ohm": round(r, 1), "v_at_nominal": round(PROFETS[part]["i_nom_a"] * r / k, 3),
            "k_ilis": k, "note": "k_ILIS is typical at I_L(NOM); accuracy is worse at small currents"}


def input_network(v_high, r_series, r_pulldown=None):
    """Drive an IN pin from a source that swings higher than the pin's 7 V clamp."""
    i_clamp = max(v_high - INPUT_CLAMP_V, 0.0) / r_series
    return {"clamp_current_ma": round(i_clamp * 1e3, 3), "within_1ma_test_condition": i_clamp <= 1e-3,
            "pin_high_v": min(v_high, INPUT_CLAMP_V), "threshold_max_v": INPUT_THRESHOLD_V[2]}


# ---- discrete stage and load -------------------------------------------------

def rl_ripple(v, r, l, f_hz, duty):
    """Steady-state current in an R-L load chopped by an ideal switch with a freewheel path.
    Exact (no continuous-conduction assumption): returns avg, min, max, peak-to-peak (A)."""
    tau = l / r
    T = 1.0 / f_hz
    a = math.exp(-duty * T / tau)
    b = math.exp(-(1.0 - duty) * T / tau)
    i_max = (v / r) * (1.0 - a) / (1.0 - a * b)
    i_min = b * i_max
    return {"avg_a": round(v * duty / r, 3), "min_a": round(i_min, 3), "max_a": round(i_max, 3),
            "pp_a": round(i_max - i_min, 3), "pp_pct_of_avg": round(100 * (i_max - i_min) / (v * duty / r), 1),
            "t_over_tau": round(T / tau, 3), "continuous": i_min > 0.01}


def mosfet_stage(rds_hot_mohm, qg_nc, qgd_nc, v_plateau, v_drive, r_gate_on, r_gate_off, i_load,
                 v_bus, f_hz, duty, qgs2_nc=0.0, rth_ja=None, t_amb=None):
    """Loss budget for one switching FET (Miller-plateau model).

    Ignores reverse recovery and Coss, so switching loss is a floor, not a ceiling. Pass
    rds_hot_mohm at the junction temperature you expect, not the 25 C figure.
    """
    ig_on = (v_drive - v_plateau) / r_gate_on
    ig_off = v_plateau / r_gate_off
    t_on = (qgd_nc + qgs2_nc) * 1e-9 / ig_on
    t_off = (qgd_nc + qgs2_nc) * 1e-9 / ig_off
    p_sw = f_hz * 0.5 * v_bus * i_load * (t_on + t_off)
    p_cond = duty * i_load ** 2 * rds_hot_mohm * 1e-3
    p_gate = qg_nc * 1e-9 * v_drive * f_hz
    tot = p_sw + p_cond
    out = {"t_on_ns": round(t_on * 1e9, 1), "t_off_ns": round(t_off * 1e9, 1), "p_switch_w": round(p_sw, 3),
           "p_conduction_w": round(p_cond, 3), "p_gate_drive_w": round(p_gate, 4), "p_fet_w": round(tot, 3),
           "switching_share_pct": round(100 * p_sw / tot, 1) if tot else 0.0}
    if rth_ja is not None and t_amb is not None:
        out["tj_c"] = round(t_amb + tot * rth_ja, 1)
    return out


def freewheel_diode(vf, i_load, duty, qrr_nc=0.0, v_bus=0.0, f_hz=0.0):
    """Loss in the diode that carries an inductive load's current while the switch is off.

    Conduction: vf * I * (1 - duty), with the load current taken as continuous. Reverse
    recovery adds qrr * v_bus * f (zero for a Schottky; pass qrr_nc for a PN diode).
    """
    p_cond = vf * i_load * (1.0 - duty)
    p_rr = qrr_nc * 1e-9 * v_bus * f_hz
    return {"p_conduction_w": round(p_cond, 3), "p_recovery_w": round(p_rr, 4),
            "p_diode_w": round(p_cond + p_rr, 3)}


def copper_area_for_dissipation(p_w, dt_k, h=12.0, sides=2, spread=0.6):
    """First-order copper area (cm^2, per side) that sheds `p_w` at a `dt_k` rise over ambient.

    h: combined convection+radiation coefficient, W/m^2/K (10-15 for still air).
    sides: 2 when the copper on both faces is tied together with vias.
    spread: fraction of the area that is effectively at device temperature (0.5-0.7 typical).
    An estimate to size a pour, to be confirmed by measurement, not a substitute for one.
    """
    a_m2 = p_w / (dt_k * h * sides * spread)
    return round(a_m2 * 1e4 / sides, 1)
