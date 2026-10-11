"""Fetch new frames from SatNOGS for every tracked satellite, decode them and store them.

Run by a systemd timer (every `poll_minutes`). Safe to run by hand:
    python -m downlink_desk.fetch                 # respects the polling interval
    python -m downlink_desk.fetch --force         # fetch now (still rate-limited)
    python -m downlink_desk.fetch --sat 62394     # just one satellite
    python -m downlink_desk.fetch --rebuild       # recompute values after editing a channels file
    python -m downlink_desk.fetch --summary       # print the morning summary (and send it, with --send)
    python -m downlink_desk.fetch --spaceweather  # refresh NOAA space weather now
    python -m downlink_desk.fetch --history 21    # fill in three weeks of older frames now
"""
import argparse
import fcntl
import json
import sys
import time

from . import baseline, catalog, spacewx, summary
from . import db as dbm
from . import decode, notify, satnogs
from .config import find_config
from .orbit import tle_epoch_ms

DAY_MS = 86_400_000
AUTO_SPEC = {"channels": [], "frame_types": []}


def spec_for(cfg, sat):
    """The channels file for a satellite, or automatic channels when it has none."""
    if sat.get("channels"):
        try:
            with open(cfg.resolve(sat["channels"]), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return AUTO_SPEC


def refresh_meta(conn, client, norad, log):
    """Once every 12 h: latest TLE and the satellite's SatNOGS record."""
    if dbm.now_ms() - (dbm.smeta_get(conn, norad, "meta_refreshed_ms", 0) or 0) < 12 * 3600_000:
        return
    try:
        sat = client.satellite(norad)
        if sat:
            dbm.smeta_set(conn, norad, "satellite", {k: sat.get(k) for k in
                          ("sat_id", "name", "status", "is_frequency_violator", "website", "reception_status")})
        t = client.tle(norad)
        if t and t.get("tle1") and t.get("tle2"):
            dbm.smeta_set(conn, norad, "tle", {"tle1": t["tle1"], "tle2": t["tle2"], "source": t.get("tle_source"),
                                              "updated": t.get("updated"), "epoch_ms": tle_epoch_ms(t["tle1"])})
        dbm.smeta_set(conn, norad, "meta_refreshed_ms", dbm.now_ms())
        log(f"[{norad}] Refreshed satellite record and TLE.")
    except satnogs.Refused as e:
        log(f"[{norad}] Could not refresh TLE/satellite record: {e}")


def refresh_observations(conn, cfg, norad, log):
    """Every 3 h: which SatNOGS stations are scheduled to listen, and when. Public, one request."""
    if dbm.now_ms() - (dbm.smeta_get(conn, norad, "obs_refreshed_ms", 0) or 0) < 3 * 3600_000:
        return
    dbm.smeta_set(conn, norad, "obs_refreshed_ms", dbm.now_ms())
    try:
        obs, complete = satnogs.scheduled_observations(cfg["satnogs"]["network_api"], norad)
    except satnogs.Refused as e:
        log(f"[{norad}] Could not load scheduled observations: {e}")
        return
    now = dbm.now_ms()
    conn.executemany(
        "INSERT INTO observations(id,start_ms,end_ms,station_id,station_name,lat,lng,max_alt,mode,fetched_ms,norad)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET start_ms=excluded.start_ms, end_ms=excluded.end_ms,"
        " max_alt=excluded.max_alt, fetched_ms=excluded.fetched_ms, norad=excluded.norad",
        [(o["id"], o["start_ms"], o["end_ms"], o["station_id"], o["station_name"], o["lat"], o["lng"],
          o["max_alt"], o["mode"], now, norad) for o in obs])
    # A scheduled observation that vanished before it started was cancelled; forget it
    # (only when we saw the whole list, so a missing one really is gone).
    if complete:
        conn.execute("DELETE FROM observations WHERE norad=? AND start_ms>? AND fetched_ms<?", (norad, now, now))
    conn.execute("DELETE FROM observations WHERE end_ms<?", (now - 30 * DAY_MS,))
    conn.commit()
    log(f"[{norad}] Loaded {len(obs)} scheduled observations.")


def _satrec(conn, norad):
    tle = dbm.smeta_get(conn, norad, "tle")
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


def store(conn, sat_row, spec, frames):
    norad, decoder = sat_row["norad"], sat_row["decoder"]
    horizon = dbm.now_ms() + 3600_000
    orbit = _satrec(conn, norad)
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
            "fetched_ms,sunlit,sub_lat,sub_lon,norad) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, str(f.get("observer") or "")[:80], str(f.get("station_id") or "")[:40] or None,
             str(f.get("app_source") or "")[:40], hexf, ftype or "Unrecognised",
             1 if ftype else 0, json.dumps(fields if ftype else {}), dbm.now_ms(), *_context(orbit, ts), norad))
        if cur.rowcount:
            new += 1
            fid = cur.lastrowid
            new_ids.append(fid)
            conn.executemany("INSERT OR IGNORE INTO points(ts_ms,channel,value,frame_id,norad) VALUES(?,?,?,?,?)",
                             [(ts, ch, v, fid, norad) for ch, v in (decode.channel_points(spec, fields) if ftype else [])])
    conn.commit()
    return new, new_ids


