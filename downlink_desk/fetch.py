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
from . import decode, satnogs
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


def store(conn, cfg, spec, frames):
    decoder = cfg["satellite"]["decoder"]
    horizon = dbm.now_ms() + 3600_000
    new = 0
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
            "INSERT OR IGNORE INTO frames(ts_ms,observer,station_id,app_source,frame_hex,frame_type,decoded_ok,fields_json,fetched_ms)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (ts, str(f.get("observer") or "")[:80], str(f.get("station_id") or "")[:40] or None,
             str(f.get("app_source") or "")[:40], hexf, ftype or "Unrecognised",
             1 if ftype else 0, json.dumps(fields if ftype else {}), dbm.now_ms()))
        if cur.rowcount:
            new += 1
            fid = cur.lastrowid
            conn.executemany("INSERT OR IGNORE INTO points(ts_ms,channel,value,frame_id) VALUES(?,?,?,?)",
                             [(ts, ch, v, fid) for ch, v in (decode.channel_points(spec, fields) if ftype else [])])
    conn.commit()
    return new


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
            log(str(e))
            return 3
        except satnogs.Refused as e:
            dbm.record_fetch(conn, False, e.status, 0, 0, str(e))
            log(str(e))
            return 4
        dbm.meta_set(conn, "auth_error", False)
        new = store(conn, cfg, spec, frames)
        dbm.meta_set(conn, "last_ok_ms", dbm.now_ms())
        dbm.record_fetch(conn, True, client.last_status, len(frames), new,
                         f"{new} new of {len(frames)} frames from SatNOGS")
        log(f"{new} new frames stored ({len(frames)} returned).")
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch telemetry from SatNOGS DB")
    ap.add_argument("--config")
    ap.add_argument("--force", action="store_true", help="fetch now instead of waiting for the interval")
    a = ap.parse_args(argv)
    sys.exit(run(find_config(a.config), force=a.force))


if __name__ == "__main__":
    main()
