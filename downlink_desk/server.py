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

from . import __version__, logchain
from . import db as dbm
from .config import find_config

WEB = Path(__file__).resolve().parent.parent / "web"
STARTED = dbm.now_ms()


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = dbm.connect(cfg.db_path)
        self.lock = threading.Lock()
        self.spec = cfg.channels()
        self._pass_cache = (0, None)
        self._fetching = False
        if not self.conn.execute("SELECT 1 FROM log LIMIT 1").fetchone():
            s = cfg["satellite"]
            logchain.append(self.conn, "system", {"norad": s["norad"]},
                            f"Console commissioned for {s['name']} (NORAD {s['norad']}).", "system")

    # ---------- reads ----------
    def config(self):
        c = self.cfg
        return {"version": __version__, "satellite": c["satellite"], "station": c["station"],
                "poll_minutes": c["satnogs"]["poll_minutes"], "stale_hours": c["alerts"]["stale_hours"],
                "write_protected": bool(c["server"]["write_passcode"]), "spec": self.spec,
                "commissioned_ms": self.conn.execute("SELECT MIN(t_ms) m FROM log").fetchone()["m"]}

    def telemetry(self, since):
        with self.lock:
            pts = self.conn.execute("SELECT ts_ms,channel,value FROM points WHERE ts_ms>? ORDER BY ts_ms LIMIT 50000",
                                    (since,)).fetchall()
            frs = self.conn.execute("SELECT id,ts_ms,observer,frame_type,decoded_ok FROM frames WHERE ts_ms>? ORDER BY ts_ms LIMIT 20000",
                                    (since,)).fetchall()
        return {"now": dbm.now_ms(), "points": [[r[0], r[1], r[2]] for r in pts],
                "frames": [[r[0], r[1], r[2], r[3], r[4]] for r in frs]}

    def frames(self, limit, before):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id,ts_ms,observer,station_id,app_source,frame_type,decoded_ok,fields_json,frame_hex FROM frames"
                " WHERE ts_ms<? ORDER BY ts_ms DESC LIMIT ?", (before, limit)).fetchall()
        return [{"id": r["id"], "t": r["ts_ms"], "observer": r["observer"], "station_id": r["station_id"],
                 "app_source": r["app_source"], "type": r["frame_type"], "ok": bool(r["decoded_ok"]),
                 "fields": json.loads(r["fields_json"] or "{}"), "hex": r["frame_hex"]} for r in rows]

    def health(self):
        with self.lock:
            g = lambda k, d=None: dbm.meta_get(self.conn, k, d)
            fetches = [dict(r) for r in self.conn.execute("SELECT * FROM fetches ORDER BY id DESC LIMIT 60")]
            counts = self.conn.execute("SELECT COUNT(*) n, MAX(ts_ms) last, SUM(decoded_ok) ok FROM frames").fetchone()
        try:
            size = self.cfg.db_path.stat().st_size
        except OSError:
            size = 0
        tle = g("tle") or {}
        return {"now": dbm.now_ms(), "server_started_ms": STARTED, "last_attempt_ms": g("last_attempt_ms"),
                "last_ok_ms": g("last_ok_ms"), "auth_error": bool(g("auth_error", False)),
                "token_present": bool(self.cfg.token), "fetches": fetches,
                "frames_total": counts["n"], "frames_decoded": counts["ok"] or 0, "last_frame_ms": counts["last"],
                "db_bytes": size, "tle_epoch_ms": tle.get("epoch_ms"), "satellite": g("satellite") or {},
                "fetching": self._fetching}

    def passes(self, hours):
        cached_t, cached = self._pass_cache
        if cached and time.time() - cached_t < 600 and cached["hours"] >= hours:
            return cached
        with self.lock:
            tle = dbm.meta_get(self.conn, "tle")
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
        self._pass_cache = (time.time(), out)
        return out

    def fetch_now(self):
        if self._fetching:
            return False, "A fetch is already running."
        last = dbm.meta_get(self.conn, "last_attempt_ms", 0) or 0
        if dbm.now_ms() - last < 120_000:
            return False, "Fetched less than two minutes ago. SatNOGS is volunteer-run, so give it a moment."

        def work():
            self._fetching = True
            try:
                from .fetch import run
                run(self.cfg, force=True, log=lambda m: None)
            finally:
                self._fetching = False
        threading.Thread(target=work, daemon=True).start()
        return True, "Fetching from SatNOGS now."


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
                if p == "/api/config":
                    return self._json(app.config())
                if p == "/api/telemetry":
                    return self._json(app.telemetry(int(q.get("since", dbm.now_ms() - 7 * 86_400_000))))
                if p == "/api/frames":
                    return self._json(app.frames(min(int(q.get("limit", 100)), 500), int(q.get("before", 2**53))))
                if p == "/api/health":
                    return self._json(app.health())
                if p == "/api/passes":
                    return self._json(app.passes(min(float(q.get("hours", 36)), 72)))
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
                    return self._text(export_csv(app), "text/csv", "telemetry.csv")
                if p == "/api/export/log.json":
                    with app.lock:
                        data = {"satellite": app.cfg["satellite"], "exported_ms": dbm.now_ms(),
                                "verify": logchain.verify(app.conn), "entries": logchain.entries(app.conn)}
                    return self._text(json.dumps(data, indent=2), "application/json", "audit-log.json")
                return self._static(p)
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e)}, 400)

        def do_POST(self):
            p = urlparse(self.path).path
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
                if p == "/api/fetch":
                    ok, msg = app.fetch_now()
                    return self._json({"ok": ok, "message": msg}, 202 if ok else 429)
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

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


def export_csv(app):
    ids = [c["id"] for c in app.spec["channels"]]
    with app.lock:
        rows = app.conn.execute("SELECT f.ts_ms, f.observer, f.frame_type, p.channel, p.value FROM frames f"
                                " JOIN points p ON p.frame_id=f.id ORDER BY f.ts_ms").fetchall()
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