def fill_history(conn, cfg, client, sat_row, spec, log, pages=None, days=None):
    """Walk back through older frames on SatNOGS, a few pages at a time, until `history_days`
    are stored (or SatNOGS has nothing older). Old frames feed the health model and the
    battery check; they never trigger phone alerts. Returns True when history is complete."""
    sc = cfg["satnogs"]
    norad = sat_row["norad"]
    days = sc["history_days"] if days is None else days
    pages = int(sc["history_pages_per_fetch"] if pages is None else pages)
    target = dbm.now_ms() - int(days * DAY_MS)
    done = dbm.smeta_get(conn, norad, "history_done_ms")
    if pages <= 0 or (done is not None and done <= target):
        return True
    oldest = conn.execute("SELECT MIN(ts_ms) FROM frames WHERE norad=?", (norad,)).fetchone()[0]
    if oldest is None:
        return False
    if oldest <= target:
        dbm.smeta_set(conn, norad, "history_done_ms", target)
        return True
    frames = client.telemetry(norad, max_pages=pages, start_ms=target, end_ms=oldest - 1)
    new, _ = store(conn, sat_row, spec, frames)
    baseline.fill_phases(conn, norad)
    older = [satnogs.parse_ts(f.get("timestamp")) for f in frames]
    older = min([t for t in older if t], default=None)
    if len(frames) < pages * 25 or older is None or older >= oldest:   # nothing older in the window
        dbm.smeta_set(conn, norad, "history_done_ms", target)
        log(f"[{norad}] History complete back to {time.strftime('%Y-%m-%d', time.gmtime((older or oldest) / 1000))}.")
        return True
    log(f"[{norad}] History: {new} older frames added, now back to "
        f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime((older or oldest) / 1000))} UTC.")
    return False


