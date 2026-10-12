"""The morning summary: what happened in the last 24 hours, pushed to your phone once a day."""
import datetime as _dt

from . import baseline, notify, spacewx
from . import db as dbm
from .analysis import observation_report, silence

DAY = 86_400_000


def _tz(cfg):
    name = str(cfg["notify"].get("timezone") or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return _dt.datetime.now().astimezone().tzinfo   # the Pi's own time zone


def _local(cfg, t_ms, fmt="%H:%M"):
    return _dt.datetime.fromtimestamp(t_ms / 1000, _tz(cfg)).strftime(fmt)


def _ago(ms):
    h = ms / 3600_000
    return f"{round(ms / 60_000)} min" if h < 1.5 else f"{h:.1f} h" if h < 48 else f"{h / 24:.0f} days"


def _fmt(v, c):
    dp = int(c.get("dp", 2))
    return f"{v:+.{dp}f}{(' ' + c['unit']) if c.get('unit') else ''}"


def satellite_section(conn, cfg, sat, now):
    from .fetch import spec_for
    n = sat["norad"]
    spec = spec_for(cfg, sat)
    lines, flags = [], []
    since = now - DAY
    c = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(decoded_ok),0) ok, COUNT(DISTINCT observer) st FROM frames "
                     "WHERE norad=? AND ts_ms>? AND ts_ms<=?", (n, since, now)).fetchone()
    newest = conn.execute("SELECT MAX(ts_ms) FROM frames WHERE norad=? AND decoded_ok=1", (n,)).fetchone()[0]
    if c["n"]:
        lines.append(f"{c['n']} frames ({c['ok']} decoded)" + (f" from {c['st']} station{'s' if c['st'] != 1 else ''}" if c["st"] else "")
                     + (f"; newest {_ago(now - newest)} ago" if newest else ""))
    else:
        lines.append("No frames in the last 24 h" + (f" (newest is {_ago(now - newest)} old)" if newest else ""))
        flags.append("no data")
    rep = observation_report(conn, cfg, since, now, n)
    done = [o for o in rep if o["status"] in ("heard", "nothing")]
    if done:
        lines.append(f"SatNOGS observations: {sum(o['status'] == 'heard' for o in done)} of {len(done)} delivered frames")
    sil = silence(conn, cfg, n, now)
    if sil["silent"]:
        lines.append(f"⚠ {sil['misses']} observations in a row heard nothing: "
                     + (f"{sil['probability']:.0%} chance it has gone quiet" if sil["basis"] == "probability" else "it may have gone quiet"))
        flags.append("silent")
    elif sil.get("misses", 0) >= 2 and sil.get("probability") is not None:
        lines.append(f"{sil['misses']} recent observations heard nothing, but those stations often miss it "
                     f"({sil['probability']:.0%} chance of real silence)")
    try:
        from . import forensics
        fr = forensics.restarts(conn, spec, n, 30, now)
        if fr["stats"]["total"]:
            lines.append("Restarts: " + forensics.summary(fr))
    except Exception:
        pass
    # restarts
    restarts = []
    for ch in [x for x in spec.get("channels", []) if x.get("counter") == "reboot"]:
        pts = conn.execute("SELECT ts_ms, value FROM points WHERE norad=? AND channel=? AND ts_ms>? AND ts_ms<=? ORDER BY ts_ms",
                           (n, ch["id"], since, now)).fetchall()
        prev = conn.execute("SELECT value FROM points WHERE norad=? AND channel=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",
                            (n, ch["id"], since)).fetchone()
        p = prev[0] if prev else None
        for r in pts:
            if p is not None and r[1] > p:
                restarts.append(f"{ch['name']} {p:.0f}→{r[1]:.0f}")
            p = r[1]
    if restarts:
        lines.append("Restarts: " + "; ".join(restarts[:4]))
        flags.append("restart")
    # learned model
    try:
        m = baseline.compute(conn, spec, n, now)
        dr = [x for x in m["channels"] if x.get("status") == "drift"]
        wt = [x for x in m["channels"] if x.get("status") == "watch"]
        un = sum(x.get("n_unusual", 0) for x in m["channels"])
        judged = [x for x in m["channels"] if x.get("status") not in ("learning", None)]
        if dr:
            lines.append("Drifting vs its normal for that point in orbit: " +
                         "; ".join(f"{x['name']} {_fmt(x['shift'], x)}" for x in dr[:4]))
            flags.append("drift")
        if wt:
            lines.append("Worth watching: " + ", ".join(x["name"] for x in wt[:4]))
        if un:
            lines.append(f"{un} unusual reading{'s' if un != 1 else ''} (over {baseline.UNUSUAL_SIGMA:.0f}σ from the model)")
        if judged and not dr and not wt and not un:
            lines.append(f"Health model: all {len(judged)} modelled channels behaving normally")
        elif not judged and m["channels"]:
            lines.append("Health model: still learning what normal looks like")
    except Exception as e:   # never lose the summary over the model
        lines.append(f"(health model unavailable: {e})")
    # battery
    try:
        from . import battery
        from .catalog import featured
        launched = next((f.get("launched") for f in featured() if f["norad"] == n), None)
        bl = battery.summary_line(battery.analyse(conn, spec, n, launched, now))
        if bl:
            lines.append(bl)
            if bl.startswith("Battery concern"):
                flags.append("battery")
    except Exception as e:
        lines.append(f"(battery check unavailable: {e})")
    # passes over the station
    st = cfg["station"]
    tle = dbm.smeta_get(conn, n, "tle")
    if tle and (st["lat"] or st["lon"]):
        from .orbit import passes
        ps = passes(tle["tle1"], tle["tle2"], now, 24, st["lat"], st["lon"], st.get("alt_m", 0), st.get("min_elevation", 10))
        if ps:
            best = max(ps, key=lambda p: p["max_el"])
            lines.append(f"Passes over you in the next 24 h: {len(ps)}; next {_local(cfg, ps[0]['aos'])} "
                         f"(max {ps[0]['max_el']:.0f}°), best {_local(cfg, best['aos'])} (max {best['max_el']:.0f}°)")
    return lines, flags


