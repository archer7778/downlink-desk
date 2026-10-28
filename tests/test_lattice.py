"""Tests for the Lattice bridge against a mock Lattice server and the mock SatNOGS.
Each test names the ICD requirements it verifies (docs/ICD-LATTICE.md §8).

    python tests/test_lattice.py        (or: python -m pytest tests/test_lattice.py -q)

With the official SDK installed (pip install -r requirements-dev.txt), every payload the bridge
sends is also checked against the SDK's own models (LAT-001).
"""
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["DOWNLINK_DESK_MIN_GAP"] = "0"

import mock_lattice  # noqa: E402
import mock_satnogs  # noqa: E402
from downlink_desk import db as dbm, fetch, lattice, logchain  # noqa: E402
from downlink_desk.config import Config  # noqa: E402

MIN = 60_000
PORTS = iter(range(8840, 8900))


class Clock:
    """The bridge's clock, which a test can move forward."""
    def __init__(self):
        self.offset = 0

    def __call__(self):
        return dbm.now_ms() + self.offset


class Env:
    def __init__(self, oauth=False, hold=0.3):
        self.sp, self.lp = next(PORTS), next(PORTS)
        self.satnogs, self.sstate, self.frames = mock_satnogs.serve(self.sp)
        threading.Thread(target=self.satnogs.serve_forever, daemon=True).start()
        self.lattice, self.state = mock_lattice.serve(self.lp, hold=hold)
        threading.Thread(target=self.lattice.serve_forever, daemon=True).start()
        self.tmp = Path(tempfile.mkdtemp())
        t = self.tmp
        (t / "secrets").mkdir()
        (t / "secrets" / "satnogs_token").write_text("test-token\n")
        (t / "secrets" / "lattice_sandbox_token").write_text("sandbox-token\n")
        if oauth:
            (t / "secrets" / "lattice_client_secret").write_text("csecret\n")
        else:
            (t / "secrets" / "lattice_token").write_text("lattice-env-token\n")
        shutil.copytree(ROOT / "channels", t / "channels")
        text = (ROOT / "config.example.toml").read_text()
        text = text.replace('"https://db.satnogs.org/api"', f'"http://127.0.0.1:{self.sp}/api"')
        text = text.replace('"https://network.satnogs.org/api"', f'"http://127.0.0.1:{self.sp}/api"')
        text = text.replace('"https://services.swpc.noaa.gov"', f'"http://127.0.0.1:{self.sp}"')
        text = text.replace("lat = 0.0", "lat = 10.8231").replace("lon = 0.0", "lon = 106.6297")
        text = text.replace("enabled = false", "enabled = true")
        text = re.sub(r'(?m)^url = ""', f'url = "http://127.0.0.1:{self.lp}"', text)
        if oauth:
            text = re.sub(r'(?m)^client_id = ""', 'client_id = "cid"', text)
        (t / "config.toml").write_text(text)
        self.cfg = Config(t / "config.toml")
        msgs = []
        assert fetch.run(self.cfg, force=True, log=msgs.append) == 0, msgs
        self.conn = dbm.open_db(self.cfg)
        self.clock = Clock()

    def bridge(self, **kw):
        return lattice.Bridge(self.cfg, log=lambda m: None, clock=self.clock, **kw)

    def close(self):
        self.satnogs.shutdown()
        self.lattice.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)


def sat_entity(env, b, norad=60237):
    return env.state["entities"].get(b.eid("satellite", norad))


def codes(e):
    return {a["alertCode"]: a["level"] for a in e["health"].get("activeAlerts", [])}


def set_limits(env, ch, lim):
    logchain.append(env.conn, "limits", {"sat": 60237, "ch": ch, "lim": lim}, f"Limits for {ch}", "TEST")


def latest(env, ch):
    return env.conn.execute("SELECT value FROM points WHERE norad=60237 AND channel=? ORDER BY ts_ms DESC LIMIT 1",
                            (ch,)).fetchone()[0]


# ---------- SDK schema check (LAT-001) ----------

