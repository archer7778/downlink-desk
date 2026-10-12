"""Restart forensics: when exactly did each subsystem restart, where was the satellite, and was
anything unusual going on?

A boot counter only says "it restarted at some point between these two frames". An uptime counter
says more: frame time minus uptime is the moment it restarted. With that moment and the orbit we
know where the satellite was (over the South Atlantic Anomaly? in sunlight?) and the geomagnetic
activity at the time. Compared with how much of its time the satellite spends in those conditions,
that shows whether restarts cluster where radiation is highest: a classic single-event-upset sign.
"""
import math

from . import db as dbm
from .orbit import in_saa

DAY = 86_400_000
UNIT_S = {"h": 3600, "min": 60, "s": 1, "": 1}


def _uptime_channels(conn, spec, norad):
    chans = spec.get("channels") or []
    if chans and not spec.get("auto"):
        return [c for c in chans if c.get("counter") == "uptime"]
    ids = [r[0] for r in conn.execute("SELECT DISTINCT channel FROM points WHERE norad=?", (norad,))]
    return [{"id": i, "name": i.replace("_", " ").capitalize(), "unit": "s"} for i in ids if "uptime" in i]


def _boot_channels(conn, spec, norad):
    chans = spec.get("channels") or []
    if chans and not spec.get("auto"):
        return [c for c in chans if c.get("counter") == "reboot"]
    ids = [r[0] for r in conn.execute("SELECT DISTINCT channel FROM points WHERE norad=?", (norad,))]
    return [{"id": i, "name": i.replace("_", " ").capitalize()} for i in ids
            if ("boot" in i or "reset_c" in i) and "since" not in i]


def _subsystem(name):
    for word in ("UHF", "VHF", "S-band", "Power", "OBC", "ADCS", "EPS", "PSU"):
        if word.lower() in name.lower():
            return {"PSU": "Power unit", "EPS": "Power unit"}.get(word, word if word != "Power" else "Power unit")
    return name


def restarts(conn, spec, norad, days=30, now=None):
    now = now or dbm.now_ms()
    since = now - days * DAY
    found = []
    # exact restart times from uptime counters
    for c in _uptime_channels(conn, spec, norad):
        k = UNIT_S.get(c.get("unit", ""), 1)
        rows = conn.execute("SELECT ts_ms, value FROM points WHERE norad=? AND channel=? AND ts_ms>? ORDER BY ts_ms",
                            (norad, c["id"], since - 7 * DAY)).fetchall()
        for (t0, v0), (t1, v1) in zip(rows, rows[1:]):
            up1 = v1 * k
            # uptime went down (beyond rounding): it restarted between the two frames
            if v1 * k < v0 * k + (t1 - t0) / 1000 - 300 and v1 * k < v0 * k:
                at = int(t1 - up1 * 1000)
                if at >= t0 - 60_000 and at >= since:
                    found.append({"t": at, "exact": True, "subsystem": _subsystem(c.get("name", c["id"])),
                                  "channel": c["id"], "seen": t1})
    # boot counters: catch restarts the uptime counters missed (time known only to a window)
    for c in _boot_channels(conn, spec, norad):
        rows = conn.execute("SELECT ts_ms, value FROM points WHERE norad=? AND channel=? AND ts_ms>? ORDER BY ts_ms",
                            (norad, c["id"], since)).fetchall()
        for (t0, v0), (t1, v1) in zip(rows, rows[1:]):
            if v1 > v0:
                sub = _subsystem(c.get("name", c["id"]))
                if not any(f["subsystem"] == sub and t0 - 600_000 <= f["t"] <= t1 + 600_000 for f in found):
                    found.append({"t": t1, "exact": False, "window": [t0, t1], "subsystem": sub,
                                  "channel": c["id"], "seen": t1, "count": int(v1 - v0)})
    found.sort(key=lambda f: f["t"])
    # where was it, and what was space weather doing?
    tle = dbm.smeta_get(conn, norad, "tle")
    sat = None
    if tle:
        from sgp4.api import Satrec
        sat = Satrec.twoline2rv(tle["tle1"], tle["tle2"])
    from .orbit import state
    kp = conn.execute("SELECT t_ms, value FROM spacewx WHERE kind='kp' AND t_ms>? ORDER BY t_ms", (since - DAY,)).fetchall()

    def kp_at(t):
        best = None
        for tk, v in kp:
            if tk <= t:
                best = v
            else:
                break
        return best

    for f in found:
        if f["exact"] and sat is not None:
            st = state(sat, f["t"])
            if st:
                f.update(lat=st["lat"], lon=st["lon"], sunlit=st["sunlit"], saa=in_saa(st["lat"], st["lon"]))
        f["kp"] = kp_at(f["t"])
    # how often would we expect those conditions by chance?
    stats = {"days": days, "total": len(found), "exact": sum(1 for f in found if f["exact"])}
    located = [f for f in found if "saa" in f]
    if sat is not None and located:
        n_s = n_saa = 0
        for t in range(now - DAY, now, 60_000):   # a day of orbit, once a minute
            st = state(sat, t)
            if st:
                n_s += 1
                n_saa += in_saa(st["lat"], st["lon"])
        p = n_saa / n_s if n_s else 0
        k = sum(1 for f in located if f["saa"])
        n = len(located)
        stats.update(saa_time_fraction=round(p, 3), in_saa=k, located=n, expected_in_saa=round(n * p, 2),
                     saa_p_value=_binom_tail(n, k, p) if p else None)
    with_kp = [f for f in found if f["kp"] is not None]
    if with_kp and kp:
        base = sum(1 for _, v in kp if v >= 5) / len(kp)
        stats.update(during_storm=sum(1 for f in with_kp if f["kp"] >= 5), storm_time_fraction=round(base, 3))
    return {"restarts": found[-50:], "stats": stats}


def _binom_tail(n, k, p):
    """Chance of at least k of n restarts landing in the SAA if restarts happened anywhere at random."""
    return round(sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)), 4)


def summary(r):
    s = r["stats"]
    if not s["total"]:
        return f"No restarts in the last {s['days']} days."
    out = f"{s['total']} restart{'s' if s['total'] != 1 else ''} in the last {s['days']} days"
    if s.get("located"):
        out += (f"; {s['in_saa']} of {s['located']} with a known time were over the South Atlantic Anomaly, "
                f"where it spends {s['saa_time_fraction']:.0%} of its time")
        if s.get("saa_p_value") is not None and s["in_saa"] >= 2 and s["saa_p_value"] < 0.05:
            out += f" (unlikely by chance, p = {s['saa_p_value']:.3f}: a radiation signature)"
    return out + "."
