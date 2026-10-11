"""Configuration loading. Paths in the config file are relative to the file's folder."""
import json
import os
import tomllib
from pathlib import Path

DEFAULTS = {
    "satellite": {"norad": 60237, "name": "GRBBeta", "decoder": "grbbeta",
                  "channels": "channels/grbbeta.json"},
    "satnogs": {"api": "https://db.satnogs.org/api", "network_api": "https://network.satnogs.org/api",
                "token_file": "secrets/satnogs_token",
                "poll_minutes": 30, "max_pages": 3, "backfill_pages": 6,
                "history_days": 21, "history_pages_per_fetch": 8},
    "station": {"name": "Home", "lat": 0.0, "lon": 0.0, "alt_m": 0.0, "min_elevation": 10.0},
    "server": {"host": "0.0.0.0", "port": 8080, "write_passcode": ""},
    "storage": {"db": "data/downlink-desk.db"},
    "alerts": {"stale_hours": 12, "upload_grace_hours": 3, "silent_after": 3, "min_obs_elevation": 15},
    "notify": {"ntfy_topic": "", "ntfy_server": "https://ntfy.sh", "console_url": "",
               "summary_hour": 7, "timezone": ""},
    "spaceweather": {"enabled": True, "api": "https://services.swpc.noaa.gov"},
}


class Config:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        with open(self.path, "rb") as f:
            raw = tomllib.load(f)
        self.data = {k: {**v, **raw.get(k, {})} for k, v in DEFAULTS.items()}
        for k, v in raw.items():
            if k not in self.data:
                self.data[k] = v

    def __getitem__(self, key):
        return self.data[key]

    def resolve(self, rel):
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self):
        return self.resolve(self["storage"]["db"])

    @property
    def token(self):
        env = os.environ.get("SATNOGS_TOKEN", "").strip()
        if env:
            return env
        try:
            return self.resolve(self["satnogs"]["token_file"]).read_text().strip()
        except OSError:
            return ""

    def channels(self):
        with open(self.resolve(self["satellite"]["channels"]), encoding="utf-8") as f:
            return json.load(f)


def find_config(arg=None):
    for cand in [arg, os.environ.get("DOWNLINK_DESK_CONFIG"), "config.toml",
                 Path(__file__).resolve().parent.parent / "config.toml"]:
        if cand and Path(cand).is_file():
            return Config(cand)
    raise SystemExit("No config.toml found. Copy config.example.toml to config.toml and edit it.")
