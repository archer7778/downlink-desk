"""Shared reasoning about the data: scheduled observations versus frames received,
the silent-satellite check, and the limits currently in force."""
import json

HOUR = 3600_000
WINDOW_PAD = 120_000   # frames timestamped up to 2 min either side of an observation count towards it


def observation_report(conn, cfg, since_ms, now_ms, norad):
    """Every observation of one satellite ending after since_ms, with what actually arrived during it."""
    grace = cfg["alerts"]["upload_grace_hours"] * HOUR
    out = []
    for o in conn.execute("SELECT * FROM observations WHERE end_ms>? AND norad=? ORDER BY start_ms",
                          (since_ms, norad)).fetchall():
        # Only this station's frames count as this observation being heard. SatNOGS names uploads
        # "Station-GRID" and the schedule says "Station"; frames from other stations at the same
        # time are kept as `others` (they show the satellite was alive, not that this station heard it).
        rows = conn.execute("SELECT observer, decoded_ok FROM frames WHERE norad=? AND ts_ms BETWEEN ? AND ?",
                            (norad, o["start_ms"] - WINDOW_PAD, o["end_ms"] + WINDOW_PAD)).fetchall()
        own = [r for r in rows if same_station(r[0], o["station_name"])]
        n, ok = len(own), sum(r[1] for r in own)
        if now_ms < o["start_ms"]:
            status = "scheduled"
        elif now_ms <= o["end_ms"]:
            status = "in_progress"
        elif n:
            status = "heard"
        elif now_ms - o["end_ms"] < grace:
            status = "awaiting"
        else:
            status = "nothing"
        out.append({"id": o["id"], "start": o["start_ms"], "end": o["end_ms"], "station": o["station_name"],
                    "station_id": o["station_id"], "lat": o["lat"], "lng": o["lng"], "max_alt": o["max_alt"],
                    "mode": o["mode"], "frames": n, "decoded": ok, "others": len(rows) - n, "status": status})
    return out


def same_station(observer, station):
    o, s = (observer or "").strip().lower(), (station or "").strip().lower()
    return bool(s) and (o == s or o.startswith(s + "-"))


def silent_streak(report, cfg):
    """Most recent run of finished, reasonably high observations that produced no frames at all.
    One empty observation is usually a station problem; several in a row across stations
    suggests the satellite itself went quiet."""
    min_el = cfg["alerts"]["min_obs_elevation"]
    done = [o for o in report if o["status"] in ("heard", "nothing") and (o["max_alt"] or 0) >= min_el]
    streak = []
    for o in sorted(done, key=lambda x: x["end"], reverse=True):
        if o["status"] == "heard":
            break
        streak.append(o)
    return list(reversed(streak))


def _good_limits(v):
    import math
    return (isinstance(v, list) and len(v) == 4 and all(
        x is None or (isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)) for x in v))


def limits_in_force(conn, spec, norad, default_norad):
    """Default limits from the channels file, overridden by signed limit changes in the log.
    Limit entries from before multi-satellite support carry no satellite and belong to the first one."""
    lim = {c["id"]: c.get("lim") for c in spec["channels"]}
    for r in conn.execute("SELECT data_json FROM log WHERE kind='limits' ORDER BY seq"):
        try:
            d = json.loads(r["data_json"])
        except ValueError:
            continue
        try:
            if not isinstance(d, dict) or int(d.get("sat") or default_norad) != norad:
                continue
        except (TypeError, ValueError):
            continue
        if not _good_limits(d.get("lim")):   # same rule as the console: four numbers or blanks
            continue
        if not spec["channels"]:
            lim.setdefault(d.get("ch"), None)
        if d.get("ch") in lim:
            lim[d["ch"]] = None if all(v is None for v in d["lim"]) else d["lim"]
    return lim


def silence_probability(report, cfg, prior=0.02, strength=4.0, alive_ms=0):
    """How likely is it that the satellite has really gone quiet, rather than the stations missing it?

    Each station has a track record in the last month: of its finished, reasonably high
    observations, how many heard the satellite. A miss by a station that almost always hears it is
    strong evidence of silence; a miss by an unreliable one is weak evidence. Bayes' rule:

        odds(silent | misses) = odds(silent) × Π 1 / (1 − p_station)

    where p_station is that station's hit rate, pulled towards the network-wide rate when it has
    few observations (a Beta prior worth `strength` observations). The prior says a satellite that
    was being heard is unlikely (2%) to die at any given moment."""
    min_el = cfg["alerts"]["min_obs_elevation"]
    done = sorted([o for o in report if o["status"] in ("heard", "nothing") and (o["max_alt"] or 0) >= min_el],
                  key=lambda o: o["end"])
    if not done:
        return {"probability": None, "misses": 0}
    hits = sum(1 for o in done if o["status"] == "heard")
    pg = (hits + 1) / (len(done) + 2)
    st = {}
    for o in done:
        h, n = st.get(o["station"], (0, 0))
        st[o["station"]] = (h + (o["status"] == "heard"), n + 1)
    rate = {k: (h + strength * pg) / (n + strength) for k, (h, n) in st.items()}
    streak = []
    for o in reversed(done):
        # stop at a hit, or at the last moment anyone heard it: misses before that aren't evidence
        if o["status"] == "heard" or o.get("others") or o["end"] < alive_ms:
            break
        streak.append(o)
    lr = 1.0
    for o in streak:
        lr *= 1 / max(0.03, 1 - min(0.97, rate[o["station"]]))
    odds = prior / (1 - prior) * lr
    return {"probability": round(odds / (1 + odds), 3), "misses": len(streak),
            "station_rates": {k: {"heard": h, "tries": n, "rate": round(rate[k], 3)} for k, (h, n) in st.items()},
            "expected_hits": round(sum(rate[o["station"]] for o in streak), 2),
            "network_hit_rate": round(pg, 3), "stations": len(st),
            "last_heard": next((o["end"] for o in reversed(done) if o["status"] == "heard"), None)}


def silence(conn, cfg, norad, now):
    """Silent-satellite verdict: the Bayesian probability over the last month of observations, with
    the plain 'N misses in a row' rule as a fallback when there's no track record yet."""
    rep = observation_report(conn, cfg, now - 30 * 86_400_000, now, norad)
    # Only misses after the last frame anyone received count: a frame from any station proves it
    # was alive then, even if the scheduled station heard nothing.
    last = conn.execute("SELECT MAX(ts_ms) FROM frames WHERE norad=?", (norad,)).fetchone()[0] or 0
    streak = [o for o in silent_streak(rep, cfg) if o["start"] > last]
    p = silence_probability(rep, cfg, alive_ms=last)
    thr = cfg["alerts"].get("silent_probability", 0.8)
    if p["probability"] is not None and p.get("stations", 0) and len(rep) >= 5:
        p["silent"] = p["probability"] >= thr and len(streak) >= 2
        p["basis"] = "probability"
    else:
        p["silent"] = len(streak) >= cfg["alerts"]["silent_after"]
        p["basis"] = "count"
    p["streak"] = streak
    return p
