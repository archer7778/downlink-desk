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
    text = text.replace('api = "https://db.satnogs.org/api"', f'api = "http://127.0.0.1:{port}/api"')
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
        conn = dbm.connect(cfg.db_path)
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
        tle = dbm.meta_get(conn, "tle")
        p = passes(tle["tle1"], tle["tle2"], dbm.now_ms(), 24, 10.82, 106.63)
        print("passes next 24h:", len(p))
        assert len(p) >= 2
        print("requests to mock:", state["requests"])
    finally:
        httpd.shutdown()
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_pipeline()
    print("OK")
