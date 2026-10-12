
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
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

