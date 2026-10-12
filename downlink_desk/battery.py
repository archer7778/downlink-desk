"""Battery health check from beacon telemetry.

What can be measured from volunteer-received frames (a few snapshots per pass, from stations
all round the world), once each frame is placed in the orbit's day/night cycle:

  - energy balance: charge in minus charge out, over a whole orbit
  - eclipse depth of discharge: how much of the battery each trip through Earth's shadow uses
  - whether the battery gets back to full in sunlight, and the voltage left at the end of eclipse
  - temperatures outside lithium-ion's safe charge and discharge windows
  - an effective internal resistance (how much the voltage sags per amp), which rises as cells age

And what can only be modelled: cycles so far, expected cycle life at this depth and temperature,
and an estimated capacity left. Those rest on assumptions about the battery that a mission team
may not publish, so every assumption lives in the channels file under "battery", with its source.
"""
import math
import random
import statistics

from . import db as dbm

DAY = 86_400_000
BIN = 5            # minutes of orbit phase per bin
WINDOW_DAYS = 14   # measured checks use the last two weeks
MIN_SAMPLES = 20

# Generic lithium-ion cycle life in low Earth orbit: cycles to 80% capacity ≈ N100 × DoD^-k.
# N100 = 1000, k = 2 gives about 11,000 cycles at 30% depth, 25,000 at 20% and 100,000 at 10%,
# in line with published LEO test results for 18650-type cells. Calendar fade 2% a year at 25 °C,
# doubling every 10 °C hotter. All of it can be overridden per satellite in its channels file.
DEFAULT_LIFE = {"cycles_at_100_dod": 1000, "dod_exponent": 2.0, "eol_capacity": 0.8,
                "calendar_fade_per_year": 0.02, "ref_temp_c": 25, "temp_doubling_c": 10}


# ---------- maths helpers ----------

# Open-circuit voltage of a typical 18650-type lithium-ion cell (NMC/LCO) against state of charge.
# Generic: real cells differ by tens of millivolts, which is why capacity from it is "experimental".
OCV = [(0.00, 3.00), (0.05, 3.45), (0.10, 3.55), (0.20, 3.62), (0.30, 3.66), (0.40, 3.70), (0.50, 3.75),
       (0.60, 3.82), (0.70, 3.90), (0.80, 3.98), (0.90, 4.07), (1.00, 4.18)]


def ocv_cell(soc):
    soc = min(1.0, max(0.0, soc))
    for (s0, v0), (s1, v1) in zip(OCV, OCV[1:]):
        if soc <= s1:
            return v0 + (v1 - v0) * (soc - s0) / (s1 - s0)
    return OCV[-1][1]


def _soc(v_cell):
    if v_cell is None:
        return None
    if v_cell <= OCV[0][1]:
        return 0.0
    for (s0, v0), (s1, v1) in zip(OCV, OCV[1:]):
        if v_cell <= v1:
            return s0 + (s1 - s0) * (v_cell - v0) / (v1 - v0)
    return 1.0


def _rt(temp_c, ea=25_000.0):
    """How much higher internal resistance is at this temperature than at 25 °C (Arrhenius)."""
    if temp_c is None:
        return 1.0
    t = max(-40.0, min(70.0, temp_c)) + 273.15
    return math.exp(ea / 8.314 * (1 / t - 1 / 298.15))


def _slope(pts):
    """Least-squares slope and correlation of y on x for points (t, x, y)."""
    if len(pts) < 3:
        return None
    mx = sum(p[1] for p in pts) / len(pts)
    my = sum(p[2] for p in pts) / len(pts)
    sxx = sum((p[1] - mx) ** 2 for p in pts)
    syy = sum((p[2] - my) ** 2 for p in pts)
    sxy = sum((p[1] - mx) * (p[2] - my) for p in pts)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / sxx, sxy / math.sqrt(sxx * syy)


def _mean2(a, b):
    v = [x for x in (a, b) if x is not None]
    return sum(v) / len(v) if v else None


