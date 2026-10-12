"""A polite SatNOGS DB client.

SatNOGS is run by volunteers (Libre Space Foundation). Its telemetry endpoint is
throttled per user (about six requests a minute) and satellites flagged as
frequency violators may only be asked for once a day. This client keeps well
inside that: at least 11 s between requests, honours Retry-After, gives up the
cycle on long refusals, and never follows links off the SatNOGS host.
"""
import http.client
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

USER_AGENT = f"DownlinkDesk/{__version__} (+https://github.com/archer7778/downlink-desk)"
MIN_GAP_S = 11.0


class AuthError(Exception):
    pass


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


_LAST = {}                  # host -> monotonic time of the last request
_PACE_LOCK = threading.Lock()


def _first(data):
    """The first record of a list answer (bare or paged), or None."""
    items = data.get("results", [data]) if isinstance(data, dict) else data
    if isinstance(items, list):
        return next((x for x in items if isinstance(x, dict)), None)
    return None


class Client:
    def __init__(self, api, token=""):
        self.api = api.rstrip("/")
        self.host = urllib.parse.urlparse(self.api).netloc
        self.token = token
        # Tests can speed up pacing, but only against a local mock server.
        local = self.host.split(":")[0] in ("127.0.0.1", "localhost")
        self.gap = float(os.environ.get("DOWNLINK_DESK_MIN_GAP", MIN_GAP_S)) if local else MIN_GAP_S
        self.last_status = None

    def _get(self, url, params=None, auth=False, timeout=30, with_link=False):
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if urllib.parse.urlparse(url).netloc != self.host:
            raise Refused(None, "refusing to follow a link off the SatNOGS host")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if auth and self.token:
            headers["Authorization"] = "Token " + self.token
        for attempt in range(3):
            # Pacing is per host and shared by every client in this process, so the catalog,
            # telemetry and history requests can't end up back to back.
            with _PACE_LOCK:
                wait = self.gap - (time.monotonic() - _LAST.get(self.host, -1e9))
                if wait > 0:
                    time.sleep(wait)
                _LAST[self.host] = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                    self.last_status = r.status
                    data = json.loads(r.read().decode("utf-8"))
                    return (data, _next_link(r.headers.get("Link"))) if with_link else data
            except urllib.error.HTTPError as e:
                self.last_status = e.code
                if e.code in (401, 403):
                    raise AuthError(f"SatNOGS refused the API key (HTTP {e.code})")
                if e.code in (429, 503):
                    try:   # seconds, or (rarely) an HTTP date: then just use the default
                        retry = max(0, int(e.headers.get("Retry-After") or 30))
                    except ValueError:
                        retry = 30
                    if retry > 120 or attempt == 2:
                        raise Refused(e.code, f"SatNOGS asked us to wait {retry} s; trying next cycle")
                    time.sleep(retry + 1)
                    continue
                raise Refused(e.code, f"SatNOGS answered HTTP {e.code}")
            except urllib.error.URLError as e:
                self.last_status = None
                raise Refused(None, f"Could not reach SatNOGS: {e.reason}")
            except (OSError, http.client.HTTPException) as e:
                # dropped mid-answer: a timeout or reset while reading isn't wrapped as a URLError
                self.last_status = None
                raise Refused(None, f"Connection to SatNOGS broke off ({type(e).__name__}); trying next cycle")
            except ValueError:   # truncated or non-JSON body (a proxy's error page, say)
                raise Refused(self.last_status, "SatNOGS sent an answer that wasn't valid JSON; trying next cycle")
        raise Refused(self.last_status, "SatNOGS kept refusing")

    def satellite(self, norad):
        return _first(self._get(f"{self.api}/satellites/", {"norad_cat_id": norad, "format": "json"}))

    def tle(self, norad):
        return _first(self._get(f"{self.api}/tle/", {"norad_cat_id": norad, "format": "json"}))

    def telemetry(self, norad, newer_than_ms=None, max_pages=3, start_ms=None, end_ms=None):
        """Newest-first frames. Stops paging once it reaches frames we already have.
        start_ms/end_ms limit the time window (used to fill in history, oldest end first).
        Handles both the cursor shape ({next, results}) and a bare list."""
        frames, url, params = [], f"{self.api}/telemetry/", {"satellite": norad, "format": "json"}
        if start_ms is not None:
            params["start"] = _iso(start_ms)
        if end_ms is not None:
            params["end"] = _iso(end_ms)
        self.exhausted = False   # True when SatNOGS had no further page (not just our page limit)
        self.pages_used = 0
        for _ in range(max_pages):
            data = self._get(url, params, auth=True)
            self.pages_used += 1
            page = data.get("results", []) if isinstance(data, dict) else data
            page = [f for f in page if isinstance(f, dict)] if isinstance(page, list) else []
            frames.extend(page)
            if newer_than_ms is not None and page:
                oldest = parse_ts(page[-1].get("timestamp"))
                if oldest is not None and oldest <= newer_than_ms:
                    break
            url = data.get("next") if isinstance(data, dict) else None
            params = None
            if not url or not page:
                self.exhausted = True
                break
        return frames


def scheduled_observations(network_api, norad):
    """Upcoming observations of the satellite scheduled on SatNOGS Network (public, no key).
    One request; the API returns the soonest-ending ~25 future observations."""
    c = Client(network_api)
    items = []
    url, params = f"{c.api}/observations/", {"norad_cat_id": norad, "status": "future", "format": "json"}
    complete = False
    # The list comes latest-first, 25 at a time, with the next page in a Link header
    # (the API rejects a page= parameter). Up to 4 pages reaches the soonest ones.
    for page in range(4):
        try:
            data, nxt = c._get(url, params, with_link=True)
        except (Refused, AuthError):
            if page == 0:
                raise
            break
        got = data.get("results", []) if isinstance(data, dict) else data
        got = got if isinstance(got, list) else []
        items.extend(o for o in got if isinstance(o, dict))
        nxt = nxt or (data.get("next") if isinstance(data, dict) else None)
        if not nxt or not got:
            complete = True
            break
        url, params = nxt, None
    out = []
    for o in items:
        if o.get("norad_cat_id") not in (None, norad):
            continue
        st, en = parse_ts(o.get("start")), parse_ts(o.get("end"))
        if st is None or en is None:
            continue
        try:   # one malformed record shouldn't lose the whole schedule
            out.append({"id": int(o["id"]), "start_ms": st, "end_ms": en, "station_id": o.get("ground_station"),
                        "station_name": str(o.get("station_name") or "")[:80], "lat": _f(o.get("station_lat"), 90),
                        "lng": _f(o.get("station_lng"), 180), "max_alt": _f(o.get("max_altitude"), 90),
                        "mode": str(o.get("transmitter_description") or o.get("transmitter_mode") or "")[:80]})
        except (KeyError, TypeError, ValueError):
            continue
    return out, complete


def _f(v, lim):
    """A coordinate or angle from outside data: a finite number within ±lim, else None."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and -lim <= v <= lim else None


def _next_link(header):
    """The rel="next" URL from an HTTP Link header, if any."""
    for part in (header or "").split(","):
        bits = part.split(";")
        if len(bits) >= 2 and 'rel="next"' in part.replace("'", '"'):
            return bits[0].strip().strip("<>")
    return None


def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


def parse_ts(s):
    if not s or not isinstance(s, str):
        return None
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)