def _sdk():
    try:
        from anduril import types as at
        return at
    except Exception:
        return None


def schema_errors(model, data, path):
    """Every key must be a field (by its REST name) of the SDK model, every enum value one the SDK
    lists, every time an RFC 3339 string, all the way down."""
    import datetime as dt
    import typing
    from pydantic import BaseModel
    errs = []
    fields = {}
    for name, f in model.model_fields.items():
        fields[f.alias or name] = f
    # the SDK's models all accept unknown keys; only google.protobuf.Any really carries arbitrary fields
    allow_extra = model.__name__ == "GoogleProtobufAny"
    for k, v in data.items():
        if k not in fields:
            if not allow_extra:
                errs.append(f"{path}.{k}: not a field of {model.__name__}")
            continue
        errs += _check(fields[k].annotation, v, f"{path}.{k}", BaseModel, dt, typing)
    return errs


def _check(ann, v, path, BaseModel, dt, typing):
    if v is None or ann is typing.Any:
        return []
    origin = typing.get_origin(ann)
    if origin is typing.Union or str(origin) in ("types.UnionType", "<class 'types.UnionType'>"):
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        lits = [a for a in args if typing.get_origin(a) is typing.Literal]
        if lits:
            allowed = set(typing.get_args(lits[0]))
            return [] if v in allowed else [f"{path}: {v!r} is not one of the SDK's values"]
        errors = None
        for a in args:
            e = _check(a, v, path, BaseModel, dt, typing)
            if not e:
                return []
            errors = errors or e
        return errors or []
    if origin is typing.Literal:
        return [] if v in typing.get_args(ann) else [f"{path}: {v!r} not allowed"]
    if origin in (list, typing.List):
        if not isinstance(v, list):
            return [f"{path}: expected a list"]
        (a,) = typing.get_args(ann) or (typing.Any,)
        out = []
        for i, item in enumerate(v):
            out += _check(a, item, f"{path}[{i}]", BaseModel, dt, typing)
        return out
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return schema_errors(ann, v, path) if isinstance(v, dict) else [f"{path}: expected an object"]
    if ann is dt.datetime:
        return [] if isinstance(v, str) and lattice.parse_iso(v) else [f"{path}: not an RFC 3339 time: {v!r}"]
    if ann is float:
        return [] if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else [f"{path}: not a number"]
    if ann is int:
        return [] if isinstance(v, int) and not isinstance(v, bool) else [f"{path}: not an integer"]
    if ann is str:
        return [] if isinstance(v, str) else [f"{path}: not a string"]
    if ann is bool:
        return [] if isinstance(v, bool) else [f"{path}: not a boolean"]
    return []


def assert_schema(env):
    at = _sdk()
    if at is None:
        print("  (SDK not installed: payload schema check skipped)")
        return 0
    errs = []
    for body in env.state["puts"]:
        errs += schema_errors(at.Entity, body, body["aliases"]["name"])
    for tid, st in env.state["statuses"]:
        errs += schema_errors(at.TaskStatus, st, f"task {tid}")
    assert not errs, "\n".join(sorted(set(errs))[:40])
    return len(env.state["puts"]) + len(env.state["statuses"])


# ---------- tests ----------

