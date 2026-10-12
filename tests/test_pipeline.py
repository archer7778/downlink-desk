"""End-to-end test of the fetch → decode → store → API pipeline against the mock.

    python -m pytest tests -q      (or: python tests/test_pipeline.py)
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["DOWNLINK_DESK_MIN_GAP"] = "0"

import mock_satnogs  # noqa: E402
from downlink_desk import db as dbm, fetch, logchain  # noqa: E402
from downlink_desk.config import Config  # noqa: E402


def make_cfg(tmp, port):
    (tmp / "secrets").mkdir()
    (tmp / "secrets" / "satnogs_token").write_text("test-token\n")
    shutil.copytree(ROOT / "channels", tmp / "channels")
    text = (ROOT / "config.example.toml").read_text()
    text = text.replace('"https://db.satnogs.org/api"', f'"http://127.0.0.1:{port}/api"')
    text = text.replace('"https://network.satnogs.org/api"', f'"http://127.0.0.1:{port}/api"')
    text = text.replace('"https://services.swpc.noaa.gov"', f'"http://127.0.0.1:{port}"')
    text = text.replace("lat = 0.0", "lat = 10.82").replace("lon = 0.0", "lon = 106.63")
    (tmp / "config.toml").write_text(text)
    return Config(tmp / "config.toml")


def test_pipeline():
    httpd, state, frames = mock_satnogs.serve(8766)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = make_cfg(tmp, 8766)
        msgs = []
        assert fetch.run(cfg, force=True, log=msgs.append) == 0, msgs
        conn = dbm.open_db(cfg)
        n = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        ok = conn.execute("SELECT COUNT(*) FROM frames WHERE decoded_ok=1").fetchone()[0]
        pts = conn.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        types = dict(conn.execute("SELECT frame_type, COUNT(*) FROM frames GROUP BY frame_type").fetchall())
        print("frames", n, "decoded", ok, "points", pts, types)
        assert n > 0 and ok > 0 and pts > 0
        assert "UHF beacon" in types and "Unrecognised" in types
        # battery temperature from decikelvin should land near 20 °C
        bt = conn.execute("SELECT AVG(value) FROM points WHERE channel='bat_t'").fetchone()[0]
        assert bt is None or 10 < bt < 30, bt
        # a second fetch straight away is refused politely
        assert fetch.run(cfg, force=False, log=msgs.append) == 0 and "recently" in msgs[-1]
        # a station uploads an older pass late: the next fetch still collects it
        newest = conn.execute("SELECT MAX(ts_ms) FROM frames").fetchone()[0]
        from datetime import datetime, timezone
        late_ts = datetime.fromtimestamp((newest - 2 * 3600_000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        late = {**frames[0], "timestamp": late_ts, "observer": "Late-Uploader"}
        frames.append(late)
        frames.sort(key=lambda f: f["timestamp"], reverse=True)
        dbm.smeta_set(conn, 60237, "last_attempt_ms", 0)
        assert fetch.run(cfg, force=True, log=msgs.append) == 0
        assert conn.execute("SELECT COUNT(*) FROM frames WHERE observer='Late-Uploader'").fetchone()[0] == 1, msgs[-3:]
        n += 1
        frames.remove(late)
        # the signed log detects tampering
        logchain.append(conn, "note", {}, "First note", "AM")
        logchain.append(conn, "note", {}, "Second note", "AM")
        assert logchain.verify(conn)["ok"]
        conn.execute("UPDATE log SET msg='Edited' WHERE seq=1")
        conn.commit()
        v = logchain.verify(conn)
        assert not v["ok"] and v["problems"][0]["seq"] == 1
        # pass prediction runs on the mock TLE
        from downlink_desk.orbit import passes
        tle = dbm.smeta_get(conn, 60237, "tle")
        p = passes(tle["tle1"], tle["tle2"], dbm.now_ms(), 24, 10.82, 106.63)
        print("passes next 24h:", len(p))
        assert len(p) >= 2
        # scheduled observations were loaded and scored against the frames
        from downlink_desk.analysis import observation_report, silent_streak
        rep = observation_report(conn, cfg, 0, dbm.now_ms(), 60237)
        st = {}
        for o in rep:
            st[o["status"]] = st.get(o["status"], 0) + 1
        print("observations:", st)
        assert st.get("heard") and st.get("scheduled") and st.get("nothing")
        # frames outside stale orbit data's range get positions once fresh orbit data arrives
        from downlink_desk import baseline
        tle0 = dbm.smeta_get(conn, 60237, "tle")
        dbm.smeta_set(conn, 60237, "tle", {**tle0, "epoch_ms": tle0["epoch_ms"] - 100 * 86_400_000})
        conn.execute("UPDATE frames SET phase_min=NULL WHERE norad=60237"); conn.commit()
        baseline.fill_phases(conn, 60237)
        assert conn.execute("SELECT COUNT(*) FROM frames WHERE norad=60237 AND phase_min=-2").fetchone()[0] > 0
        dbm.smeta_set(conn, 60237, "tle", tle0)
        baseline.fill_phases(conn, 60237)
        assert conn.execute("SELECT COUNT(*) FROM frames WHERE norad=60237 AND phase_min=-2").fetchone()[0] == 0
        conn.execute("UPDATE frames SET sunlit=NULL, sub_lat=NULL, phase_min=NULL WHERE norad=60237"); conn.commit()
        baseline.fill_phases(conn, 60237)
        assert conn.execute("SELECT COUNT(*) FROM frames WHERE norad=60237 AND sunlit IS NULL").fetchone()[0] == 0
        # orbit context stored for frames
        sl = conn.execute("SELECT COUNT(*) FROM frames WHERE sunlit IS NOT NULL").fetchone()[0]
        assert sl == n, (sl, n)
        # silent satellite: three empty high observations after the last heard one form a streak
        fake = [{"id": 1, "end": 1, "status": "heard", "max_alt": 50},
                {"id": 2, "end": 2, "status": "nothing", "max_alt": 40},
                {"id": 3, "end": 3, "status": "nothing", "max_alt": 5},     # too low to count
                {"id": 4, "end": 4, "status": "nothing", "max_alt": 30},
                {"id": 5, "end": 5, "status": "nothing", "max_alt": 60},
                {"id": 6, "end": 6, "status": "awaiting", "max_alt": 60}]   # still within the upload grace
        assert [o["id"] for o in silent_streak(fake, cfg)] == [2, 4, 5]
        # eclipse model: a 560 km orbit spends roughly 30-36 minutes per orbit in shadow
        from downlink_desk.orbit import eclipses
        ec = eclipses(tle["tle1"], tle["tle2"], dbm.now_ms(), dbm.now_ms() + 6 * 3600_000)
        assert ec and all(25 < (b - a) / 60000 < 40 for a, b in ec[1:-1]), ec
        # phone alerts: a restart in newly fetched frames reaches the ntfy server once
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from downlink_desk import notify
        got = []

        class N(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                got.append((self.path, self.headers.get("Title"), self.rfile.read(int(self.headers["Content-Length"])).decode()))
                self.send_response(200)
                self.end_headers()
        ns = HTTPServer(("127.0.0.1", 8767), N)
        threading.Thread(target=ns.serve_forever, daemon=True).start()
        cfg.data["notify"] = {"ntfy_topic": "dd-test", "ntfy_server": "http://127.0.0.1:8767", "console_url": ""}
        t0 = dbm.now_ms()
        conn.execute("INSERT INTO frames(id,ts_ms,frame_hex,decoded_ok,fetched_ms,norad) VALUES(900001,?,'00',1,?,60237)", (t0, t0))
        conn.execute("INSERT INTO points(ts_ms,channel,value,frame_id,norad) VALUES(?, 'uhf_boots', 999, 900001, 60237)", (t0,))
        conn.commit()
        notify.after_fetch(conn, cfg, cfg.channels(), [900001], log=lambda m: None)
        notify.after_fetch(conn, cfg, cfg.channels(), [900001], log=lambda m: None)   # same event: no repeat
        restarts = [g for g in got if "restart" in g[1]]
        print("phone alerts:", got)
        assert len(restarts) == 1 and restarts[0][0] == "/dd-test"
        ns.shutdown()
        cfg.data["notify"]["ntfy_topic"] = ""

        # ---- several satellites: the catalog, tracking a second one, automatic channels ----
        from downlink_desk import catalog
        from downlink_desk.server import App
        catalog.refresh(conn, cfg, log=lambda m: None, force=True)
        cat = catalog.entries(conn, cfg)
        names = [e["name"] for e in cat]
        print("catalog:", names[:10], "...", len(cat))
        assert names[0] == "GRBBeta" and cat[0]["featured"] and cat[0]["tracked"]
        assert "TESTSAT" in names and "GRBAlpha" not in names and "NODECODER" not in names
        app = App(cfg)
        app.track(99999, "AF")
        assert fetch.run(cfg, force=True, log=msgs.append, only=99999) == 0, msgs
        n2 = conn.execute("SELECT COUNT(*) FROM frames WHERE norad=99999").fetchone()[0]
        spec2 = app.spec(app.sat({"sat": "99999"}))
        print("TESTSAT frames", n2, "auto channels", len(spec2["channels"]), [c["id"] for c in spec2["channels"]][:6])
        assert n2 > 0 and spec2.get("auto") and len(spec2["channels"]) > 10
        assert all(not c["id"].startswith("csp_hdr") for c in spec2["channels"])
        fleet = {f["norad"]: f for f in app.fleet()}
        assert fleet[60237]["bio"] and fleet[99999]["frames_24h"] >= 0
        # GRBBeta's own data is untouched by the second satellite
        assert conn.execute("SELECT COUNT(*) FROM frames WHERE norad=60237").fetchone()[0] == n + 1
        app.untrack(99999, "AF")
        assert [s["norad"] for s in dbm.satellites(conn)] == [60237]
        assert logchain.verify(conn)["problems"][0]["seq"] == 1   # only the edit we made on purpose
        print("requests to mock:", state["requests"])
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp)


def test_migrates_v01_database():
    """A database created by the very first release (one satellite, no orbit columns, no
    observations or satellites tables) opens cleanly, keeps its data and works with today's code."""
    import sqlite3
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = make_cfg(tmp, 8766)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        old = sqlite3.connect(cfg.db_path)
        old.executescript((ROOT / "tests" / "schema_v01.sql").read_text())
        now = dbm.now_ms()
        for i in range(30):
            t = now - i * 3600_000
            old.execute("INSERT INTO frames(id,ts_ms,observer,frame_hex,frame_type,decoded_ok,fields_json,fetched_ms) "
                        "VALUES(?,?,?,?,?,1,'{}',?)", (i + 1, t, "IV3DXW-JN65qq", "ab%02x" % i, "UHF beacon", t))
            old.execute("INSERT INTO points(ts_ms,channel,value,frame_id) VALUES(?,?,?,?)", (t, "bat_t", 15.0 + i % 3, i + 1))
        old.execute("INSERT INTO meta(key,value) VALUES('tle',?)", (json.dumps({"tle1": mock_satnogs.TLE[0],
                    "tle2": mock_satnogs.TLE[1], "epoch_ms": now}),))
        old.execute("INSERT INTO meta(key,value) VALUES('last_ok_ms',?)", (str(now),))
        old.commit()
        old.close()
        conn = dbm.open_db(cfg)
        assert conn.execute("SELECT COUNT(*) FROM frames WHERE norad=60237").fetchone()[0] == 30
        assert conn.execute("SELECT COUNT(*) FROM points WHERE norad=60237").fetchone()[0] == 30
        assert dbm.satellites(conn)[0]["norad"] == 60237
        assert dbm.smeta_get(conn, 60237, "tle") and dbm.meta_get(conn, "tle") is None
        from downlink_desk import baseline
        from downlink_desk.server import App
        baseline.fill_phases(conn, 60237)
        assert conn.execute("SELECT COUNT(*) FROM frames WHERE phase_min IS NULL OR sunlit IS NULL").fetchone()[0] == 0
        app = App(cfg)
        sat = app.sat({})
        for fn in (app.health, app.model, app.battery, app.restarts, app.ground_track):
            fn(sat)
        app.observations(sat, 3)
        app.summary()
        print("migrated a v0.1 database: 30 frames, every view works")
    finally:
        shutil.rmtree(tmp)


