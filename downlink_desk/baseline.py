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
import random
import re
import statistics
import zlib

from . import db as dbm

DAY = 86_400_000
BASE_DAYS = 21        # learn from up to three weeks...
RECENT_DAYS = 2       # ...before the last two days, which are what gets judged
BIN_MIN = 5           # orbit-phase resolution, minutes
MIN_BASE = 25         # samples needed before a channel is judged
MIN_RECENT = 6
MIN_RECENT_PASSES = 3
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


def _solve(A, b):
    """Solve A x = b for a small dense system (Gaussian elimination with partial pivoting)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            if f:
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


class _Fit:
    """Expected value of a channel at a given point in the orbit.

    value ≈ a0 + Σₖ (aₖ cos kθ + bₖ sin kθ) + c · (shadow length − typical)

    θ is the position in the orbit's day/night cycle (2π × minutes since sunrise / period), so the
    Fourier terms describe the smooth warm-in-sunlight, cool-in-shadow swing; the last term follows
    the seasons as Earth's shadow grows and shrinks. Fitted by robust least squares (Huber weights,
    a little ridge damping), so a few odd frames don't bend the curve, and with fewer harmonics when
    the data are sparse or leave gaps around the orbit. Frames with no orbit position fall back to
    the sunlit/shadow median."""

    def __init__(self, base, period):
        self.period = period
        ph = [r for r in base if r[2] is not None and r[2] >= 0]
        sh = sorted(r[4] for r in base if r[4] is not None)
        self.sh0 = statistics.median(sh) if sh and (_q(sh, .9) - _q(sh, .1)) >= 3 else None
        cover = len({int(r[2] // BIN_MIN) for r in ph}) / max(1, math.ceil(period / BIN_MIN))
        self.K = 0 if len(ph) < 8 else 1 if (len(ph) < 40 or cover < .5) else 2 if (len(ph) < 120 or cover < .7) else 3
        self.coef = None
        if ph:
            # Fit to medians of small cells (1 minute of orbit × 1 minute of shadow length), weighted
            # by how many samples each holds: the same curve as fitting every sample, a robust start,
            # and a few hundred rows instead of thousands (this has to run on a Pi Zero).
            cells = {}
            for r in ph:
                key = (int(r[2]), round(r[4]) if (r[4] is not None and self.sh0 is not None) else None)
                cells.setdefault(key, []).append(r[1])
            rows = [(k, statistics.median(v), len(v)) for k, v in cells.items()]
            X = [self._feats((0, 0, k[0] + .5, 0, k[1])) for k, _, _ in rows]
            y = [m for _, m, _ in rows]
            cnt = [n_ for _, _, n_ in rows]
            n = len(X[0])
            w = [1.0] * len(y)
            for _ in range(4):   # iteratively reweighted least squares with Huber weights
                A = [[0.0] * n for _ in range(n)]
                b = [0.0] * n
                for xi, yi, wi, ci in zip(X, y, w, cnt):
                    wi *= ci
                    for i in range(n):
                        b[i] += wi * xi[i] * yi
                        for j in range(i, n):
                            A[i][j] += wi * xi[i] * xi[j]
                for i in range(n):
                    for j in range(i):
                        A[i][j] = A[j][i]
                    if i:
                        A[i][i] += 1e-3 * sum(cnt)   # ridge: keeps sparse fits from oscillating
                coef = _solve(A, b)
                if coef is None:
                    break
                self.coef = coef
                res = [yi - sum(c * f for c, f in zip(coef, xi)) for xi, yi in zip(X, y)]
                s = max(_robust_sigma(res), 1e-9)
                w = [1.0 if abs(r) <= 1.5 * s else 1.5 * s / abs(r) for r in res]
        sun = [r[1] for r in base if r[3] == 1]
        shade = [r[1] for r in base if r[3] == 0]
        self.sun = statistics.median(sun) if len(sun) >= 4 else None
        self.shade = statistics.median(shade) if len(shade) >= 4 else None
        self.all = statistics.median([r[1] for r in base])
        self.k = self.coef[-1] if (self.coef and self.sh0 is not None) else 0.0
        # The curve is smooth, so tabulate it every 15 s of orbit and look values up (linear
        # interpolation) instead of evaluating sines and cosines for every sample.
        self.table = None
        if self.coef:
            n = int(period * 4) + 2
            self.table = [self.curve(i / 4) for i in range(n)]

    def _feats(self, r, phase=None, shadow=None):
        th = 2 * math.pi * ((r[2] if phase is None else phase) / self.period)
        f = [1.0]
        for k in range(1, self.K + 1):
            f += [math.cos(k * th), math.sin(k * th)]
        if self.sh0 is not None:
            s = r[4] if (r is not None and shadow is None) else shadow
            f.append((s - self.sh0) if s is not None else 0.0)
        return f

    def curve(self, phase):
        """The fitted value at a phase, for the typical shadow length (used for the chart)."""
        if not self.coef:
            return None
        return sum(c * f for c, f in zip(self.coef, self._feats((0, 0, phase, 0, self.sh0))))

    def expect(self, r):
        if self.table and r[2] is not None and r[2] >= 0:
            x = (r[2] % self.period) * 4
            i = int(x)
            v = self.table[i] + (self.table[i + 1] - self.table[i]) * (x - i)
            if self.sh0 is not None and r[4] is not None:
                v += self.k * (r[4] - self.sh0)
            return v
        base = self.sun if r[3] == 1 and self.sun is not None else self.shade if r[3] == 0 and self.shade is not None else self.all
        if self.sh0 is not None and r[4] is not None:
            base += self.k * (r[4] - self.sh0)
        return base


def _passes(rows, gap_ms=15 * 60_000):
    """Group time-sorted samples into passes: frames heard within 20 minutes of each other.
    Frames heard within 15 minutes of each other share the satellite's state at that moment, so they aren't independent
    evidence; the statistics below count passes, not frames."""
    groups, cur, last = [], [], None
    for r in rows:
        if last is not None and r[0] - last > gap_ms:
            groups.append(cur)
            cur = []
        cur.append(r)
        last = r[0]
    if cur:
        groups.append(cur)
    return groups


def _passes_pairs(pairs, gap_ms=15 * 60_000):
    """_passes for (row, residual) pairs."""
    groups, cur, last = [], [], None
    for p in pairs:
        if last is not None and p[0][0] - last > gap_ms:
            groups.append(cur)
            cur = []
        cur.append(p)
        last = p[0][0]
    if cur:
        groups.append(cur)
    return groups


def _theil_sen(pts):
    """Median of pairwise slopes: a trend line that a few odd points can't drag."""
    sl = [(pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0]) for i in range(len(pts)) for j in range(i + 1, len(pts))
          if pts[j][0] != pts[i][0]]
    return statistics.median(sl) if sl else None


