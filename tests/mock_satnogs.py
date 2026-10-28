"""A stand-in for the SatNOGS DB API, for tests and offline demos.

Frames are SYNTHETIC: built to the published GRBBeta frame format so the real
decoder can parse them, with made-up values. They are not GRBBeta data.

    python tests/mock_satnogs.py --port 8765
Then point config.toml's [satnogs] api at http://127.0.0.1:8765/api
"""
import argparse
import json
import sys
from pathlib import Path
import random
import struct
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run on its own: find downlink_desk

BASE_TLE = ("1 60237U 24128C   26283.93962216  .00003793  00000-0  26257-3 0  9991",
            "2 60237  61.9894 169.9307 0023442 160.3425 199.8578 15.07305724123713")


def _checksum(line):
    return str(sum(int(c) if c.isdigit() else c == "-" for c in line[:68]) % 10)


def _with_raan(tle, raan):
    l2 = tle[1][:17] + f"{raan % 360:8.4f}" + tle[1][25:68]
    return tle[0], l2 + _checksum(l2)


def _seasonal_tle(base, now_ms=None):
    """The base orbit with its plane turned so the satellite passes through Earth's shadow every
    orbit for the three weeks before today and the day after. A 62° orbit spends a few days of
    every two months in constant sunlight; tests that build weeks of synthetic data ending today
    need shadow, whatever today's date is. Only the node angle changes (still a valid orbit)."""
    from downlink_desk.orbit import eclipses
    now_ms = now_ms or int(time.time() * 1000)
    samples = [now_ms + d * 86_400_000 for d in (-22, -15, -8, -5, -1, 0, 1)]
    best, best_raan = -1.0, float(base[1][17:25])
    for k in range(24):
        raan = float(base[1][17:25]) + 15 * k
        cand = _with_raan(base, raan)
        worst = 99.0
        for t in samples:
            ec = [b - a for a, b in eclipses(*cand, t, t + 5 * 3600_000, 60)[1:-1]]
            worst = min(worst, min(ec) / 60_000 if ec else 0.0)
            if worst <= best:
                break
        if worst > best:
            best, best_raan = worst, raan
        if best >= 30:
            break
    return _with_raan(base, best_raan)


TLE = _seasonal_tle(BASE_TLE)
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


STATIONS = [("Piszkesteto UHF", 2380, 47.917, 19.895), ("berlin_ma_sat", 4601, 42.378, -71.658),
            ("IV3DXW", 3210, 46.07, 13.23), ("KM7DOS", 2767, 47.6, -122.3)]


def make_observations(frames, now=None, seed=11):
    """SatNOGS Network-shaped observations: one around each burst of frames (heard), a few
    scheduled ones that produced nothing, and some in the future."""
    rnd = random.Random(seed)
    now = now or time.time()
    ts = sorted((datetime.fromisoformat(f["timestamp"].replace("Z", "+00:00")).timestamp(), f["observer"]) for f in frames)
    bursts, cur = [], [ts[0]]
    for t in ts[1:]:
        if t[0] - cur[-1][0] > 1800:
            bursts.append(cur)
            cur = [t]
        else:
            cur.append(t)
    bursts.append(cur)
    # each burst was heard by a station whose schedule name is the observer without its grid square
    windows = [(b[0][0] - 90, b[-1][0] + 90, rnd.uniform(20, 70), b[0][1].rsplit("-", 1)[0]) for b in bursts]
    for k in range(4):   # scheduled but nothing came down
        t = now - rnd.uniform(6, 60) * 3600
        windows.append((t, t + 540, rnd.uniform(18, 50), None))
    for k in range(6):   # still to come
        t = now + (k + 1) * rnd.uniform(1.2, 2.5) * 3600
        windows.append((t, t + 600, rnd.uniform(10, 80), None))
    out = []
    for i, (a, b, el, who) in enumerate(windows):
        name, sid, lat, lng = STATIONS[i % len(STATIONS)]
        if who:
            match = [st for st in STATIONS if st[0] == who]
            name, sid, lat, lng = match[0] if match else (who, 9000 + i, 40.0, 0.0)
        iso = lambda x: datetime.fromtimestamp(x, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"id": 15170000 + i, "start": iso(a), "end": iso(b), "ground_station": sid, "station_name": name,
                    "station_lat": lat, "station_lng": lng, "max_altitude": round(el, 1), "norad_cat_id": 60237,
                    "status": "future" if a > now else "good", "transmitter_description": "Mode U - GFSK9k6 - AX.25"})
    return sorted(out, key=lambda o: o["start"], reverse=True)