def test_model_spaceweather_summary_stream():
    """The learned model spots a drift that limits miss; space weather, the morning summary
    and the live stream all work end to end."""
    import math
    import random
    import time
    import urllib.request
    from downlink_desk import baseline, spacewx, summary
    from downlink_desk.orbit import in_saa, sun_phases
    from downlink_desk.server import App, make_handler
    from http.server import ThreadingHTTPServer
    httpd, _, _ = mock_satnogs.serve(8769)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = make_cfg(tmp, 8769)
        conn = dbm.open_db(cfg)
        dbm.smeta_set(conn, 60237, "tle", {"tle1": mock_satnogs.TLE[0], "tle2": mock_satnogs.TLE[1],
                                           "epoch_ms": dbm.now_ms()})
        # orbit phase from the TLE: ~95 min period, phase in range, shadow ~30-36 min
        now = dbm.now_ms()
        ph = sun_phases(mock_satnogs.TLE[0], mock_satnogs.TLE[1], [now, now + 600_000, now - 5 * 86_400_000])
        print("sun phases:", ph)
        assert all(p is not None and 0 <= p <= 100 and 25 < sh < 40 for p, sh in ph.values()), ph
        assert in_saa(-25, -45) and not in_saa(50, 10) and not in_saa(16, 108)
        # 20 days of synthetic battery temperatures following the orbit, then a +3 °C drift
        rnd = random.Random(3)
        P = 95.6
        fid = 0
        for i in range(20 * 48):
            t = now - 20 * 86_400_000 + i * 1_800_000 + rnd.randint(0, 600_000)
            phase = rnd.uniform(0, P)
            shadow = 33 + 3 * math.sin(i / 200)
            sunlit = 1 if phase < P - shadow else 0
            base_t = 12 + 6 * math.sin(2 * math.pi * phase / P) + 0.4 * (shadow - 33) + rnd.gauss(0, .4)
            drift = 3.0 if t > now - 2 * 86_400_000 else 0.0
            fid += 1
            conn.execute("INSERT INTO frames(id,ts_ms,frame_hex,decoded_ok,fetched_ms,norad,sunlit,phase_min,shadow_min,sub_lat,sub_lon)"
                         " VALUES(?,?,?,1,?,60237,?,?,?,0,0)", (fid, t, "%04x" % fid, t, sunlit, phase, shadow))
            conn.executemany("INSERT INTO points(ts_ms,channel,value,frame_id,norad) VALUES(?,?,?,?,60237)",
                             [(t, "bat_t", base_t + drift, fid), (t, "psu_t", base_t + 6, fid)])
        conn.commit()
        m = baseline.compute(conn, cfg.channels(), 60237)
        by = {c["id"]: c for c in m["channels"]}
        print("model:", {k: (v.get("status"), round(v.get("shift", 0), 2), round(v.get("sigma", 0), 2)) for k, v in by.items()
                         if k in ("bat_t", "psu_t")})
        assert by["bat_t"]["status"] == "drift" and 2.4 < by["bat_t"]["shift"] < 3.6
        assert by["psu_t"]["status"] == "normal" and abs(by["psu_t"]["shift"]) < .5
        assert by["bat_t"]["sigma"] < 0.5   # the orbit swing (±6 °C) is explained, not counted as noise
        lo, hi = by["bat_t"]["shift_range"]
        assert lo < 3.0 < hi or abs(by["bat_t"]["shift"] - 3.0) < 0.3, by["bat_t"]["shift_range"]
        ch = by["bat_t"].get("change")
        print("change:", ch and round((now - ch["began"]) / 3600_000, 1), "h ago; seasonal", by["bat_t"]["seasonal_per_min"])
        assert ch and abs((now - ch["began"]) - 2 * 86_400_000) < 8 * 3600_000 and ch["direction"] == "up"
        assert abs(by["bat_t"]["seasonal_per_min"] - 0.4) < 0.15
        assert "uhf_digi" not in by and "uhf_boots" not in by
        assert by["bat_t"]["n_unusual"] < 3   # a drift is reported once as a drift, not as dozens of oddities
        # a channel that spikes routinely (like discharge current when the radio transmits) isn't "unusual"
        spiky = [(r["id"], r["ts_ms"]) for r in conn.execute("SELECT id, ts_ms FROM frames WHERE id<=? ORDER BY id", (20 * 48,))]
        conn.executemany("INSERT INTO points(ts_ms,channel,value,frame_id,norad) VALUES(?,?,?,?,60237)",
                         [(t, "bat_dis", 535 + (400 if i % 12 == 0 else 0), fid) for i, (fid, t) in enumerate(spiky)])
        conn.commit()
        m2 = {c["id"]: c for c in baseline.compute(conn, cfg.channels(), 60237)["channels"]}
        print("spiky channel:", m2["bat_dis"]["status"], "unusual", m2["bat_dis"]["n_unusual"])
        assert m2["bat_dis"]["n_unusual"] == 0 and m2["bat_dis"]["status"] == "normal"
        pr = baseline.profile(conn, cfg.channels(), 60237, "bat_t")
        assert len([b for b in pr["bins"] if b]) > 15 and pr["recent"] and pr["base"]
        # space weather from the NOAA look-alike
        spacewx.refresh(conn, cfg, log=print, force=True)
        sw = spacewx.view(conn, now - 86_400_000)
        print("space weather:", sw["kp_peak_24h"], sw["xray_peak_24h"]["class"], sw["f107"])
        assert sw["kp_peak_24h"]["g"] == "G3" and sw["xray_peak_24h"]["class"] == "M2.4" and sw["f107"]["value"] == 120
        assert spacewx.parse_kp([["time_tag", "Kp"], ["2026-10-10 15:00:00.000", "7.00"]])[0][1] == 7.0
        # morning summary
        cfg.data["notify"]["timezone"] = "Asia/Ho_Chi_Minh"
        s = summary.build(conn, cfg)
        print(s["title"])
        print(s["text"])
        assert "Battery temperature" in s["text"] and "G3" in s["text"] and "drift" in s["flags"]
        from http.server import BaseHTTPRequestHandler, HTTPServer
        got = []

        class N(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                got.append(self.headers.get("Title"))
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
        ns = HTTPServer(("127.0.0.1", 8770), N)
        threading.Thread(target=ns.serve_forever, daemon=True).start()
        cfg.data["notify"].update(ntfy_topic="dd-test", ntfy_server="http://127.0.0.1:8770", summary_hour=0)
        assert summary.maybe_send(conn, cfg, log=lambda m: None)
        assert not summary.maybe_send(conn, cfg, log=lambda m: None)   # once a day
        from downlink_desk import notify
        notify.model_changes(conn, cfg, {"norad": 60237, "name": "GRBBeta"}, m, log=lambda m: None)
        notify.model_changes(conn, cfg, {"norad": 60237, "name": "GRBBeta"}, m, log=lambda m: None)
        print("pushed:", got)
        assert len([g for g in got if "summary" in g]) == 1 and len([g for g in got if "drifting" in g]) == 1
        ns.shutdown()
        # live stream: a frame stored by the fetch job reaches an open console within seconds
        app = App(cfg)
        web = ThreadingHTTPServer(("127.0.0.1", 8771), make_handler(app))
        threading.Thread(target=web.serve_forever, daemon=True).start()
        r = urllib.request.urlopen("http://127.0.0.1:8771/api/stream", timeout=15)
        assert r.readline().startswith(b"event: hello")
        conn.execute("INSERT INTO frames(id,ts_ms,frame_hex,decoded_ok,fetched_ms,norad,observer,frame_type,sub_lat,sub_lon)"
                     " VALUES(999999,?,'beef',1,?,60237,'Test-Station','UHF beacon',-25,-45)", (now, now))
        conn.commit()
        t0 = time.time()
        lines = []
        while time.time() - t0 < 10:
            ln = r.readline()
            lines.append(ln)
            if ln.startswith(b"data:") and b"999999" in ln:
                break
        ev = json.loads(lines[-1][5:])
        print("streamed:", ev, f"after {time.time() - t0:.1f}s")
        assert ev["observer"] == "Test-Station" and ev["saa"] is True
        r.close()
        # incremental telemetry: only frames stored after the given id
        j = json.loads(urllib.request.urlopen("http://127.0.0.1:8771/api/telemetry?after_id=999998", timeout=15).read())
        assert [f[0] for f in j["frames"]] == [999999] and j["max_id"] >= 999999
        j = json.loads(urllib.request.urlopen("http://127.0.0.1:8771/api/telemetry?after_id=999999", timeout=15).read())
        assert j["frames"] == [] and j["channels"] == {}
        full = json.loads(urllib.request.urlopen(f"http://127.0.0.1:8771/api/telemetry?since={now - 30 * 86_400_000}", timeout=30).read())
        assert sum(len(v) for v in full["channels"].values()) == conn.execute(
            "SELECT COUNT(*) FROM points WHERE norad=60237 AND ts_ms>?", (now - 30 * 86_400_000,)).fetchone()[0]   # nothing cut off
        # compressed when the browser asks, and the page itself revalidates with a 304
        r2 = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8771/api/model", headers={"Accept-Encoding": "gzip"}))
        assert r2.headers.get("Content-Encoding") == "gzip"
        r3 = urllib.request.urlopen("http://127.0.0.1:8771/")
        try:
            urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8771/", headers={"If-None-Match": r3.headers["ETag"]}))
            raise AssertionError("expected 304")
        except urllib.error.HTTPError as e:
            assert e.code == 304
        # the public demo: a read-only static snapshot of the same console
        from downlink_desk import export_static
        export_static.export(cfg, tmp / "site", log=lambda m: None)
        page = (tmp / "site" / "index.html").read_text()
        assert "window.DD_STATIC" in page and (tmp / "site" / "training.html").exists()
        for f in ("config", "telemetry", "health", "model", "battery", "summary", "now", "frames", "log", "verify"):
            json.loads((tmp / "site" / "api" / f"{f}.json").read_text())
        assert json.loads((tmp / "site" / "api" / "health.json").read_text())["storage"] is None
        assert (tmp / "site" / "api" / "profile" / "bat_t.json").exists()
        # changes only from the console's own page: JSON, and no foreign Origin
        def post(headers, body=b'{"kind":"note","msg":"hello","cs":"T"}'):
            try:
                return urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8771/api/log", data=body,
                                                                     headers=headers, method="POST")).status
            except urllib.error.HTTPError as e:
                return e.code
        assert post({"Content-Type": "text/plain"}) == 403                      # a cross-site form
        assert post({"Content-Type": "application/json", "Origin": "https://evil.example"}) == 403
        assert post({"Content-Type": "application/json", "Origin": "http://127.0.0.1:8771"}) == 201
        assert post({"Content-Type": "application/json"}, b"[1,2,3]") == 400                    # not an object
        assert post({"Content-Type": "application/json", "Content-Length": "-1"}, b"") in (400, 411)
        # hostile or broken inputs
        from downlink_desk.server import _cell, _finite
        assert _finite({"a": float("nan"), "b": [1.0, float("inf")]}) == {"a": None, "b": [1.0, None]}
        assert _cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _cell("Piszkesteto") == "Piszkesteto"
        assert spacewx.parse_kp([{"time_tag": "2026-10-10T00:00:00", "Kp": "NaN"},
                                 {"time_tag": "2026-10-10T03:00:00", "Kp": 99}]) == []
        lim = json.loads(urllib.request.urlopen("http://127.0.0.1:8771/api/frames?limit=-5", timeout=15).read())
        assert len(lim) == 1                                                                     # no "LIMIT -5" = everything
        csv_text = urllib.request.urlopen("http://127.0.0.1:8771/api/export/telemetry.csv", timeout=30).read().decode()
        assert csv_text.startswith("utc,observer,frame_type") and csv_text.count("\n") > 900
        tr = json.loads(urllib.request.urlopen("http://127.0.0.1:8771/api/track", timeout=30).read())
        assert len(tr["points"]) > 250 and all(-90 <= p[1] <= 90 and -180 <= p[2] <= 180 for p in tr["points"])
        assert (tmp / "site" / "api" / "track.json").exists() and (tmp / "site" / "world.json").exists()
        for path in ("/api/model", "/api/profile?ch=bat_t", "/api/spaceweather", "/api/summary"):
            j = json.loads(urllib.request.urlopen("http://127.0.0.1:8771" + path, timeout=30).read())
            assert "error" not in j, (path, j)
        web.shutdown()
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp)


