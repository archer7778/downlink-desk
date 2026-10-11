"""Shared reasoning about the data: scheduled observations versus frames received,
the silent-satellite check, and the limits currently in force."""
import json

HOUR = 3600_000
WINDOW_PAD = 120_000   # frames timestamped up to 2 min either side of an observation count towards it


def observation_report(conn, cfg, since_ms, now_ms):
    """Every observation ending after since_ms, with what actually arrived during it."""
    grace = cfg["alerts"]["upload_grace_hours"] * HOUR
    out = []
    for o in conn.execute("SELECT * FROM observations WHERE end_ms>? ORDER BY start_ms", (since_ms,)).fetchall():
        n, ok = conn.execute("SELECT COUNT(*), COALESCE(SUM(decoded_ok),0) FROM frames WHERE ts_ms BETWEEN ? AND ?",
                             (o["start_ms"] - WINDOW_PAD, o["end_ms"] + WINDOW_PAD)).fetchone()
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
                    "mode": o["mode"], "frames": n, "decoded": ok, "status": status})
    return out


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


def limits_in_force(conn, spec):
    """Default limits from the channels file, overridden by signed limit changes in the log."""
    lim = {c["id"]: c.get("lim") for c in spec["channels"]}
    for r in conn.execute("SELECT data_json FROM log WHERE kind='limits' ORDER BY seq"):
        try:
            d = json.loads(r["data_json"])
        except ValueError:
            continue
        if d.get("ch") in lim and isinstance(d.get("lim"), list) and len(d["lim"]) == 4:
            lim[d["ch"]] = None if all(v is None for v in d["lim"]) else d["lim"]
    return lim