def test_entities_published():
    """LAT-002, LAT-005, LAT-013, plus OAuth client credentials."""
    for oauth in (False, True):
        env = Env(oauth=oauth)
        try:
            b = env.bridge()
            b.publish_cycle()
            ents = env.state["entities"]
            assert b.link["state"] == "ONLINE", b.link
            node = ents[b.node_entity]
            sat = sat_entity(env, b)
            assert sat and node, list(ents)
            assert sat["ontology"] == {"template": "TEMPLATE_ASSET", "platformType": "Satellite", "specificType": "2U CubeSat"}
            assert {"id": "60237", "type": "ALT_ID_TYPE_NORAD_CAT_ID"} in sat["aliases"]["alternateIds"]
            assert sat["orbit"]["orbitMeanElements"]["tleParameters"]["noradCatId"] == 60237
            assert 400_000 < sat["location"]["position"]["altitudeHaeMeters"] < 700_000
            assert sat["health"]["components"] and sat["milView"]["environment"] == "ENVIRONMENT_SPACE"
            assert node["taskCatalog"]["taskDefinitions"][0]["taskSpecificationUrl"] == lattice.TASK_TYPE
            # LAT-013: home location rounded to 0.1 degree
            assert node["location"]["position"] == {"latitudeDegrees": 10.8, "longitudeDegrees": 106.6}, node["location"]
            stations = [e for e in ents.values() if e["ontology"]["specificType"] == "SatNOGS station"]
            assert stations and all("health" not in e for e in stations)
            assert all(lattice.parse_iso(e["expiryTime"]) > dbm.now_ms() for e in ents.values())
            # LAT-005: a restarted bridge updates the same entities
            before = set(ents)
            env.clock.offset += 30_000
            b2 = env.bridge()
            b2.publish_cycle()
            assert b2.node == b.node and set(env.state["entities"]) == before
            assert env.state["oauth_issued"] == (2 if oauth else 0)   # one token per bridge instance
            n = assert_schema(env)
            print(f"entities ({'oauth' if oauth else 'token'}): {len(ents)} published, {n} payloads schema-checked")
        finally:
            env.close()


def test_position_and_velocity():
    """LAT-003: position and velocity against independent calculations; orbit elements against
    sgp4's own TLE parser."""
    from sgp4.api import Satrec
    from downlink_desk.orbit import _jd, gmst, observer_ecef
    tle = {"tle1": mock_satnogs.TLE[0], "tle2": mock_satnogs.TLE[1], "source": "test", "updated": "2026-10-11T03:24:03Z"}
    sat = Satrec.twoline2rv(tle["tle1"], tle["tle2"])
    worst_m, worst_v = 0.0, 0.0
    for k in range(12):
        t = dbm.now_ms() + k * 7 * MIN
        p = lattice.position(tle, t)

        def ecef(tt):
            jd, fr = _jd(tt)
            _, r, _ = sat.sgp4(jd, fr)
            th = gmst(jd + fr)
            return (r[0] * math.cos(th) + r[1] * math.sin(th), -r[0] * math.sin(th) + r[1] * math.cos(th), r[2])
        # geodetic -> Earth-fixed with the console's own (independent) observer formula: same point
        x, y, z = observer_ecef(p["lat"], p["lon"], p["hae_m"] / 1000)
        X = ecef(t)
        worst_m = max(worst_m, 1000 * math.dist((x, y, z), X))
        # velocity: finite difference of Earth-fixed positions one second apart, in east-north-up
        a, c = ecef(t - 500), ecef(t + 500)
        v = [(c[i] - a[i]) * 1000 for i in range(3)]
        la, lo = math.radians(p["lat"]), math.radians(p["lon"])
        enu = (-math.sin(lo) * v[0] + math.cos(lo) * v[1],
               -math.sin(la) * math.cos(lo) * v[0] - math.sin(la) * math.sin(lo) * v[1] + math.cos(la) * v[2],
               math.cos(la) * math.cos(lo) * v[0] + math.cos(la) * math.sin(lo) * v[1] + math.sin(la) * v[2])
        worst_v = max(worst_v, math.dist(enu, p["enu"]))
        assert 6_800 < p["speed"] < 7_700, p["speed"]
    print(f"position: round trip within {worst_m:.3f} m, velocity within {worst_v:.3f} m/s of finite differences")
    assert worst_m < 1000 and worst_v < 10
    m = lattice.orbit_component(tle)["orbitMeanElements"]
    k, tp = m["meanKeplerianElements"], m["tleParameters"]
    assert abs(math.radians(k["inclinationDeg"]) - sat.inclo) < 1e-9
    assert abs(math.radians(k["raOfAscNodeDeg"]) - sat.nodeo) < 1e-9
    assert abs(math.radians(k["argOfPericenterDeg"]) - sat.argpo) < 1e-9
    assert abs(math.radians(k["meanAnomalyDeg"]) - sat.mo) < 1e-9
    assert abs(k["eccentricity"] - sat.ecco) < 1e-12
    assert abs(k["meanMotion"] * 2 * math.pi / 1440 - sat.no_kozai) < 1e-12
    assert abs(tp["bstar"] - sat.bstar) < 1e-15 and tp["noradCatId"] == sat.satnum
    ep = lattice.parse_iso(k["epoch"]) / 86_400_000 + 2440587.5
    assert abs(ep - (sat.jdsatepoch + sat.jdsatepochF)) < 1e-7
    assert lattice.parse_iso("2026-10-28T02:30:00.123456789Z") == lattice.parse_iso("2026-10-28T02:30:00.123Z")


