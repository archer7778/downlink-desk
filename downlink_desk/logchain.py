"""The signed audit trail. The server is the only writer, so entries form a
single chain: each one stores the SHA-256 of its own content plus the previous
entry's hash. Editing or deleting an entry breaks the chain from that point."""
import hashlib
import json
import threading

from .db import now_ms

KINDS = {"ack", "shelve", "unshelve", "check", "note", "duty", "handover", "equip", "limits", "system", "satellite"}
GENESIS = "genesis"
_lock = threading.Lock()


def canon(seq, t_ms, kind, data_json, msg, cs, prev):
    return "␟".join([prev, str(seq), str(t_ms), kind, data_json, msg, cs])


def digest(*parts):
    return hashlib.sha256(canon(*parts).encode("utf-8")).hexdigest()


def append(conn, kind, data, msg, cs):
    if kind not in KINDS:
        raise ValueError("unknown entry kind")
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    msg = str(msg).strip()[:1200]
    cs = str(cs).strip()[:16]
    if not msg or not cs:
        raise ValueError("message and call sign are required")
    data_json = json.dumps(data, sort_keys=True, separators=(",", ":"))
    if len(data_json) > 4000:
        raise ValueError("entry data too large")
    with _lock:
        last = conn.execute("SELECT seq, hash FROM log ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev = last["hash"] if last else GENESIS
        t = now_ms()
        h = digest(seq, t, kind, data_json, msg, cs, prev)
        conn.execute("INSERT INTO log(seq,t_ms,kind,data_json,msg,cs,prev,hash) VALUES(?,?,?,?,?,?,?,?)",
                     (seq, t, kind, data_json, msg, cs, prev, h))
        conn.commit()
    return row_to_entry({"seq": seq, "t_ms": t, "kind": kind, "data_json": data_json,
                         "msg": msg, "cs": cs, "prev": prev, "hash": h})


def row_to_entry(r):
    return {"seq": r["seq"], "t": r["t_ms"], "kind": r["kind"], "data": json.loads(r["data_json"]),
            "msg": r["msg"], "cs": r["cs"], "prev": r["prev"], "hash": r["hash"]}


def entries(conn, after=0, limit=5000):
    rows = conn.execute("SELECT * FROM log WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
    return [row_to_entry(r) for r in rows]


def verify(conn):
    prev, expected_seq, problems, n = GENESIS, 1, [], 0
    for r in conn.execute("SELECT * FROM log ORDER BY seq"):
        n += 1
        if r["seq"] != expected_seq:
            problems.append({"seq": r["seq"], "problem": f"entries {expected_seq}-{r['seq'] - 1} are missing"})
        if r["prev"] != prev:
            problems.append({"seq": r["seq"], "problem": "does not link to the entry before it"})
        h = digest(r["seq"], r["t_ms"], r["kind"], r["data_json"], r["msg"], r["cs"], r["prev"])
        if h != r["hash"]:
            problems.append({"seq": r["seq"], "problem": "content changed after signing"})
        prev, expected_seq = r["hash"], r["seq"] + 1
    return {"ok": not problems, "entries": n, "head": prev, "problems": problems[:20]}
