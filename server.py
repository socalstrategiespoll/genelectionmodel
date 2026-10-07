# Render web service: stdlib server plus a poller thread. Routes: /health, /api/projection, /api/race/<id>
# Writes projection.json (the file the site reads) every cycle. Races with no civicAPI id run pre-election.
import json, os, sys, time, hashlib, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import engine, civicapi_feed, method_feeds, counties

HERE = os.path.dirname(os.path.abspath(__file__))
POLL = int(os.environ.get("POLL_SECONDS", "60"))
OUT = os.environ.get("PROJECTION_PATH", os.path.join(HERE, "projection.json"))
LOCK = threading.Lock()
STATE = {"projection": {"updated": None, "mode": "starting", "races": {}}, "detail": {}}

def load():
    data = json.load(open(os.path.join(HERE, "data.json")))
    rmap = json.load(open(os.path.join(HERE, "race_map.json")))
    counties.install(data["races"])
    return data["races"], rmap

def cycle(races, rmap, preps, cache):
    out, live = {}, 0
    for r in races:
        rid = r["id"]; cid = rmap.get(rid); counted = {}; err = None
        if cid:
            try:
                payload = civicapi_feed.fetch(cid)
                counted = civicapi_feed.parse_counties(payload, r)
                counted = method_feeds.attach(counted, r, engine_keys=preps[rid]["keys"])
                live += 1
            except Exception as e:      # keep the last good projection for this race
                err = str(e)[:120]
                if rid in STATE["detail"]:
                    out[rid] = dict(STATE["projection"]["races"].get(rid, {}), feed_error=err); continue
        key = hashlib.md5(json.dumps(counted, sort_keys=True).encode()).hexdigest()
        if cache.get(rid) != key or rid not in STATE["detail"]:
            res = engine.project(preps[rid], counted)
            cache[rid] = key
            STATE["detail"][rid] = res
        res = STATE["detail"][rid]
        out[rid] = {"state": res["state"], "status": {"pre": "Pre-election forecast", "counting": "Counting", "complete": "All counties reporting"}[res["state"]],
                    "margin": res["margin"], "p05": res["p05"], "p95": res["p95"], "win_prob": res["win_prob_R"],
                    "reporting": res["reporting"], "call_ready": res["call_ready"], "percentiles": res["percentiles"],
                    "bucket_gap": res.get("bucket_gap", {}), "method_counties": res.get("counties_with_method_data", 0)}
        if err: out[rid]["feed_error"] = err
        time.sleep(0.2 if cid else 0)
    mode = "live" if any(v.get("state") != "pre" for v in out.values()) else "pre"
    proj = {"updated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "mode": mode, "races": out}
    with LOCK:
        STATE["projection"] = proj
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f: json.dump(proj, f, separators=(",", ":"))
    os.replace(tmp, OUT)

def poller():
    races, rmap = load()
    last_resolve = 0
    preps = {r["id"]: engine.prep(r) for r in races}
    cache = {}
    while True:
        t = time.time()
        if t - last_resolve > 3600 and any(not rmap.get(r["id"]) for r in races):
            rmap, warns = civicapi_feed.resolve(races, rmap); last_resolve = t
            for w in warns: print("resolve:", w, flush=True)
            print("race ids set:", sum(1 for r in races if rmap.get(r["id"])), "of", len(races), flush=True)
        try: cycle(races, rmap, preps, cache)
        except Exception as e: print("cycle failed:", e, flush=True)
        print("cycle done", round(time.time() - t, 1), "s", flush=True)
        time.sleep(max(1, POLL - (time.time() - t)))

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health": return self._send(200, {"ok": True, "updated": STATE["projection"]["updated"]})
        if p == "/api/projection":
            with LOCK: return self._send(200, STATE["projection"])
        if p == "/api/status":
            return self._send(200, {"updated": STATE["projection"]["updated"], "county_errors": method_feeds.ERRORS,
                                    "by_race": method_feeds.STATUS, "wired_counties": len(method_feeds.COUNTIES)})
        if p.startswith("/api/race/"):
            d = STATE["detail"].get(p.rsplit("/", 1)[1])
            return self._send(200 if d else 404, d or {"error": "unknown race"})
        self._send(404, {"error": "not found"})
    def log_message(self, *a): pass

if __name__ == "__main__":
    if "--once" in sys.argv:        # write projection.json and exit
        races, rmap = load()
        cycle(races, rmap, {r["id"]: engine.prep(r) for r in races}, {})
        print("wrote", OUT)
    else:
        threading.Thread(target=poller, daemon=True).start()
        port = int(os.environ.get("PORT", "8000"))
        ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
