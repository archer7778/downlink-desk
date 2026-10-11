"""SQLite storage. One small file; WAL mode keeps the SD card happier and lets
the web server read while the fetch job writes."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
  id INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  observer TEXT,
  station_id TEXT,
  app_source TEXT,
  frame_hex TEXT NOT NULL,
  frame_type TEXT,
  decoded_ok INTEGER NOT NULL DEFAULT 0,
  fields_json TEXT,
  fetched_ms INTEGER NOT NULL,
  UNIQUE (ts_ms, frame_hex)
);
CREATE INDEX IF NOT EXISTS frames_ts ON frames (ts_ms);
CREATE TABLE IF NOT EXISTS points (
  ts_ms INTEGER NOT NULL,
  channel TEXT NOT NULL,
  value REAL NOT NULL,
  frame_id INTEGER NOT NULL,
  PRIMARY KEY (channel, ts_ms, frame_id)
);
CREATE INDEX IF NOT EXISTS points_ts ON points (ts_ms);
CREATE TABLE IF NOT EXISTS log (
  seq INTEGER PRIMARY KEY,
  t_ms INTEGER NOT NULL,
  kind TEXT NOT NULL,
  data_json TEXT NOT NULL,
  msg TEXT NOT NULL,
  cs TEXT NOT NULL,
  prev TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fetches (
  id INTEGER PRIMARY KEY,
  t_ms INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  http_status INTEGER,
  frames_seen INTEGER NOT NULL DEFAULT 0,
  frames_new INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  station_id INTEGER,
  station_name TEXT,
  lat REAL,
  lng REAL,
  max_alt REAL,
  mode TEXT,
  fetched_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_end ON observations (end_ms);
CREATE TABLE IF NOT EXISTS satellites (
  norad INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  decoder TEXT NOT NULL,
  channels TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  added_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS spacewx (
  kind TEXT NOT NULL,
  t_ms INTEGER NOT NULL,
  value REAL NOT NULL,
  PRIMARY KEY (kind, t_ms)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def now_ms():
    return int(time.time() * 1000)


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(frames)")}
    for col, typ in (("sunlit", "INTEGER"), ("sub_lat", "REAL"), ("sub_lon", "REAL"),
                     ("phase_min", "REAL"), ("shadow_min", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE frames ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


PER_SAT_META = ("tle", "satellite", "last_attempt_ms", "last_ok_ms", "auth_error", "meta_refreshed_ms",
                "obs_refreshed_ms")


def migrate(conn, cfg):
    """Bring a single-satellite (v0.1/v0.2) database up to multi-satellite form, once.
    Existing rows belong to the satellite in config.toml."""
    s = cfg["satellite"]
    norad = int(s["norad"])
    for t in ("frames", "points", "observations", "fetches"):
        if "norad" not in {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}:
            conn.execute(f"ALTER TABLE {t} ADD COLUMN norad INTEGER")
            conn.execute(f"UPDATE {t} SET norad=?", (norad,))
    conn.execute("CREATE INDEX IF NOT EXISTS frames_sat_ts ON frames (norad, ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS points_sat_ts ON points (norad, ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS obs_sat_end ON observations (norad, end_ms)")
    if not conn.execute("SELECT 1 FROM satellites LIMIT 1").fetchone():
        conn.execute("INSERT INTO satellites(norad,name,decoder,channels,enabled,added_ms) VALUES(?,?,?,?,1,?)",
                     (norad, s["name"], s["decoder"], s.get("channels", ""), now_ms()))
    for k in PER_SAT_META:
        old = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        if old and not conn.execute("SELECT 1 FROM meta WHERE key=?", (f"{k}:{norad}",)).fetchone():
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", (f"{k}:{norad}", old["value"]))
            conn.execute("DELETE FROM meta WHERE key=?", (k,))
    conn.commit()


def satellites(conn, enabled_only=True):
    q = "SELECT * FROM satellites" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY added_ms"
    return [dict(r) for r in conn.execute(q)]


def smeta_get(conn, norad, key, default=None):
    return meta_get(conn, f"{key}:{norad}", default)


def smeta_set(conn, norad, key, value):
    meta_set(conn, f"{key}:{norad}", value)


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


def record_fetch(conn, ok, status, seen, new, message, norad=None):
    conn.execute("INSERT INTO fetches(t_ms,ok,http_status,frames_seen,frames_new,message,norad) VALUES(?,?,?,?,?,?,?)",
                 (now_ms(), 1 if ok else 0, status, seen, new, message[:500], norad))
    conn.execute("DELETE FROM fetches WHERE id NOT IN (SELECT id FROM fetches ORDER BY id DESC LIMIT 500)")
    conn.commit()


def open_db(cfg):
    conn = connect(cfg.db_path)
    migrate(conn, cfg)
    return conn
