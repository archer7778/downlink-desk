"""Pass prediction over your station from the latest TLE, using SGP4."""
import math

from sgp4.api import Satrec, jday

WGS84_A = 6378.137
WGS84_F = 1 / 298.257223563


def gmst(jd_ut1):
    t = (jd_ut1 - 2451545.0) / 36525.0
    g = 67310.54841 + (876600 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3
    return math.radians((g % 86400) / 240.0)


def observer_ecef(lat, lon, alt_km):
    la, lo = math.radians(lat), math.radians(lon)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return ((n + alt_km) * math.cos(la) * math.cos(lo),
            (n + alt_km) * math.cos(la) * math.sin(lo),
            (n * (1 - e2) + alt_km) * math.sin(la))


def look(sat, t_ms, lat, lon, alt_km):
    """Elevation and azimuth in degrees at a unix time in ms, or None on SGP4 error."""
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(t_ms / 1000, _dt.timezone.utc)
    jd, fr = jday(d.year, d.month, d.day, d.hour, d.minute, d.second + d.microsecond / 1e6)
    err, r, _ = sat.sgp4(jd, fr)
    if err:
        return None
    th = gmst(jd + fr)
    x = r[0] * math.cos(th) + r[1] * math.sin(th)
    y = -r[0] * math.sin(th) + r[1] * math.cos(th)
    z = r[2]
    ox, oy, oz = observer_ecef(lat, lon, alt_km)
    rx, ry, rz = x - ox, y - oy, z - oz
    la, lo = math.radians(lat), math.radians(lon)
    s = math.sin(la) * math.cos(lo) * rx + math.sin(la) * math.sin(lo) * ry - math.cos(la) * rz
    e = -math.sin(lo) * rx + math.cos(lo) * ry
    zz = math.cos(la) * math.cos(lo) * rx + math.cos(la) * math.sin(lo) * ry + math.sin(la) * rz
    rng = math.sqrt(rx * rx + ry * ry + rz * rz)
    el = math.degrees(math.asin(zz / rng))
    az = (math.degrees(math.atan2(e, -s)) + 360) % 360
    return el, az


def passes(tle1, tle2, start_ms, hours, lat, lon, alt_m=0.0, min_el=10.0, step_s=30):
    sat = Satrec.twoline2rv(tle1, tle2)
    alt_km = alt_m / 1000
    out, cur, prev_el = [], None, None
    end = start_ms + int(hours * 3600_000)
    t = start_ms

    def refine(a, b):
        for _ in range(12):
            m = (a + b) // 2
            lm = look(sat, m, lat, lon, alt_km)
            if lm and lm[0] > 0:
                b = m
            else:
                a = m
        return b

    while t <= end:
        lk = look(sat, t, lat, lon, alt_km)
        if lk is None:
            break
        el, az = lk
        if el > 0 and (prev_el is None or prev_el <= 0):
            aos = refine(t - step_s * 1000, t) if prev_el is not None else t
            cur = {"aos": aos, "max_el": el, "max_t": t, "az_aos": round(look(sat, aos, lat, lon, alt_km)[1])}
        if cur and el > cur["max_el"]:
            cur["max_el"], cur["max_t"] = el, t
        if el <= 0 and cur:
            def refine_los(a, b):
                for _ in range(12):
                    m = (a + b) // 2
                    lm = look(sat, m, lat, lon, alt_km)
                    if lm and lm[0] > 0:
                        a = m
                    else:
                        b = m
                return a
            los = refine_los(t - step_s * 1000, t)
            cur["los"] = los
            cur["az_los"] = round(look(sat, los, lat, lon, alt_km)[1])
            cur["max_el"] = round(cur["max_el"], 1)
            if cur["max_el"] >= min_el:
                out.append(cur)
            cur = None
        prev_el = el
        t += step_s * 1000
    return out


def tle_epoch_ms(tle1):
    import datetime as _dt
    yy = int(tle1[18:20])
    doy = float(tle1[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    d = _dt.datetime(year, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(days=doy - 1)
    return int(d.timestamp() * 1000)