def test_late_uploads():
    """A pass uploaded hours late sorts behind frames we already have; the fetch must still get it."""
    from datetime import datetime, timezone
    httpd, state, frames = mock_satnogs.serve(8781, page_size=3)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = make_cfg(tmp, 8781)
        cfg.data["satnogs"].update(backfill_pages=4, history_pages_per_fetch=0)
        assert fetch.run(cfg, force=True, log=lambda m: None) == 0
        conn = dbm.open_db(cfg)
        newest = conn.execute("SELECT MAX(ts_ms) FROM frames").fetchone()[0]
        ts = lambda f: datetime.fromisoformat(f["timestamp"].replace("Z", "+00:00")).timestamp() * 1000
        # the late frame sits behind the first page (3 newer frames) but within the 6 h look-back
        for hours in [x / 4 for x in range(2, 24)]:
            late_ms = newest - hours * 3600_000
            newer = sum(1 for f in frames if ts(f) > late_ms)
            if 4 <= newer <= 9:
                break
        late = {**frames[0], "observer": "Late-Uploader",
                "timestamp": datetime.fromtimestamp(late_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        frames.append(late)
        frames.sort(key=lambda f: f["timestamp"], reverse=True)
        dbm.smeta_set(conn, 60237, "last_attempt_ms", 0)
        assert fetch.run(cfg, force=True, log=lambda m: None) == 0
        got = conn.execute("SELECT COUNT(*) FROM frames WHERE observer='Late-Uploader'").fetchone()[0]
        print(f"late upload {hours} h behind the newest frame, behind {newer} newer frames: stored {got}")
        assert got == 1
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp)


def test_forensics_and_silence():
    """Restarts timed from uptime counters land where they really happened, a cluster over the
    South Atlantic Anomaly is called out, and misses by reliable stations count for more."""
    from downlink_desk import forensics
    from downlink_desk.analysis import silence_probability
    from downlink_desk.orbit import in_saa, state
    from sgp4.api import Satrec
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = make_cfg(tmp, 8774)
        conn = dbm.open_db(cfg)
        dbm.smeta_set(conn, 60237, "tle", {"tle1": mock_satnogs.TLE[0], "tle2": mock_satnogs.TLE[1], "epoch_ms": dbm.now_ms()})
        sat = Satrec.twoline2rv(*mock_satnogs.TLE)
        now = dbm.now_ms()
        # pick restart moments: four over the SAA, one elsewhere
        saa_times, other = [], None
        t = now - 9 * 86_400_000
        while len(saa_times) < 4 or other is None:
            st = state(sat, t)
            deep = ((st["lat"] + 25) / 22) ** 2 + ((st["lon"] + 45) / 60) ** 2 < 0.25   # well inside, not at the edge
            if deep and (not saa_times or t - saa_times[-1] > 1.5 * 86_400_000):
                saa_times.append(t)
            elif other is None and t > now - 3 * 86_400_000 and not in_saa(st["lat"], st["lon"]) and abs(st["lat"]) > 40:
                other = t
            t += 60_000
        boots = sorted(saa_times[:4] + [other])
        fid = 0
        for k in range(10 * 12):   # an uptime reading every 2 hours, in hours, like GRBBeta's
            ts = now - 10 * 86_400_000 + k * 7_200_000 + 1234
            last = max([b for b in boots if b <= ts], default=now - 60 * 86_400_000)
            fid += 1
            conn.execute("INSERT INTO frames(id,ts_ms,frame_hex,decoded_ok,fetched_ms,norad) VALUES(?,?,?,1,?,60237)",
                         (fid, ts, "u%04x" % fid, ts))
            conn.execute("INSERT INTO points(ts_ms,channel,value,frame_id,norad) VALUES(?,?,?,?,60237)",
                         (ts, "uhf_up", round((ts - last) / 3_600_000, 1), fid))
        conn.commit()
        r = forensics.restarts(conn, cfg.channels(), 60237, 30)
        got = [f for f in r["restarts"] if f["exact"]]
        print("restarts:", len(got), r["stats"], "|", forensics.summary(r))
        assert len(got) == 5
        assert all(min(abs(f["t"] - b) for b in boots) < 6 * 60_000 for f in got)   # within the 0.1 h rounding
        assert r["stats"]["in_saa"] == 4 and r["stats"]["saa_p_value"] < 0.05
        # frames only count for the station that was scheduled; others' frames still prove it's alive
        from downlink_desk.analysis import observation_report, silence
        t0 = now - 5 * 3600_000
        conn.execute("INSERT INTO observations(id,start_ms,end_ms,station_id,station_name,lat,lng,max_alt,mode,fetched_ms,norad)"
                     " VALUES(1,?,?,7,'Quiet Station',0,0,45,'',?,60237)", (t0, t0 + 600_000, now))
        conn.execute("INSERT INTO frames(ts_ms,frame_hex,decoded_ok,fetched_ms,norad,observer) VALUES(?,'aa',1,?,60237,'Other-AB12cd')",
                     (t0 + 60_000, now))
        conn.commit()
        o = [x for x in observation_report(conn, cfg, now - 86_400_000, now, 60237) if x["id"] == 1][0]
        assert o["status"] == "nothing" and o["frames"] == 0 and o["others"] == 1, o
        assert silence(conn, cfg, 60237, now)["misses"] == 0   # it was alive then, so that's no evidence of silence
        # silence: the same three misses mean very different things from good and flaky stations
        def obs(station, status, i):
            return {"id": i, "station": station, "status": status, "max_alt": 40, "end": i}
        good = [obs("Reliable", "heard", i) for i in range(20)]
        flaky = [obs("Flaky", "heard" if i % 6 == 0 else "nothing", 100 + i) for i in range(18)]
        a = silence_probability(good + flaky + [obs("Reliable", "nothing", 200 + i) for i in range(3)], cfg)
        b = silence_probability(good + flaky + [obs("Flaky", "nothing", 200 + i) for i in range(3)], cfg)
        print("silence: reliable station missed 3 ->", a["probability"], "| flaky station missed 3 ->", b["probability"])
        assert a["probability"] > 0.8 and b["probability"] < 0.2
    finally:
        shutil.rmtree(tmp)


def test_history_fill():
    """A fresh install only takes the newest page; history then fills in from SatNOGS by time window."""
    httpd, state, frames = mock_satnogs.serve(8773)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = make_cfg(tmp, 8773)
        cfg.data["satnogs"].update(backfill_pages=1, history_pages_per_fetch=0)
        msgs = []
        assert fetch.run(cfg, force=True, log=msgs.append) == 0
        conn = dbm.open_db(cfg)
        n1 = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        assert fetch.history(cfg, 21, log=msgs.append) == 0, msgs
        n2, oldest = conn.execute("SELECT COUNT(*), MIN(ts_ms) FROM frames").fetchone()
        print("history:", n1, "->", n2, "of", len(frames), "|", [m for m in msgs if "History" in m][-1])
        assert n1 == 25 and n2 == len(frames)
        assert any(w[0] and w[1] for w in state["windows"])
        assert dbm.smeta_get(conn, 60237, "history_done_ms")
        assert conn.execute("SELECT COUNT(*) FROM frames WHERE phase_min IS NULL").fetchone()[0] == 0
        # once complete, regular fetches don't ask for history again
        before = len(state["windows"])
        cfg.data["satnogs"]["history_pages_per_fetch"] = 8
        fetch.run(cfg, force=True, log=msgs.append)
        assert all(not (w[0] and w[1]) for w in state["windows"][before:])
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp)