def test_health_mapping():
    """LAT-004: limits, drift, staleness and silence map to health and alerts, and alerts clear."""
    env = Env()
    try:
        b = env.bridge()
        b.publish_cycle()
        v = latest(env, "bat_t")
        set_limits(env, "bat_t", [v - 20, v - 10, v - 2, v - 1])   # latest value above the alarm limit
        env.clock.offset += 30_000
        b.publish_cycle()
        e = sat_entity(env, b)
        assert codes(e).get("LIMIT_ALARM:bat_t") == "ALERT_LEVEL_WARNING", codes(e)
        th = next(c for c in e["health"]["components"] if c["name"] == "Thermal")
        assert th["health"] == "HEALTH_STATUS_FAIL" and e["health"]["healthStatus"] == "HEALTH_STATUS_FAIL"
        set_limits(env, "bat_t", [v - 20, v - 10, v - 2, v + 10])   # caution only
        env.clock.offset += 30_000
        b.publish_cycle()
        e = sat_entity(env, b)
        assert codes(e).get("LIMIT_CAUTION:bat_t") == "ALERT_LEVEL_CAUTION" and "LIMIT_ALARM:bat_t" not in codes(e)
        set_limits(env, "bat_t", [None, None, None, None])          # cleared
        dbm.smeta_set(env.conn, 60237, "model_status", {"bat_t": "drift"})
        env.clock.offset += 30_000
        b.publish_cycle()
        e = sat_entity(env, b)
        assert not any(k.startswith("LIMIT_") for k in codes(e)), codes(e)
        assert codes(e).get("DRIFT:bat_t") == "ALERT_LEVEL_ADVISORY"
        dbm.smeta_set(env.conn, 60237, "model_status", {})
        # a subsystem without limits is NOT_READY on its own, and doesn't drag the roll-up down
        pw = next(c for c in e["health"]["components"] if c["name"] == "Power")
        assert pw["health"] == "HEALTH_STATUS_NOT_READY" and "aren't judged" in pw["messages"][0]["message"]
        # stale: caution at stale_hours, alarm at twice that
        last = env.conn.execute("SELECT MAX(ts_ms) FROM frames WHERE norad=60237 AND decoded_ok=1").fetchone()[0]
        for hours, level in ((13, "ALERT_LEVEL_CAUTION"), (25, "ALERT_LEVEL_WARNING")):
            env.clock.offset = last + hours * 3600_000 - dbm.now_ms()
            b._cache.clear()
            b.publish_cycle()
            e = sat_entity(env, b)
            assert codes(e).get("STALE") == level, (hours, codes(e))
        # silent: six high passes by stations that usually hear it, all empty, after the last frame
        now = env.clock()
        rows = [(98000000 + i, last + (i + 1) * 3 * 3600_000, last + (i + 1) * 3 * 3600_000 + 600_000, 2380,
                 "Piszkesteto UHF", 47.917, 19.895, 70.0, "test", now, 60237) for i in range(6)]
        env.conn.executemany("INSERT INTO observations(id,start_ms,end_ms,station_id,station_name,lat,lng,max_alt,mode,"
                             "fetched_ms,norad) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        env.conn.commit()
        env.clock.offset = rows[-1][2] + 4 * 3600_000 - dbm.now_ms()
        b._cache.clear()
        b.publish_cycle()
        e = sat_entity(env, b)
        assert codes(e).get("SILENT") == "ALERT_LEVEL_WARNING", codes(e)
        assert e["health"]["healthStatus"] == "HEALTH_STATUS_OFFLINE"
        print("health:", sorted(codes(e)), e["health"]["healthStatus"], f"({assert_schema(env)} payloads schema-checked)")
    finally:
        env.close()


def test_outage_and_replay():
    """LAT-006, LAT-007, LAT-008."""
    env = Env()
    try:
        b = env.bridge()
        b.publish_cycle()
        sid = b.eid("satellite", 60237)
        v = latest(env, "bat_t")
        env.state["chaos"] = "down"
        plan = [None, [v - 20, v - 10, v - 2, v - 1], None, None, [None] * 4, None, None]
        for i, lim in enumerate(plan):
            if lim:
                set_limits(env, "bat_t", lim)
            if i == len(plan) - 1:
                b._cycle = 0   # stations go out every 5 minutes; include them in this one
            env.clock.offset += 30_000
            b._next_send = 0
            b.publish_cycle()
        assert b.link["state"] == "OFFLINE", b.link
        depth, _ = b.box.depth()
        sat_rows = [r for r in b.box.head(500) if r["key"] == sid]
        print(f"outage: {depth} updates queued after {len(plan)} publish cycles, {len(sat_rows)} of them for the "
              f"satellite after coalescing")
        # alarm raised and cleared, plus the latest state; position-only updates between were superseded
        assert len(sat_rows) == 3 and [r["transition"] for r in sat_rows] == [1, 1, 0], sat_rows
        # LAT-007: the fetch job is unaffected
        new = {**env.frames[0], "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120)),
               "observer": "During-Outage-JN97ww"}
        env.frames.insert(0, new)
        dbm.smeta_set(env.conn, 60237, "last_attempt_ms", 0)
        assert fetch.run(env.cfg, force=True, log=lambda m: None) == 0
        assert env.conn.execute("SELECT COUNT(*) FROM frames WHERE observer='During-Outage-JN97ww'").fetchone()[0] == 1
        # LAT-008: one entity Lattice rejects doesn't hold up the rest
        poisoned = next(r["key"] for r in b.box.head(500) if r["key"] not in (sid, b.node_entity))
        env.state["poison"].add(poisoned)
        env.state["chaos"] = None
        hist_before = len(env.state["history"].get(sid, []))
        b._next_send = 0
        b.drain()
        assert b.box.depth()[0] == 0 and b.link["state"] == "ONLINE"
        assert b.box.get("dropped_invalid") == 1
        hist = env.state["history"][sid][hist_before:]
        seen = [sorted(k for k in codes(e) if k.startswith("LIMIT_")) for e in hist]
        assert seen == [["LIMIT_ALARM:bat_t"], [], []], seen
        times = [lattice.parse_iso(e["provenance"]["sourceUpdateTime"]) for e in env.state["history"][sid]]
        assert times == sorted(times) and len(set(times)) == len(times)
        print(f"replay: {len(hist)} satellite snapshots delivered in order; alarm and its clearing both reached Lattice")
    finally:
        env.close()


def _add_pass(env, obs_id, start, end, station=("Piszkesteto UHF", 2380, 47.917, 19.895), el=60.0):
    env.conn.execute("INSERT INTO observations(id,start_ms,end_ms,station_id,station_name,lat,lng,max_alt,mode,"
                     "fetched_ms,norad) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (obs_id, start, end, station[1], station[0], station[2], station[3], el, "test", dbm.now_ms(), 60237))
    env.conn.commit()


def _pump(b, until, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        b.process_inbox()
        b.publish_cycle()
        b._next_send = 0
        b.drain()
        if until():
            return True
        time.sleep(0.2)
    return False


def _statuses(env, tid):
    return [s["status"] for t, s in env.state["statuses"] if t == tid]


def test_expedite_pass_done():
    """LAT-009, LAT-011, LAT-012: the task runs through the real fetch path to DONE_OK."""
    env = Env()
    try:
        calls = []

        def fetcher(norad):
            calls.append(env.clock())
            dbm.smeta_set(env.conn, norad, "last_attempt_ms", 0)   # the test, not SatNOGS politeness, sets the pace
            fetch.run(env.cfg, force=True, log=lambda m: None, only=norad)

        b = env.bridge(fetcher=fetcher)
        b.publish_cycle()
        threading.Thread(target=b.listen_loop, daemon=True).start()
        now = env.clock()
        _add_pass(env, 99000001, now - 12 * MIN, now - 2 * MIN)
        tid = env.state["create_task"](b.node_entity, {"@type": lattice.TASK_TYPE, "noradCatId": 60237,
                                                        "observationId": 99000001})
        assert _pump(b, lambda: "STATUS_EXECUTING" in _statuses(env, tid)), _statuses(env, tid)
        assert _statuses(env, tid)[:3] == ["STATUS_MACHINE_RECEIPT", "STATUS_WILCO", "STATUS_EXECUTING"]
        prog = [s for t, s in env.state["statuses"] if t == tid and s["status"] == "STATUS_EXECUTING"][0]["progress"]
        assert prog["@type"] == lattice.PROGRESS_TYPE and prog["observationId"] == 99000001
        assert _pump(b, lambda: len(calls) == 1 and b._fetching is None)
        node = env.state["entities"][b.node_entity]
        assert node["status"]["platformActivity"] == "EXPEDITING GRBBETA PASS 99000001", node["status"]
        # LAT-011: no second fetch inside the gap
        for _ in range(3):
            b.publish_cycle()
        assert len(calls) == 1, calls
        b.stop.set()
        # LAT-012: a restarted bridge picks the task up where it was
        b2 = env.bridge(fetcher=fetcher)
        t = b2.box.task(tid)
        assert t["state"] == "executing" and t["fetches"] == 1, t
        # the station uploads the pass; the next expedited fetch, 10 minutes later, collects it
        pass_frames = [{**f, "observer": "Piszkesteto UHF-JN97ww",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now - 10 * MIN) / 1000 + 20 * i))}
                       for i, f in enumerate(env.frames[:4])]
        env.frames.extend(pass_frames)
        env.frames.sort(key=lambda f: f["timestamp"], reverse=True)
        env.clock.offset += 10 * MIN
        threading.Thread(target=b2.listen_loop, daemon=True).start()
        assert _pump(b2, lambda: "STATUS_DONE_OK" in _statuses(env, tid)), _statuses(env, tid)
        assert len(calls) == 2 and calls[1] - calls[0] >= 10 * MIN
        res = [s for t, s in env.state["statuses"] if t == tid and s["status"] == "STATUS_DONE_OK"][0]["result"]
        assert res["@type"] == lattice.RESULT_TYPE and res["framesReceived"] == 4 and res["fetches"] == 2, res
        assert env.state["tasks"][tid]["status"]["status"] == "STATUS_DONE_OK"
        print(f"expedite: {' -> '.join(s.replace('STATUS_', '') for s in _statuses(env, tid))}; "
              f"{res['framesReceived']} frames after {res['fetches']} expedited fetches "
              f"({assert_schema(env)} payloads schema-checked)")
        b2.stop.set()
    finally:
        env.close()


