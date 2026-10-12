"""Decode raw frames with the SatNOGS community decoders and map fields to channels."""
import math
import re

_decoder_mod = None


def _decoders():
    global _decoder_mod
    if _decoder_mod is None:
        from satnogsdecoders import decoder  # imports every decoder; done once per run
        _decoder_mod = decoder
    return _decoder_mod


def decode(decoder_name, frame_hex):
    """Return a flat {field: value} dict, or {} when the frame isn't one this decoder knows."""
    mod = _decoders()
    cls = getattr(mod, decoder_name.capitalize().replace("_", ""), None)
    if cls is None:
        raise SystemExit(f"satnogs-decoders has no decoder called '{decoder_name}'")
    try:
        raw = bytes.fromhex(frame_hex)
        return mod.get_fields(cls.from_bytes(raw))
    except Exception:
        return {}


def clean(fields):
    """Keep JSON-friendly scalars; drop protocol noise. Capped, since frames come from anyone."""
    out = {}
    for k, v in list(fields.items())[:300]:
        k = str(k)[:80]
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (int, float)) and math.isfinite(v):
            out[k] = v
        elif isinstance(v, str) and len(v) <= 200:
            out[k] = v
    return out


# Protocol plumbing rather than spacecraft health; left out of automatic channels.
JUNK = re.compile(r"(callsign|ssid|(^|_)ctl$|(^|_)pid$|crc|hbit|csp_hdr|_raw$|message|rpt_|repeater|(^|_)digi_|"
                  r"_mask$|frame_?type|packet_?type|_spare|reserved|padding)", re.I)
AUTO_MAX = 80


def auto_points(fields):
    """Every numeric, non-protocol field becomes a channel, in raw units."""
    out = []
    for k, v in fields.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or JUNK.search(k):
            continue
        out.append((k[:60], float(v)))
        if len(out) >= AUTO_MAX:
            break
    return out


def frame_type(spec, fields):
    if not spec.get("channels"):          # automatic mode: anything with health-like fields counts
        return "Decoded" if auto_points(fields) else None
    for ft in spec.get("frame_types", []):
        if ft["if_field"] in fields:
            return ft["name"]
    return None


def transform(name, x, ch):
    if name in (None, "", "none"):
        return x
    if name == "scale":
        return x * ch.get("scale", 1) + ch.get("offset", 0)
    if name == "mv_auto":            # millivolts unless it already looks like volts
        return x / 1000 if abs(x) > 100 else x
    if name == "ma_auto":
        return x
    if name == "kelvin_auto":        # kelvin, or tenths of a kelvin
        return (x / 10 if x > 1000 else x) - 273.15
    if name == "s_to_h":
        return x / 3600
    raise ValueError(f"unknown transform {name}")


def channel_points(spec, fields):
    if not spec.get("channels"):
        return auto_points(fields)
    pts = []
    for ch in spec["channels"]:
        for field in ch.get("fields", [ch.get("field")]):
            v = fields.get(field)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                y = transform(ch.get("transform"), float(v), ch)
                if math.isfinite(y):
                    pts.append((ch["id"], round(y, 6)))
                break
    return pts