def test_battery_health():
    """Synthetic 2S lithium-ion pack: the check should recover the energy balance, eclipse depth
    and internal resistance that were put into the data."""
    import random
    from downlink_desk import battery
    from downlink_desk.orbit import period_min
    from sgp4.api import Satrec
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = make_cfg(tmp, 8772)
        conn = dbm.open_db(cfg)
        dbm.smeta_set(conn, 60237, "tle", {"tle1": mock_satnogs.TLE[0], "tle2": mock_satnogs.TLE[1], "epoch_ms": dbm.now_ms()})
        spec = cfg.channels()
        P = period_min(Satrec.twoline2rv(*mock_satnogs.TLE))
        from downlink_desk.orbit import shadow_minutes
        sh = shadow_minutes(*mock_satnogs.TLE, dbm.now_ms())
        sunset, cap, R25 = P - sh, 20 / 7.4 * 1000, 0.10
        use = sh * 540 / 60   # average draw in shadow: 450 mA plus transmit bursts (30% of the time, +300 mA)
        rnd = random.Random(5)
        now = dbm.now_ms()
        for i in range(14 * 72 * 3):
            # passes of three frames 40 s apart, every 20 minutes; the radio transmits on some frames
            t = now - 14 * 86_400_000 + (i // 3) * 1_200_000 + (i % 3) * 40_000
            if i % 3 == 0:
                ph0 = rnd.uniform(0, P)
            ph = (ph0 + (i % 3) * 40 / 60) % P
            if ph >= sunset:
                soc, ic, idc = 1 - (ph - sunset) * 540 / 60 / cap, 0.0, 450.0
            else:
                soc, ic, idc = min(1.0, 1 - use / cap + ph * 360 / 60 / cap), 900.0, 450.0
            ic = max(0.0, ic + rnd.gauss(0, 50)) if ic else 0.0
            idc += rnd.gauss(0, 50) + (300 if rnd.random() < .3 else 0)
            temp = 12 + 6 * (1 if ph < sunset else -1) + rnd.gauss(0, 1)
            # two cells on a real lithium-ion voltage curve, sagging with a resistance that rises in the cold
            v = 2 * battery.ocv_cell(soc) + R25 * battery._rt(temp) * (ic - idc) / 1000 + rnd.gauss(0, .003)
            fid = 700000 + i
            conn.execute("INSERT INTO frames(id,ts_ms,frame_hex,decoded_ok,fetched_ms,norad,sunlit,phase_min,shadow_min)"
                         " VALUES(?,?,?,1,?,60237,?,?,?)", (fid, t, "b%05x" % i, t, 0 if ph >= sunset else 1, ph, sh))
            conn.executemany("INSERT INTO points(ts_ms,channel,value,frame_id,norad) VALUES(?,?,?,?,60237)",
                             [(t, "bat_v", round(v / 0.0005), fid), (t, "bat_chg", ic, fid), (t, "bat_dis", idc, fid),
                              (t, "bat_t", temp, fid)])
        conn.commit()
        r = battery.analyse(conn, spec, 60237, "2024-07-09")
        print("battery:", {k: r.get(k) for k in ("grade", "volt_scale", "balance_mah", "dod", "v_top", "v_end_eclipse",
                                                 "resistance_mohm", "cycles", "life")})
        for c in r["checks"]:
            print(f"  [{c['status']}] {c['title']}: {c['value']}")
        expect_bal = (sunset * 360 - sh * 540) / 60
        assert r["volt_scale"] == 0.0005
        assert abs(r["balance_mah"] - expect_bal) < 25, (r["balance_mah"], expect_bal)
        assert abs(r["dod"] - use / cap) < 0.02, (r["dod"], use / cap)
        assert 75 < r["resistance_mohm"] < 125, r["resistance_mohm"]   # 100 mΩ at 25 °C went in
        lo, hi = r["resistance_range_mohm"]
        assert lo < 100 < hi or abs(r["resistance_mohm"] - 100) < 15
        print("capacity:", r.get("capacity_measured_mah"), r.get("capacity_range_mah"), "of", round(cap))
        assert abs(r["capacity_measured_mah"] - cap) / cap < 0.2   # the pack really holds 2703 mAh
        assert r["cycles"] > 10_000 and 0.8 < r["life"]["capacity_now"] < 1.0
        assert r["grade"] in ("good", "watch")
        print(" ", battery.summary_line(r))
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_migrates_v01_database()
    test_pipeline()
    test_model_spaceweather_summary_stream()
    test_battery_health()
    test_history_fill()
    test_forensics_and_silence()
    test_late_uploads()
    print("OK")
