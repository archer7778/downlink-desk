"""The console's web server: serves the page and a small JSON API.

Standard library only, so it runs comfortably on a Raspberry Pi Zero 2 W.
    python -m downlink_desk.server [--config config.toml]
"""
import argparse
import csv
import hmac
import io
import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, baseline, battery, logchain, spacewx, summary
from . import db as dbm
from .analysis import observation_report, silent_streak
from .catalog import channels_for, entries, featured, find
from .config import find_config
from .fetch import spec_for
from .orbit import in_saa

WEB = Path(__file__).resolve().parent.parent / "web"
STARTED = dbm.now_ms()


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = dbm.open_db(cfg)
        self.lock = threading.Lock()
        self._pass_cache = {}
        self._ecl_cache = {}
        self._spec_cache = {}
        self._model_cache = {}
        self._fetching = False
        self._streams = 0
        self.max_streams = 12
        if not self.conn.execute("SELECT 1 FROM log LIMIT 1").fetchone():
            s = cfg["satellite"]
            logchain.append(self.conn, "system", {"norad": s["norad"]},
                            f"Console commissioned for {s['name']} (NORAD {s['norad']}).", "system")

    # ---------- satellites ----------
    def sats(self, enabled_only=True):
        with self.lock:
            return dbm.satellites(self.conn, enabled_only)

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
            with self.lock:
                rows = self.conn.execute("SELECT channel, COUNT(*) n, MAX(ABS(value - CAST(value AS INTEGER))) frac "
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
            with self.lock:
                c = self.conn.execute("SELECT MAX(ts_ms) last, SUM(CASE WHEN ts_ms>? THEN 1 ELSE 0 END) day "
                                      "FROM frames WHERE norad=? AND decoded_ok=1", (now - 86_400_000, n)).fetchone()
                rep = observation_report(self.conn, self.cfg, now - 3 * 86_400_000, now, n)
                last_ok = dbm.smeta_get(self.conn, n, "last_ok_ms")
            streak = silent_streak(rep, self.cfg)
            nxt = next((o for o in sorted(rep, key=lambda o: o["start"]) if o["end"] > now), None)
            f = feat.get(n, {})
            out.append({**s, "last_frame_ms": c["last"], "frames_24h": c["day"] or 0, "last_ok_ms": last_ok,
                        "silent": len(streak) >= self.cfg["alerts"]["silent_after"],
                        "next_obs_ms": nxt["start"] if nxt else None,
                        "bio": f.get("bio", ""), "size": f.get("size", ""), "builder": f.get("builder", ""),
                        "country": f.get("country", ""), "launched": f.get("launched", ""), "website": f.get("website", "")})
        return out

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
                "station": c["station"], "poll_minutes": c["satnogs"]["poll_minutes"], "stale_hours": c["alerts"]["stale_hours"],
                "alerts": c["alerts"], "phone_alerts": bool(str(c["notify"].get("ntfy_topic") or "").strip()),
                "write_protected": bool(c["server"]["write_passcode"]), "spec": self.spec(sat),
                "default_norad": int(c["satellite"]["norad"]),
                "commissioned_ms": self.conn.execute("SELECT MIN(t_ms) m FROM log").fetchone()["m"]}

    def telemetry(self, sat, since):
        n = sat["norad"]
        with self.lock:
            pts = self.conn.execute("SELECT ts_ms,channel,value FROM points WHERE norad=? AND ts_ms>? ORDER BY ts_ms LIMIT 60000",
                                    (n, since)).fetchall()
            frs = self.conn.execute("SELECT id,ts_ms,observer,frame_type,decoded_ok,sunlit FROM frames WHERE norad=? AND ts_ms>? "
                                    "ORDER BY ts_ms LIMIT 20000", (n, since)).fetchall()
        return {"now": dbm.now_ms(), "points": [[r[0], r[1], r[2]] for r in pts],
                "frames": [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in frs]}

    def frames(self, sat, limit, before):
        with self.lock:
            rows = self.conn.execute(
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
        with self.lock:
            rep = observation_report(self.conn, self.cfg, now - days * 86_400_000, now, sat["norad"])
            refreshed = dbm.smeta_get(self.conn, sat["norad"], "obs_refreshed_ms")
        streak = silent_streak(rep, self.cfg)
        return {"now": now, "observations": rep, "refreshed_ms": refreshed,
                "silent_streak": [o["id"] for o in streak], "silent_after": self.cfg["alerts"]["silent_after"]}

    def _tle(self, sat):
        with self.lock:
            return dbm.smeta_get(self.conn, sat["norad"], "tle")

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
        return now_state(tle["tle1"], tle["tle2"], dbm.now_ms()) or {}

    def health(self, sat):
        n = sat["norad"]
        with self.lock:
            g = lambda k, d=None: dbm.meta_get(self.conn, k, d)
            fetches = [dict(r) for r in self.conn.execute("SELECT * FROM fetches WHERE norad=? OR norad IS NULL "
                                                         "ORDER BY id DESC LIMIT 60", (n,))]
            counts = self.conn.execute("SELECT COUNT(*) n, MAX(ts_ms) last, SUM(decoded_ok) ok FROM frames WHERE norad=?",
                                       (n,)).fetchone()
            tle = dbm.smeta_get(self.conn, n, "tle") or {}
            satmeta = dbm.smeta_get(self.conn, n, "satellite") or {}
            last_attempt = dbm.smeta_get(self.conn, n, "last_attempt_ms")
            last_ok = dbm.smeta_get(self.conn, n, "last_ok_ms")
        size = 0
        for suffix in ("", "-wal"):   # in WAL mode recent data lives in the -wal file until a checkpoint
            try:
                size += self.cfg.db_path.with_name(self.cfg.db_path.name + suffix).stat().st_size
            except OSError:
                pass
        return {"now": dbm.now_ms(), "server_started_ms": STARTED, "last_attempt_ms": last_attempt,
                "last_ok_ms": last_ok, "auth_error": bool(g("auth_error", False)),
                "token_present": bool(self.cfg.token), "fetches": fetches,
                "frames_total": counts["n"], "frames_decoded": counts["ok"] or 0, "last_frame_ms": counts["last"],
                "db_bytes": size, "tle_epoch_ms": tle.get("epoch_ms"), "satellite": satmeta,
                "fetching": self._fetching}

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
        with self.lock:
            sig = self.conn.execute("SELECT COUNT(*), MAX(ts_ms), (SELECT COUNT(*) FROM frames WHERE norad=? AND phase_min IS NULL) "
                                    "FROM points WHERE norad=?", (n, n)).fetchone()
        key = (n, tuple(sig), dbm.now_ms() // 600_000)
        hit = self._model_cache.get(n)
        if hit and hit[0] == key:
            return hit[1]
        spec = self.spec(sat)
        with self.lock:
            m = baseline.compute(self.conn, spec, n)
        self._model_cache[n] = (key, m)
        return m

    def battery(self, sat):
        launched = next((f.get("launched") for f in featured() if f["norad"] == sat["norad"]), None)
        spec = self.spec(sat)
        with self.lock:
            return battery.analyse(self.conn, spec, sat["norad"], launched)

    def profile(self, sat, ch):
        with self.lock:
            return baseline.profile(self.conn, self.spec(sat), sat["norad"], ch)

    def spaceweather(self, days):
        now = dbm.now_ms()
        with self.lock:
            return spacewx.view(self.conn, now - days * 86_400_000, now)

    def summary(self):
        with self.lock:
            s = summary.build(self.conn, self.cfg)
            s["sent_day"] = dbm.meta_get(self.conn, "summary_sent")
        s["phone"] = bool(str(self.cfg["notify"].get("ntfy_topic") or "").strip())
        s["timezone"] = str(self.cfg["notify"].get("timezone") or "") or "the Pi's local time"
        return s

    def stream_open(self):
        with self.lock:
            if self._streams >= self.max_streams:
                return False
            self._streams += 1
            return True

    def stream_close(self):
        with self.lock:
            self._streams -= 1

    def fetch_now(self, sat=None):
        if self._fetching:
            return False, "A fetch is already running."
        with self.lock:
            last = dbm.smeta_get(self.conn, sat["norad"], "last_attempt_ms", 0) if sat else dbm.meta_get(self.conn, "last_attempt_ms", 0)
            never_ok = bool(sat) and dbm.smeta_get(self.conn, sat["norad"], "last_ok_ms") is None
        if dbm.now_ms() - (last or 0) < 120_000 and not never_ok:
            return False, "Fetched less than two minutes ago. SatNOGS is volunteer-run, so give it a moment."

        def work():
            self._fetching = True
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

        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, text, ctype, filename=None):
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self):
            code = app.cfg["server"]["write_passcode"]
            return not code or hmac.compare_digest(self.headers.get("X-Passcode", ""), str(code))

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            try:
                if p == "/api/satellites":
                    return self._json(app.fleet())
                if p == "/api/spaceweather":
                    return self._json(app.spaceweather(min(float(q.get("days", 7)), 60)))
                if p == "/api/summary":
                    return self._json(app.summary())
                if p == "/api/catalog":
                    with app.lock:
                        return self._json(entries(app.conn, app.cfg))
                if p.startswith("/api/") and p not in ("/api/log", "/api/log/verify", "/api/notes", "/api/export/log.json", "/api/stream"):
                    sat = app.sat(q)
                if p == "/api/config":
                    return self._json(app.config(sat))
                if p == "/api/telemetry":
                    return self._json(app.telemetry(sat, int(q.get("since", dbm.now_ms() - 7 * 86_400_000))))
                if p == "/api/frames":
                    return self._json(app.frames(sat, min(int(q.get("limit", 100)), 500), int(q.get("before", 2**53))))
                if p == "/api/health":
                    return self._json(app.health(sat))
                if p == "/api/passes":
                    return self._json(app.passes(sat, min(float(q.get("hours", 36)), 72)))
                if p == "/api/observations":
                    return self._json(app.observations(sat, min(float(q.get("days", 3)), 14)))
                if p == "/api/eclipses":
                    now = dbm.now_ms()
                    since = max(int(q.get("since", now - 7 * 86_400_000)), now - 14 * 86_400_000)
                    return self._json(app.eclipses(sat, since, min(int(q.get("until", now)), now + 86_400_000)))
                if p == "/api/now":
                    return self._json(app.now_state(sat))
                if p == "/api/battery":
                    return self._json(app.battery(sat))
                if p == "/api/model":
                    return self._json(app.model(sat))
                if p == "/api/profile":
                    return self._json(app.profile(sat, q.get("ch", "")))
                if p == "/api/stream":
                    return self._stream()
                if p == "/api/log":
                    with app.lock:
                        return self._json(logchain.entries(app.conn, int(q.get("after", 0))))
                if p == "/api/log/verify":
                    with app.lock:
                        return self._json(logchain.verify(app.conn))
                if p == "/api/notes":
                    with app.lock:
                        return self._json(dbm.meta_get(app.conn, "notes", {"text": "", "cs": "", "t": 0}))
                if p == "/api/export/telemetry.csv":
                    return self._text(export_csv(app, sat), "text/csv", f"{sat['name']}-telemetry.csv")
                if p == "/api/export/log.json":
                    with app.lock:
                        data = {"satellite": app.cfg["satellite"], "exported_ms": dbm.now_ms(),
                                "verify": logchain.verify(app.conn), "entries": logchain.entries(app.conn)}
                    return self._text(json.dumps(data, indent=2), "application/json", "audit-log.json")
                return self._static(p)
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)

        def do_POST(self):
            u = urlparse(self.path)
            p = u.path
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self._authorised():
                return self._json({"error": "This console needs the operator passcode for changes."}, 401)
            try:
                body = self._body()
                if p == "/api/log":
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
                    e = app.track(int(body.get("norad", 0)), str(body.get("cs", ""))[:16])
                    return self._json(e, 201)
                if p == "/api/satellites/untrack":
                    return self._json(app.untrack(int(body.get("norad", 0)), str(body.get("cs", ""))[:16]))
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        def _event(self, name, data):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())

        def _stream(self):
            """Server-sent events: frames the moment the Pi stores them, and fetch progress.
            The fetch job is a separate process, so this watches the database (cheap: two
            indexed lookups every 2 s per open console)."""
            if not app.stream_open():
                return self._json({"error": "Too many consoles are streaming at once."}, 503)
            try:
                with app.lock:
                    last_f = app.conn.execute("SELECT COALESCE(MAX(id),0) FROM frames").fetchone()[0]
                    last_x = app.conn.execute("SELECT COALESCE(MAX(id),0) FROM fetches").fetchone()[0]
                    attempt = dbm.meta_get(app.conn, "last_attempt_ms", 0)
                    names = {s["norad"]: s["name"] for s in dbm.satellites(app.conn, False)}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self._event("hello", {"now": dbm.now_ms(), "version": __version__})
                end, beat = time.time() + 1800, time.time()
                fetching = app._fetching
                while time.time() < end:
                    time.sleep(2)
                    with app.lock:
                        frs = app.conn.execute(
                            "SELECT id,ts_ms,observer,frame_type,decoded_ok,sunlit,sub_lat,sub_lon,norad,fetched_ms FROM frames "
                            "WHERE id>? ORDER BY id LIMIT 300", (last_f,)).fetchall()
                        fxs = app.conn.execute("SELECT * FROM fetches WHERE id>? ORDER BY id", (last_x,)).fetchall()
                        att = dbm.meta_get(app.conn, "last_attempt_ms", 0)
                    for r in frs:
                        last_f = r["id"]
                        if r["norad"] not in names:
                            names[r["norad"]] = str(r["norad"])
                        self._event("frame", {"id": r["id"], "norad": r["norad"], "sat": names.get(r["norad"]),
                                              "t": r["ts_ms"], "stored": r["fetched_ms"], "observer": r["observer"],
                                              "type": r["frame_type"], "ok": bool(r["decoded_ok"]), "sunlit": r["sunlit"],
                                              "lat": r["sub_lat"], "lon": r["sub_lon"], "saa": in_saa(r["sub_lat"], r["sub_lon"])})
                    for r in fxs:
                        last_x = r["id"]
                        self._event("fetch", {"t": r["t_ms"], "ok": bool(r["ok"]), "message": r["message"],
                                              "new": r["frames_new"], "norad": r["norad"]})
                    if att != attempt or app._fetching != fetching:
                        if (att != attempt) or app._fetching:
                            self._event("fetching", {"t": att})
                        attempt, fetching = att, app._fetching
                    if time.time() - beat > 15:
                        self.wfile.write(b": ping\n\n")
                        beat = time.time()
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
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(str(f))[0] or "application/octet-stream")
                             + ("; charset=utf-8" if f.suffix in (".html", ".js", ".css", ".json") else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
    return H


def export_csv(app, sat):
    ids = [c["id"] for c in app.spec(sat)["channels"]]
    with app.lock:
        rows = app.conn.execute("SELECT f.ts_ms, f.observer, f.frame_type, p.channel, p.value FROM frames f"
                                " JOIN points p ON p.frame_id=f.id WHERE f.norad=? ORDER BY f.ts_ms", (sat["norad"],)).fetchall()
    by = {}
    for t, obs, ft, ch, v in rows:
        by.setdefault((t, obs, ft), {})[ch] = v
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["utc", "observer", "frame_type"] + ids)
    for (t, obs, ft), vals in by.items():
        w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t / 1000)), obs, ft] + [vals.get(i, "") for i in ids])
    return buf.getvalue()


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