def _slope2(pts):
    """Fit y = R·x + β·dt for points (t, x, y, dt); returns (R, partial correlation of y with x)."""
    if len(pts) < 5:
        return None
    sxx = sum(p[1] * p[1] for p in pts)
    sdd = sum(p[3] * p[3] for p in pts)
    sxd = sum(p[1] * p[3] for p in pts)
    sxy = sum(p[1] * p[2] for p in pts)
    sdy = sum(p[3] * p[2] for p in pts)
    det = sxx * sdd - sxd * sxd
    if det <= 0:
        return None
    r = (sxy * sdd - sdy * sxd) / det
    beta = (sdy * sxx - sxy * sxd) / det
    res = [p[2] - beta * p[3] for p in pts]   # remove the drift part, then correlate with current
    mx = sum(p[1] for p in pts) / len(pts)
    my = sum(res) / len(res)
    cxx = sum((p[1] - mx) ** 2 for p in pts)
    cyy = sum((y - my) ** 2 for y in res)
    cxy = sum((p[1] - mx) * (y - my) for p, y in zip(pts, res))
    return r, (cxy / math.sqrt(cxx * cyy) if cxx > 0 and cyy > 0 else 0.0)


def _passes(pts, gap_ms=15 * 60_000):
    """Group time-sorted tuples (t, ...) into passes; frames within one pass aren't independent."""
    out, cur, last = [], [], None
    for p in sorted(pts, key=lambda p: p[0]):
        if last is not None and p[0] - last > gap_ms:
            out.append(cur)
            cur = []
        cur.append(p)
        last = p[0]
    if cur:
        out.append(cur)
    return out


def _boot(groups, stat, n=200):
    """90% range of a statistic by resampling whole passes."""
    if len(groups) < 5:
        return None
    rnd = random.Random(len(groups))
    vals = []
    for _ in range(n):
        v = stat([groups[rnd.randrange(len(groups))] for _ in groups])
        if v is not None:
            vals.append(v)
    if len(vals) < n // 2:
        return None
    vals.sort()
    return vals[int(.05 * len(vals))], vals[int(.95 * len(vals)) - 1]


def _med(v):
    return statistics.median(v) if v else None


def _load(conn, norad, b, since, now):
    vch, cch, dch, tch = b["voltage"]["channel"], b.get("charge"), b.get("discharge"), b.get("temperature")
    ids = [x for x in (vch, cch, dch, tch) if x]
    q = ",".join("?" * len(ids))
    frames = {}
    for r in conn.execute(f"SELECT p.frame_id, p.channel, p.value, p.ts_ms, f.phase_min, f.shadow_min, f.sunlit "
                          f"FROM points p JOIN frames f ON f.id=p.frame_id WHERE p.norad=? AND p.ts_ms>? AND p.ts_ms<=? "
                          f"AND p.channel IN ({q}) ORDER BY p.ts_ms", (norad, since, now, *ids)):
        fr = frames.setdefault(r[0], {"t": r[3], "phase": r[4] if (r[4] is not None and r[4] >= 0) else None,
                                      "shadow": r[5], "sunlit": r[6]})
        fr[r[1]] = r[2]
    scale = _volt_scale(b, [f[vch] for f in frames.values() if vch in f])
    out = []
    for f in frames.values():
        out.append({"t": f["t"], "phase": f["phase"], "shadow": f["shadow"], "sunlit": f["sunlit"],
                    "v": f[vch] * scale if vch in f and scale else None,
                    "ic": f.get(cch), "id": f.get(dch), "temp": f.get(tch)})
    return sorted(out, key=lambda x: x["t"]), scale


def _volt_scale(b, raw):
    """Volts per count. 'auto' picks the scale that puts the median inside the pack's range."""
    s = b["voltage"].get("scale", "auto")
    if s != "auto":
        return float(s)
    if not raw:
        return None
    m = statistics.median(raw)
    lo, hi = b["v_empty"] * 0.9, b["v_full"] * 1.05
    for cand in (1.0, 0.001, 0.0005, 0.0001, 0.01, 0.1):
        if lo <= m * cand <= hi:
            return cand
    return None


