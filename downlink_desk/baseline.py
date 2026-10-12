"""A learned model of what normal looks like, channel by channel.

A small satellite's temperatures (and its power numbers) swing with every orbit: it warms in
sunlight and cools in Earth's shadow. So "is 12 °C normal?" depends on where in that cycle the
reading was taken. This module learns, for each channel, the typical value at each point of the
orbit's day/night cycle (minutes since the satellite last came out of shadow), plus how that
shifts as the shadow gets longer or shorter through the seasons. Then it compares the last
couple of days against what the previous three weeks predict.

That catches slow drifts that fixed limits never would: a battery running 3 °C warmer than it
used to at the same point in the orbit is still far inside its limits, but it is a change worth
noticing. Real spacecraft health monitoring works the same way, with far bigger models.
"""
import math
import re
import statistics

from . import db as dbm

DAY = 86_400_000
BASE_DAYS = 21        # learn from up to three weeks...
RECENT_DAYS = 2       # ...before the last two days, which are what gets judged
BIN_MIN = 5           # orbit-phase resolution, minutes
MIN_BASE = 25         # samples needed before a channel is judged
MIN_RECENT = 6
UNUSUAL_SIGMA = 5.0

# Automatic channels that are counters, clocks, flags or identifiers rather than physical values
SKIP = re.compile(r"(cnt|count|boot|uptime|since|time|status|state|mode|flag|_id$|^id|seq|ver|reset|addr|crc|"
                  r"call|err|fail|ack|tx_|rx_|packet|msg|digi|num)", re.I)


# ---------- orbit phase for stored frames ----------

def fill_phases(conn, norad, limit=20_000):
    """Work out minutes-since-sunrise and shadow length for frames that don't have them yet.
    Uses the stored TLE, so only frames within 45 days of its epoch are done; older ones get
    -2 (unknown). Full-sunlight seasons get phase -1."""
    tle = dbm.smeta_get(conn, norad, "tle")
    if not tle:
        return 0
    rows = conn.execute("SELECT id, ts_ms FROM frames WHERE norad=? AND phase_min IS NULL ORDER BY ts_ms LIMIT ?",
                        (norad, limit)).fetchall()
    if not rows:
        return 0
    from .orbit import sun_phases
    epoch = tle.get("epoch_ms") or 0
    near = [r for r in rows if abs(r["ts_ms"] - epoch) < 45 * DAY]
    ph = sun_phases(tle["tle1"], tle["tle2"], [r["ts_ms"] for r in near]) if near else {}
    upd = []
    for r in rows:
        if abs(r["ts_ms"] - epoch) < 45 * DAY:
            p, sh = ph.get(r["ts_ms"], (None, None))
            upd.append((-1 if p is None and sh == 0.0 else (-2 if p is None else p), sh, r["id"]))
        else:
            upd.append((-2, None, r["id"]))
    conn.executemany("UPDATE frames SET phase_min=?, shadow_min=? WHERE id=?", upd)
    conn.commit()
    return len(upd)


# ---------- which channels get a model ----------

def model_channels(conn, spec, norad):
    chans = spec.get("channels") or []
    if chans and not spec.get("auto"):
        return [c for c in chans if not c.get("counter") and c.get("model", True)]
    if not chans:
        chans = [{"id": ch, "name": ch.replace("_", " ").capitalize(), "unit": "", "dp": 2}
                 for (ch,) in conn.execute("SELECT DISTINCT channel FROM points WHERE norad=?", (norad,))]
    return [c for c in chans if not c.get("counter") and not SKIP.search(c["id"])]


# ---------- statistics helpers ----------

def _q(sorted_v, f):
    if not sorted_v:
        return None
    i = f * (len(sorted_v) - 1)
    lo = int(math.floor(i))
    hi = min(lo + 1, len(sorted_v) - 1)
    return sorted_v[lo] + (sorted_v[hi] - sorted_v[lo]) * (i - lo)


def _robust_sigma(v):
    if len(v) < 2:
        return 0.0
    m = statistics.median(v)
    return 1.4826 * statistics.median([abs(x - m) for x in v])


