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


def test_migrates_v02_database():
    """A database from the single-satellite version opens cleanly and keeps its data."""
    src = Path("/tmp/claude-0/v02.db")
    if not src.exists():
        print("(no v0.2 database to migrate; skipped)")
        return
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = make_cfg(tmp, 8766)
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, cfg.db_path)
        conn = dbm.open_db(cfg)
        n = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        nn = conn.execute("SELECT COUNT(*) FROM frames WHERE norad=60237").fetchone()[0]
        assert n and n == nn, (n, nn)
        assert dbm.satellites(conn)[0]["norad"] == 60237
        assert dbm.smeta_get(conn, 60237, "tle") and dbm.meta_get(conn, "tle") is None
        print("migrated v0.2 database:", n, "frames")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_migrates_v02_database()
    test_pipeline()
    print("OK")