def fetch_one(conn, cfg, client, sat_row, force, log):
    sc = cfg["satnogs"]
    norad = sat_row["norad"]
    now = dbm.now_ms()
    last = dbm.smeta_get(conn, norad, "last_attempt_ms", 0) or 0
    min_gap = (2 * 60_000) if force else int(sc["poll_minutes"] * 60_000 * 0.8)
    never_ok = dbm.smeta_get(conn, norad, "last_ok_ms") is None
    if now - last < min_gap and not (force and never_ok):   # a newly tracked satellite may retry straight away
        log(f"[{norad}] Fetched recently; skipping to stay polite to SatNOGS.")
        return 0
    refresh_meta(conn, client, norad, log)
    refresh_observations(conn, cfg, norad, log)
    meta = dbm.smeta_get(conn, norad, "satellite", {}) or {}
    if meta.get("is_frequency_violator") and now - last < DAY_MS:
        dbm.record_fetch(conn, True, None, 0, 0, "Satellite is flagged by SatNOGS; telemetry limited to once a day.", norad)
        log(f"[{norad}] Flagged as a frequency violator; holding to one request a day.")
        return 0
    if not client.token:
        dbm.record_fetch(conn, False, None, 0, 0, "No SatNOGS API key. Add it to secrets/satnogs_token.", norad)
        log("No SatNOGS API key configured.")
        return 2
    dbm.smeta_set(conn, norad, "last_attempt_ms", now)
    newest = conn.execute("SELECT MAX(ts_ms) m FROM frames WHERE norad=?", (norad,)).fetchone()["m"]
    pages = int(sc["max_pages"] if newest else sc["backfill_pages"])
    spec = spec_for(cfg, sat_row)
    try:
        frames = client.telemetry(norad, newer_than_ms=newest, max_pages=pages)
    except satnogs.AuthError as e:
        dbm.record_fetch(conn, False, client.last_status, 0, 0, str(e), norad)
        dbm.meta_set(conn, "auth_error", True)
        notify.fetch_failed(conn, cfg)
        log(str(e))
        return 3
    except satnogs.Refused as e:
        dbm.record_fetch(conn, False, e.status, 0, 0, str(e), norad)
        notify.fetch_failed(conn, cfg)
        log(f"[{norad}] {e}")
        return 4
    dbm.meta_set(conn, "auth_error", False)
    new, new_ids = store(conn, sat_row, spec, frames)
    dbm.smeta_set(conn, norad, "last_ok_ms", dbm.now_ms())
    dbm.meta_set(conn, "last_ok_ms", dbm.now_ms())
    dbm.record_fetch(conn, True, client.last_status, len(frames), new,
                     f"{sat_row['name']}: {new} new of {len(frames)} frames", norad)
    log(f"[{norad}] {sat_row['name']}: {new} new frames stored ({len(frames)} returned).")
    notify.fetch_ok(conn)
    baseline.fill_phases(conn, norad)   # orbit phase for the new frames, before the model looks at them
    notify.after_fetch(conn, cfg, spec, new_ids, log, sat_row)
    try:
        fill_history(conn, cfg, client, sat_row, spec, log)
    except satnogs.Refused as e:
        log(f"[{norad}] History paused: {e}")
    try:
        notify.model_changes(conn, cfg, sat_row, baseline.compute(conn, spec, norad), log)
    except Exception as e:   # the model is an extra; a bad channel must never stop fetching
        log(f"[{norad}] Health model skipped: {e}")
    return 0