def _clipped_mean(v):
    """Mean, after dropping readings wildly out of line (decoding glitches). Currents are averaged
    rather than taking the median, because short bursts (the radio transmitting) really use charge."""
    if not v:
        return None
    m = statistics.median(v)
    s = 1.4826 * statistics.median([abs(x - m) for x in v]) or (max(v) - min(v)) or 1.0
    keep = [x for x in v if abs(x - m) <= 8 * s]
    return sum(keep) / len(keep)


def _bins(rows, key, period, agg=None):
    nb = max(1, int(math.ceil(period / BIN)))
    by = [[] for _ in range(nb)]
    for r in rows:
        if r["phase"] is not None and r.get(key) is not None:
            by[int(r["phase"] // BIN) % nb].append(r[key])
    out = []
    for i in range(nb):
        pool = by[i] + by[(i - 1) % nb] + by[(i + 1) % nb]
        out.append((agg or _med)(pool) if len(pool) >= 3 else None)
    return out, by


def _fill(bins):
    """Fill gaps by linear interpolation around the orbit (it's a cycle)."""
    n = len(bins)
    known = [i for i, v in enumerate(bins) if v is not None]
    if not known:
        return None
    out = list(bins)
    for i in range(n):
        if out[i] is None:
            prev = max([k for k in known if k < i], default=known[-1] - n)
            nxt = min([k for k in known if k > i], default=known[0] + n)
            a, b = bins[prev % n], bins[nxt % n]
            out[i] = a + (b - a) * (i - prev) / (nxt - prev)
    return out


def _period(conn, norad):
    tle = dbm.smeta_get(conn, norad, "tle")
    if tle:
        from sgp4.api import Satrec
        from .orbit import period_min, shadow_minutes
        return period_min(Satrec.twoline2rv(tle["tle1"], tle["tle2"])), shadow_minutes(tle["tle1"], tle["tle2"], dbm.now_ms())
    return 95.0, None


def _check(cid, title, status, value, detail):
    return {"id": cid, "title": title, "status": status, "value": value, "detail": detail}


def analyse(conn, spec, norad, launched=None, now=None):
    b = spec.get("battery")
    if not b:
        return {"available": False}
    now = now or dbm.now_ms()
    life_p = {**DEFAULT_LIFE, **(b.get("life") or {})}
    period, shadow = _period(conn, norad)
    cap_mah = b["capacity_wh"] / b["nominal_v"] * 1000
    rows, scale = _load(conn, norad, b, now - 120 * DAY, now)
    recent = [r for r in rows if r["t"] > now - WINDOW_DAYS * DAY]
    for r in recent:
        r["net"] = (r["ic"] - r["id"]) if r["ic"] is not None and r["id"] is not None else None
    phased = [r for r in recent if r["phase"] is not None]
    res = {"available": True, "now": now, "period_min": round(period, 2), "shadow_min": shadow,
           "capacity_mah": round(cap_mah), "volt_scale": scale, "samples": len(recent), "phased": len(phased),
           "window_days": WINDOW_DAYS, "assumptions": {k: v for k, v in b.items() if k != "life"}, "life_params": life_p,
           "checks": []}
    C = res["checks"]
    if len(phased) < MIN_SAMPLES:
        res["grade"] = "learning"
        res["need"] = MIN_SAMPLES - len(phased)
        return res

    # ---- energy balance over one orbit ----
    nb = max(1, int(math.ceil(period / BIN)))
    net_b, net_raw = _bins(phased, "net", period, _clipped_mean)
    v_b, _ = _bins(phased, "v", period)
    cover = sum(1 for x in net_raw if x) / nb
    netf = _fill(net_b)
    vf = _fill(v_b)
    sunset = (period - shadow) if shadow else None
    res["profile"] = [[(i + .5) * BIN, net_b[i], v_b[i], len(net_raw[i])] for i in range(nb)]
    res["coverage"] = round(cover, 2)
    balance = None
    if netf and cover >= 0.5:
        widths = [min(BIN, period - i * BIN) for i in range(nb)]   # the last bin is only part-full
        balance = sum(x * w for x, w in zip(netf, widths)) / 60   # mAh per orbit
        res["balance_mah"] = round(balance, 1)
        st = "ok" if balance > 0.01 * cap_mah else "warn" if balance > -0.02 * cap_mah else "crit"
        C.append(_check("balance", "Energy balance per orbit", st, f"{balance:+.0f} mAh",
                        ("Takes in more charge in sunlight than it uses over the whole orbit, so the battery is kept topped up."
                         if st == "ok" else "Roughly break-even over an orbit; little margin if loads go up or panels degrade."
                         if st == "warn" else "Uses more than it takes in: the battery is running down orbit by orbit.")
                        + f" Based on {len(phased)} frames covering {cover:.0%} of the orbit."))
    else:
        C.append(_check("balance", "Energy balance per orbit", "unknown", "—",
                        f"Frames so far cover only {cover:.0%} of the orbit; need about half to integrate it."))

    # ---- eclipse depth of discharge ----
    dod = None
    if shadow and sunset is not None:
        shade_net = [r["net"] for r in phased if r["net"] is not None and r["phase"] >= sunset]
        if len(shade_net) >= 5:   # average drain in shadow (trimmed mean: energy needs a mean) × time in shadow
            used = -_clipped_mean(shade_net) * shadow / 60
        else:
            used = None
        if used is not None:
            dod = max(0.0, used / cap_mah)
            res["eclipse_mah"] = round(used, 1)
            res["dod"] = round(dod, 4)
            st = "ok" if dod < 0.2 else "warn" if dod < 0.35 else "crit"
            C.append(_check("dod", "Depth of discharge per eclipse", st, f"{dod:.0%}",
                            f"About {used:.0f} mAh of an assumed {cap_mah:.0f} mAh drawn during each {shadow:.0f}-minute eclipse. "
                            "Under 20% is gentle for lithium-ion in orbit; deeper cycles wear cells out much faster."))
    elif shadow == 0:
        C.append(_check("dod", "Depth of discharge per eclipse", "ok", "0%", "In a full-sunlight season: no eclipses, no cycling."))

    # ---- full charge and end-of-eclipse voltage ----
    if scale:
        sun_v = [r["v"] for r in phased if r["v"] is not None and sunset and sunset - 20 <= r["phase"] < sunset]
        eoe = [r for r in rows if r["v"] is not None and r["phase"] is not None and sunset and r["phase"] >= period - 12]
        top = _med(sun_v)
        if top is not None:
            full = top >= b["v_full"] - 0.15
            res["v_top"] = round(top, 3)
            C.append(_check("full", "Reaches full charge in sunlight", "ok" if full else "warn", f"{top:.2f} V",
                            f"Typical voltage in the last 20 minutes of sunlight, against a full pack at {b['v_full']} V. "
                            + ("It tops up each orbit." if full else "It isn't getting back to full: less solar input, more load, or an ageing pack.")))
        if len(eoe) >= 5:
            vmin = _med([r["v"] for r in eoe if r["t"] > now - WINDOW_DAYS * DAY] or [r["v"] for r in eoe])
            margin = vmin - b["v_empty"]
            res["v_end_eclipse"] = round(vmin, 3)
            res["eoe_series"] = [[r["t"], round(r["v"], 4)] for r in eoe]
            st = "ok" if margin > 0.8 else "warn" if margin > 0.3 else "crit"
            trend = None
            if eoe[-1]["t"] - eoe[0]["t"] >= 21 * DAY:
                mt = sum(r["t"] for r in eoe) / len(eoe)
                mv = sum(r["v"] for r in eoe) / len(eoe)
                stt = sum((r["t"] - mt) ** 2 for r in eoe)
                trend = sum((r["t"] - mt) * (r["v"] - mv) for r in eoe) / stt * 30 * DAY if stt else None
                res["eoe_trend_v_per_month"] = trend
            C.append(_check("eoe", "Voltage at the end of eclipse", st, f"{vmin:.2f} V",
                            f"The lowest point of each orbit, {margin:.2f} V above the empty level ({b['v_empty']} V). "
                            + (f"Trend {trend * 1000:+.0f} mV per month; a steady fall is the classic sign of an ageing battery."
                               if trend is not None else "A trend needs about three weeks of data.")))
    else:
        C.append(_check("full", "Voltage checks", "unknown", "—",
                        "Couldn't work out the voltage scale; set battery.voltage.scale in the channels file."))

    # ---- temperature windows ----
    temps = [r for r in recent if r["temp"] is not None]
    if temps:
        lo_c, hi_c = b.get("charge_temp", [0, 45])
        lo_d, hi_d = b.get("discharge_temp", [-20, 60])
        chg = [r for r in temps if r["net"] is not None and r["net"] > 0]
        bad_c = sum(1 for r in chg if not lo_c <= r["temp"] <= hi_c)
        bad_d = sum(1 for r in temps if not lo_d <= r["temp"] <= hi_d)
        tv = sorted(r["temp"] for r in temps)
        res["temp"] = {"min": tv[0], "median": _med(tv), "max": tv[-1]}
        frac = (bad_c + bad_d) / len(temps)
        st = "ok" if frac == 0 else "warn" if frac < 0.05 else "crit"
        C.append(_check("temp", "Battery temperature", st, f"{tv[0]:.0f} to {tv[-1]:.0f} °C",
                        f"Median {res['temp']['median']:.1f} °C. Lithium-ion charges safely at {lo_c}–{hi_c} °C and discharges at "
                        f"{lo_d}–{hi_d} °C; {bad_c} charging and {bad_d} other samples fell outside. "
                        "The pack has a heater, so cold readings are usually brief."))

    # ---- effective internal resistance, normalised to 25 °C ----
    # Remove the orbit pattern from voltage and current, then ask how much the voltage moves per amp.
    # Lithium-ion resistance roughly doubles from 25 °C down to 0 °C (Arrhenius, Ea ≈ 25 kJ/mol), so
    # each sample is put on a 25 °C footing before the fit; that way a cold week doesn't look like ageing.
    r25 = None
    method = None
    if scale:
        # Best: frames seconds apart in the same pass. State of charge and temperature can't change
        # in that time, but the current does (the radio transmitting), so ΔV/ΔI is the resistance.
        # A small Δt term soaks up the slow drift in charge across the pair.
        seq = [r for r in recent if r["v"] is not None and r["net"] is not None]
        pairs = []
        for a, b_ in zip(seq, seq[1:]):
            dt = (b_["t"] - a["t"]) / 1000
            if 0 < dt <= 180 and abs(b_["net"] - a["net"]) >= 20:
                f = _rt(_mean2(a["temp"], b_["temp"]))
                pairs.append((a["t"], (b_["net"] - a["net"]) * f, b_["v"] - a["v"], dt))
        if len(pairs) >= 20:
            method = "within-pass"
            fitr = _slope2(pairs)
        elif netf and vf:
            # Fallback: remove the orbit pattern from voltage and current, then ask how much the
            # voltage moves per amp across many passes.
            method = "across-orbits"
            pairs = []
            for r in phased:
                if r["v"] is None or r["net"] is None:
                    continue
                i = int(r["phase"] // BIN) % nb
                pairs.append((r["t"], (r["net"] - netf[i]) * _rt(r["temp"]), r["v"] - vf[i]))
            fitr = _slope(pairs)
        else:
            fitr = None
        res["resistance_method"] = method
        if fitr and len(pairs) >= 30:
            r_ohm, corr = fitr[0] * 1000, fitr[1]          # V per mA → ohms
            ok = corr > 0.3 and 0 < r_ohm < 2
            res["resistance_mohm"] = round(r_ohm * 1000)
            res["resistance_corr"] = round(corr, 2)
            if ok:
                r25 = r_ohm
                sl = _slope2 if method == "within-pass" else _slope
                rng = _boot(_passes(pairs), lambda ps: (sl([x for g in ps for x in g]) or [None])[0], 200)
                if rng:
                    res["resistance_range_mohm"] = [round(rng[0] * 1e6), round(rng[1] * 1e6)]
                # ageing check: first half of the window against the second
                if pairs[-1][0] - pairs[0][0] >= 10 * DAY:
                    mid = pairs[0][0] + (pairs[-1][0] - pairs[0][0]) / 2
                    sl = _slope2 if method == "within-pass" else _slope
                    a, b2 = sl([p for p in pairs if p[0] < mid]), sl([p for p in pairs if p[0] >= mid])
                    if a and b2 and a[0] > 0:
                        res["resistance_change_pct"] = round((b2[0] / a[0] - 1) * 100, 1)
            ch = res.get("resistance_change_pct")
            rr = res.get("resistance_range_mohm")
            C.append(_check("res", "Internal resistance (at 25 °C)", "info" if ok else "unknown",
                            f"{r_ohm * 1000:.0f} mΩ" if ok else "—",
                            (f"How much the voltage sags per amp, corrected to 25 °C, from {len(pairs)} "
                             f"{'pairs of frames seconds apart' if method == 'within-pass' else 'frames'} in "
                             f"{len(_passes(pairs))} passes (90% range {rr[0]}–{rr[1]} mΩ, correlation {corr:.2f}). " if ok and rr else "")
                            + (f"Change across the window: {ch:+.0f}%. " if ch is not None else "")
                            + ("It includes wiring and sensing, so the trend matters more than the number: cells age "
                               "by rising resistance." if ok else f"Not clear yet from {len(pairs)} frames (correlation "
                               f"{corr:.2f}); needs more passes with varied current.")))

    # ---- state of charge and measured capacity (experimental) ----
    # Voltage minus the resistive sag gives the open-circuit voltage, and a lithium-ion cell's
    # open-circuit voltage maps to its state of charge. Through the eclipse we know how much charge
    # has been drawn (the measured currents, integrated from sunset). Charge drawn ÷ fall in state of
    # charge = capacity: a measurement of how much the battery actually holds, not the datasheet figure.
    if scale and netf and shadow and sunset is not None:
        cells = b.get("cells_series") or max(1, round(b["v_full"] / 4.2))
        r_use = (r25 if r25 is not None else 0.1)
        # charge drawn since sunset, at each phase (mAh), from the orbit's average net current
        drawn, acc, step = {}, 0.0, BIN / 60
        for i in range(nb):
            mid = (i + .5) * BIN
            if mid >= sunset:
                acc += -netf[i] * step
                drawn[i] = acc - (-netf[i] * step) / 2
        pts = []
        for r in phased:
            if r["v"] is None or r["net"] is None or r["phase"] < sunset:
                continue
            i = int(r["phase"] // BIN) % nb
            if i not in drawn:
                continue
            ocv = r["v"] - r_use * _rt(r["temp"]) * r["net"] / 1000
            soc = _soc(ocv / cells)
            if soc is not None:
                pts.append((r["t"], drawn[i], soc))
        if len(pts) >= 15 and (max(p[1] for p in pts) - min(p[1] for p in pts)) > 0.3 * (res.get("eclipse_mah") or 1):
            fitc = _slope([(t, x, y) for t, x, y in pts])
            if fitc and fitc[0] < 0:
                cap = -1 / fitc[0]
                rng = _boot(_passes(pts), lambda ps: (lambda f: -1 / f[0] if f and f[0] < 0 else None)(
                    _slope([x for g in ps for x in g])), 200)
                res["capacity_measured_mah"] = round(cap)
                if rng:
                    res["capacity_range_mah"] = [round(rng[0]), round(rng[1])]
                frac = cap / cap_mah
                res["capacity_vs_rated"] = round(frac, 3)
                top = _med([_soc((r["v"] - r_use * _rt(r["temp"]) * (r["net"] or 0) / 1000) / cells) for r in phased
                            if r["v"] is not None and r["phase"] is not None and sunset - 15 <= r["phase"] < sunset])
                if top is not None:
                    res["soc_top"] = round(top, 3)
                st = "ok" if frac >= 0.85 else "warn" if frac >= 0.7 else "crit"
                cr = res.get("capacity_range_mah")
                C.append(_check("capacity", "Measured capacity (experimental)", st, f"≈{cap:,.0f} mAh",
                                f"From {len(pts)} eclipse frames: charge drawn since sunset against the fall in state of "
                                f"charge read from voltage{f' (90% range {cr[0]:,}–{cr[1]:,} mAh)' if cr else ''}. That is "
                                f"{frac:.0%} of the assumed {cap_mah:,.0f} mAh. It relies on a generic lithium-ion voltage "
                                "curve, so treat it as a cross-check on the model below, not a calibrated reading."))

    # ---- cycles and modelled life ----
    if launched and shadow:
        import datetime as _dt
        t0 = int(_dt.datetime.fromisoformat(launched).replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
        years = (now - t0) / (365.25 * DAY)
        cycles = (now - t0) / (period * 60_000)
        res["cycles"] = round(cycles)
        res["years"] = round(years, 2)
        if dod:
            med_t = (res.get("temp") or {}).get("median", life_p["ref_temp_c"])
            arr = 2 ** ((med_t - life_p["ref_temp_c"]) / life_p["temp_doubling_c"])
            tf = max(1.0, arr)        # heat shortens cycle life; cold gives no credit (it risks lithium plating)
            tf_cal = max(0.5, arr)    # calendar ageing does slow down when cool
            fade_budget = 1 - life_p["eol_capacity"]

            def life(n100, k):
                n_eol = n100 * max(dod, 0.02) ** (-k) / tf             # cycles to end of life at this depth
                cyc_rate = fade_budget / n_eol                           # capacity lost per cycle
                cal_rate = life_p["calendar_fade_per_year"] * tf_cal     # per year
                per_year = cyc_rate * (365.25 * 1440 / period) + cal_rate
                used = cyc_rate * cycles + cal_rate * years
                left = max(0.0, fade_budget - used)
                return n_eol, 1 - used, left / per_year if per_year else None

            mid = life(life_p["cycles_at_100_dod"], life_p["dod_exponent"])
            pess = life(life_p["cycles_at_100_dod"] / 1.6, life_p["dod_exponent"] - 0.2)
            opt = life(life_p["cycles_at_100_dod"] * 1.6, life_p["dod_exponent"] + 0.2)
            res["life"] = {"cycle_life": round(mid[0]), "capacity_now": round(mid[1], 3),
                           "capacity_range": [round(pess[1], 3), round(opt[1], 3)],
                           "years_left": round(mid[2], 1) if mid[2] is not None else None,
                           "years_left_range": [round(pess[2], 1), round(opt[2], 1)],
                           "temp_factor": round(tf, 2)}
            L = res["life"]
            st = "ok" if L["years_left"] and L["years_left"] > 2 else "warn" if L["years_left"] and L["years_left"] > 0.5 else "crit"
            C.append(_check("life", "Estimated battery life (model)", st,
                            f"≈{L['capacity_now']:.0%} capacity",
                            f"{cycles:,.0f} charge cycles in {years:.1f} years since launch. At {dod:.0%} depth and a median "
                            f"{med_t:.0f} °C, cells like these last roughly {L['cycle_life']:,} cycles to {life_p['eol_capacity']:.0%} capacity. "
                            f"Estimated {L['capacity_range'][0]:.0%}–{L['capacity_range'][1]:.0%} of original capacity left, and "
                            f"{L['years_left_range'][0]:.0f}–{L['years_left_range'][1]:.0f} years before it reaches "
                            f"{life_p['eol_capacity']:.0%}. A model from assumed battery data, not a measurement."))

    order = {"crit": 3, "warn": 2, "ok": 1}
    worst = max((order.get(c["status"], 0) for c in C), default=0)
    res["grade"] = {3: "concern", 2: "watch", 1: "good"}.get(worst, "learning")
    return res


def summary_line(r):
    if not r.get("available"):
        return None
    if r.get("grade") == "learning":
        return f"Battery: still gathering data ({r.get('need', 0)} more frames with an orbit position needed)"
    bits = []
    if "balance_mah" in r:
        bits.append(f"{r['balance_mah']:+.0f} mAh per orbit")
    if "dod" in r:
        bits.append(f"{r['dod']:.0%} used per eclipse")
    if "v_end_eclipse" in r:
        bits.append(f"{r['v_end_eclipse']:.2f} V at end of eclipse")
    if "life" in r:
        bits.append(f"≈{r['life']['capacity_now']:.0%} capacity (model)")
    return f"Battery {r['grade']}: " + ", ".join(bits)
