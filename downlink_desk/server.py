"""The console's web server: serves the page and a small JSON API.

Standard library only, so it runs comfortably on a Raspberry Pi Zero 2 W.
    python -m downlink_desk.server [--config config.toml]
"""
import argparse
import contextlib
import csv
import gzip
import hashlib
import hmac
import io
import json
import math
import mimetypes
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, baseline, battery, logchain, spacewx, summary
from . import db as dbm
from .analysis import _good_limits, observation_report, silence
from .catalog import channels_for, entries, featured, find
from .config import find_config
from .fetch import spec_for
from .orbit import in_saa

WEB = Path(__file__).resolve().parent.parent / "web"
STARTED = dbm.now_ms()
STATIC = {}


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = dbm.open_db(cfg)
        self.lock = threading.Lock()
        self._pass_cache = {}
        self._ecl_cache = {}
        self._spec_cache = {}
        self._model_cache = {}
        self._cache = {}
        self._pool = queue.LifoQueue()
        self._fetching = False
        self._streams = 0
        self.max_streams = 12
        self._cond = threading.Condition()
        self._events, self._ev_total = [], 0
        self._watcher = None
        if not self.conn.execute("SELECT 1 FROM log LIMIT 1").fetchone():
            s = cfg["satellite"]
            logchain.append(self.conn, "system", {"norad": s["norad"]},
                            f"Console commissioned for {s['name']} (NORAD {s['norad']}).", "system")

    # ---------- plumbing ----------
    @contextlib.contextmanager
    def ro(self):
        """A read-only connection from a small pool. SQLite's WAL mode lets these read while the
        fetch job writes, so a slow read (the health model, the summary) never blocks the rest."""
        try:
            c = self._pool.get_nowait()
        except queue.Empty:
            c = dbm.connect_ro(self.cfg.db_path)
        try:
            yield c
        finally:
            if self._pool.qsize() < 6:
                self._pool.put(c)
            else:
                c.close()

    def cached(self, key, ttl, fn, sig=None):
        """Small time-and-signature cache for results that are costly to rebuild."""
        hit = self._cache.get(key)
        now = time.time()
        if hit and now - hit[0] < ttl and hit[1] == sig:
            return hit[2]
        val = fn()
        self._cache[key] = (now, sig, val)
        return val

    def data_sig(self, norad=None):
        """Changes whenever a frame is stored (cheap: two index lookups)."""
        with self.ro() as c:
            if norad is None:
                return (c.execute("SELECT MAX(id) FROM frames").fetchone()[0],)
            return (c.execute("SELECT MAX(id) FROM frames").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM frames WHERE norad=? AND phase_min IS NULL", (norad,)).fetchone()[0])

    def _storage(self):
        with self.ro() as c:
            return dbm.storage(c, self.cfg.db_path)

    def _commissioned(self):
        with self.ro() as c:
            return c.execute("SELECT MIN(t_ms) m FROM log").fetchone()["m"]

    # ---------- satellites ----------
    def sats(self, enabled_only=True):
        with self.ro() as c:
            return dbm.satellites(c, enabled_only)

    def sat(self, q):
        """The satellite a request is about: ?sat=NORAD, else the first one tracked."""
        rows = self.sats(False)
        want = q.get("sat")
        for r in rows:
            if want and str(r["norad"]) == str(want):
                return r
        enabled = [r for r in rows if r["enabled"]]
        if not enabled and not rows:
            raise ValueError("no satellites tracked")
        return (enabled or rows)[0]

    def spec(self, sat):
        """Curated channels file, or channels built from whatever fields have arrived."""
        key = sat["norad"]
        hit = self._spec_cache.get(key)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        spec = spec_for(self.cfg, sat)
        if not spec.get("channels"):
            with self.ro() as c:
                rows = c.execute("SELECT channel, COUNT(*) n, MAX(ABS(value - CAST(value AS INTEGER))) frac "
                                         "FROM points WHERE norad=? GROUP BY channel ORDER BY channel", (key,)).fetchall()
            chans = []
            for r in rows:
                head = r["channel"].split("_")[0]
                chans.append({"id": r["channel"], "sub": head.upper() if len(head) <= 5 else head.capitalize(),
                              "name": r["channel"].replace("_", " ").capitalize(), "field": r["channel"], "unit": "",
                              "dp": 2 if (r["frac"] or 0) > 0 else 0, "lim": None,
                              "counter": "reboot" if ("boot" in r["channel"] or "reset_c" in r["channel"]) and "since" not in r["channel"] else None})
            spec = {"auto": True, "channels": chans, "frame_types": [{"name": "Decoded", "if_field": ""}],
                    "notes": "Automatic channels: every numeric field the community decoder produces, in raw units. "
                             "No limits until you set them (or use Suggest from data). A channels file in channels/ "
                             "can give them names, units and limits."}
        self._spec_cache[key] = (time.time(), spec)
        return spec

    def fleet(self):
        """Each tracked satellite with a little status, for the picker."""
        now = dbm.now_ms()
        out = []
        feat = {f["norad"]: f for f in featured()}
        for s in self.sats(False):
            n = s["norad"]
            with self.ro() as c:
                cnt = c.execute("SELECT MAX(ts_ms) last, SUM(CASE WHEN ts_ms>? THEN 1 ELSE 0 END) day "
                                "FROM frames WHERE norad=? AND decoded_ok=1", (now - 86_400_000, n)).fetchone()
                rep = observation_report(c, self.cfg, now - 3 * 86_400_000, now, n)
                last_ok = dbm.smeta_get(c, n, "last_ok_ms")
                obs_ref = dbm.smeta_get(c, n, "obs_refreshed_ms")
            sil = self.cached(("silence", n), 300, lambda n=n: self._silence(n, now),
                              self.data_sig(n) + (obs_ref, now // 600_000))
            nxt = next((o for o in sorted(rep, key=lambda o: o["start"]) if o["end"] > now), None)
            f = feat.get(n, {})
            out.append({**s, "last_frame_ms": cnt["last"], "frames_24h": cnt["day"] or 0, "last_ok_ms": last_ok,
                        "silent": sil["silent"], "silence_probability": sil["probability"],
                        "next_obs_ms": nxt["start"] if nxt else None,
                        "bio": f.get("bio", ""), "size": f.get("size", ""), "builder": f.get("builder", ""),
                        "country": f.get("country", ""), "launched": f.get("launched", ""), "website": f.get("website", "")})
        return out

    def _silence(self, norad, now):
        with self.ro() as c:
            return silence(c, self.cfg, norad, now)

    def restarts(self, sat):
        from . import forensics
        n = sat["norad"]
        spec = self.spec(sat)

        def build():
            with self.ro() as c:
                return forensics.restarts(c, spec, n, 30)
        return self.cached(("restarts", n), 600, build, self.data_sig(n))

    def track(self, norad, cs):
        with self.lock:
            e = find(self.conn, self.cfg, norad)
        if not e:
            raise ValueError("That satellite isn't in the catalog of decodable satellites on this Pi.")
        with self.lock:
            existing = self.conn.execute("SELECT * FROM satellites WHERE norad=?", (norad,)).fetchone()
            if existing:
                self.conn.execute("UPDATE satellites SET enabled=1 WHERE norad=?", (norad,))
            else:
                self.conn.execute("INSERT INTO satellites(norad,name,decoder,channels,enabled,added_ms) VALUES(?,?,?,?,1,?)",
                                  (norad, e["name"], e["decoder"], channels_for(e), dbm.now_ms()))
            self.conn.commit()
            entry = logchain.append(self.conn, "satellite", {"norad": norad, "action": "track"},
                                    f"Started tracking {e['name']} (NORAD {norad}).", cs or "system")
        self._spec_cache.pop(norad, None)
        return entry

    def untrack(self, norad, cs):
        with self.lock:
            row = self.conn.execute("SELECT * FROM satellites WHERE norad=?", (norad,)).fetchone()
            if not row:
                raise ValueError("Not tracking that satellite.")
            if self.conn.execute("SELECT COUNT(*) FROM satellites WHERE enabled=1").fetchone()[0] <= 1 and row["enabled"]:
                raise ValueError("Keep at least one satellite tracked.")
            self.conn.execute("UPDATE satellites SET enabled=0 WHERE norad=?", (norad,))
            self.conn.commit()
            return logchain.append(self.conn, "satellite", {"norad": norad, "action": "untrack"},
                                   f"Stopped tracking {row['name']} (NORAD {norad}). Its stored data is kept.", cs or "system")

    # ---------- reads ----------
    def config(self, sat):
        c = self.cfg
        return {"version": __version__, "satellite": {"norad": sat["norad"], "name": sat["name"], "decoder": sat["decoder"]},
                "station": c["station"], "poll_minutes": max(20, c["satnogs"]["poll_minutes"]), "stale_hours": c["alerts"]["stale_hours"],
                "alerts": c["alerts"], "phone_alerts": bool(str(c["notify"].get("ntfy_topic") or "").strip()),
                "write_protected": bool(c["server"]["write_passcode"]), "spec": self.spec(sat),
                "default_norad": int(c["satellite"]["norad"]),
                "commissioned_ms": self._commissioned()}

    def telemetry(self, sat, since=None, after_id=None):
        """Points grouped by channel, plus frame summaries.
        since: everything heard after a time (first load).
        after_id: only frames stored since the console last asked, whenever they were heard,
        so late uploads and filled-in history arrive too, and an idle poll costs almost nothing."""
        n = sat["norad"]
        with self.ro() as c:
            max_id = c.execute("SELECT COALESCE(MAX(id),0) FROM frames").fetchone()[0]
            if after_id is not None:
                frs = c.execute("SELECT id,ts_ms,observer,frame_type,decoded_ok,sunlit FROM frames WHERE norad=? AND id>? "
                                "ORDER BY id LIMIT 5000", (n, after_id)).fetchall()
                if frs:
                    max_id = frs[-1][0] if len(frs) == 5000 else max(max_id, frs[-1][0])   # a frame stored in between
                    t0, t1 = min(r[1] for r in frs), max(r[1] for r in frs)
                    lo = min(r[0] for r in frs)
                    pts = c.execute("SELECT ts_ms,channel,value FROM points WHERE norad=? AND ts_ms BETWEEN ? AND ? "
                                    "AND frame_id>=? AND frame_id<=?", (n, t0, t1, lo, frs[-1][0])).fetchall()
                else:
                    pts = []
            else:
                pts = c.execute("SELECT ts_ms,channel,value FROM points WHERE norad=? AND ts_ms>? ORDER BY ts_ms",
                                (n, since)).fetchall()
                frs = c.execute("SELECT id,ts_ms,observer,frame_type,decoded_ok,sunlit FROM frames WHERE norad=? AND ts_ms>? "
                                "ORDER BY ts_ms", (n, since)).fetchall()
        chans = {}
        for t, ch, v in pts:
            chans.setdefault(ch, []).append([t, round(v, 4)])
        return {"now": dbm.now_ms(), "max_id": max_id, "more": len(frs) == 5000, "channels": chans,
                "frames": [list(r) for r in frs]}

    def frames(self, sat, limit, before):
        with self.ro() as c:
            rows = c.execute(
                "SELECT id,ts_ms,observer,station_id,app_source,frame_type,decoded_ok,fields_json,frame_hex,sunlit,sub_lat,sub_lon,phase_min,shadow_min FROM frames"
                " WHERE norad=? AND ts_ms<? ORDER BY ts_ms DESC LIMIT ?", (sat["norad"], before, limit)).fetchall()
        return [{"id": r["id"], "t": r["ts_ms"], "observer": r["observer"], "station_id": r["station_id"],
                 "app_source": r["app_source"], "type": r["frame_type"], "ok": bool(r["decoded_ok"]),
                 "fields": json.loads(r["fields_json"] or "{}"), "hex": r["frame_hex"],
                 "sunlit": r["sunlit"], "sub_lat": r["sub_lat"], "sub_lon": r["sub_lon"],
                 "phase": r["phase_min"] if (r["phase_min"] or 0) >= 0 else None, "shadow": r["shadow_min"],
                 "saa": in_saa(r["sub_lat"], r["sub_lon"])} for r in rows]

    def observations(self, sat, days):
        now = dbm.now_ms()
        with self.ro() as c:
            rep = observation_report(c, self.cfg, now - days * 86_400_000, now, sat["norad"])
            refreshed = dbm.smeta_get(c, sat["norad"], "obs_refreshed_ms")
        sil = self.cached(("silence", sat["norad"]), 300, lambda: self._silence(sat["norad"], now),
                          self.data_sig(sat["norad"]) + (refreshed, now // 600_000))
        return {"now": now, "observations": rep, "refreshed_ms": refreshed,
                "silent_streak": [o["id"] for o in sil["streak"]] if sil["silent"] else [], "silent_after": 1,
                # the streak can reach back further than the observations listed, so send it whole
                "silent_detail": sorted(({"id": o["id"], "station": o["station"], "end": o["end"]} for o in sil["streak"]),
                                        key=lambda o: o["end"]) if sil["silent"] else [],
                "silence": {k: v for k, v in sil.items() if k != "streak"},
                "missed_run": [o["id"] for o in sil["streak"]]}

    def _tle(self, sat):
        with self.ro() as c:
            return dbm.smeta_get(c, sat["norad"], "tle")

    def eclipses(self, sat, since, until):
        key = (sat["norad"], since // 1800_000, until // 1800_000)
        if key in self._ecl_cache:
            return self._ecl_cache[key]
        tle = self._tle(sat)
        out = {"eclipses": []}
        if tle:
            from .orbit import eclipses
            out["eclipses"] = eclipses(tle["tle1"], tle["tle2"], since, until, 60)
        self._ecl_cache = {k: v for k, v in self._ecl_cache.items() if k[0] != sat["norad"]}
        self._ecl_cache[key] = out
        return out

    def now_state(self, sat):
        tle = self._tle(sat)
        if not tle:
            return {}
        from .orbit import now_state
        return self.cached(("now", sat["norad"]), 15, lambda: now_state(tle["tle1"], tle["tle2"], dbm.now_ms()) or {},
                           tle.get("tle1"))

    def ground_track(self, sat, before_min=50, after_min=100, step_s=30):
        """Where the satellite was and will be: [t, lat, lon, alt_km, sunlit] every step_s seconds,
        for the globe's ground track and its moving dot (the browser interpolates between points)."""
        tle = self._tle(sat)
        if not tle:
            return {"points": []}

        def build():
            from sgp4.api import Satrec
            from .orbit import period_min, state
            s = Satrec.twoline2rv(tle["tle1"], tle["tle2"])
            now = dbm.now_ms() // (step_s * 1000) * step_s * 1000
            pts = []
            for t in range(now - before_min * 60_000, now + after_min * 60_000 + 1, step_s * 1000):
                st = state(s, t)
                if st:
                    pts.append([t, st["lat"], st["lon"], st["alt_km"], 1 if st["sunlit"] else 0])
            return {"now": dbm.now_ms(), "period_min": round(period_min(s), 2), "step_s": step_s, "points": pts}
        return self.cached(("track", sat["norad"], before_min, after_min), 300, build, tle.get("tle1"))

    def health(self, sat):
        n = sat["norad"]
        with self.ro() as c:
            auth_error = bool(dbm.meta_get(c, "auth_error", False))
            fetches = [dict(r) for r in c.execute("SELECT * FROM fetches WHERE norad=? OR norad IS NULL "
                                                         "ORDER BY id DESC LIMIT 60", (n,))]
            counts = self.cached(("counts", n), 300, lambda: dict(c.execute(
                "SELECT COUNT(*) n, MAX(ts_ms) last, SUM(decoded_ok) ok, MAX(CASE WHEN decoded_ok=1 THEN ts_ms END) last_ok "
                "FROM frames WHERE norad=?", (n,)).fetchone()),
                self.data_sig())
            tle = dbm.smeta_get(c, n, "tle") or {}
            satmeta = dbm.smeta_get(c, n, "satellite") or {}
            last_attempt = dbm.smeta_get(c, n, "last_attempt_ms")
            last_ok = dbm.smeta_get(c, n, "last_ok_ms")
        store = self.cached("storage", 60, lambda: self._storage())
        size = store.get("db_bytes", 0)
        try:
            upd = json.loads((self.cfg.db_path.parent / "update.json").read_text())
        except (OSError, ValueError):
            upd = None
        return {"now": dbm.now_ms(), "server_started_ms": STARTED, "last_attempt_ms": last_attempt,
                "last_ok_ms": last_ok, "auth_error": auth_error,
                "token_present": bool(self.cfg.token), "fetches": fetches,
                "frames_total": counts["n"], "frames_decoded": counts["ok"] or 0, "last_frame_ms": counts["last"],
                "last_decoded_ms": counts["last_ok"],
                "db_bytes": size, "tle_epoch_ms": tle.get("epoch_ms"), "satellite": satmeta,
                "fetching": self._fetching, "storage": store, "update": upd,
                "auto_update": bool(self.cfg["updates"].get("auto_update", True)), "version": __version__}

    def passes(self, sat, hours):
        cached = self._pass_cache.get(sat["norad"])
        if cached and time.time() - cached[0] < 600 and cached[1]["hours"] >= hours:
            return cached[1]
        tle = self._tle(sat)
        st = self.cfg["station"]
        out = {"hours": hours, "station": st, "passes": [], "tle_epoch_ms": None, "note": ""}
        if not tle:
            out["note"] = "No orbit data yet. It downloads with the first successful fetch."
        elif not (st["lat"] or st["lon"]):
            out["note"] = "Set your station's latitude and longitude in config.toml to predict passes."
        else:
            from .orbit import passes
            start = dbm.now_ms() - 3600_000
            out["passes"] = passes(tle["tle1"], tle["tle2"], start, hours + 1, st["lat"], st["lon"],
                                   st.get("alt_m", 0), st.get("min_elevation", 10))
            out["tle_epoch_ms"] = tle.get("epoch_ms")
        self._pass_cache[sat["norad"]] = (time.time(), out)
        return out

    def model(self, sat):
        n = sat["norad"]
        spec = self.spec(sat)

        def build():
            with self.ro() as c:
                return baseline.compute(c, spec, n)
        return self.cached(("model", n), 600, build, self.data_sig(n))

    def battery(self, sat):
        n = sat["norad"]
        launched = next((f.get("launched") for f in featured() if f["norad"] == n), None)
        spec = self.spec(sat)

        def build():
            with self.ro() as c:
                return battery.analyse(c, spec, n, launched)
        return self.cached(("battery", n), 600, build, self.data_sig(n))

    def profile(self, sat, ch):
        with self.ro() as c:
            return baseline.profile(c, self.spec(sat), sat["norad"], ch)

    def spaceweather(self, days):
        now = dbm.now_ms()
        with self.ro() as c:
            return spacewx.view(c, now - days * 86_400_000, now)

    def summary(self):
        def build():
            with self.ro() as c:
                out = summary.build(c, self.cfg)
                out["sent_day"] = dbm.meta_get(c, "summary_sent")
            return out
        s = dict(self.cached("summary", 900, build, self.data_sig()))
        s["phone"] = bool(str(self.cfg["notify"].get("ntfy_topic") or "").strip())
        s["timezone"] = str(self.cfg["notify"].get("timezone") or "") or "the Pi's local time"
        return s

    # ---------- live stream: one watcher thread shared by every open console ----------
    def stream_open(self):
        with self.lock:
            if self._streams >= self.max_streams:
                return False
            self._streams += 1
            if self._watcher is None:   # bookmark the database now, so nothing stored from here on is missed
                with self.ro() as c:
                    start = self._heads(c)
                self._watcher = threading.Thread(target=self._watch, args=(start,), daemon=True)
                self._watcher.start()
            return True

    def stream_close(self):
        with self.lock:
            self._streams -= 1

    def _publish(self, evs):
        with self._cond:
            self._events.extend(evs)
            self._ev_total += len(evs)
            if len(self._events) > 2000:
                del self._events[:len(self._events) - 2000]
            self._cond.notify_all()

    def events_since(self, idx, timeout):
        """Events after position idx; waits up to timeout for new ones. Returns (events, new idx)."""
        with self._cond:
            self._cond.wait_for(lambda: self._ev_total > idx, timeout)
            have = self._ev_total
            first = have - len(self._events)
            return self._events[max(0, idx - first):], have

    @staticmethod
    def _heads(c):
        return (c.execute("SELECT COALESCE(MAX(id),0) FROM frames").fetchone()[0],
                c.execute("SELECT COALESCE(MAX(id),0) FROM fetches").fetchone()[0],
                c.execute("SELECT COALESCE(MAX(seq),0) FROM log").fetchone()[0],
                dbm.meta_get(c, "last_attempt_ms", 0))

    def _watch(self, start):
        """Every 2 s: anything new in the database? Turn it into events for the open consoles.
        The fetch job is a separate process, so the database is the only place to look."""
        heads = self._heads
        last_f, last_x, last_l, attempt = start
        fetching = self._fetching
        while True:
            time.sleep(2)
            try:
                with self.ro() as c:
                    if not self._streams:   # nobody watching: just keep the bookmarks current
                        last_f, last_x, last_l, attempt = heads(c)
                        continue
                    f, x, lg, att = heads(c)
                    if (f, x, lg, att) == (last_f, last_x, last_l, attempt) and self._fetching == fetching:
                        continue
                    names = {r["norad"]: r["name"] for r in dbm.satellites(c, False)}
                    frs = c.execute("SELECT id,ts_ms,observer,frame_type,decoded_ok,sunlit,sub_lat,sub_lon,norad,fetched_ms "
                                    "FROM frames WHERE id>? ORDER BY id LIMIT 2000", (last_f,)).fetchall() if f != last_f else []
                    fxs = c.execute("SELECT * FROM fetches WHERE id>? ORDER BY id", (last_x,)).fetchall() if x != last_x else []
            except Exception:
                continue
            evs, hist_n, hist_old = [], 0, None
            for r in frs:
                if r["fetched_ms"] - r["ts_ms"] > 6 * 3600_000:   # filled-in history, not news
                    hist_n += 1
                    hist_old = r["ts_ms"] if hist_old is None else min(hist_old, r["ts_ms"])
                    continue
                evs.append(("frame", {"id": r["id"], "norad": r["norad"], "sat": names.get(r["norad"], str(r["norad"])),
                                      "t": r["ts_ms"], "stored": r["fetched_ms"], "observer": r["observer"],
                                      "type": r["frame_type"], "ok": bool(r["decoded_ok"]), "sunlit": r["sunlit"],
                                      "lat": r["sub_lat"], "lon": r["sub_lon"], "saa": in_saa(r["sub_lat"], r["sub_lon"])}))
            if hist_n:
                evs.append(("history", {"n": hist_n, "oldest": hist_old}))
            if frs:
                evs.append(("data", {"max_id": frs[-1]["id"], "norads": sorted({r["norad"] for r in frs})}))
            for r in fxs:
                evs.append(("fetch", {"t": r["t_ms"], "ok": bool(r["ok"]), "message": r["message"],
                                      "new": r["frames_new"], "norad": r["norad"]}))
            if lg != last_l:
                evs.append(("log", {"seq": lg}))
            if att != attempt or (self._fetching and not fetching):
                evs.append(("fetching", {"t": att}))
            last_f = frs[-1]["id"] if len(frs) == 2000 else f
            last_x, last_l, attempt, fetching = x, lg, att, self._fetching
            if evs:
                self._publish(evs)

    def fetch_now(self, sat=None):
        with self.ro() as c:
            last = dbm.smeta_get(c, sat["norad"], "last_attempt_ms", 0) if sat else dbm.meta_get(c, "last_attempt_ms", 0)
            never_ok = bool(sat) and dbm.smeta_get(c, sat["norad"], "last_ok_ms") is None
        with self.lock:   # check and claim in one step, so two quick clicks can't both start one
            if self._fetching:
                return False, "A fetch is already running."
            # a satellite never fetched yet may go sooner (it was probably just added), but a failing
            # one still can't be retried back to back
            if dbm.now_ms() - (last or 0) < (20_000 if never_ok else 120_000):
                return False, "Fetched less than two minutes ago. SatNOGS is volunteer-run, so give it a moment."
            self._fetching = True

        def work():
            try:
                from .fetch import run
                run(self.cfg, force=True, log=lambda m: None, only=sat["norad"] if sat else None)
            finally:
                self._fetching = False
                self._spec_cache.clear()
        threading.Thread(target=work, daemon=True).start()
        return True, f"Fetching {sat['name'] if sat else 'all satellites'} from SatNOGS now."


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "DownlinkDesk/" + __version__
        timeout = 60   # a client that stops sending mid-request is dropped, not kept on a thread forever

        def log_message(self, fmt, *args):
            pass

        def _gzip_ok(self):
            return "gzip" in (self.headers.get("Accept-Encoding") or "")

        def _json(self, obj, status=200):
            try:
                body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
            except ValueError:   # a NaN or infinity somewhere: browsers can't parse those, so send null
                body = json.dumps(_finite(obj), separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            if len(body) > 1400 and self._gzip_ok():   # telemetry JSON shrinks ~10x; cheap even on a Pi Zero
                body = gzip.compress(body, 4)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, text, ctype, filename=None):
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if filename:
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:80]   # names come from SatNOGS; keep headers clean
                self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _same_origin(self):
            """Block other websites from making changes through a visitor's browser (CSRF): the
            console always sends JSON, which a plain cross-site form can't, and a browser that
            sends an Origin must be on this console's own address."""
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return False
            origin = self.headers.get("Origin")
            if origin:
                return urlparse(origin).netloc == (self.headers.get("Host") or "")
            return True

        def _authorised(self):
            code = app.cfg["server"]["write_passcode"]
            return not code or hmac.compare_digest(self.headers.get("X-Passcode", ""), str(code))

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 64_000:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            return body

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            try:
                if p == "/api/satellites":
                    return self._json(app.fleet())
                if p == "/api/spaceweather":
                    return self._json(app.spaceweather(_num(q, "days", 7, 0.1, 60, False)))
                if p == "/api/summary":
                    return self._json(app.summary())
                if p == "/api/catalog":
                    with app.ro() as c:
                        return self._json(entries(c, app.cfg))
                if p.startswith("/api/") and p not in ("/api/log", "/api/log/verify", "/api/notes", "/api/export/log.json", "/api/stream"):
                    sat = app.sat(q)
                if p == "/api/config":
                    return self._json(app.config(sat))
                if p == "/api/telemetry":
                    if "after_id" in q:
                        return self._json(app.telemetry(sat, after_id=_num(q, "after_id", 0, 0, I64)))
                    since = _num(q, "since", dbm.now_ms() - 7 * 86_400_000, dbm.now_ms() - 31 * 86_400_000, I64)
                    return self._json(app.telemetry(sat, since=since))   # at most a month at once (memory)
                if p == "/api/frames":
                    return self._json(app.frames(sat, _num(q, "limit", 100, 1, 500), _num(q, "before", I64, 0, I64)))
                if p == "/api/health":
                    return self._json(app.health(sat))
                if p == "/api/passes":
                    return self._json(app.passes(sat, _num(q, "hours", 36, 1, 72, False)))
                if p == "/api/observations":
                    return self._json(app.observations(sat, _num(q, "days", 3, 0.1, 14, False)))
                if p == "/api/eclipses":
                    now = dbm.now_ms()
                    since = _num(q, "since", now - 7 * 86_400_000, now - 14 * 86_400_000, now + 86_400_000)
                    return self._json(app.eclipses(sat, since, _num(q, "until", now, since, now + 86_400_000)))
                if p == "/api/now":
                    return self._json(app.now_state(sat))
                if p == "/api/battery":
                    return self._json(app.battery(sat))
                if p == "/api/track":
                    return self._json(app.ground_track(sat))
                if p == "/api/restarts":
                    return self._json(app.restarts(sat))
                if p == "/api/model":
                    return self._json(app.model(sat))
                if p == "/api/profile":
                    return self._json(app.profile(sat, q.get("ch", "")))
                if p == "/api/stream":
                    return self._stream()
                if p == "/api/log":
                    with app.ro() as c:
                        return self._json(logchain.entries(c, _num(q, "after", 0, 0, I64)))
                if p == "/api/log/verify":
                    with app.ro() as c:
                        return self._json(logchain.verify(c))
                if p == "/api/notes":
                    with app.ro() as c:
                        return self._json(dbm.meta_get(c, "notes", {"text": "", "cs": "", "t": 0}))
                if p == "/api/export/telemetry.csv":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{sat['name']}-telemetry.csv")[:80]
                    self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
                    self.end_headers()   # no length: streamed, and the connection closes at the end (HTTP/1.0)
                    for chunk in export_csv_rows(app, sat):
                        self.wfile.write(chunk.encode())
                    return
                if p == "/api/export/log.json":
                    with app.ro() as c:
                        data = {"satellite": app.cfg["satellite"], "exported_ms": dbm.now_ms(),
                                "verify": logchain.verify(c), "entries": logchain.entries(c)}
                    return self._text(json.dumps(data, indent=2), "application/json", "audit-log.json")
                return self._static(p)
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:   # never drop the connection without an answer; the detail goes to the log
                import traceback
                traceback.print_exc()
                return self._json({"error": "internal error (details in the Pi's log)"}, 500)

        def do_POST(self):
            u = urlparse(self.path)
            p = u.path
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._same_origin():
                return self._json({"error": "Changes are only accepted from this console's own page."}, 403)
            if not self._authorised():
                return self._json({"error": "This console needs the operator passcode for changes."}, 401)
            try:
                body = self._body()
                if p == "/api/log":
                    if body.get("kind") == "limits":
                        d = body.get("data") if isinstance(body.get("data"), dict) else {}
                        if not isinstance(d.get("ch"), str) or not _good_limits(d.get("lim")):
                            raise ValueError("limits must be four numbers (or blanks) for a channel")
                    with app.lock:
                        e = logchain.append(app.conn, body.get("kind", ""), body.get("data", {}),
                                            body.get("msg", ""), body.get("cs", ""))
                    return self._json(e, 201)
                if p == "/api/notes":
                    text = str(body.get("text", ""))[:4000]
                    with app.lock:
                        dbm.meta_set(app.conn, "notes", {"text": text, "cs": str(body.get("cs", ""))[:16],
                                                         "t": dbm.now_ms()})
                    return self._json({"ok": True})
                if p == "/api/summary/send":
                    with app.lock:
                        sent = summary.maybe_send(app.conn, app.cfg, log=lambda m: None, force=True)
                    return self._json({"ok": sent, "message": "Sent to your phone." if sent else
                                       "Not sent. Set ntfy_topic in config.toml to get it on your phone."}, 200 if sent else 409)
                if p == "/api/fetch":
                    ok, msg = app.fetch_now(app.sat(q) if q.get("sat") else None)
                    return self._json({"ok": ok, "message": msg}, 202 if ok else 429)
                if p == "/api/satellites/track":
                    e = app.track(_num(body, "norad", 0, 0, 10**7), str(body.get("cs", ""))[:16])
                    return self._json(e, 201)
                if p == "/api/satellites/untrack":
                    return self._json(app.untrack(_num(body, "norad", 0, 0, 10**7), str(body.get("cs", ""))[:16]))
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception:
                import traceback
                traceback.print_exc()
                return self._json({"error": "internal error (details in the Pi's log)"}, 500)

        def _event(self, name, data):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())

        def _stream(self):
            """Server-sent events: frames the moment the Pi stores them, fetch progress, new log
            entries. One shared watcher feeds every open console."""
            if not app.stream_open():
                return self._json({"error": "Too many consoles are streaming at once."}, 503)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self._event("hello", {"now": dbm.now_ms(), "version": __version__})
                _, idx = app.events_since(10**12, 0)
                end = time.time() + 1800
                while time.time() < end:
                    evs, idx2 = app.events_since(idx, 15)
                    if idx2 == idx:
                        self.wfile.write(b": ping\n\n")
                    for name, data in evs:
                        self._event(name, data)
                    idx = idx2
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.stream_close()

        def _static(self, p):
            if p == "/":
                p = "/index.html"
            if p == "/training":
                p = "/training.html"
            f = (WEB / p.lstrip("/")).resolve()
            if WEB not in f.parents or not f.is_file():
                return self._json({"error": "not found"}, 404)
            st = f.stat()
            key = (str(f), st.st_mtime_ns, st.st_size)
            hit = STATIC.get(str(f))
            if not hit or hit[0] != key:   # read, compress and fingerprint once per file version
                raw = f.read_bytes()
                hit = (key, raw, gzip.compress(raw, 9), '"' + hashlib.sha1(raw).hexdigest()[:16] + '"')
                STATIC[str(f)] = hit
            _, raw, gz, etag = hit
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            body = gz if self._gzip_ok() else raw
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(str(f))[0] or "application/octet-stream")
                             + ("; charset=utf-8" if f.suffix in (".html", ".js", ".css", ".json") else ""))
            if body is gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")   # always revalidate, but a 304 costs nothing
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


I64 = 2**62


def _num(src, name, default, lo, hi, integer=True):
    """A number from a request, clamped to [lo, hi]. Rejects NaN, infinity and absurd sizes
    (SQLite integers are 64-bit) with a clean 400 instead of a crash."""
    raw = src.get(name, default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number")
    v = min(max(v, lo), hi)
    return int(v) if integer else v


def _finite(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def _cell(v):
    """Spreadsheet-safe text: a cell starting with = + - @ would run as a formula in Excel, and
    observer names come from volunteers."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def export_csv_rows(app, sat):
    """Yields CSV text a few hundred rows at a time, so a year of data never sits in memory."""
    ids = [c["id"] for c in app.spec(sat)["channels"]]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["utc", "observer", "frame_type"] + ids)
    with app.ro() as c:
        cur = c.execute("SELECT f.id, f.ts_ms, f.observer, f.frame_type, p.channel, p.value FROM frames f"
                        " JOIN points p ON p.frame_id=f.id WHERE f.norad=? ORDER BY f.ts_ms, f.id", (sat["norad"],))
        key, vals, n = None, {}, 0
        for fid, t, obs, ft, ch, v in cur:
            if key and key[0] != fid:
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(key[1] / 1000)), _cell(key[2]), _cell(key[3])]
                           + [vals.get(i, "") for i in ids])
                vals, n = {}, n + 1
                if n % 500 == 0:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            key = (fid, t, obs, ft)
            vals[ch] = v
        if key:
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(key[1] / 1000)), _cell(key[2]), _cell(key[3])]
                       + [vals.get(i, "") for i in ids])
    yield buf.getvalue()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Downlink Desk console server")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = find_config(a.config)
    app = App(cfg)
    s = cfg["server"]
    httpd = ThreadingHTTPServer((s["host"], int(s["port"])), make_handler(app))
    print(f"Downlink Desk {__version__} serving on http://{s['host']}:{s['port']}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
