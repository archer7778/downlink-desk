"""Optional phone alerts through ntfy (https://ntfy.sh): install the ntfy app, subscribe to a
topic name only you know, and put that name in config.toml under [notify]."""
import urllib.request

from . import __version__
from . import db as dbm
from .analysis import limits_in_force, observation_report, silent_streak


def _ascii(s):
    return s.encode("latin-1", "replace").decode("latin-1")


def send(cfg, title, message, priority="default", tags=""):
    n = cfg["notify"]
    topic = str(n.get("ntfy_topic") or "").strip()
    if not topic:
        return False
    headers = {"Title": _ascii(title), "Priority": priority, "Tags": tags,
               "User-Agent": f"DownlinkDesk/{__version__}"}
    if n.get("console_url"):
        headers["Click"] = n["console_url"]
    req = urllib.request.Request(n["ntfy_server"].rstrip("/") + "/" + topic, data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def _once(conn, key):
    """True the first time a key is seen; remembers the last 500 keys."""
    seen = dbm.meta_get(conn, "notified", []) or []
    if key in seen:
        return False
    dbm.meta_set(conn, "notified", (seen + [key])[-500:])
    return True


def after_fetch(conn, cfg, spec, new_ids, log=print):
    """Look at what this fetch brought in and push anything an operator should hear about."""
    if not str(cfg["notify"].get("ntfy_topic") or "").strip():
        return
    sat = cfg["satellite"]["name"]
    chans = {c["id"]: c for c in spec["channels"]}
    lims = limits_in_force(conn, spec)
    sent = 0
    if new_ids:
        q = ",".join("?" * len(new_ids))
        rows = conn.execute(f"SELECT p.ts_ms, p.channel, p.value FROM points p WHERE p.frame_id IN ({q}) ORDER BY p.ts_ms",
                            new_ids).fetchall()
        first_ts = rows[0]["ts_ms"] if rows else None
        prev = {}
        for r in rows:
            ch, v, t = r["channel"], r["value"], r["ts_ms"]
            c = chans.get(ch)
            if not c:
                continue
            lim = lims.get(ch)
            if lim:
                rl, _, _, rh = lim
                if (rl is not None and v < rl) or (rh is not None and v > rh):
                    if _once(conn, f"lim:{ch}:{t // 3600_000}"):
                        send(cfg, f"{sat}: ALARM {c['name']}", f"{c['name']} = {v:.{c['dp']}f} {c['unit']} "
                             f"(alarm limits {rl} / {rh}).", "high", "rotating_light")
                        sent += 1
            if c.get("counter") == "reboot":
                if ch not in prev:
                    p = conn.execute("SELECT value FROM points WHERE channel=? AND ts_ms<? ORDER BY ts_ms DESC LIMIT 1",
                                     (ch, first_ts)).fetchone()
                    prev[ch] = p["value"] if p else None
                if prev[ch] is not None and v > prev[ch] and _once(conn, f"reboot:{ch}:{t}"):
                    send(cfg, f"{sat}: restart detected", f"{c['name']} went from {prev[ch]:.0f} to {v:.0f}.",
                         "default", "arrows_counterclockwise")
                    sent += 1
                prev[ch] = v
    report = observation_report(conn, cfg, dbm.now_ms() - 3 * 86_400_000, dbm.now_ms())
    streak = silent_streak(report, cfg)
    if len(streak) >= cfg["alerts"]["silent_after"] and _once(conn, f"silent:{streak[0]['id']}"):
        names = ", ".join(sorted({o["station"] for o in streak}))
        send(cfg, f"{sat}: may have gone silent", f"{len(streak)} scheduled SatNOGS observations in a row "
             f"produced no frames (stations: {names}).", "high", "satellite")
        sent += 1
    if sent:
        log(f"Sent {sent} phone alert(s).")


def fetch_failed(conn, cfg):
    rows = conn.execute("SELECT ok, message FROM fetches ORDER BY id DESC LIMIT 3").fetchall()
    if len(rows) == 3 and not any(r["ok"] for r in rows) and not dbm.meta_get(conn, "fail_notified", False):
        if send(cfg, f"{cfg['satellite']['name']}: can't reach SatNOGS",
                f"The last 3 fetches failed. Latest: {rows[0]['message']}", "high", "warning"):
            dbm.meta_set(conn, "fail_notified", True)


def fetch_ok(conn):
    if dbm.meta_get(conn, "fail_notified", False):
        dbm.meta_set(conn, "fail_notified", False)