def serve(port, token="test-token", page_size=25, require_token=True):
    frames = make_frames()
    observations = make_observations(frames)
    # a second, made-up satellite with the same frame format, for automatic-channel tests
    shifted = [{**f, "norad_cat_id": 99999, "timestamp": f["timestamp"][:-3] + "%02dZ" % ((int(f["timestamp"][-3:-1]) + 7) % 60)}
               for f in frames]
    state = {"requests": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200, link=None):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            if link:
                self.send_header("Link", link)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            state["requests"] += 1
            u = urlparse(self.path)
            mode = (state.get("chaos") or {}).get(u.path)   # fault injection for failure tests
            if mode == "drop":
                self.close_connection = True
                return
            if mode == "half":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "5000")
                self.end_headers()
                self.wfile.write(b'{"next": null, "results": [{"times')
                self.close_connection = True
                return
            if mode == "html":
                b = b"<html><body>502 Bad Gateway</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            if mode == "500":
                return self._json({"detail": "server error"}, 500)
            if mode == "junk":
                return self._json([1, "x", None, [2], {"timestamp": 5, "frame": 7}, {"norad_cat_id": "abc", "telemetries": "z"}])
            if mode == "badtle":
                return self._json([{"tle1": "1 garbage", "tle2": "2 more garbage", "norad_cat_id": 60237}])
            if mode == "error_obj":
                return self._json({"detail": "Not found."})
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            base = f"http://127.0.0.1:{port}"
            if u.path == "/api/tle/":
                return self._json([{"tle0": "0 GRBBETA", "tle1": TLE[0], "tle2": TLE[1], "tle_source": "mock",
                                    "norad_cat_id": 60237, "updated": "2026-10-11T03:24:03Z"}])
            if u.path == "/api/satellites/":
                cat = [{"sat_id": "FVZM-4350-0869-7957-1010", "norad_cat_id": 60237, "name": "GRBBeta", "status": "alive",
                        "is_frequency_violator": False, "telemetries": [{"decoder": "grbbeta"}], "decayed": None,
                        "countries": "SK", "launched": "2024-07-09T19:00:00Z", "website": "https://grbbeta.tuke.sk/"},
                       {"norad_cat_id": 99999, "name": "TESTSAT", "status": "alive", "telemetries": [{"decoder": "grbbeta"}],
                        "decayed": None, "countries": "XX", "launched": "2026-01-01T00:00:00Z", "is_frequency_violator": False},
                       {"norad_cat_id": 43880, "name": "UWE-4", "status": "alive", "telemetries": [{"decoder": "uwe4"}],
                        "decayed": None, "countries": "DE", "launched": "2018-12-27T00:00:00Z"},
                       {"norad_cat_id": 47959, "name": "GRBAlpha", "status": "re-entered", "telemetries": [{"decoder": "grbalpha"}],
                        "decayed": "2025-06-09T19:53:00Z"},
                       {"norad_cat_id": 11111, "name": "NODECODER", "status": "alive", "telemetries": [], "decayed": None}]
                if "norad_cat_id" in q:
                    cat = [c for c in cat if str(c["norad_cat_id"]) == q["norad_cat_id"]]
                return self._json(cat)
            if u.path == "/api/observations/":
                if "page" in q:   # like the real API: cursor pagination only
                    return self._json({"detail": "Invalid page."}, 400)
                if q.get("norad_cat_id", "60237") != "60237":
                    return self._json([])
                cur = int(q.get("cursor", 0))
                page = observations[cur: cur + 25]
                link = (f'<{base}/api/observations/?cursor={cur + 25}&norad_cat_id=60237&status=future>; rel="next"'
                        if cur + 25 < len(observations) else None)
                return self._json(page, link=link)
            if u.path == "/api/telemetry/":
                if require_token and self.headers.get("Authorization") != f"Token {token}":
                    return self._json({"detail": "Authentication credentials were not provided."}, 401)
                if "satellite" not in q and "cursor" not in q:
                    return self._json({"detail": "satellite is required"}, 400)
                sat = q.get("satellite") or q.get("sat", "60237")
                src = frames if sat == "60237" else shifted if sat == "99999" else []
                if q.get("start"):
                    src = [f for f in src if f["timestamp"] >= q["start"]]
                if q.get("end"):
                    src = [f for f in src if f["timestamp"] <= q["end"]]
                state.setdefault("windows", []).append((q.get("start"), q.get("end")))
                start = int(q.get("cursor", 0))
                page = src[start:start + page_size]
                extra = "".join(f"&{k}={q[k]}" for k in ("start", "end") if q.get(k))
                nxt = f"{base}/api/telemetry/?cursor={start + page_size}&sat={sat}{extra}" if start + page_size < len(src) else None
                return self._json({"next": nxt, "previous": None, "results": page})
            # NOAA SWPC look-alikes (same shapes as the real products, October 2026)
            if u.path == "/products/noaa-planetary-k-index.json":
                now = int(time.time()) // 10800 * 10800
                return self._json([{"time_tag": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - i * 10800)),
                                    "Kp": 7.0 if i == 3 else 2.33, "a_running": 9, "station_count": 8} for i in range(24, -1, -1)])
            if u.path == "/json/f107_cm_flux.json":
                return self._json([{"time_tag": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 7200)),
                                    "frequency": 2800, "flux": 120.0}])
            if u.path == "/json/goes/primary/xrays-6-hour.json":
                t0 = int(time.time()) // 60 * 60
                return self._json([{"time_tag": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 - i * 60)), "satellite": 18,
                                    "flux": 2.4e-5 if i == 30 else 1.5e-6, "energy": e}
                                   for i in range(120) for e in ("0.05-0.4nm", "0.1-0.8nm")])
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
