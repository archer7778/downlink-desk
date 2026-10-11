"""Fetch new frames from SatNOGS, decode them and store them.

Run by a systemd timer (every `poll_minutes`). Safe to run by hand:
    python -m downlink_desk.fetch            # respects the polling interval
    python -m downlink_desk.fetch --force    # fetch now (still rate-limited)
"""
import argparse
import fcntl
import json
import sys

from . import db as dbm
from . import decode, notify, satnogs
from .config import find_config
from .orbit import tle_epoch_ms

DAY_MS = 86_400_000


def refresh_meta(conn, client, norad, log):
    """Once every 12 h: latest TLE and the satellite's SatNOGS record."""
    if dbm.now_ms() - (dbm.meta_get(conn, "meta_refreshed_ms", 0) or 0) < 12 * 3600_000:
        return
    try:
        sat = client.satellite(norad)
        if sat:
            dbm.meta_set(conn, "satellite", {k: sat.get(k) for k in
                         ("sat_id", "name", "status", "is_frequency_violator", "website", "reception_status")})
        t = client.tle(norad)
        if t and t.get("tle1") and t.get("tle2"):
            dbm.meta_set(conn, "tle", {"tle1": t["tle1"], "tle2": t["tle2"], "source": t.get("tle_source"),
                                      "updated": t.get("updated"), "epoch_ms": tle_epoch_ms(t["tle1"])})
        dbm.meta_set(conn, "meta_refreshed_ms", dbm.now_ms())
        log("Refreshed satellite record and TLE.")
    except satnogs.Refused as e:
        log(f"Could not refresh TLE/satellite record: {e}")


def refresh_observations(conn, cfg, norad, log):
    """Every 3 h: which SatNOGS stations are scheduled to listen, and when. Public, one request."""
    if dbm.now_ms() - (dbm.meta_get(conn, "obs_refreshed_ms", 0) or 0) < 3 * 3600_000:
        return
    dbm.meta_set(conn, "obs_refreshed_ms", dbm.now_ms())
    try:
        obs, complete = satnogs.scheduled_observations(cfg["satnogs"]["network_api"], norad)
    except satnogs.Refused as e:
        log(f"Could not load scheduled observations: {e}")
        return
    now = dbm.now_ms()
    conn.executemany(
        "INSERT INTO observations(id,start_ms,end_ms,station_id,station_name,lat,lng,max_alt,mode,fetched_ms)"
        " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET start_ms=excluded.start_ms, end_ms=excluded.end_ms,"
        " max_alt=excluded.max_alt, fetched_ms=excluded.fetched_ms",
        [(o["id"], o["start_ms"], o["end_ms"], o["station_id"], o["station_name"], o["lat"], o["lng"],
          o["max_alt"], o["mode"], now) for o in obs])
    # A scheduled observation that vanished before it started was cancelled; forget it
    # (only when we saw the whole list, so a missing one really is gone).
    if complete:
        conn.execute("DELETE FROM observations WHERE start_ms>? AND fetched_ms<?", (now, now))
    conn.execute("DELETE FROM observations WHERE end_ms<?", (now - 30 * DAY_MS,))
    conn.commit()
    log(f"Loaded {len(obs)} scheduled observations.")


def _satrec(conn):
    tle = dbm.meta_get(conn, "tle")
    if not tle:
        return None
    from sgp4.api import Satrec
    return Satrec.twoline2rv(tle["tle1"], tle["tle2"])


def _context(sat, ts):
    if sat is None:
        return (None, None, None)
    from .orbit import state
    st = state(sat, ts)
    return (1 if st["sunlit"] else 0, st["lat"], st["lon"]) if st else (None, None, None)


def store(conn, cfg, spec, frames):
    decoder = cfg["satellite"]["decoder"]
    horizon = dbm.now_ms() + 3600_000
    sat = _satrec(conn)
    new, new_ids = 0, []
    for f in frames:
        ts = satnogs.parse_ts(f.get("timestamp"))
        hexf = (f.get("frame") or "").strip()
        if ts is None or not hexf or ts > horizon:   # volunteers occasionally upload future-dated frames
            continue
        try:
            bytes.fromhex(hexf)
        except ValueError:
            continue
        fields = decode.clean(decode.decode(decoder, hexf))
        ftype = decode.frame_type(spec, fields)   # None: the decoder didn't recognise it
        cur = conn.execute(
            "INSERT OR IGNORE INTO frames(ts_ms,observer,station_id,app_source,frame_hex,frame_type,decoded_ok,fields_json,"
            "fetched_ms,sunlit,sub_lat,sub_lon) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, str(f.get("observer") or "")[:80], str(f.get("station_id") or "")[:40] or None,
             str(f.get("app_source") or "")[:40], hexf, ftype or "Unrecognised",
             1 if ftype else 0, json.dumps(fields if ftype else {}), dbm.now_ms(), *_context(sat, ts)))
        if cur.rowcount:
            new += 1
            fid = cur.lastrowid
            new_ids.append(fid)
            conn.executemany("INSERT OR IGNORE INTO points(ts_ms,channel,value,frame_id) VALUES(?,?,?,?)",
                             [(ts, ch, v, fid) for ch, v in (decode.channel_points(spec, fields) if ftype else [])])
    conn.commit()
    return new, new_ids


