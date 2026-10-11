"""Decode raw frames with the SatNOGS community decoders and map fields to channels."""
import math

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
    """Keep JSON-friendly scalars; drop protocol noise."""
    out = {}
    for k, v in fields.items():
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, (int, float)) and math.isfinite(v):
            out[k] = v
        elif isinstance(v, str) and len(v) <= 200:
            out[k] = v
    return out


def frame_type(spec, fields):
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