def _change_point(series):
    """When did the level change? The single most likely change point in a series of (time, value):
    the split that makes the cumulative sum of deviations from the overall mean largest
    (the classic CUSUM change-point estimator). Returns when it began and the levels either side."""
    if len(series) < 6:
        return None
    m = sum(v for _, v in series) / len(series)
    c, best, at = 0.0, 0.0, None
    for i, (_, v) in enumerate(series[:-1]):
        c += v - m
        if abs(c) > abs(best):
            best, at = c, i
    if at is None or at < 1:
        return None
    before = statistics.median([v for _, v in series[:at + 1]])
    after = statistics.median([v for _, v in series[at + 1:]])
    return {"began": series[at + 1][0], "before": before, "after": after,
            "direction": "up" if after > before else "down"}


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
        # residual of every sample from the fitted curve, worked out once (it's the costly bit)
        resid = [r[1] - fit.expect(r) for r in rows]
        nb_ = len(base)
        bres = resid[:nb_]
        floor = 0.5 * 10 ** -int(c.get("dp", 2))
        # Normal scatter. The robust estimate ignores rare spikes, but for a channel that sits on one
        # value and occasionally jumps it collapses to nearly zero, so it's floored at half the plain
        # standard deviation: tiny wobbles in such a channel then don't count as shifts.
        sigma = max(_robust_sigma(bres), 0.5 * statistics.pstdev(bres), floor, 1e-9)
        rres = list(zip(rec, resid[nb_:]))
        res["sigma"] = sigma
        res["seasonal_per_min"] = round(fit.k, 4) if fit.sh0 is not None else None
        if rows:
            last = rows[-1]
            e = fit.expect(last)
            res["latest"] = {"t": last[0], "value": last[1], "expected": e, "sigmas": (last[1] - e) / sigma}
        # Judge the last days at the level of passes: each pass contributes its median residual.
        rp = [statistics.median([x for _, x in g]) for g in _passes_pairs(rres)]
        res["n_recent_passes"] = len(rp)
        if len(rp) >= MIN_RECENT_PASSES:
            shift = statistics.median(rp)
            z = shift / sigma
            # 90% range for the shift by resampling whole passes (bootstrap)
            rnd = random.Random(zlib.crc32(c["id"].encode()))
            boots = sorted(statistics.median([rp[rnd.randrange(len(rp))] for _ in rp]) for _ in range(200))
            lo, hi = boots[10], boots[189]
            res.update(shift=shift, z=z, shift_range=[lo, hi])
            # Size (in units of normal scatter) AND evidence (the 90% range keeps clear of the
            # threshold): a shift has to be both real and big enough to matter.
            near = min(abs(lo), abs(hi)) / sigma if lo * hi > 0 else 0.0
            res["status"] = ("drift" if abs(z) >= 2.0 and near >= 1.0 else
                             "watch" if abs(z) >= 1.0 and near >= 0.5 else "normal")
        else:
            res["status"] = "quiet"
        # when did it change? CUSUM over every pass in the window, oldest first
        allp = [(g[-1][0][0], statistics.median([x for _, x in g])) for g in _passes_pairs(list(zip(rows, resid)))]
        cp = _change_point(allp)
        if cp and res["status"] in ("watch", "drift"):
            res["change"] = cp
        # long-term trend of the residuals, per week (robust slope over passes)
        if allp and allp[-1][0] - allp[0][0] >= 7 * DAY and len(allp) >= 10:
            days = {}
            for t, v in allp:   # trend over daily medians: as robust, and a few dozen points not hundreds
                days.setdefault(t // DAY, []).append(v)
            slope = _theil_sen([(d * DAY + DAY // 2, statistics.median(v)) for d, v in sorted(days.items())])
            if slope is not None:
                res["trend_per_week"] = slope * 7 * DAY
        # one-off oddities: far from the model even after allowing for any drift already flagged
        lvl = res.get("shift", 0.0)
        # Some channels spike as part of normal life (discharge current jumps whenever the radio
        # transmits). If the learning period had spikes that big too, they aren't oddities: raise
        # the bar to just above the largest routine excursions seen while learning.
        mb = statistics.median(bres)
        dev = sorted(abs(x - mb) for x in bres)
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
        if fit.coef:
            rs = sorted(r[1] - fit.expect(r) for r in base if r[2] is not None and r[2] >= 0)
            q10, q90 = _q(rs, .1) or 0, _q(rs, .9) or 0
            steps = int(period // 2) + 1
            out["bins"] = [[m * 2.0, fit.curve(m * 2.0), fit.curve(m * 2.0) + q10, fit.curve(m * 2.0) + q90]
                           for m in range(steps)]
            out["harmonics"] = fit.K
    step = max(1, len(base) // 2500)
    out["base"] = [[r[2], r[1]] for r in base[::step] if r[2] is not None and r[2] >= 0]
    out["recent"] = [[r[2], r[1], r[0]] for r in rec if r[2] is not None and r[2] >= 0]
    return out