def run(cfg, force=False, log=print):
    conn = dbm.connect(cfg.db_path)
    lock = open(cfg.db_path.parent / "fetch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another fetch is running; skipping.")
        return 0
    try:
        sc = cfg["satnogs"]
        norad = int(cfg["satellite"]["norad"])
        now = dbm.now_ms()
        last = dbm.meta_get(conn, "last_attempt_ms", 0) or 0
        min_gap = (2 * 60_000) if force else int(sc["poll_minutes"] * 60_000 * 0.8)
        if now - last < min_gap:
            log("Fetched recently; skipping to stay polite to SatNOGS.")
            return 0
        client = satnogs.Client(sc["api"], cfg.token)
        refresh_meta(conn, client, norad, log)
        refresh_observations(conn, cfg, norad, log)
        sat = dbm.meta_get(conn, "satellite", {}) or {}
        if sat.get("is_frequency_violator") and now - last < DAY_MS:
            dbm.record_fetch(conn, True, None, 0, 0, "Satellite is flagged by SatNOGS; telemetry limited to once a day.")
            log("Satellite flagged as a frequency violator; holding to one request a day.")
            return 0
        if not client.token:
            dbm.record_fetch(conn, False, None, 0, 0, "No SatNOGS API key. Add it to secrets/satnogs_token.")
            log("No SatNOGS API key configured.")
            return 2
        dbm.meta_set(conn, "last_attempt_ms", now)
        newest = conn.execute("SELECT MAX(ts_ms) m FROM frames").fetchone()["m"]
        pages = int(sc["max_pages"] if newest else sc["backfill_pages"])
        spec = cfg.channels()
        try:
            frames = client.telemetry(norad, newer_than_ms=newest, max_pages=pages)
        except satnogs.AuthError as e:
            dbm.record_fetch(conn, False, client.last_status, 0, 0, str(e))
            dbm.meta_set(conn, "auth_error", True)
            notify.fetch_failed(conn, cfg)
            log(str(e))
            return 3
        except satnogs.Refused as e:
            dbm.record_fetch(conn, False, e.status, 0, 0, str(e))
            notify.fetch_failed(conn, cfg)
            log(str(e))
            return 4
        dbm.meta_set(conn, "auth_error", False)
        new, new_ids = store(conn, cfg, spec, frames)
        dbm.meta_set(conn, "last_ok_ms", dbm.now_ms())
        dbm.record_fetch(conn, True, client.last_status, len(frames), new,
                         f"{new} new of {len(frames)} frames from SatNOGS")
        log(f"{new} new frames stored ({len(frames)} returned).")
        notify.fetch_ok(conn)
        notify.after_fetch(conn, cfg, spec, new_ids, log)
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def rebuild(cfg, log=print):
    """Recompute every channel value from the stored decoded fields, using the
    current channels file. Run after changing units or conversions."""
    conn = dbm.connect(cfg.db_path)
    spec = cfg.channels()
    sat = _satrec(conn)
    if sat is not None:
        for r in conn.execute("SELECT id, ts_ms FROM frames").fetchall():
            conn.execute("UPDATE frames SET sunlit=?, sub_lat=?, sub_lon=? WHERE id=?", (*_context(sat, r["ts_ms"]), r["id"]))
    rows = conn.execute("SELECT id, ts_ms, fields_json FROM frames WHERE decoded_ok=1").fetchall()
    conn.execute("DELETE FROM points")
    n = 0
    for r in rows:
        pts = decode.channel_points(spec, json.loads(r["fields_json"] or "{}"))
        conn.executemany("INSERT OR IGNORE INTO points(ts_ms,channel,value,frame_id) VALUES(?,?,?,?)",
                         [(r["ts_ms"], ch, v, r["id"]) for ch, v in pts])
        n += len(pts)
    conn.commit()
    log(f"Rebuilt {n} channel values from {len(rows)} decoded frames.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch telemetry from SatNOGS DB")
    ap.add_argument("--config")
    ap.add_argument("--force", action="store_true", help="fetch now instead of waiting for the interval")
    ap.add_argument("--rebuild", action="store_true", help="recompute channel values after editing the channels file")
    a = ap.parse_args(argv)
    cfg = find_config(a.config)
    sys.exit(rebuild(cfg) if a.rebuild else run(cfg, force=a.force))


if __name__ == "__main__":
    main()
