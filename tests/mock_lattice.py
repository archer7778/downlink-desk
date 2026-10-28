"""A stand-in for the Lattice REST API, for testing the bridge (downlink_desk/lattice.py).

It enforces the rules the real API documents that the bridge depends on:
- an entity update is applied only if provenance.sourceUpdateTime is newer than the stored one;
- expiryTime must be in the future and less than 30 days ahead;
- task status updates must carry the next status version, and DONE states are final;
- tasks reach an agent through a long poll on /api/v1/agent/listen.
Plus OAuth client credentials, the sandbox header, and fault injection. Not Anduril's code.
"""
import json
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _ms(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        i = 0
        while i < len(rest) and rest[i].isdigit():
            i += 1
        frac, tz = rest[:i], rest[i:]
        s = head + "." + (frac + "000000")[:6] + tz
    return int(datetime.fromisoformat(s).timestamp() * 1000)


def serve(port, token="lattice-env-token", sandbox="sandbox-token", client=("cid", "csecret"), hold=0.5):
    state = {"entities": {}, "history": {}, "puts": [], "tasks": {}, "queue": [], "statuses": [],
             "chaos": None, "reject_version_once": set(), "poison": set(), "oauth_issued": 0, "listens": 0}
    lock = threading.Condition()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200, headers=None):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _chaos(self):
            mode = state["chaos"]
            if mode == "down":   # as if the network were gone: hang up without an answer
                self.close_connection = True
                self.connection.close()
                return True
            if mode == "503":
                self._json({"error": "unavailable"}, 503)
                return True
            if mode == "429":
                self._json({"error": "slow down"}, 429, {"Retry-After": "1"})
                return True
            return False

        def _authed(self):
            if self.headers.get("Anduril-Sandbox-Authorization") != f"Bearer {sandbox}":
                self._json({"error": "sandbox header missing"}, 401)
                return False
            auth = self.headers.get("Authorization", "")
            ok = auth == f"Bearer {token}" or (auth.startswith("Bearer oauth-") and auth[7:] in state.get("oauth_tokens", ()))
            if not ok:
                self._json({"error": "unauthorized"}, 401)
                return False
            return True

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            raw = self._body()
            if self._chaos():
                return
            if u.path == "/api/v1/oauth/token":
                f = dict(urllib.parse.parse_qsl(raw.decode()))
                if f.get("grant_type") != "client_credentials" or (f.get("client_id"), f.get("client_secret")) != client:
                    return self._json({"error": "invalid_client"}, 401)
                tok = f"oauth-{uuid.uuid4().hex[:8]}"
                state.setdefault("oauth_tokens", set()).add(tok)
                state["oauth_issued"] += 1
                return self._json({"access_token": tok, "token_type": "Bearer", "expires_in": 3600})
            if not self._authed():
                return
            body = json.loads(raw or b"{}")
            if u.path == "/api/v1/agent/listen":
                ids = set(((body.get("agentSelector") or {}).get("entityIds")) or [])
                state["listens"] += 1
                end = time.time() + hold
                with lock:
                    while True:
                        for i, (agent, req) in enumerate(state["queue"]):
                            if agent in ids:
                                state["queue"].pop(i)
                                return self._json(req)
                        left = end - time.time()
                        if left <= 0:
                            return self._json({})
                        lock.wait(left)
            if u.path == "/api/v1/tasks":
                tid = body.get("taskId") or uuid.uuid4().hex[:12]
                assignee = (((body.get("relations") or {}).get("assignee") or {}).get("system") or {}).get("entityId")
                task = {"version": {"taskId": tid, "definitionVersion": 1, "statusVersion": 1},
                        "displayName": body.get("displayName"), "specification": body.get("specification"),
                        "status": {"status": "STATUS_SENT"}, "relations": body.get("relations")}
                with lock:
                    state["tasks"][tid] = task
                    state["queue"].append((assignee, {"executeRequest": {"task": task}}))
                    lock.notify_all()
                return self._json(task)
            return self._json({"error": "not found"}, 404)

        def do_GET(self):
            if self._chaos() or not self._authed():
                return
            parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
            if parts[:3] == ["api", "v1", "entities"] and len(parts) == 4:
                e = state["entities"].get(parts[3])
                return self._json(e) if e else self._json({"error": "not found"}, 404)
            if parts[:3] == ["api", "v1", "tasks"] and len(parts) == 4:
                t = state["tasks"].get(parts[3])
                return self._json(t) if t else self._json({"error": "not found"}, 404)
            return self._json({"error": "not found"}, 404)

        def do_PUT(self):
            raw = self._body()
            if self._chaos() or not self._authed():
                return
            parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
            body = json.loads(raw or b"{}")
            now = time.time() * 1000
            if parts == ["api", "v1", "entities"]:
                eid = body.get("entityId")
                src = _ms(((body.get("provenance") or {}).get("sourceUpdateTime")))
                exp = _ms(body.get("expiryTime"))
                if not eid or not body.get("isLive") or src is None:
                    return self._json({"error": "entityId, isLive and provenance.sourceUpdateTime are required"}, 400)
                if not body.get("noExpiry") and (exp is None or exp <= now or exp >= now + 30 * 86_400_000):
                    return self._json({"error": "expiryTime must be in the future and less than 30 days away"}, 400)
                if eid in state["poison"]:
                    return self._json({"error": "invalid entity"}, 400)
                with lock:
                    state["puts"].append(body)
                    cur = state["entities"].get(eid)
                    if cur is None or src > _ms(cur["provenance"]["sourceUpdateTime"]):
                        state["entities"][eid] = body
                        state["history"].setdefault(eid, []).append(body)
                    return self._json(state["entities"][eid])
            if parts[:3] == ["api", "v1", "tasks"] and len(parts) == 5 and parts[4] == "status":
                tid = parts[3]
                with lock:
                    t = state["tasks"].get(tid)
                    if not t:
                        return self._json({"error": "not found"}, 404)
                    cur = t["version"]["statusVersion"]
                    if t["status"]["status"] in ("STATUS_DONE_OK", "STATUS_DONE_NOT_OK"):
                        return self._json({"error": "task is finished"}, 400)
                    if tid in state["reject_version_once"]:
                        state["reject_version_once"].discard(tid)
                        t["version"]["statusVersion"] = cur = cur + 1   # someone else updated it meanwhile
                        return self._json({"error": "STATUS_VERSION_REJECTED"}, 409)
                    if body.get("statusVersion") != cur + 1:
                        return self._json({"error": "STATUS_VERSION_REJECTED", "current": cur}, 409)
                    t["version"]["statusVersion"] = cur + 1
                    t["status"] = body.get("newStatus") or {}
                    state["statuses"].append((tid, t["status"]))
                    return self._json(t)
            return self._json({"error": "not found"}, 404)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    httpd.daemon_threads = True

    def create_task(agent_entity, spec, task_id=None):
        """What an operator does in the Lattice UI: create a task assigned to an agent."""
        tid = task_id or uuid.uuid4().hex[:12]
        task = {"version": {"taskId": tid, "definitionVersion": 1, "statusVersion": 1},
                "displayName": "Expedite pass", "specification": spec, "status": {"status": "STATUS_SENT"},
                "relations": {"assignee": {"system": {"entityId": agent_entity}}}}
        with lock:
            state["tasks"][tid] = task
            state["queue"].append((agent_entity, {"executeRequest": {"task": task}}))
            lock.notify_all()
        return tid

    def send(agent_entity, req):
        with lock:
            state["queue"].append((agent_entity, req))
            lock.notify_all()

    state["create_task"], state["send"] = create_task, send
    return httpd, state


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args()
    httpd, _ = serve(a.port)
    print(f"Mock Lattice on http://127.0.0.1:{a.port} (token lattice-env-token, sandbox token sandbox-token)")
    httpd.serve_forever()
