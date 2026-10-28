"""Bridge to Anduril Lattice: publish satellites, this node and SatNOGS stations as Lattice entities,
and accept one task, ExpeditePass. Interface: docs/ICD-LATTICE.md.

A separate process from the console and the fetch job. It reads the console's database without
writing to it and keeps its own queue and task state in lattice-outbox.db, so if it stops or
loses its link, nothing else on the Pi notices.

    python -m downlink_desk.lattice --dry-run     # print the entities it would publish, send nothing
    python -m downlink_desk.lattice --once        # one publish cycle
    python -m downlink_desk.lattice               # run (systemd: downlink-desk-lattice.service)
    python -m downlink_desk.lattice --status      # link state and queue depth

Standard library only, like the rest of Downlink Desk, so nothing extra to install on a Pi Zero.
"""
import argparse
import json
import math
import queue
import random
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from . import __version__
from . import db as dbm
from .analysis import limits_in_force, observation_report, silence
from .config import find_config
from .fetch import spec_for
from .orbit import WGS84_A, WGS84_F, _jd, gmst, in_saa, state, tle_epoch_ms

INTEGRATION = "downlink-desk"
TASK_TYPE = "type.googleapis.com/downlinkdesk.v1.ExpeditePass"
PROGRESS_TYPE = "type.googleapis.com/downlinkdesk.v1.ExpeditePassProgress"
RESULT_TYPE = "type.googleapis.com/downlinkdesk.v1.ExpeditePassResult"
NAMESPACE = uuid.UUID("6f1c2a4e-1d5b-5c39-9a51-6b2e0a7d3c11")   # fixed: entity IDs depend on it

MIN = 60_000
HOUR = 3600_000
DAY = 86_400_000
MAX_EXPIRY = 29 * DAY            # Lattice: expiry must be less than 30 days ahead
MAX_TRANSITIONS = 200            # per entity, kept through an outage
MAX_ROWS = 5000                  # whole queue
STATION_AHEAD = 12 * HOUR        # stations with an observation starting this soon are published
STATION_AFTER = 30 * MIN         # ... or that ended this recently
ACTIVE = ("received", "wilco", "executing")
TERMINAL = ("done_ok", "done_not_ok")

EARTH_RATE = 7.2921158553e-5     # rad/s


# ---------- time ----------

def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ms) % 1000:03d}Z"


def parse_iso(s):
    """Milliseconds from an RFC 3339 time (any number of fraction digits), or None."""
    if not isinstance(s, str) or not s:
        return None
    s = s.strip().replace("z", "Z")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, rest = s.split(".", 1)
        i = 0
        while i < len(rest) and rest[i].isdigit():
            i += 1
        s = head + "." + (rest[:i] + "000000")[:6] + rest[i:]
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


# ---------- orbit ----------

def _tle_exp(s):
    """TLE 'assumed decimal point' notation: ' 26257-3' is 0.26257e-3."""
    s = s.strip()
    if not s:
        return 0.0
    sign = -1.0 if s[0] == "-" else 1.0
    if s[0] in "+-":
        s = s[1:]
    return sign * float(f"0.{s[:-2]}e{int(s[-2:])}")


def orbit_component(tle):
    """Lattice's orbit component from a TLE: CCSDS OMM mean elements for SGP4, in the TEME frame.
    The derivative terms are the TLE's own fields (n-dot/2 and n-ddot/6), as OMM carries them."""
    l1, l2 = tle["tle1"], tle["tle2"]
    meta = {"originator": "SatNOGS DB" + (f" (source: {tle['source']})" if tle.get("source") else ""),
            "messageId": f"{int(l1[2:7])}-{l1[64:68].strip() or '0'}",
            "refFrame": "ECI_REFERENCE_FRAME_TEME", "meanElementTheory": "MEAN_ELEMENT_THEORY_SGP4"}
    created = parse_iso(tle.get("updated"))
    if created:
        meta["creationDate"] = iso(created)
    return {"orbitMeanElements": {
        "metadata": meta,
        "meanKeplerianElements": {
            "epoch": iso(tle_epoch_ms(l1)),
            "meanMotion": float(l2[52:63]),
            "eccentricity": float("0." + l2[26:33].strip()),
            "inclinationDeg": float(l2[8:16]),
            "raOfAscNodeDeg": float(l2[17:25]),
            "argOfPericenterDeg": float(l2[34:42]),
            "meanAnomalyDeg": float(l2[43:51])},
        "tleParameters": {
            "ephemerisType": int(l1[62]) if l1[62].isdigit() else 0,
            "classificationType": l1[7],
            "noradCatId": int(l1[2:7]),
            "elementSetNo": int(l1[64:68]) if l1[64:68].strip().isdigit() else 0,
            "revAtEpoch": int(l2[63:68]) if l2[63:68].strip().isdigit() else 0,
            "bstar": _tle_exp(l1[53:61]),
            "meanMotionDot": float(l1[33:43]),
            "meanMotionDdot": _tle_exp(l1[44:52])}}}


def geodetic(x, y, z):
    """WGS84 geodetic latitude and longitude (degrees) and height above the ellipsoid (km)
    from Earth-fixed coordinates in km."""
    e2 = WGS84_F * (2 - WGS84_F)
    p = math.hypot(x, y)
    lon = math.atan2(y, x)
    lat = math.atan2(z, p * (1 - e2))
    h = 0.0
    for _ in range(8):
        n = WGS84_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n if abs(lat) < math.radians(80) else z / math.sin(lat) - n * (1 - e2)
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def position(tle, t_ms):
    """Where the satellite is at t_ms, from SGP4: geodetic position, Earth-relative velocity in a
    local east-north-up frame (m/s), and speed. None if the orbit model fails."""
    from sgp4.api import Satrec
    sat = Satrec.twoline2rv(tle["tle1"], tle["tle2"])
    jd, fr = _jd(t_ms)
    err, r, v = sat.sgp4(jd, fr)
    if err or not all(math.isfinite(a) for a in (*r, *v)):
        return None
    th = gmst(jd + fr)
    c, s = math.cos(th), math.sin(th)
    x, y, z = r[0] * c + r[1] * s, -r[0] * s + r[1] * c, r[2]
    vx, vy, vz = v[0] * c + v[1] * s, -v[0] * s + v[1] * c, v[2]
    vx, vy = vx + EARTH_RATE * y, vy - EARTH_RATE * x     # take out the Earth's rotation
    lat, lon, h = geodetic(x, y, z)
    la, lo = math.radians(lat), math.radians(lon)
    e = -math.sin(lo) * vx + math.cos(lo) * vy
    n = -math.sin(la) * math.cos(lo) * vx - math.sin(la) * math.sin(lo) * vy + math.cos(la) * vz
    u = math.cos(la) * math.cos(lo) * vx + math.cos(la) * math.sin(lo) * vy + math.sin(la) * vz
    return {"lat": lat, "lon": (lon + 540) % 360 - 180, "hae_m": h * 1000,
            "enu": (e * 1000, n * 1000, u * 1000), "speed": math.sqrt(vx * vx + vy * vy + vz * vz) * 1000}


