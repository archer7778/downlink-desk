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


def valid_tle(tle1, tle2):
    """True if the two lines parse and propagate: a garbled TLE from upstream must never replace a
    good one, since every orbit calculation would then fail."""
    try:
        if not (isinstance(tle1, str) and isinstance(tle2, str) and tle1.startswith("1 ") and tle2.startswith("2 ")):
            return False
        sat = Satrec.twoline2rv(tle1.strip(), tle2.strip())
        e, r, _ = sat.sgp4(sat.jdsatepoch, sat.jdsatepochF)
        return e == 0 and all(math.isfinite(x) for x in r) and math.isfinite(tle_epoch_ms(tle1))
    except Exception:
        return False


def tle_epoch_ms(tle1):
    import datetime as _dt
    yy = int(tle1[18:20])
    doy = float(tle1[20:32])
    year = 2000 + yy if yy < 57 else 1900 + yy
    d = _dt.datetime(year, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(days=doy - 1)
    return int(d.timestamp() * 1000)


# ---------- sunlight, eclipse and ground track ----------

def _jd(t_ms):
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(t_ms / 1000, _dt.timezone.utc)
    return jday(d.year, d.month, d.day, d.hour, d.minute, d.second + d.microsecond / 1e6)


def sun_direction(jd):
    """Unit vector to the Sun in an Earth-centred inertial frame (low-precision almanac formula,
    good to about 0.01°, far better than the shadow model needs)."""
    n = jd - 2451545.0
    L = math.radians((280.460 + 0.9856474 * n) % 360)
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = L + math.radians(1.915) * math.sin(g) + math.radians(0.020) * math.sin(2 * g)
    eps = math.radians(23.439 - 0.0000004 * n)
    return (math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam))


def state(sat, t_ms):
    """Where the satellite is and whether it's in sunlight. Cylindrical Earth-shadow model.
    None if it can't be worked out (a nonsense time, or the orbit model gives up)."""
    try:
        jd, fr = _jd(t_ms)
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    err, r, _ = sat.sgp4(jd, fr)
    if err or not all(math.isfinite(x) for x in r):
        return None
    s = sun_direction(jd + fr)
    dot = r[0] * s[0] + r[1] * s[1] + r[2] * s[2]
    px, py, pz = r[0] - dot * s[0], r[1] - dot * s[1], r[2] - dot * s[2]
    sunlit = not (dot < 0 and math.sqrt(px * px + py * py + pz * pz) < WGS84_A)
    th = gmst(jd + fr)
    x = r[0] * math.cos(th) + r[1] * math.sin(th)
    y = -r[0] * math.sin(th) + r[1] * math.cos(th)
    lat = math.degrees(math.atan2(r[2], math.hypot(x, y)))
    lon = (math.degrees(math.atan2(y, x)) + 540) % 360 - 180
    alt = math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2) - WGS84_A
    return {"sunlit": sunlit, "lat": round(lat, 2), "lon": round(lon, 2), "alt_km": round(alt, 1)}


def eclipses(tle1, tle2, start_ms, end_ms, step_s=60):
    """Eclipse intervals [[start_ms, end_ms], ...] between two times."""
    sat = Satrec.twoline2rv(tle1, tle2)
    out, cur = [], None
    t = start_ms
    while t <= end_ms:
        st = state(sat, t)
        if st is None:
            break
        if not st["sunlit"] and cur is None:
            cur = [t, None]
        elif st["sunlit"] and cur is not None:
            cur[1] = t
            out.append(cur)
            cur = None
        t += step_s * 1000
    if cur is not None:
        cur[1] = end_ms
        out.append(cur)
    return out


def period_min(sat):
    """Orbital period in minutes (no_kozai is mean motion in radians per minute)."""
    return 2 * math.pi / sat.no_kozai


def in_saa(lat, lon):
    """Roughly inside the South Atlantic Anomaly at low-Earth-orbit heights, where the inner
    radiation belt dips closest to Earth and spacecraft see the most particle hits and upsets.
    An ellipse around the region (centre about 25°S 45°W); the real edge depends on altitude
    and drifts slowly west over the years, so treat this as approximate."""
    if lat is None or lon is None:
        return False
    return ((lat + 25) / 22) ** 2 + ((lon + 45) / 60) ** 2 <= 1


def sun_phases(tle1, tle2, times):
    """For each time: (minutes since the satellite last came out of Earth's shadow,
    length of that orbit's shadow in minutes). Minutes since sunrise is a position in the
    day/night cycle of the orbit, which is what drives a small satellite's temperatures.
    (None, 0.0) in a season with no shadow at all; (None, None) if it can't be worked out."""
    import bisect
    sat = Satrec.twoline2rv(tle1, tle2)
    P = period_min(sat) * 60_000
    ts = sorted(set(times))
    out = {}
    i = 0
    while i < len(ts):
        j = i
        while j + 1 < len(ts) and ts[j + 1] - ts[j] < P:
            j += 1
        a, b = ts[i] - int(2.2 * P), ts[j] + 60_000
        allecl = eclipses(tle1, tle2, a, b, 30)
        ecl = [e for e in allecl if e[1] < b]   # drop one still going at the end
        rises = [e[1] for e in ecl]
        lens = [None if e[0] <= a else (e[1] - e[0]) / 60_000 for e in ecl]   # first may be cut off
        for t in ts[i:j + 1]:
            k = bisect.bisect_right(rises, t) - 1
            if k >= 0 and t - rises[k] <= 1.05 * P:
                ln = lens[k] if lens[k] is not None else (lens[k + 1] if k + 1 < len(lens) else None)
                out[t] = (round((t - rises[k]) / 60_000, 2), None if ln is None else round(ln, 1))
            elif not ecl or (k == len(rises) - 1 and not any(e[0] <= t for e in allecl if e[1] >= b)):
                # no shadow at all, or none for over an orbit since the last one: a full-sunlight season
                out[t] = (None, 0.0)
            else:
                out[t] = (None, None)
        i = j + 1
    return out


def shadow_minutes(tle1, tle2, t_ms):
    """Length of the shadow in the orbit around t_ms (0 in a full-sunlight season)."""
    sat = Satrec.twoline2rv(tle1, tle2)
    P = period_min(sat) * 60_000
    a = t_ms - int(1.2 * P)
    full = [e for e in eclipses(tle1, tle2, a, t_ms + int(1.2 * P), 30) if e[0] > a and e[1] < t_ms + int(1.2 * P)]
    return round((full[0][1] - full[0][0]) / 60_000, 1) if full else 0.0


def now_state(tle1, tle2, t_ms):
    """Current position, sunlight, and when sunlight next changes (searches 2 h ahead)."""
    sat = Satrec.twoline2rv(tle1, tle2)
    st = state(sat, t_ms)
    if st is None:
        return None
    st["saa"] = in_saa(st["lat"], st["lon"])
    st["period_min"] = round(period_min(sat), 2)
    t = t_ms
    while t < t_ms + 2 * 3600_000:
        t += 30_000
        nx = state(sat, t)
        if nx and nx["sunlit"] != st["sunlit"]:
            st["next_change_ms"] = t
            break
    return st