def build(conn, cfg, now=None):
    now = now or dbm.now_ms()
    sats = dbm.satellites(conn)
    out, flags = [], []
    for s in sats:
        lines, f = satellite_section(conn, cfg, s, now)
        flags += f
        out.append(s["name"] if len(sats) > 1 else None)
        out += [("  " if len(sats) > 1 else "") + "• " + x for x in lines]
    sw = spacewx.view(conn, now - DAY, now)
    bits = []
    if sw["kp_peak_24h"]["value"] is not None:
        k = sw["kp_peak_24h"]
        g = k["g"]
        bits.append(f"Kp peaked {k['value']:.1f}" + (f" ({g}, {spacewx.G_NAME[g]} geomagnetic storm)" if g else " (quiet)"))
        if g:
            flags.append("storm")
    if sw["xray_peak_24h"]["class"]:
        bits.append(f"strongest solar flare {sw['xray_peak_24h']['class']}")
    if sw["f107"]["value"] is not None:
        bits.append(f"solar flux {sw['f107']['value']:.0f}")
    if bits:
        out.append("Space weather: " + ", ".join(bits))
    st = dbm.storage(conn, cfg.db_path)
    if "disk_total" in st:
        out.append(f"Pi: SD card {1 - st['disk_free'] / st['disk_total']:.0%} used ({st['disk_free'] / 1e9:.1f} GB free of "
                   f"{st['disk_total'] / 1e9:.0f} GB), database {st['db_bytes'] / 1e6:.1f} MB"
                   + (" ⚠" if st.get("level") != "ok" else ""))
        if st.get("level") != "ok":
            flags.append("disk")
    try:
        upd = __import__("json").loads((cfg.db_path.parent / "update.json").read_text())
        if upd.get("result") in ("updated", "failed", "held", "available"):
            out.append("Software: " + upd["message"])
    except (OSError, ValueError):
        pass
    text = "\n".join(x for x in out if x)
    local_date = _dt.datetime.fromtimestamp(now / 1000, _tz(cfg)).strftime("%a %d %b")
    names = " + ".join(s["name"] for s in sats) or "Downlink Desk"
    title = f"{names}: morning summary, {local_date}"
    return {"title": title, "text": text, "flags": sorted(set(flags)), "now": now,
            "next_ms": next_time(cfg, now), "hour": cfg["notify"].get("summary_hour", 7)}


def next_time(cfg, now):
    h = int(cfg["notify"].get("summary_hour", 7))
    if h < 0:
        return None
    tz = _tz(cfg)
    d = _dt.datetime.fromtimestamp(now / 1000, tz)
    t = d.replace(hour=h, minute=0, second=0, microsecond=0)
    if t <= d:
        t += _dt.timedelta(days=1)
    return int(t.timestamp() * 1000)


def maybe_send(conn, cfg, log=print, force=False):
    """Called after every fetch: sends once a day, at the first fetch after summary_hour (local time)."""
    if not str(cfg["notify"].get("ntfy_topic") or "").strip():
        return False
    h = int(cfg["notify"].get("summary_hour", 7))
    if h < 0 and not force:
        return False
    d = _dt.datetime.fromtimestamp(dbm.now_ms() / 1000, _tz(cfg))
    key = d.strftime("%Y-%m-%d")
    if not force and (d.hour < h or dbm.meta_get(conn, "summary_sent") == key):
        return False
    s = build(conn, cfg)
    prio = "high" if {"silent", "drift"} & set(s["flags"]) else "default"
    if notify.send(cfg, s["title"], s["text"], prio, "sunrise"):
        dbm.meta_set(conn, "summary_sent", key)
        log("Sent the morning summary.")
        return True
    return False
