"""A stand-in for the SatNOGS DB API, for tests and offline demos.

Frames are SYNTHETIC: built to the published GRBBeta frame format so the real
decoder can parse them, with made-up values. They are not GRBBeta data.

    python tests/mock_satnogs.py --port 8765
Then point config.toml's [satnogs] api at http://127.0.0.1:8765/api
"""
import argparse
import json
import random
import struct
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TLE = ("1 60237U 24128C   26283.93962216  .00003793  00000-0  26257-3 0  9991",
       "2 60237  61.9894 169.9307 0023442 160.3425 199.8578 15.07305724123713")
OBSERVERS = ["N0SSE-EM13ke", "KM7DOS-CN87xi", "IV3DXW-JN65qq", "Piszkesteto UHF-JN97ww", "KC1OCA-FN32uc"]


def _call(c, last=False):
    c = c.ljust(6)[:6]
    return bytes(ord(x) << 1 for x in c) + bytes([0x60 | (1 if last else 0)])


def ax25(payload):
    return _call("HA2GRB") + _call("HA2GRB", True) + bytes([0x03, 0xF0]) + payload


def beacon(band, up_rst, up_tot, boot, mcu, pa, rssi, fwd):
    """Temperatures in °C and RSSI in dBm, encoded the way GRBBeta sends them:
    temperatures in 0.01 °C, signal level as raw where dBm = raw/2 - 134."""
    raw_rssi = int((rssi + 134) * 2)
    p = (f"{band},{up_rst},{up_tot},{boot},1,{mcu * 100},{(mcu + 3) * 100},{pa * 100},{fwd},OM1ABC,"
         f"{fwd * 2},{fwd * 3},{raw_rssi},{raw_rssi + 10}").encode() + b"\x00"
    return ax25(p)


def psu(bat_mv, chg, dis, bat_dk, sys_dk, pv1, pv2):
    # temperatures arrive in tenths of a kelvin here and are re-encoded in 1/128 K steps
    bat_dk, sys_dk = int(bat_dk / 10 * 128), int(sys_dk / 10 * 128)
    hdr = (2 << 30) | (2 << 25) | (10 << 20) | (10 << 14) | (9 << 8)
    body = struct.pack("<IIIIHII", int(time.time()), 3600, 900000, 7, 1, 55, 0)
    body += struct.pack("<9H", 4800, 4700, 0, 90, 85, 0, pv1, pv2, 0) + bytes([3, 0x7F, 3])
    body += struct.pack("<7H", 10, 20, 30, 40, 50, 60, 70) + struct.pack("<5H", bat_mv, chg, dis, bat_dk, sys_dk)
    return struct.pack(">I", hdr) + body


def make_frames(now=None, days=3, seed=7):
    """Newest-first list of SatNOGS-shaped telemetry records."""
    rnd = random.Random(seed)
    now = now or time.time()
    t = now - days * 86400
    out, boot, boot_t = [], 41, now - days * 86400 - 50 * 3600
    reboot_at = now - 20 * 3600
    while t < now - 1800:
        t += rnd.uniform(1.5, 4.5) * 3600              # a volunteer hears a pass every few hours
        n = rnd.randint(2, 6)
        obs = rnd.choice(OBSERVERS)
        for i in range(n):
            ft = t + i * 20
            if boot_t < reboot_at <= ft:
                boot += 1
                boot_t = reboot_at
            hot = 1 if now - 9 * 3600 < ft < now - 7 * 3600 else 0   # a warm spell on the UHF PA
            phase = (ft / 5700) % 1
            r = rnd.random()
            if r < 0.55:
                fr = beacon("U", int(ft - boot_t), int(ft - 1.2e7), boot, 18 + rnd.randint(-2, 3),
                            24 + rnd.randint(-2, 3) + hot * 45, -112 + rnd.randint(-3, 3), 4000 + int(ft / 3600) % 500)
            elif r < 0.75:
                fr = beacon("V", int(ft - boot_t), int(ft - 1.2e7), boot, 17 + rnd.randint(-2, 3),
                            22 + rnd.randint(-2, 3), -110 + rnd.randint(-3, 3), 2000)
            elif r < 0.85:
                sun = phase < 0.64
                fr = psu(3950 + rnd.randint(-40, 40) - (0 if sun else 120), 140 if sun else 0, 60 if sun else 180,
                         2930 + rnd.randint(-15, 15), 2990 + rnd.randint(-15, 15), 420 if sun else 0, 390 if sun else 0)
            else:
                fr = bytes(rnd.getrandbits(8) for _ in range(40))   # a frame the decoder can't parse
            out.append({"sat_id": "FVZM-4350-0869-7957-1010", "norad_cat_id": 60237, "transmitter": "",
                        "app_source": "network", "decoded": "", "frame": fr.hex().upper(), "observer": obs,
                        "timestamp": datetime.fromtimestamp(ft, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return sorted(out, key=lambda f: f["timestamp"], reverse=True)


def serve(port, token="test-token", page_size=25, require_token=True):
    frames = make_frames()
    state = {"requests": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            state["requests"] += 1
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            base = f"http://127.0.0.1:{port}"
            if u.path == "/api/tle/":
                return self._json([{"tle0": "0 GRBBETA", "tle1": TLE[0], "tle2": TLE[1], "tle_source": "mock",
                                    "norad_cat_id": 60237, "updated": "2026-10-11T03:24:03Z"}])
            if u.path == "/api/satellites/":
                return self._json([{"sat_id": "FVZM-4350-0869-7957-1010", "norad_cat_id": 60237, "name": "GRBBeta",
                                    "status": "alive", "is_frequency_violator": False}])
            if u.path == "/api/telemetry/":
                if require_token and self.headers.get("Authorization") != f"Token {token}":
                    return self._json({"detail": "Authentication credentials were not provided."}, 401)
                if "satellite" not in q and "cursor" not in q:
                    return self._json({"detail": "satellite is required"}, 400)
                start = int(q.get("cursor", 0))
                page = frames[start:start + page_size]
                nxt = f"{base}/api/telemetry/?cursor={start + page_size}" if start + page_size < len(frames) else None
                return self._json({"next": nxt, "previous": None, "results": page})
            self._json({"detail": "not found"}, 404)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    return httpd, state, frames


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    httpd, _, frames = serve(a.port)
    print(f"Mock SatNOGS on http://127.0.0.1:{a.port}/api with {len(frames)} synthetic frames (token: test-token)")
    httpd.serve_forever()