def run(cfg, force=False, log=print, only=None):
    conn = dbm.open_db(cfg)
    lock = open(cfg.db_path.parent / "fetch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another fetch is running; skipping.")
        return 0
    try:
        client = satnogs.Client(cfg["satnogs"]["api"], cfg.token)   # one client: pacing is shared
        dbm.meta_set(conn, "last_attempt_ms", dbm.now_ms())
        worst = 0
        for sat_row in dbm.satellites(conn):
            if only and sat_row["norad"] != only:
                continue
            rc = fetch_one(conn, cfg, client, sat_row, force, log)
            worst = max(worst, rc)
            if rc == 3:   # the key is bad for every satellite; stop
                break
        catalog.refresh(conn, cfg, log)
        spacewx.refresh(conn, cfg, log)
        for sat_row in dbm.satellites(conn):   # catch up orbit phases (also after an upgrade)
            baseline.fill_phases(conn, sat_row["norad"])
        try:
            summary.maybe_send(conn, cfg, log)
        except Exception as e:
            log(f"Morning summary skipped: {e}")
        return worst
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def history(cfg, days, only=None, log=print):
    """One-off: fill in history now instead of a few pages per fetch."""
    conn = dbm.open_db(cfg)
    lock = open(cfg.db_path.parent / "fetch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("A fetch is running; try again in a minute.")
        return 1
    try:
        client = satnogs.Client(cfg["satnogs"]["api"], cfg.token)
        for sat_row in dbm.satellites(conn):
            if only and sat_row["norad"] != only:
                continue
            dbm.smeta_set(conn, sat_row["norad"], "history_done_ms", None)
            spec = spec_for(cfg, sat_row)
            log(f"[{sat_row['norad']}] Filling in {days:g} days of {sat_row['name']} history from SatNOGS "
                "(one request every 11 s; Ctrl+C is safe, it carries on at the next fetch)...")
            try:
                while not fill_history(conn, cfg, client, sat_row, spec, log, pages=10, days=days):
                    pass
            except satnogs.Refused as e:
                log(f"SatNOGS paused us: {e}. The rest will fill in with the regular fetches.")
        n = conn.execute("SELECT COUNT(*), MIN(ts_ms) FROM frames").fetchone()
        log(f"Done: {n[0]} frames stored, oldest {time.strftime('%Y-%m-%d', time.gmtime((n[1] or 0) / 1000))}.")
        return 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def rebuild(cfg, log=print):
    """Recompute every channel value and the orbit context from the stored frames,
    using the current channels files. Run after changing units or conversions."""
    conn = dbm.open_db(cfg)
    total = 0
    for sat_row in dbm.satellites(conn, enabled_only=False):
        norad = sat_row["norad"]
        spec = spec_for(cfg, sat_row)
        orbit = _satrec(conn, norad)
        if orbit is not None:
            for r in conn.execute("SELECT id, ts_ms FROM frames WHERE norad=?", (norad,)).fetchall():
                conn.execute("UPDATE frames SET sunlit=?, sub_lat=?, sub_lon=?, phase_min=NULL, shadow_min=NULL WHERE id=?",
                             (*_context(orbit, r["ts_ms"]), r["id"]))
            conn.commit()
            baseline.fill_phases(conn, norad, limit=10**9)
        rows = conn.execute("SELECT id, ts_ms, fields_json FROM frames WHERE decoded_ok=1 AND norad=?", (norad,)).fetchall()
        conn.execute("DELETE FROM points WHERE norad=?", (norad,))
        for r in rows:
            pts = decode.channel_points(spec, json.loads(r["fields_json"] or "{}"))
            conn.executemany("INSERT OR IGNORE INTO points(ts_ms,channel,value,frame_id,norad) VALUES(?,?,?,?,?)",
                             [(r["ts_ms"], ch, v, r["id"], norad) for ch, v in pts])
            total += len(pts)
        log(f"[{norad}] {sat_row['name']}: rebuilt from {len(rows)} decoded frames.")
    conn.commit()
    log(f"Rebuilt {total} channel values.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch telemetry from SatNOGS DB")
    ap.add_argument("--config")
    ap.add_argument("--force", action="store_true", help="fetch now instead of waiting for the interval")
    ap.add_argument("--sat", type=int, help="only this NORAD number")
    ap.add_argument("--rebuild", action="store_true", help="recompute channel values after editing a channels file")
    ap.add_argument("--catalog", action="store_true", help="refresh the satellite catalog now")
    ap.add_argument("--summary", action="store_true", help="print the morning summary")
    ap.add_argument("--send", action="store_true", help="with --summary: also push it to your phone now")
    ap.add_argument("--spaceweather", action="store_true", help="refresh NOAA space weather now")
    ap.add_argument("--history", type=float, metavar="DAYS",
                    help="fill in this many days of older frames now (polite: about 2 min per 1,000 frames)")
    a = ap.parse_args(argv)
    cfg = find_config(a.config)
    if a.history:
        sys.exit(history(cfg, a.history, only=a.sat))
    if a.spaceweather:
        spacewx.refresh(dbm.open_db(cfg), cfg, force=True)
        sys.exit(0)
    if a.summary:
        conn = dbm.open_db(cfg)
        s = summary.build(conn, cfg)
        print(s["title"])
        print(s["text"])
        if a.send:
            print("Sent." if summary.maybe_send(conn, cfg, force=True) else "Not sent: set ntfy_topic in config.toml.")
        sys.exit(0)
    if a.catalog:
        catalog.refresh(dbm.open_db(cfg), cfg, force=True)
        sys.exit(0)
    sys.exit(rebuild(cfg) if a.rebuild else run(cfg, force=a.force, only=a.sat))


if __name__ == "__main__":
    main()