class _Fit:
    """Expected value = typical value at this orbit phase + a shadow-length correction."""

    def __init__(self, base, period):
        self.nb = max(1, int(math.ceil(period / BIN_MIN)))
        self.period = period
        by = [[] for _ in range(self.nb)]
        for r in base:
            if r[2] is not None and r[2] >= 0:
                by[int(r[2] // BIN_MIN) % self.nb].append(r[1])
        self.bins = []
        for b in range(self.nb):
            pool = by[b] + by[(b - 1) % self.nb] + by[(b + 1) % self.nb]   # smooth over neighbours
            if len(pool) >= 4:
                s = sorted(pool)
                self.bins.append((statistics.median(s), _q(s, .1), _q(s, .9), len(by[b])))
            else:
                self.bins.append(None)
        sun = [r[1] for r in base if r[3] == 1]
        shade = [r[1] for r in base if r[3] == 0]
        self.sun = statistics.median(sun) if len(sun) >= 4 else None
        self.shade = statistics.median(shade) if len(shade) >= 4 else None
        self.all = statistics.median([r[1] for r in base])
        # seasonal correction: how the residual moves with shadow length
        self.k, self.sh0 = 0.0, None
        pairs = [(r[4], r[1] - self._phase_only(r)) for r in base if r[4] is not None]
        if len(pairs) >= 20:
            xs = sorted(p[0] for p in pairs)
            if _q(xs, .9) - _q(xs, .1) >= 3:
                mx = sum(p[0] for p in pairs) / len(pairs)
                my = sum(p[1] for p in pairs) / len(pairs)
                sxx = sum((p[0] - mx) ** 2 for p in pairs)
                if sxx > 0:
                    self.k = sum((p[0] - mx) * (p[1] - my) for p in pairs) / sxx
                    self.sh0 = mx

    def _phase_only(self, r):
        if r[2] is not None and r[2] >= 0:
            b = self.bins[int(r[2] // BIN_MIN) % self.nb]
            if b:
                return b[0]
        if r[3] == 1 and self.sun is not None:
            return self.sun
        if r[3] == 0 and self.shade is not None:
            return self.shade
        return self.all

    def expect(self, r):
        e = self._phase_only(r)
        if self.sh0 is not None and r[4] is not None:
            e += self.k * (r[4] - self.sh0)
        return e


def _load(conn, norad, ids, since, until):
    q = ",".join("?" * len(ids))
    data = {i: [] for i in ids}
    for r in conn.execute(f"SELECT p.channel, p.ts_ms, p.value, f.phase_min, f.sunlit, f.shadow_min FROM points p "
                          f"JOIN frames f ON f.id=p.frame_id WHERE p.norad=? AND p.ts_ms>? AND p.ts_ms<=? "
                          f"AND p.channel IN ({q}) ORDER BY p.ts_ms", (norad, since, until, *ids)):
        data[r[0]].append((r[1], r[2], r[3], r[4], r[5]))   # t, value, phase, sunlit, shadow
    return data


def _period(conn, norad):
    tle = dbm.smeta_get(conn, norad, "tle")
    if tle:
        try:
            from sgp4.api import Satrec
            from .orbit import period_min
            return period_min(Satrec.twoline2rv(tle["tle1"], tle["tle2"])), tle
        except Exception:
            pass
    return 95.0, tle


def compute(conn, spec, norad, now=None):
    """Judge every modelled channel. Returns {'channels': [...], 'period_min', 'shadow_min_now', ...}."""
    now = now or dbm.now_ms()
    split = now - RECENT_DAYS * DAY
    chans = model_channels(conn, spec, norad)
    period, tle = _period(conn, norad)
    out = {"now": now, "period_min": round(period, 2), "recent_days": RECENT_DAYS, "baseline_days": BASE_DAYS,
           "channels": []}
    if tle:
        try:
            from .orbit import shadow_minutes
            out["shadow_min_now"] = shadow_minutes(tle["tle1"], tle["tle2"], now)
        except Exception:
            pass
    if not chans:
        return out
    data = _load(conn, norad, [c["id"] for c in chans], split - BASE_DAYS * DAY, now)
    for c in chans:
        rows = data.get(c["id"], [])
        base = [r for r in rows if r[0] < split]
        rec = [r for r in rows if r[0] >= split]
        res = {"id": c["id"], "name": c.get("name", c["id"]), "unit": c.get("unit", ""), "dp": c.get("dp", 2),
               "n_base": len(base), "n_recent": len(rec)}
        if spec.get("auto") or not spec.get("channels"):
            if len({r[1] for r in rows}) < 6:   # a flag or a constant, not a measurement
                continue
        if len(base) < MIN_BASE:
            res.update(status="learning", need=MIN_BASE - len(base))
            out["channels"].append(res)
            continue
        fit = _Fit(base, period)
        bres = [r[1] - fit.expect(r) for r in base]
        floor = 0.5 * 10 ** -int(c.get("dp", 2))
        # Normal scatter. The robust estimate ignores rare spikes, but for a channel that sits on one
        # value and occasionally jumps it collapses to nearly zero, so it's floored at half the plain
        # standard deviation: tiny wobbles in such a channel then don't count as shifts.
        sigma = max(_robust_sigma(bres), 0.5 * statistics.pstdev(bres), floor, 1e-9)
        rres = [(r, r[1] - fit.expect(r)) for r in rec]
        res["sigma"] = sigma
        res["seasonal_per_min"] = round(fit.k, 4) if fit.sh0 is not None else None
        if rows:
            last = rows[-1]
            e = fit.expect(last)
            res["latest"] = {"t": last[0], "value": last[1], "expected": e, "sigmas": (last[1] - e) / sigma}
        if len(rres) >= MIN_RECENT:
            shift = statistics.median([x for _, x in rres])
            z = shift / sigma
            se = 1.2533 * sigma / math.sqrt(len(rres))
            sig = abs(shift) / se
            res.update(shift=shift, z=z)
            # Judged on size (how far, in units of the channel's normal scatter) and on evidence
            # (enough samples that it isn't chance). Size thresholds keep tiny-but-certain shifts quiet.
            res["status"] = "drift" if abs(z) >= 2.0 and sig >= 4 else "watch" if abs(z) >= 1.0 and sig >= 4 else "normal"
        else:
            res["status"] = "quiet"
        # long-term trend of the residuals, per week
        allr = [(r[0], r[1] - fit.expect(r)) for r in rows]
        if allr and allr[-1][0] - allr[0][0] >= 7 * DAY:
            mt = sum(t for t, _ in allr) / len(allr)
            mv = sum(v for _, v in allr) / len(allr)
            stt = sum((t - mt) ** 2 for t, _ in allr)
            if stt > 0:
                res["trend_per_week"] = sum((t - mt) * (v - mv) for t, v in allr) / stt * 7 * DAY
        # one-off oddities: far from the model even after allowing for any drift already flagged
        lvl = res.get("shift", 0.0)
        # Some channels spike as part of normal life (discharge current jumps whenever the radio
        # transmits). If the learning period had spikes that big too, they aren't oddities: raise
        # the bar to just above the largest routine excursions seen while learning.
        dev = sorted(abs(x - statistics.median(bres)) for x in bres)
        bar = max(UNUSUAL_SIGMA * sigma, 1.2 * _q(dev, 0.995))
        res["unusual_bar"] = bar
        unusual = [{"t": r[0], "value": r[1], "expected": r[1] - x, "sigmas": x / sigma}
                   for r, x in rres if abs(x - lvl) >= bar]
        res["unusual"] = unusual[-5:]
        res["n_unusual"] = len(unusual)
        out["channels"].append(res)
    return out


def profile(conn, spec, norad, ch, now=None):
    """Everything needed to draw one channel's orbit profile: the learned curve, the samples it
    learned from, and the recent samples being judged, against minutes since sunrise."""
    now = now or dbm.now_ms()
    split = now - RECENT_DAYS * DAY
    period, tle = _period(conn, norad)
    rows = _load(conn, norad, [ch], split - BASE_DAYS * DAY, now)[ch]
    base = [r for r in rows if r[0] < split]
    rec = [r for r in rows if r[0] >= split]
    out = {"period_min": round(period, 2), "bin_min": BIN_MIN, "bins": [], "base": [], "recent": [],
           "shadow_min": None, "unphased": sum(1 for r in rows if r[2] is None or r[2] < 0)}
    if tle:
        try:
            from .orbit import shadow_minutes
            out["shadow_min"] = shadow_minutes(tle["tle1"], tle["tle2"], now)
        except Exception:
            pass
    if len(base) >= MIN_BASE:
        fit = _Fit(base, period)
        out["bins"] = [[(i + .5) * BIN_MIN, b[0], b[1], b[2]] if b else None for i, b in enumerate(fit.bins)]
    step = max(1, len(base) // 2500)
    out["base"] = [[r[2], r[1]] for r in base[::step] if r[2] is not None and r[2] >= 0]
    out["recent"] = [[r[2], r[1], r[0]] for r in rec if r[2] is not None and r[2] >= 0]
    return out
