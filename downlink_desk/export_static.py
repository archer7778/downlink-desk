"""Write a read-only snapshot of the console as static files, for the public demo on GitHub Pages.

    python -m downlink_desk.export_static --out site

Every API response the console needs is saved as JSON next to a copy of the console page,
which reads them instead of a live Pi. Positions for the next few hours are precomputed so
"where is it now" keeps moving between snapshots. Writing actions are switched off.
"""
import argparse
import json
import shutil
import time
from pathlib import Path

from . import __version__, baseline, logchain
from . import db as dbm
from .catalog import entries
from .config import find_config
from .orbit import now_state
from .server import WEB, App

DAY = 86_400_000


def _w(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")


def export(cfg, out, refresh_hours=6, log=print):
    out = Path(out)
    api = out / "api"
    if api.exists():
        shutil.rmtree(api)
    app = App(cfg)
    now = dbm.now_ms()
    sat = app.sat({})
    n = sat["norad"]
    t0 = time.time()

    conf = app.config(sat)
    conf["poll_minutes"] = refresh_hours * 60    # the snapshot is rebuilt every few hours
    conf["write_protected"] = False
    conf["static"] = True
    _w(api / "config.json", conf)
    _w(api / "satellites.json", [f for f in app.fleet() if f["norad"] == n])
    _w(api / "telemetry.json", app.telemetry(sat, since=now - 8 * DAY))
    health = app.health(sat)
    health.update(storage=None, update=None, auto_update=False, server_started_ms=now)
    _w(api / "health.json", health)
    _w(api / "passes.json", app.passes(sat, 48))
    _w(api / "observations.json", app.observations(sat, 3))
    _w(api / "eclipses.json", app.eclipses(sat, now - 7 * DAY, now + DAY))
    model = app.model(sat)
    _w(api / "model.json", model)
    _w(api / "battery.json", app.battery(sat))
    _w(api / "spaceweather.json", app.spaceweather(7))
    summ = app.summary()
    summ.update(phone=False)
    _w(api / "summary.json", summ)
    with app.ro() as c:
        _w(api / "catalog.json", [e for e in entries(c, cfg) if e["norad"] == n])
        _w(api / "log.json", logchain.entries(c))
        _w(api / "verify.json", logchain.verify(c))
    _w(api / "notes.json", {"text": "", "cs": "", "t": 0})
    _w(api / "frames.json", app.frames(sat, 100, 2**53))
    for ch in model.get("channels", []):
        with app.ro() as c:
            _w(api / "profile" / f"{ch['id']}.json", baseline.profile(c, app.spec(sat), n, ch["id"]))
    # where the satellite will be, every 2 minutes for the next refresh period and a bit
    tle = app._tle(sat)
    track = []
    if tle:
        for t in range(now, now + (refresh_hours + 2) * 3600_000, 120_000):
            st = now_state(tle["tle1"], tle["tle2"], t)
            if st:
                track.append({"t": t, **st})
    _w(api / "now.json", track)

    page = (WEB / "index.html").read_text(encoding="utf-8")
    inject = (f'<script>window.DD_STATIC={{"base":"api/","snapshot":{now},"refresh_hours":{refresh_hours},'
              f'"repo":"https://github.com/archer7778/downlink-desk"}};</script>\n')
    page = page.replace("<script>\n(()=>{", inject + "<script>\n(()=>{", 1)
    page = page.replace("<title>Downlink Desk Console</title>",
                        f"<title>Downlink Desk: live demo with real {sat['name']} data</title>", 1)
    (out / "index.html").write_text(page, encoding="utf-8")
    shutil.copy(WEB / "training.html", out / "training.html")
    (out / ".nojekyll").write_text("")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    log(f"Exported {sat['name']} snapshot to {out} ({size / 1e6:.1f} MB) in {time.time() - t0:.1f}s, "
        f"Downlink Desk {__version__}.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export a read-only static demo of the console")
    ap.add_argument("--config")
    ap.add_argument("--out", default="site")
    ap.add_argument("--refresh-hours", type=int, default=6)
    a = ap.parse_args(argv)
    export(find_config(a.config), a.out, a.refresh_hours)


if __name__ == "__main__":
    main()