def test_expedite_pass_rejects_timeout_cancel():
    """LAT-010, LAT-011 (task limit), LAT-014."""
    env = Env()
    try:
        b = env.bridge(fetcher=lambda norad: None)
        b.publish_cycle()
        threading.Thread(target=b.listen_loop, daemon=True).start()
        now = env.clock()
        spec = lambda **kw: {"@type": lattice.TASK_TYPE, "noradCatId": 60237, **kw}
        mk = env.state["create_task"]

        def ends(tid, status):
            return _pump(b, lambda: status in _statuses(env, tid))

        def error(tid):
            return [s for t, s in env.state["statuses"] if t == tid][-1].get("taskError", {})

        # rejected: untracked satellite, wrong type, unknown observation
        for bad, why in ((spec(noradCatId=12345), "isn't tracked"), ({"@type": "type.googleapis.com/x.Y"}, "Unsupported"),
                         (spec(observationId=42), "isn't a known observation")):
            tid = mk(b.node_entity, bad)
            assert ends(tid, "STATUS_DONE_NOT_OK")
            assert error(tid)["code"] == "ERROR_CODE_REJECTED" and why in error(tid)["message"], error(tid)
            assert _statuses(env, tid) == ["STATUS_MACHINE_RECEIPT", "STATUS_DONE_NOT_OK"]
        # timeout: no frames by the deadline
        _add_pass(env, 99000010, now - 10 * MIN, now - 1 * MIN, ("KM7DOS", 2767, 47.6, -122.3))
        tid = mk(b.node_entity, spec(observationId=99000010, deadlineMinutes=5))
        assert ends(tid, "STATUS_EXECUTING")
        env.clock.offset += 5 * MIN
        assert ends(tid, "STATUS_DONE_NOT_OK") and error(tid)["code"] == "ERROR_CODE_TIMEOUT", error(tid)
        # cancel and complete on future passes; LAT-014: a stale version is resent after reading the task
        later = env.clock() + 2 * 3600_000
        for i in range(4):
            _add_pass(env, 99000020 + i, later + i * 3600_000, later + i * 3600_000 + 600_000)
        t_cancel = mk(b.node_entity, spec(observationId=99000020))
        env.state["reject_version_once"].add(t_cancel)
        assert ends(t_cancel, "STATUS_WILCO")
        t_dup = mk(b.node_entity, spec(observationId=99000020))
        assert ends(t_dup, "STATUS_DONE_NOT_OK") and "already being expedited" in error(t_dup)["message"]
        t_complete = mk(b.node_entity, spec(observationId=99000021))
        t_third = mk(b.node_entity, spec(observationId=99000022))
        assert ends(t_complete, "STATUS_WILCO") and ends(t_third, "STATUS_WILCO")
        t_over = mk(b.node_entity, spec(observationId=99000023))
        assert ends(t_over, "STATUS_DONE_NOT_OK") and "3 active tasks" in error(t_over)["message"], error(t_over)
        env.state["send"](b.node_entity, {"cancelRequest": {"taskId": t_cancel}})
        assert ends(t_cancel, "STATUS_DONE_NOT_OK") and error(t_cancel)["code"] == "ERROR_CODE_CANCELLED"
        env.state["send"](b.node_entity, {"completeRequest": {"taskId": t_complete}})
        assert ends(t_complete, "STATUS_DONE_OK")
        # no next pass named: the bridge picks the next one that hasn't ended
        t_next = mk(b.node_entity, spec())
        assert ends(t_next, "STATUS_WILCO") and b.box.task(t_next)["obs_id"], b.box.task(t_next)
        assert env.state["tasks"][t_cancel]["status"]["status"] == "STATUS_DONE_NOT_OK"
        assert env.state["tasks"][t_complete]["status"]["status"] == "STATUS_DONE_OK"
        # LAT-014: Lattice refused t_cancel's first update as stale; it was resent and nothing was lost
        assert t_cancel not in env.state["reject_version_once"]
        assert _statuses(env, t_cancel) == ["STATUS_MACHINE_RECEIPT", "STATUS_WILCO", "STATUS_DONE_NOT_OK"]
        print(f"tasks: rejections, timeout, duplicate, task limit, cancel, complete and version resync all as specified "
              f"({assert_schema(env)} payloads schema-checked)")
        b.stop.set()
    finally:
        env.close()


if __name__ == "__main__":
    test_position_and_velocity()
    test_entities_published()
    test_health_mapping()
    test_outage_and_replay()
    test_expedite_pass_done()
    test_expedite_pass_rejects_timeout_cancel()
    print("OK")