# ---------- HTTP ----------

class Unreachable(Exception):
    """No answer at all: no network, DNS, refused, reset, timeout."""


class HttpError(Exception):
    def __init__(self, status, body, retry_after=None):
        super().__init__(f"HTTP {status}: {str(body)[:200]}")
        self.status, self.body, self.retry_after = status, body, retry_after


class Client:
    """Minimal Lattice REST client: bearer token (environment token, or one obtained by OAuth client
    credentials), plus the sandbox header when Lattice Sandboxes is in use."""

    def __init__(self, base_url, token="", client_id="", client_secret="", sandbox_token="", timeout=20):
        base_url = (base_url or "").strip().rstrip("/")
        if base_url and "://" not in base_url:
            base_url = "https://" + base_url
        u = urllib.parse.urlparse(base_url)
        if base_url and u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit("config.toml [lattice] url must use https:// (it carries your token).")
        self.base = base_url
        self.token, self.client_id, self.client_secret = token, client_id, client_secret
        self.sandbox_token, self.timeout = sandbox_token, timeout
        self._oauth = (None, 0)
        self._lock = threading.Lock()

    def _headers(self):
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "User-Agent": f"DownlinkDesk/{__version__}"}
        tok = self._bearer()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        if self.sandbox_token:
            h["Anduril-Sandbox-Authorization"] = f"Bearer {self.sandbox_token}"
        return h

    def _bearer(self):
        if not (self.client_id and self.client_secret):
            return self.token
        with self._lock:
            tok, until = self._oauth
            if tok and time.time() < until - 60:
                return tok
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                           "client_secret": self.client_secret}).encode()
            h = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
            if self.sandbox_token:
                h["Anduril-Sandbox-Authorization"] = f"Bearer {self.sandbox_token}"
            st, data = self._send("POST", "api/v1/oauth/token", body, h, self.timeout)
            if not isinstance(data, dict) or not data.get("access_token"):
                raise HttpError(st, "token response without access_token")
            self._oauth = (data["access_token"], time.time() + float(data.get("expires_in") or 3600))
            return data["access_token"]

    def forget_token(self):
        self._oauth = (None, 0)

    def request(self, method, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        return self._send(method, path, data, self._headers(), timeout or self.timeout)

    def _send(self, method, path, data, headers, timeout):
        req = urllib.request.Request(f"{self.base}/{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            raw = e.read() if hasattr(e, "read") else b""
            try:
                body = json.loads(raw or b"null")
            except ValueError:
                body = raw[:300].decode("utf-8", "replace")
            ra = e.headers.get("Retry-After") if e.headers else None
            try:
                ra = float(ra) if ra is not None else None
            except ValueError:
                ra = None
            raise HttpError(e.code, body, ra) from None
        except (urllib.error.URLError, OSError, ValueError) as e:   # ValueError: http.client's bad status line
            raise Unreachable(f"{type(e).__name__}: {e}") from None
        if not raw.strip():
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            raise Unreachable("answer was not JSON") from None


# ---------- local queue and task state ----------

class Outbox:
    """Store-and-forward queue and task state, in its own SQLite file next to the console's database."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
        source_ms INTEGER NOT NULL, body TEXT NOT NULL, transition INTEGER NOT NULL DEFAULT 0,
        queued_ms INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS outbox_key ON outbox (key, id);
    CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, norad INTEGER, obs_id INTEGER, station TEXT,
        pass_start_ms INTEGER, pass_end_ms INTEGER, deadline_ms INTEGER, state TEXT NOT NULL,
        status_version INTEGER NOT NULL DEFAULT 0, fetches INTEGER NOT NULL DEFAULT 0, last_fetch_ms INTEGER,
        received_ms INTEGER NOT NULL, start_ms INTEGER, end_ms INTEGER, spec TEXT, result TEXT, message TEXT);
    CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()
        self.lock = threading.RLock()

    def get(self, key, default=None):
        with self.lock:
            r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if r is None else json.loads(r["value"])

    def set(self, key, value):
        with self.lock:
            self.conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, json.dumps(value)))
            self.conn.commit()

    def bump(self, key, n=1):
        self.set(key, (self.get(key, 0) or 0) + n)

    def node_id(self):
        nid = self.get("node_id")
        if not nid:
            nid = str(uuid.uuid4())
            self.set("node_id", nid)
        return nid

    def put_entity(self, entity_id, source_ms, body, sig):
        """Queue a snapshot. It's a transition if its health, alerts or activity changed; unsent
        snapshots of the same entity that weren't transitions are superseded by this one."""
        with self.lock:
            last = self.get(f"sig:{entity_id}")
            transition = 1 if sig != last else 0
            self.conn.execute("DELETE FROM outbox WHERE kind='entity' AND key=? AND transition=0", (entity_id,))
            self.conn.execute("INSERT INTO outbox(kind,key,source_ms,body,transition,queued_ms) VALUES('entity',?,?,?,?,?)",
                              (entity_id, source_ms, json.dumps(body, separators=(",", ":")), transition, dbm.now_ms()))
            if transition:
                self.set(f"sig:{entity_id}", sig)
                extra = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='entity' AND key=? AND transition=1",
                                          (entity_id,)).fetchone()[0] - MAX_TRANSITIONS
                if extra > 0:
                    self.conn.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE kind='entity' AND key=? "
                                      "AND transition=1 ORDER BY id LIMIT ?)", (entity_id, extra))
                    self.bump("dropped_overflow", extra)
            self._bound()
            self.conn.commit()

    def put_status(self, task_id, status):
        with self.lock:
            self.conn.execute("INSERT INTO outbox(kind,key,source_ms,body,transition,queued_ms) VALUES('status',?,?,?,1,?)",
                              (task_id, dbm.now_ms(), json.dumps(status, separators=(",", ":")), dbm.now_ms()))
            self.conn.commit()

    def _bound(self):
        n = self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if n > MAX_ROWS:   # oldest entity snapshots go first; task updates are never dropped
            cut = n - MAX_ROWS
            self.conn.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE kind='entity' "
                              "ORDER BY transition, id LIMIT ?)", (cut,))
            self.bump("dropped_overflow", cut)

    def head(self, limit=50):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM outbox ORDER BY id LIMIT ?", (limit,))]

    def done(self, row_id):
        with self.lock:
            self.conn.execute("DELETE FROM outbox WHERE id=?", (row_id,))
            self.conn.commit()

    def depth(self):
        with self.lock:
            r = self.conn.execute("SELECT COUNT(*) n, MIN(queued_ms) oldest FROM outbox").fetchone()
        return r["n"], r["oldest"]

    # tasks
    def task(self, task_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None

    def tasks(self, states=ACTIVE):
        q = ",".join("?" * len(states))
        with self.lock:
            return [dict(r) for r in self.conn.execute(f"SELECT * FROM tasks WHERE state IN ({q}) ORDER BY received_ms",
                                                       states)]

    def save_task(self, t):
        cols = ["task_id", "norad", "obs_id", "station", "pass_start_ms", "pass_end_ms", "deadline_ms", "state",
                "status_version", "fetches", "last_fetch_ms", "received_ms", "start_ms", "end_ms", "spec", "result",
                "message"]
        with self.lock:
            self.conn.execute(f"INSERT OR REPLACE INTO tasks({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [t.get(c) for c in cols])
            self.conn.commit()

    def set_version(self, task_id, v):
        with self.lock:
            self.conn.execute("UPDATE tasks SET status_version=? WHERE task_id=? AND status_version<?", (v, task_id, v))
            self.conn.commit()


# ---------- the bridge ----------

WORST = ["HEALTH_STATUS_HEALTHY", "HEALTH_STATUS_NOT_READY", "HEALTH_STATUS_WARN", "HEALTH_STATUS_FAIL",
         "HEALTH_STATUS_OFFLINE"]


def _worst(statuses):
    return max(statuses, key=WORST.index) if statuses else "HEALTH_STATUS_NOT_READY"


def _fmt(v, c):
    dp = int(c.get("dp", 2) or 0)
    return f"{v:.{dp}f}" + (f" {c['unit']}" if c.get("unit") else "")


def _ago(ms):
    h = ms / HOUR
    return f"{ms / MIN:.0f} min" if h < 1 else f"{h:.1f} h" if h < 48 else f"{h / 24:.0f} days"


def _hm(ms):
    return time.strftime("%H:%MZ", time.gmtime(ms / 1000))


class Bridge:
    def __init__(self, cfg, log=print, clock=None, fetcher=None):
        self.cfg = cfg
        self.L = cfg["lattice"]
        self.log = log
        self.clock = clock or dbm.now_ms
        self.box = Outbox(cfg.db_path.parent / "lattice-outbox.db")
        self.node = self.box.node_id()
        self.client = Client(self.L["url"], token=_read(cfg, self.L["token_file"]),
                             client_id=str(self.L.get("client_id") or ""),
                             client_secret=_read(cfg, self.L["client_secret_file"]) if self.L.get("client_id") else "",
                             sandbox_token=_read(cfg, self.L["sandbox_token_file"]))
        self.fetcher = fetcher or self._fetch
        self.inbox = queue.Queue()
        self.stop = threading.Event()
        self._cache = {}
        self._fetching = None             # NORAD being fetched in the background, if any
        self._fetch_lock = threading.Lock()
        self._backoff = 0.0
        self._next_send = 0.0
        self._node_published = False
        self._cycle = 0
        self.link = {"state": "STARTING", "last_ok_ms": None, "last_error": None}
        from .catalog import featured
        self.featured = {f["norad"]: f for f in featured()}

    # ----- identity -----
    def eid(self, kind, key=""):
        return str(uuid.uuid5(NAMESPACE, f"{INTEGRATION}:{self.node}:{kind}" + (f":{key}" if key != "" else "")))

    @property
    def node_entity(self):
        return self.eid("node")

    def _cached(self, key, ttl_ms, fn):
        hit = self._cache.get(key)
        now = self.clock()
        if hit and now - hit[0] < ttl_ms:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    def _db(self):
        return dbm.connect_ro(self.cfg.db_path)

    # ----- common components -----
    def _base(self, entity_id, now, template, platform, specific, name, disposition, environment, source_id):
        e = {"entityId": entity_id, "isLive": True,
             "aliases": {"name": name},
             "ontology": {"template": template, "platformType": platform, "specificType": specific},
             "milView": {"disposition": disposition, "environment": environment},
             "provenance": {"integrationName": INTEGRATION, "dataType": "SatNOGS telemetry + SGP4",
                            "sourceId": source_id, "sourceUpdateTime": iso(now)},
             "dataClassification": {"default": {"level": "CLASSIFICATION_LEVELS_UNCLASSIFIED"}}}
        if self.L.get("simulated"):
            e["indicators"] = {"simulated": True}
        return e

    # ----- satellites -----
    def _channels(self, conn, sat, spec):
        if spec.get("channels"):
            return spec["channels"]
        out = []
        for (ch,) in conn.execute("SELECT DISTINCT channel FROM points WHERE norad=?", (sat["norad"],)):
            head = ch.split("_")[0]
            out.append({"id": ch, "sub": head.upper() if len(head) <= 5 else head.capitalize(),
                        "name": ch.replace("_", " ").capitalize(), "unit": "", "dp": 2, "lim": None})
        return out

    def satellite_health(self, conn, sat, now):
        """Health, alerts and the Downlink component for one satellite, following ICD §4.5."""
        norad = sat["norad"]
        spec = spec_for(self.cfg, sat)
        chans = self._channels(conn, sat, spec)
        lims = limits_in_force(conn, spec, norad, int(self.cfg["satellite"]["norad"]))
        drift = {k for k, v in (dbm.smeta_get(conn, norad, "model_status", {}) or {}).items() if v == "drift"}
        alerts, comps, groups = [], [], {}
        for c in chans:
            groups.setdefault(c.get("sub") or "Other", []).append(c)
        for sub, cs in groups.items():
            msgs, st, newest, judged, have = [], [], None, 0, 0
            for c in cs:
                r = conn.execute("SELECT ts_ms, value FROM points WHERE norad=? AND channel=? ORDER BY ts_ms DESC LIMIT 1",
                                 (norad, c["id"])).fetchone()
                if not r:
                    continue
                have += 1
                t, v = r["ts_ms"], r["value"]
                newest = max(newest or 0, t)
                lim = lims.get(c["id"])
                if lim and any(x is not None for x in lim):
                    judged += 1
                    al, cl, ch, ah = lim
                    if (al is not None and v < al) or (ah is not None and v > ah):
                        side, bound = ("below", al) if al is not None and v < al else ("above", ah)
                        text = f"{c['name']} {_fmt(v, c)}, {side} alarm limit {bound}"
                        st.append("HEALTH_STATUS_FAIL")
                        msgs.append({"status": "HEALTH_STATUS_FAIL", "message": text})
                        alerts.append(self._alert(f"LIMIT_ALARM:{c['id']}", text, "ALERT_LEVEL_WARNING", t))
                    elif (cl is not None and v < cl) or (ch is not None and v > ch):
                        side, bound = ("below", cl) if cl is not None and v < cl else ("above", ch)
                        text = f"{c['name']} {_fmt(v, c)}, {side} caution limit {bound}"
                        st.append("HEALTH_STATUS_WARN")
                        msgs.append({"status": "HEALTH_STATUS_WARN", "message": text})
                        alerts.append(self._alert(f"LIMIT_CAUTION:{c['id']}", text, "ALERT_LEVEL_CAUTION", t))
                if c["id"] in drift:
                    text = f"{c['name']} drifting from its learned normal for this point in the orbit"
                    st.append("HEALTH_STATUS_WARN")
                    msgs.append({"status": "HEALTH_STATUS_WARN", "message": text})
                    alerts.append(self._alert(f"DRIFT:{c['id']}", text, "ALERT_LEVEL_ADVISORY", t))
            if not have:
                health, msgs = "HEALTH_STATUS_NOT_READY", [{"status": "HEALTH_STATUS_NOT_READY", "message": "No data yet."}]
            elif st:
                health = _worst(st)
            elif not judged:
                health = "HEALTH_STATUS_NOT_READY"
                msgs = [{"status": "HEALTH_STATUS_NOT_READY",
                         "message": f"No limits set for these {have} channels, so their values aren't judged."}]
            else:
                health = "HEALTH_STATUS_HEALTHY"
                msgs = [{"status": "HEALTH_STATUS_HEALTHY", "message": f"{judged} channels with limits, all within them."}]
            comp = {"id": "subsystem:" + sub.lower().replace(" ", "-"), "name": sub, "health": health, "messages": msgs}
            if newest:
                comp["updateTime"] = iso(newest)
            comps.append(comp)
        # restarts in the last day (exact times from uptime counters, or boot counter jumps)
        from . import forensics
        rs = self._cached(("restarts", norad), 5 * MIN, lambda: forensics.restarts(conn, spec, norad, days=1, now=now))
        seen = set()
        for r in reversed(rs.get("restarts", [])):
            if r["subsystem"] in seen:
                continue
            seen.add(r["subsystem"])
            where = " over the South Atlantic Anomaly" if r.get("saa") else ""
            alerts.append(self._alert(f"RESTART:{r['subsystem'].lower()}",
                                      f"{r['subsystem']} restarted {'at' if r.get('exact') else 'around'} "
                                      f"{_hm(r['t'])}{where}", "ALERT_LEVEL_ADVISORY", r["t"]))
        # contact: stale and silent
        al = self.cfg["alerts"]
        last = conn.execute("SELECT ts_ms, observer FROM frames WHERE norad=? AND decoded_ok=1 ORDER BY ts_ms DESC LIMIT 1",
                            (norad,)).fetchone()
        sil = self._cached(("silence", norad), 5 * MIN, lambda: silence(conn, self.cfg, norad, now))
        stale = al["stale_hours"] * HOUR
        if not last:
            dl = {"health": "HEALTH_STATUS_NOT_READY", "message": "No frames received yet."}
        else:
            age = now - last["ts_ms"]
            who = f" ({last['observer'].rsplit('-', 1)[0]})" if last["observer"] else ""
            dl = {"health": "HEALTH_STATUS_HEALTHY", "message": f"Last frame {_ago(age)} ago{who}."}
            if age >= stale:
                lvl = "ALERT_LEVEL_WARNING" if age >= 2 * stale else "ALERT_LEVEL_CAUTION"
                dl["health"] = "HEALTH_STATUS_FAIL" if age >= 2 * stale else "HEALTH_STATUS_WARN"
                alerts.append(self._alert("STALE", f"No frames for {_ago(age)}", lvl, last["ts_ms"] + stale))
        if sil.get("silent"):
            p = sil.get("probability")
            text = (f"{len(sil.get('streak') or [])} scheduled SatNOGS observations in a row heard nothing"
                    + (f"; chance it has really gone quiet {p:.0%}" if p is not None and sil.get("basis") == "probability" else ""))
            dl = {"health": "HEALTH_STATUS_FAIL", "message": text}
            first = (sil.get("streak") or [{}])[0].get("end") or now
            alerts.append(self._alert("SILENT", text, "ALERT_LEVEL_WARNING", first))
        comps.append({"id": "downlink", "name": "Downlink", "health": dl["health"],
                      "messages": [{"status": dl["health"], "message": dl["message"]}],
                      **({"updateTime": iso(last["ts_ms"])} if last else {})})
        # the worst judged component: a subsystem with no limits shows NOT_READY on its own, but doesn't
        # make the whole satellite "not ready" when everything that is judged is fine
        judged = [c["health"] for c in comps if c["health"] != "HEALTH_STATUS_NOT_READY"]
        roll = "HEALTH_STATUS_OFFLINE" if sil.get("silent") else _worst(judged) if judged else "HEALTH_STATUS_NOT_READY"
        health = {"healthStatus": roll, "components": comps, "activeAlerts": alerts, "updateTime": iso(now)}
        return health

    @staticmethod
    def _alert(code, text, level, t):
        return {"alertCode": code, "description": text, "level": level, "activatedTime": iso(t),
                "activeConditions": [{"conditionCode": code.split(":")[0], "description": text}]}

    def satellite_entity(self, conn, sat, now):
        norad = sat["norad"]
        tle = dbm.smeta_get(conn, norad, "tle")
        f = self.featured.get(norad, {})
        e = self._base(self.eid("satellite", norad), now, "TEMPLATE_ASSET", "Satellite", f.get("size") or "Satellite",
                       sat["name"], "DISPOSITION_NEUTRAL", "ENVIRONMENT_SPACE", f"norad:{norad}")
        e["aliases"]["alternateIds"] = [{"id": str(norad), "type": "ALT_ID_TYPE_NORAD_CAT_ID"}]
        e["health"] = self.satellite_health(conn, sat, now)
        rep = self._cached(("obs", norad), MIN, lambda: observation_report(conn, self.cfg, now - STATION_AFTER, now, norad))
        bits, desc = [], f"{sat['name']} (NORAD {norad}), watched by Downlink Desk node {self.node_name}. Telemetry from SatNOGS."
        if tle:
            e["orbit"] = orbit_component(tle)
            p = position(tle, now)
            if p:
                e["location"] = {"position": {"latitudeDegrees": round(p["lat"], 5), "longitudeDegrees": round(p["lon"], 5),
                                              "altitudeHaeMeters": round(p["hae_m"], 1)},
                                 "velocityEnu": {"e": round(p["enu"][0], 2), "n": round(p["enu"][1], 2), "u": round(p["enu"][2], 2)},
                                 "speedMps": round(p["speed"], 2)}
                from sgp4.api import Satrec
                st = state(Satrec.twoline2rv(tle["tle1"], tle["tle2"]), now)
                if st:
                    bits.append("SUNLIT" if st["sunlit"] else "IN SHADOW")
                if in_saa(p["lat"], p["lon"]):
                    bits.append("IN SAA")
            age = now - (tle.get("epoch_ms") or tle_epoch_ms(tle["tle1"]))
            desc += f" Position is an SGP4 estimate from a TLE {_ago(max(age, 0))} old, not a measurement."
        else:
            desc += " No orbit data yet."
        live = [o for o in rep if o["status"] == "in_progress"]
        nxt = next((o for o in sorted(rep, key=lambda o: o["start"]) if o["start"] > now), None)
        if live:
            bits.append(f"DOWNLINK NOW {live[0]['station'].upper()}")
        elif nxt:
            bits.append(f"NEXT DOWNLINK {_hm(nxt['start'])} {nxt['station'].upper()}")
        if any(t["norad"] == norad for t in self.box.tasks(("executing",))):
            bits.append("EXPEDITING")
        if bits:
            e["status"] = {"platformActivity": " · ".join(bits)[:120]}
        rel = []
        for o in rep:
            if o["status"] == "in_progress" or (o["status"] == "heard" and now - o["end"] <= STATION_AFTER):
                if o.get("station_id") is None:
                    continue
                last = conn.execute("SELECT MAX(ts_ms) FROM frames WHERE norad=? AND ts_ms BETWEEN ? AND ?",
                                    (norad, o["start"] - 120_000, o["end"] + 120_000)).fetchone()[0]
                tb = {"activelyTrackingSensors": {"sensors": [{"sensorId": f"satnogs-station-{o['station_id']}",
                                                               "sensorDescription": f"SatNOGS {o['station_id']} {o['station']}"}]}}
                if last:
                    tb["lastMeasurementTimestamp"] = iso(last)
                rel.append({"relatedEntityId": self.eid("satnogs-station", o["station_id"]),
                            "relationshipId": f"obs-{o['id']}", "relationshipType": {"trackedBy": tb}})
        if rel:
            e["relationships"] = {"relationships": rel}
        e["description"] = desc
        e["_expiry_rel"] = int(self.L["expiry_minutes"]) * MIN
        return e

    # ----- node -----
    @property
    def node_name(self):
        return str(self.L.get("node_name") or self.cfg["station"].get("name") or "Downlink Desk")

    def node_entity_body(self, conn, sats, now):
        e = self._base(self.node_entity, now, "TEMPLATE_ASSET", "Ground Station", f"Downlink Desk {__version__}",
                       f"Downlink Desk {self.node_name}", "DISPOSITION_FRIENDLY", "ENVIRONMENT_LAND", f"node:{self.node}")
        e["provenance"]["dataType"] = "Downlink Desk status"
        st = self.cfg["station"]
        mode = str(self.L.get("node_location") or "rounded").lower()
        if (st.get("lat") or st.get("lon")) and mode in ("rounded", "exact"):
            nd = 1 if mode == "rounded" else 5
            e["location"] = {"position": {"latitudeDegrees": round(float(st["lat"]), nd),
                                          "longitudeDegrees": round(float(st["lon"]), nd)}}
        comps = []
        fails = [dict(r) for r in conn.execute("SELECT ok, t_ms, message FROM fetches ORDER BY id DESC LIMIT 3")]
        last_ok = dbm.meta_get(conn, "last_ok_ms")
        poll = max(20, self.cfg["satnogs"]["poll_minutes"]) * MIN
        if dbm.meta_get(conn, "auth_error", False):
            comps.append(("satnogs-db", "SatNOGS DB", "HEALTH_STATUS_FAIL", "SatNOGS rejected the API key."))
        elif len(fails) == 3 and not any(f["ok"] for f in fails):
            comps.append(("satnogs-db", "SatNOGS DB", "HEALTH_STATUS_FAIL", f"Last 3 fetches failed: {fails[0]['message']}"))
        elif fails and not fails[0]["ok"]:
            comps.append(("satnogs-db", "SatNOGS DB", "HEALTH_STATUS_WARN", f"Last fetch failed: {fails[0]['message']}"))
        elif last_ok and now - last_ok < 3 * poll:
            comps.append(("satnogs-db", "SatNOGS DB", "HEALTH_STATUS_HEALTHY", f"Last good fetch {_ago(now - last_ok)} ago."))
        elif last_ok:
            comps.append(("satnogs-db", "SatNOGS DB", "HEALTH_STATUS_WARN", f"No fetch for {_ago(now - last_ok)}."))
        else:
            comps.append(("satnogs-db", "SatNOGS DB", "HEALTH_STATUS_NOT_READY", "No successful fetch yet."))
        sched = [dbm.smeta_get(conn, s["norad"], "obs_refreshed_ms") for s in sats]
        sched = [x for x in sched if x]
        if sched and now - min(sched) < 7 * HOUR:
            comps.append(("schedule", "Observation schedule", "HEALTH_STATUS_HEALTHY",
                          f"SatNOGS Network schedule loaded {_ago(now - min(sched))} ago."))
        else:
            comps.append(("schedule", "Observation schedule", "HEALTH_STATUS_WARN" if sched else "HEALTH_STATUS_NOT_READY",
                          "SatNOGS Network schedule is out of date." if sched else "Schedule not loaded yet."))
        stor = self._cached("storage", 10 * MIN, lambda: dbm.storage(conn, self.cfg.db_path))
        lvl = {"ok": "HEALTH_STATUS_HEALTHY", "warn": "HEALTH_STATUS_WARN", "crit": "HEALTH_STATUS_FAIL"}.get(stor.get("level"),
                                                                                                            "HEALTH_STATUS_NOT_READY")
        free = stor.get("disk_free")
        comps.append(("storage", "Storage", lvl, f"{free / 1e9:.1f} GB free." if free else "Disk size unknown."))
        n, oldest = self.box.depth()
        lost = self.box.get("dropped_overflow", 0) + self.box.get("dropped_invalid", 0)
        if n and oldest and now - oldest > 5 * MIN:
            link = ("HEALTH_STATUS_WARN", f"{n} updates waiting, oldest from {_ago(now - oldest)} ago.")
        else:
            link = ("HEALTH_STATUS_HEALTHY", "Publishing normally.")
        if lost:
            link = (link[0], link[1] + f" {lost} updates dropped since install (queue limits or rejected by Lattice).")
        comps.append(("lattice-link", "Lattice link", *link))
        try:
            upd = json.loads((self.cfg.db_path.parent / "update.json").read_text())
        except (OSError, ValueError):
            upd = None
        sw = f"Version {__version__}." + (f" Updater: {upd.get('status') or upd.get('message') or ''}".rstrip(": ")
                                         if isinstance(upd, dict) else "")
        comps.append(("software", "Software", "HEALTH_STATUS_HEALTHY", sw))
        e["health"] = {"healthStatus": _worst([c[2] for c in comps]), "updateTime": iso(now),
                       "connectionStatus": "CONNECTION_STATUS_ONLINE",
                       "components": [{"id": i, "name": nm, "health": h, "messages": [{"status": h, "message": m}],
                                       "updateTime": iso(now)} for i, nm, h, m in comps]}
        busy = self.box.tasks(("executing",))
        if busy:
            t = busy[0]
            name = next((s["name"] for s in sats if s["norad"] == t["norad"]), str(t["norad"]))
            act = f"EXPEDITING {name.upper()} PASS {t['obs_id']}"
        else:
            act = f"MONITORING {len(sats)} SATELLITE{'S' if len(sats) != 1 else ''}"
        e["status"] = {"platformActivity": act[:120]}
        if self.L.get("tasks", True):
            e["taskCatalog"] = {"taskDefinitions": [{"taskSpecificationUrl": TASK_TYPE}]}
        e["description"] = (f"Downlink Desk ground-segment console '{self.node_name}': monitors satellites through "
                            f"the SatNOGS network. Cannot command any spacecraft. Accepts ExpeditePass.")
        e["_expiry_rel"] = int(self.L["expiry_minutes"]) * MIN
        return e

    # ----- SatNOGS stations -----
    def station_entities(self, conn, sats, now):
        rows = {}
        for s in sats:
            norad = s["norad"]
            rep = self._cached(("obs", norad), MIN, lambda: observation_report(conn, self.cfg, now - STATION_AFTER, now, norad))
            rates = (self._cached(("silence", norad), 5 * MIN, lambda: silence(conn, self.cfg, norad, now))
                     .get("station_rates") or {})
            for o in rep:
                if o.get("station_id") is None or o.get("lat") is None or o["start"] > now + STATION_AHEAD:
                    continue
                rows.setdefault(o["station_id"], []).append((s, o, rates.get(o["station"])))
        out = []
        for sid, items in rows.items():
            items.sort(key=lambda x: x[1]["start"])
            s0, o0, rate = items[0]
            e = self._base(self.eid("satnogs-station", sid), now, "TEMPLATE_ASSET", "Ground Station", "SatNOGS station",
                           f"SatNOGS {sid} {o0['station']}", "DISPOSITION_NEUTRAL", "ENVIRONMENT_LAND",
                           f"satnogs-station:{sid}")
            e["provenance"]["dataType"] = "SatNOGS Network schedule"
            e["aliases"]["alternateIds"] = [{"id": f"satnogs-station-{sid}", "type": "ALT_ID_TYPE_ASSET_ID"}]
            e["location"] = {"position": {"latitudeDegrees": round(o0["lat"], 4), "longitudeDegrees": round(o0["lng"], 4)}}
            live = next((x for x in items if x[1]["status"] == "in_progress"), None)
            heard = [x for x in items if x[1]["status"] == "heard" and now - x[1]["end"] <= STATION_AFTER]
            future = [x for x in items if x[1]["start"] > now]
            if live:
                act = f"OBSERVING {live[0]['name'].upper()}"
            elif future:
                s1, o1, _ = future[0]
                act = f"SCHEDULED {s1['name'].upper()} {_hm(o1['start'])}–{_hm(o1['end'])} EL {o1['max_alt'] or 0:.0f}°"
            elif heard:
                act = f"HEARD {heard[-1][0]['name'].upper()} {_hm(heard[-1][1]['start'])}"
            else:
                act = f"PASS ENDED {items[-1][0]['name'].upper()} {_hm(items[-1][1]['end'])}"
            e["status"] = {"platformActivity": act[:120]}
            desc = f"Volunteer SatNOGS ground station {sid}. Its health isn't known to Downlink Desk."
            if rate:
                desc += (f" Heard {rate['heard']} of its last {rate['tries']} finished observations of "
                         f"{s0['name']} (estimated hit rate {rate['rate']:.0%}).")
            e["description"] = desc
            e["_expiry_abs"] = min(max(x[1]["end"] for x in items) + STATION_AFTER, now + MAX_EXPIRY)
            out.append(e)
        return out

    # ----- cycle -----
    def clock_ok(self, conn):
        """A Pi has no clock battery: don't stamp anything until the clock is past the newest data."""
        newest = conn.execute("SELECT MAX(fetched_ms) FROM frames").fetchone()[0] or 0
        return newest <= self.clock() + 10 * MIN

    def build(self, conn, stations=True):
        now = self.clock()
        sats = dbm.satellites(conn)
        ents = [self.node_entity_body(conn, sats, now)]
        for s in sats:
            try:
                ents.append(self.satellite_entity(conn, s, now))
            except Exception as ex:   # one satellite's bad data mustn't stop the others
                self.log(f"[{s['norad']}] Skipped in this publish: {type(ex).__name__}: {ex}")
        if stations:
            try:
                ents += self.station_entities(conn, sats, now)
            except Exception as ex:
                self.log(f"Stations skipped in this publish: {type(ex).__name__}: {ex}")
        return now, ents

    @staticmethod
    def signature(e):
        h = e.get("health") or {}
        return json.dumps([h.get("healthStatus"), sorted(a["alertCode"] for a in h.get("activeAlerts", [])),
                           (e.get("status") or {}).get("platformActivity")])

    def publish_cycle(self):
        """Build every entity, queue it, then send what the link allows."""
        try:
            conn = self._db()
        except sqlite3.Error as ex:
            self.log(f"Console database not readable yet: {ex}")
            return
        try:
            if not conn.execute("SELECT name FROM sqlite_master WHERE name='satellites'").fetchone():
                self.log("Console database not set up yet; waiting for the first fetch.")
                return
            if not self.clock_ok(conn):
                self.log("System clock is behind the newest stored data (not synced yet?); not publishing.")
                return
            stations = self._cycle % max(1, int(self.L["station_minutes"] * 60 // max(5, self.L["publish_seconds"]))) == 0
            self._cycle += 1
            now, ents = self.build(conn, stations=stations)
            for e in ents:
                self.box.put_entity(e["entityId"], now, e, self.signature(e))
            self.run_tasks(conn)
        finally:
            conn.close()
        self.drain()
        self.write_status()

    # ----- sending -----
    def drain(self, budget_s=20):
        if time.time() < self._next_send:
            return
        t0 = time.time()
        while time.time() - t0 < budget_s:
            rows = self.box.head(50)
            if not rows:
                break
            for r in rows:
                try:
                    self._send_row(r)
                except Unreachable as ex:
                    return self._fail(f"Lattice unreachable: {ex}")
                except HttpError as ex:
                    if ex.status == 401 and self.client.client_id:
                        self.client.forget_token()
                    if ex.status in (401, 403):
                        self.link["state"] = "AUTH_ERROR"
                        return self._fail(f"Lattice refused the credentials (HTTP {ex.status}).", keep_state=True)
                    if ex.status == 429 or ex.status >= 500 or ex.status == 408:
                        return self._fail(f"Lattice busy (HTTP {ex.status}).", ex.retry_after)
                    # 400 and other 4xx: this item is invalid; retrying won't help
                    self.log(f"Lattice rejected a {r['kind']} update for {r['key']}: {ex}")
                    self.box.bump("dropped_invalid")
                    self.box.done(r["id"])
                    continue
                self.box.done(r["id"])
                self._backoff = 0.0
                self.link.update(state="ONLINE", last_ok_ms=dbm.now_ms(), last_error=None)
                if time.time() - t0 >= budget_s:
                    break

    def _fail(self, msg, retry_after=None, keep_state=False):
        self._backoff = min(300.0, max(5.0, self._backoff * 2 or 5.0))
        wait = retry_after if retry_after else self._backoff * random.uniform(0.8, 1.2)
        self._next_send = time.time() + wait
        if not keep_state:
            self.link["state"] = "OFFLINE"
        if self.link.get("last_error") != msg:
            self.log(f"{msg} Retrying in {wait:.0f} s; updates are queued.")
        self.link["last_error"] = msg

    def _send_row(self, r):
        body = json.loads(r["body"])
        if r["kind"] == "entity":
            now = dbm.now_ms()
            rel, ab = body.pop("_expiry_rel", None), body.pop("_expiry_abs", None)
            exp = now + rel if rel else ab
            if not exp or exp <= now + 5000:
                return   # no longer relevant (a station whose passes ended during an outage)
            body["expiryTime"] = iso(min(exp, now + MAX_EXPIRY))
            self.client.request("PUT", "api/v1/entities", body)
            if r["key"] == self.node_entity:
                self._node_published = True
            return
        self._send_status(r["key"], body)

    def _send_status(self, task_id, status, retried=False):
        t = self.box.task(task_id) or {"status_version": 0}
        v = int(t["status_version"]) + 1
        payload = {"statusVersion": v, "newStatus": status,
                   "author": {"system": {"serviceName": INTEGRATION, "entityId": self.node_entity}}}
        try:
            _, resp = self.client.request("PUT", f"api/v1/tasks/{urllib.parse.quote(task_id)}/status", payload)
        except HttpError as ex:
            if ex.status in (400, 409) and not retried:   # stale version: read the task and try once more
                _, cur = self.client.request("GET", f"api/v1/tasks/{urllib.parse.quote(task_id)}")
                sv = int(((cur or {}).get("version") or {}).get("statusVersion") or 0)
                if (((cur or {}).get("status") or {}).get("status") or "") in ("STATUS_DONE_OK", "STATUS_DONE_NOT_OK"):
                    self.log(f"Task {task_id} already finished in Lattice; dropping a late status update.")
                    return
                with self.box.lock:
                    self.box.conn.execute("UPDATE tasks SET status_version=? WHERE task_id=?", (sv, task_id))
                    self.box.conn.commit()
                return self._send_status(task_id, status, retried=True)
            raise
        sv = ((resp or {}).get("version") or {}).get("statusVersion")
        self.box.set_version(task_id, int(sv) if sv is not None else v)

    # ----- tasks -----
    def handle(self, req):
        """One AgentRequest from Lattice: execute, cancel or complete."""
        if not isinstance(req, dict):
            return
        if req.get("executeRequest"):
            self._execute((req["executeRequest"] or {}).get("task") or {})
        elif req.get("cancelRequest"):
            self._finish((req["cancelRequest"] or {}).get("taskId"), False, "ERROR_CODE_CANCELLED", "Cancelled by request.")
        elif req.get("completeRequest"):
            tid = (req["completeRequest"] or {}).get("taskId")
            t = self.box.task(tid) if tid else None
            if t and t["state"] in ACTIVE:
                self._finish(tid, True, None, "Completed by request.", result=self._result(t, self._report_row(t)))

    def _execute(self, task):
        tid = ((task.get("version") or {}).get("taskId")) or task.get("taskId")
        if not tid:
            return
        if self.box.task(tid):   # delivered again (reconnect): already handled
            return
        now = self.clock()
        t = {"task_id": tid, "state": "received", "received_ms": now, "fetches": 0,
             "status_version": int((task.get("version") or {}).get("statusVersion") or 0),
             "spec": json.dumps(task.get("specification") or {})}
        self.box.save_task(t)
        self._status(t, "STATUS_MACHINE_RECEIPT")
        problem = self._validate(t, task.get("specification") or {}, now)
        if problem:
            return self._finish(tid, False, "ERROR_CODE_REJECTED", problem)
        t = self.box.task(tid)
        t["state"] = "wilco"
        self.box.save_task(t)
        self._status(t, "STATUS_WILCO")
        self.log(f"Task {tid}: expediting {t['station']} pass {t['obs_id']} of NORAD {t['norad']}.")
        try:
            conn = self._db()
            try:
                self._advance(conn, t)
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    def _validate(self, t, spec, now):
        if spec.get("@type") != TASK_TYPE:
            return f"Unsupported task type {spec.get('@type')!r}; this node accepts {TASK_TYPE}."

        def num(k):
            v = spec.get(k)
            try:
                return int(v) if v not in (None, "") else 0
            except (TypeError, ValueError):
                return None
        norad, obs_id, dl = num("noradCatId"), num("observationId"), num("deadlineMinutes")
        if not norad or obs_id is None or dl is None or dl < 0:
            return "noradCatId is required; observationId and deadlineMinutes must be whole numbers."
        if dl > 720:
            return "deadlineMinutes can be at most 720 (12 h)."
        conn = self._db()
        try:
            sat = next((s for s in dbm.satellites(conn) if s["norad"] == norad), None)
            if not sat:
                return f"NORAD {norad} isn't tracked by this node."
            rep = observation_report(conn, self.cfg, now - 13 * HOUR, now, norad)
        finally:
            conn.close()
        grace = int(self.cfg["alerts"]["upload_grace_hours"] * 60)
        if obs_id:
            o = next((x for x in rep if x["id"] == obs_id), None)
            if not o:
                return f"SatNOGS observation {obs_id} isn't a known observation of NORAD {norad}."
        else:
            o = next((x for x in sorted(rep, key=lambda x: x["start"]) if x["end"] > now), None)
            if not o:
                return f"No scheduled SatNOGS observation of NORAD {norad} in the next 12 hours."
        deadline = o["end"] + (dl or grace) * MIN
        if deadline <= now and o["status"] != "heard":
            return f"Observation {o['id']} ended at {_hm(o['end'])}; its deadline has passed."
        busy = self.box.tasks()
        dup = next((x for x in busy if x["obs_id"] == o["id"] and x["task_id"] != t["task_id"]), None)
        if dup:
            return f"Observation {o['id']} is already being expedited by task {dup['task_id']}."
        if len([x for x in busy if x["task_id"] != t["task_id"]]) >= int(self.L["max_tasks"]):
            return f"This node already has {self.L['max_tasks']} active tasks."
        t.update(norad=norad, obs_id=o["id"], station=o["station"], pass_start_ms=o["start"], pass_end_ms=o["end"],
                 deadline_ms=deadline)
        self.box.save_task(t)
        return None

    def _report_row(self, t, conn=None):
        own = conn is None
        conn = conn or self._db()
        try:
            rep = observation_report(conn, self.cfg, t["pass_end_ms"] - 1, max(self.clock(), t["pass_end_ms"] + 1), t["norad"])
            o = next((x for x in rep if x["id"] == t["obs_id"]), None)
            if o and o["frames"]:
                pad = 120_000
                rows = conn.execute("SELECT observer, ts_ms FROM frames WHERE norad=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
                                    (t["norad"], o["start"] - pad, o["end"] + pad)).fetchall()
                from .analysis import same_station
                ts = [r["ts_ms"] for r in rows if same_station(r["observer"], o["station"])]
                if ts:
                    o["first"], o["last"] = ts[0], ts[-1]
            return o
        finally:
            if own:
                conn.close()

    def _result(self, t, o):
        r = {"@type": RESULT_TYPE, "observationId": t["obs_id"], "stationName": t["station"] or "",
             "framesReceived": (o or {}).get("frames", 0), "framesDecoded": (o or {}).get("decoded", 0),
             "fetches": t["fetches"]}
        if o and o.get("first"):
            r["firstFrameTime"], r["lastFrameTime"] = iso(o["first"]), iso(o["last"])
        return r

    def run_tasks(self, conn):
        for t in self.box.tasks():
            try:
                self._advance(conn, t)
            except Exception as ex:
                self.log(f"Task {t['task_id']}: {type(ex).__name__}: {ex}")

    def _advance(self, conn, t):
        now = self.clock()
        if t["state"] not in ("wilco", "executing"):
            return
        o = self._report_row(t, conn)
        if o and o["status"] == "heard":
            return self._finish(t["task_id"], True, None, f"{o['frames']} frames from {o['station']} received.",
                                result=self._result(t, o))
        if now >= t["deadline_ms"]:
            return self._finish(t["task_id"], False, "ERROR_CODE_TIMEOUT",
                                f"No frames from {t['station']} uploaded by {_hm(t['deadline_ms'])} "
                                f"({_ago(t['deadline_ms'] - t['pass_end_ms'])} after the pass).")
        if now < t["pass_end_ms"]:
            return
        gap = max(5, int(self.L["expedite_minutes"])) * MIN
        if t["state"] == "wilco":
            t.update(state="executing", start_ms=now)
            self.box.save_task(t)
            self._status(t, "STATUS_EXECUTING", progress=self._progress(t, now))
        if self._fetching is None and now - (t["last_fetch_ms"] or 0) >= gap:
            t.update(last_fetch_ms=now, fetches=t["fetches"] + 1)
            self.box.save_task(t)
            self._start_fetch(t["norad"])

    def _progress(self, t, now):
        nxt = max(now, (t["last_fetch_ms"] or now) + max(5, int(self.L["expedite_minutes"])) * MIN)
        return {"@type": PROGRESS_TYPE, "observationId": t["obs_id"], "stationName": t["station"] or "",
                "passEndTime": iso(t["pass_end_ms"]), "deadlineTime": iso(t["deadline_ms"]),
                "fetches": t["fetches"], "nextFetchTime": iso(nxt)}

    def _start_fetch(self, norad):
        with self._fetch_lock:
            if self._fetching is not None:
                return False
            self._fetching = norad

        def work():
            try:
                self.fetcher(norad)
            except Exception as ex:
                self.log(f"Expedited fetch of {norad} failed: {type(ex).__name__}: {ex}")
            finally:
                self._fetching = None
        threading.Thread(target=work, daemon=True).start()
        return True

    def _fetch(self, norad):
        """The normal fetch job for one satellite: same lock and pacing, so it can't overlap the timer's."""
        from .fetch import run
        run(self.cfg, force=True, log=lambda m: None, only=norad)

    def _status(self, t, status, progress=None, result=None, error=None):
        s = {"status": status}
        if progress:
            s["progress"] = progress
        if result:
            s["result"] = result
        if error:
            s["taskError"] = error
        if status == "STATUS_EXECUTING" and t.get("start_ms"):
            s["startTime"] = iso(t["start_ms"])
        if status in ("STATUS_DONE_OK", "STATUS_DONE_NOT_OK"):
            s["endTime"] = iso(t.get("end_ms") or self.clock())
            if t.get("start_ms"):
                s["startTime"] = iso(t["start_ms"])
        self.box.put_status(t["task_id"], s)

    def _finish(self, tid, ok, code, message, result=None):
        t = self.box.task(tid) if tid else None
        if not t or t["state"] in TERMINAL:
            return
        t.update(state="done_ok" if ok else "done_not_ok", end_ms=self.clock(), message=message,
                 result=json.dumps(result) if result else None)
        self.box.save_task(t)
        err = None if ok else {"code": code, "message": message}
        self._status(t, "STATUS_DONE_OK" if ok else "STATUS_DONE_NOT_OK", result=result, error=err)
        self.log(f"Task {tid}: {'done' if ok else 'not done'}: {message}")

    # ----- listening -----
    def listen_loop(self):
        """Long poll for tasks addressed to this node. Lattice holds each request up to 5 minutes."""
        backoff = 0.0
        while not self.stop.is_set():
            if not self._node_published or not self.L.get("tasks", True):
                self.stop.wait(1)
                continue
            try:
                _, data = self.client.request("POST", "api/v1/agent/listen",
                                              {"agentSelector": {"entityIds": [self.node_entity]}}, timeout=330)
                backoff = 0.0
                if data and any(data.get(k) for k in ("executeRequest", "cancelRequest", "completeRequest")):
                    self.inbox.put(data)
            except (Unreachable, HttpError) as ex:
                if isinstance(ex, Unreachable) and "timed out" in str(ex):
                    continue   # the hold ended without a task; ask again
                backoff = min(300.0, max(5.0, backoff * 2 or 5.0))
                self.stop.wait(backoff * random.uniform(0.8, 1.2))

    def process_inbox(self):
        while True:
            try:
                req = self.inbox.get_nowait()
            except queue.Empty:
                return
            try:
                self.handle(req)
            except Exception as ex:
                self.log(f"Task request not handled: {type(ex).__name__}: {ex}")

    def write_status(self):
        n, oldest = self.box.depth()
        st = {"state": self.link["state"], "last_ok_ms": self.link["last_ok_ms"], "last_error": self.link["last_error"],
              "queued": n, "oldest_queued_ms": oldest, "node_id": self.node, "node_entity": self.node_entity,
              "active_tasks": len(self.box.tasks()), "dropped_invalid": self.box.get("dropped_invalid", 0),
              "dropped_overflow": self.box.get("dropped_overflow", 0), "written_ms": dbm.now_ms()}
        try:
            p = self.cfg.db_path.parent / "lattice-status.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(st))
            tmp.replace(p)
        except OSError:
            pass
        return st

    def run(self):
        self.log(f"Lattice bridge for node {self.node_name} ({self.node_entity}) publishing to {self.client.base}.")
        threading.Thread(target=self.listen_loop, daemon=True).start()
        period = max(5, int(self.L["publish_seconds"]))
        while not self.stop.is_set():
            t0 = time.time()
            self.process_inbox()
            self.publish_cycle()
            while not self.stop.is_set() and time.time() - t0 < period:
                if not self.inbox.empty():
                    self.process_inbox()
                    self.drain()
                self.stop.wait(0.5)


def _read(cfg, rel):
    if not rel:
        return ""
    try:
        return cfg.resolve(rel).read_text().strip()
    except OSError:
        return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Publish Downlink Desk to Anduril Lattice (docs/ICD-LATTICE.md)")
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true", help="print the entities it would publish; send nothing")
    ap.add_argument("--once", action="store_true", help="one publish cycle, then exit")
    ap.add_argument("--status", action="store_true", help="link state and queue depth")
    a = ap.parse_args(argv)
    cfg = find_config(a.config)
    if a.status:
        try:
            print(json.dumps(json.loads((cfg.db_path.parent / "lattice-status.json").read_text()), indent=2))
        except (OSError, ValueError):
            print("No status yet: the bridge hasn't run.")
        return 0
    if a.dry_run:
        b = Bridge(cfg, log=lambda m: print(m, file=sys.stderr))
        conn = b._db()
        _, ents = b.build(conn)
        for e in ents:
            e.pop("_expiry_rel", None), e.pop("_expiry_abs", None)
        print(json.dumps(ents, indent=2, ensure_ascii=False))
        return 0
    if not cfg["lattice"].get("enabled"):
        raise SystemExit("The Lattice bridge is off. Set enabled = true under [lattice] in config.toml.")
    if not cfg["lattice"].get("url"):
        raise SystemExit("Set url under [lattice] in config.toml to your Lattice environment's address.")
    b = Bridge(cfg)
    if a.once:
        b.publish_cycle()
        print(json.dumps(b.write_status(), indent=2))
        return 0
    try:
        b.run()
    except KeyboardInterrupt:
        b.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
