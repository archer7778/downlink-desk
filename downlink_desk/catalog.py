"""Which satellites Downlink Desk can follow: everything in orbit on SatNOGS that has a decoder
installed on this Pi, with hand-written bios for a featured few."""
import importlib.util
import json
import re
from pathlib import Path

from . import db as dbm
from . import satnogs

ROOT = Path(__file__).resolve().parent.parent
DAY = 86_400_000


def featured():
    try:
        return json.loads((ROOT / "catalog" / "featured.json").read_text(encoding="utf-8"))["satellites"]
    except (OSError, ValueError, KeyError):
        return []


def installed_decoders():
    spec = importlib.util.find_spec("satnogsdecoders")
    if not spec or not spec.submodule_search_locations:
        return set()
    d = Path(list(spec.submodule_search_locations)[0]) / "decoder"
    return {p.stem for p in d.glob("*.py") if not p.stem.startswith("_")}


def _path(cfg):
    return cfg.db_path.parent / "catalog.json"


def refresh(conn, cfg, log=print, force=False):
    """Once a day: the full SatNOGS satellite list, filtered to ones we can decode. Public, no key."""
    if not force and dbm.now_ms() - (dbm.meta_get(conn, "catalog_refreshed_ms", 0) or 0) < DAY:
        return
    dbm.meta_set(conn, "catalog_refreshed_ms", dbm.now_ms())
    decs = installed_decoders()
    c = satnogs.Client(cfg["satnogs"]["api"])
    url, params, items = f"{c.api}/satellites/", {"format": "json"}, []
    try:
        for _ in range(30):
            data = c._get(url, params, timeout=60)
            items.extend(data.get("results", []) if isinstance(data, dict) else data)
            url = data.get("next") if isinstance(data, dict) else None
            params = None
            if not url:
                break
    except satnogs.Refused as e:
        log(f"Could not refresh the satellite catalog: {e}")
        return
    out = []
    for s in items:
        if s.get("decayed") or str(s.get("status", "")).lower() in ("dead", "re-entered", "future"):
            continue
        norad = s.get("norad_cat_id")
        tels = s.get("telemetries") or []
        dec = next((t.get("decoder") for t in tels if isinstance(t, dict) and t.get("decoder") in decs), None)
        if not norad or not dec:
            continue
        out.append({"norad": int(norad), "name": str(s.get("name") or "")[:60], "decoder": dec,
                    "names": str(s.get("names") or "")[:120], "countries": str(s.get("countries") or "")[:40],
                    "launched": (s.get("launched") or "")[:10], "website": str(s.get("website") or "")[:200],
                    "operator": "" if str(s.get("operator")) == "None" else str(s.get("operator") or "")[:80],
                    "status": str(s.get("status") or "")})
    _path(cfg).write_text(json.dumps(out), encoding="utf-8")
    log(f"Catalog: {len(out)} decodable satellites in orbit (from {len(items)} on SatNOGS).")


def entries(conn, cfg):
    """Featured satellites first (with bios), then everything else we can decode."""
    try:
        rest = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rest = []
    decs = installed_decoders()
    tracked = {s["norad"]: s for s in dbm.satellites(conn, enabled_only=False)}
    out, seen = [], set()
    by_norad = {r["norad"]: r for r in rest}
    for f in featured():
        if decs and f["decoder"] not in decs:
            continue
        e = {**by_norad.get(f["norad"], {}), **f, "featured": True}
        out.append(e)
        seen.add(f["norad"])
    for r in sorted(rest, key=lambda r: r["name"].lower()):
        if r["norad"] not in seen:
            out.append({**r, "featured": False})
            seen.add(r["norad"])
    for e in out:
        t = tracked.get(e["norad"])
        e["tracked"] = bool(t and t["enabled"])
    return out


def find(conn, cfg, norad):
    return next((e for e in entries(conn, cfg) if e["norad"] == norad), None)


def channels_for(entry):
    ch = entry.get("channels") or ""
    return ch if ch and re.fullmatch(r"channels/[\w.-]+\.json", ch) and (ROOT / ch).is_file() else ""
