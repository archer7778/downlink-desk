"""A polite SatNOGS DB client.

SatNOGS is run by volunteers (Libre Space Foundation). Its telemetry endpoint is
throttled per user (about six requests a minute) and satellites flagged as
frequency violators may only be asked for once a day. This client keeps well
inside that: at least 11 s between requests, honours Retry-After, gives up the
cycle on long refusals, and never follows links off the SatNOGS host.
"""
import json
import os
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


class Client:
    def __init__(self, api, token=""):
        self.api = api.rstrip("/")
        self.host = urllib.parse.urlparse(self.api).netloc
        self.token = token
        self._last = 0.0
        # Tests can speed up pacing, but only against a local mock server.
        local = self.host.split(":")[0] in ("127.0.0.1", "localhost")
        self.gap = float(os.environ.get("DOWNLINK_DESK_MIN_GAP", MIN_GAP_S)) if local else MIN_GAP_S
        self.last_status = None

    def _get(self, url, params=None, auth=False, timeout=30):
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if urllib.parse.urlparse(url).netloc != self.host:
            raise Refused(None, "refusing to follow a link off the SatNOGS host")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if auth and self.token:
            headers["Authorization"] = "Token " + self.token
        for attempt in range(3):
            wait = self.gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                    self.last_status = r.status
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                self.last_status = e.code
                if e.code in (401, 403):
                    raise AuthError(f"SatNOGS refused the API key (HTTP {e.code})")
                if e.code in (429, 503):
                    retry = int(e.headers.get("Retry-After") or 30)
                    if retry > 120 or attempt == 2:
                        raise Refused(e.code, f"SatNOGS asked us to wait {retry} s; trying next cycle")
                    time.sleep(retry + 1)
                    continue
                raise Refused(e.code, f"SatNOGS answered HTTP {e.code}")
            except urllib.error.URLError as e:
                self.last_status = None
                raise Refused(None, f"Could not reach SatNOGS: {e.reason}")
        raise Refused(self.last_status, "SatNOGS kept refusing")

    def satellite(self, norad):
        data = self._get(f"{self.api}/satellites/", {"norad_cat_id": norad, "format": "json"})
        items = data.get("results", data) if isinstance(data, dict) else data
        return items[0] if items else None

    def tle(self, norad):
        data = self._get(f"{self.api}/tle/", {"norad_cat_id": norad, "format": "json"})
        items = data.get("results", data) if isinstance(data, dict) else data
        return items[0] if items else None

    def telemetry(self, norad, newer_than_ms=None, max_pages=3):
        """Newest-first frames. Stops paging once it reaches frames we already have.
        Handles both the cursor shape ({next, results}) and a bare list."""
        frames, url, params = [], f"{self.api}/telemetry/", {"satellite": norad, "format": "json"}
        for _ in range(max_pages):
            data = self._get(url, params, auth=True)
            page = data.get("results", []) if isinstance(data, dict) else data
            frames.extend(page)
            if newer_than_ms is not None and page:
                oldest = parse_ts(page[-1].get("timestamp"))
                if oldest is not None and oldest <= newer_than_ms:
                    break
            url = data.get("next") if isinstance(data, dict) else None
            params = None
            if not url:
                break
        return frames


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
