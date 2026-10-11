"""Space weather from NOAA's Space Weather Prediction Center (public, no key needed).

Geomagnetic storms (the Kp index) heat and puff up the upper atmosphere, which drags harder on
low satellites, and come with more energetic particles. Solar flares (GOES X-ray flux) and the
10.7 cm solar flux are the Sun's side of the story. Putting them next to the telemetry lets you
ask: did that restart, or that odd reading, line up with a storm?

Fetched every 3 hours, a few hundred kB at most, and stored in the same database.
"""
import datetime as _dt
import json
import urllib.request

from . import __version__
from . import db as dbm

DAY = 86_400_000
PATHS = {"kp": "/products/noaa-planetary-k-index.json",
         "f107": "/json/f107_cm_flux.json",
         "xray": "/json/goes/primary/xrays-6-hour.json"}


def _t(s):
    s = str(s).strip().replace(" ", "T").rstrip("Z").split(".")[0]
    d = _dt.datetime.fromisoformat(s)
    return int(d.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"DownlinkDesk/{__version__} (+github.com/archer7778/downlink-desk)",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read(4_000_000))


def parse_kp(data):
    out = []
    for row in data or []:
        try:
            if isinstance(row, dict):
                v = row.get("Kp", row.get("kp_index", row.get("kp")))
                t = row.get("time_tag")
            else:   # older format: array of arrays with a header row
                if row[0] == "time_tag":
                    continue
                t, v = row[0], row[1]
            out.append((_t(t), float(v)))
        except (ValueError, TypeError, IndexError, KeyError):
            continue
    return out


def parse_f107(data):
    out = []
    for row in data or []:
        try:
            out.append((_t(row["time_tag"]), float(row["flux"])))
        except (ValueError, TypeError, KeyError):
            continue
    return out


def parse_xray(data):
    """Long-wavelength channel (0.1-0.8 nm, the one flare classes use), as 30-minute peaks."""
    peaks = {}
    for row in data or []:
        try:
            if row.get("energy") != "0.1-0.8nm" or row.get("flux") is None:
                continue
            t = _t(row["time_tag"]) // 1_800_000 * 1_800_000
            f = float(row["flux"])
            if f > 0:
                peaks[t] = max(peaks.get(t, 0), f)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return sorted(peaks.items())


PARSE = {"kp": parse_kp, "f107": parse_f107, "xray": parse_xray}


def refresh(conn, cfg, log=print, force=False):
    sw = cfg["spaceweather"]
    if not sw.get("enabled", True):
        return
    now = dbm.now_ms()
    if not force and now - (dbm.meta_get(conn, "swx_refreshed_ms", 0) or 0) < 3 * 3600_000:
        return
    dbm.meta_set(conn, "swx_refreshed_ms", now)
    got = []
    for kind, path in PATHS.items():
        try:
            rows = PARSE[kind](_get(sw["api"].rstrip("/") + path))
        except Exception as e:   # space weather is a bonus; never let it break a fetch
            log(f"Space weather ({kind}) unavailable: {e}")
            continue
        if kind == "xray":   # keep the bigger of an existing and a new peak for the same half hour
            conn.executemany("INSERT INTO spacewx(kind,t_ms,value) VALUES('xray',?,?) ON CONFLICT(kind,t_ms) "
                             "DO UPDATE SET value=MAX(value, excluded.value)", rows)
        else:
            conn.executemany("INSERT OR REPLACE INTO spacewx(kind,t_ms,value) VALUES(?,?,?)", [(kind, t, v) for t, v in rows])
        got.append(f"{kind} {len(rows)}")
    conn.execute("DELETE FROM spacewx WHERE t_ms<?", (now - 90 * DAY,))
    conn.commit()
    if got:
        log("Space weather updated: " + ", ".join(got) + ".")


def g_scale(kp):
    """NOAA geomagnetic storm level: Kp 5 is G1 (minor) up to Kp 9 G5 (extreme)."""
    if kp is None:
        return None
    r = int(round(kp))
    return f"G{r - 4}" if r >= 5 else None


G_NAME = {"G1": "minor", "G2": "moderate", "G3": "strong", "G4": "severe", "G5": "extreme"}


def flare_class(flux):
    if not flux or flux <= 0:
        return None
    for letter, base in (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7), ("A", 1e-8)):
        if flux >= base:
            return f"{letter}{flux / base:.1f}"
    return "A0.0"


def view(conn, since_ms, now=None):
    now = now or dbm.now_ms()
    series = {k: [[r[0], r[1]] for r in conn.execute("SELECT t_ms, value FROM spacewx WHERE kind=? AND t_ms>? ORDER BY t_ms",
                                                      (k, since_ms))] for k in PATHS}

    def last(kind):
        r = conn.execute("SELECT t_ms, value FROM spacewx WHERE kind=? AND t_ms<=? ORDER BY t_ms DESC LIMIT 1",
                         (kind, now)).fetchone()
        return (r[0], r[1]) if r else (None, None)

    def peak(kind, hours):
        r = conn.execute("SELECT t_ms, value FROM spacewx WHERE kind=? AND t_ms>? AND t_ms<=? ORDER BY value DESC LIMIT 1",
                         (kind, now - hours * 3600_000, now)).fetchone()
        return (r[0], r[1]) if r else (None, None)

    kt, kv = last("kp")
    pkt, pkv = peak("kp", 24)
    xt, xv = last("xray")
    pxt, pxv = peak("xray", 24)
    ft, fv = last("f107")
    return {"now": now, "series": series, "refreshed_ms": dbm.meta_get(conn, "swx_refreshed_ms"),
            "kp": {"t": kt, "value": kv, "g": g_scale(kv)},
            "kp_peak_24h": {"t": pkt, "value": pkv, "g": g_scale(pkv)},
            "xray": {"t": xt, "flux": xv, "class": flare_class(xv)},
            "xray_peak_24h": {"t": pxt, "flux": pxv, "class": flare_class(pxv)},
            "f107": {"t": ft, "value": fv}}
