# Live Kalshi prices for every race (public market data, no login). One event per race (race["kalshi"]["ev"]).
# Reports the Republican "yes" price: bid, ask, last and mid, in cents. A Democratic-only market is flipped (100 - price).
import json, time, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLL = 15
PRICES = {}      # race id -> {"bid","ask","last","mid","prev_mid","change","ticker","t"}
ERRORS = {}      # race id -> last error
LOCK = threading.Lock()

def _get(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "socal-midterm-model"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

def _cents(m, name):
    """Kalshi returns cents (yes_bid) and, on newer markets, dollars (yes_bid_dollars). Return cents or None."""
    v = m.get(name)
    if isinstance(v, (int, float)) and not isinstance(v, bool): return float(v)
    d = m.get(name + "_dollars")
    if d not in (None, ""):
        try: return float(d) * 100
        except ValueError: pass
    return None

def _is_side(m, side):
    t = str(m.get("ticker", "")).upper(); suf = t.rsplit("-", 1)[-1]
    text = " ".join(str(m.get(k, "")) for k in ("yes_sub_title", "subtitle", "title", "custom_strike")).lower()
    word = "republican" if side == "R" else "democrat"
    return suf == side or suf.startswith(side + "_") or word in text or (side == "R" and "gop" in text)

def pick(markets):
    """Return (market, flip) for the Republican market, or the Democratic one flipped."""
    for m in markets:
        if _is_side(m, "R") and not _is_side(m, "D"): return m, False
    for m in markets:
        if _is_side(m, "R"): return m, False
    for m in markets:
        if _is_side(m, "D"): return m, True
    return None, False

def fetch_event(ev):
    try:
        d = _get("%s/events/%s?with_nested_markets=true" % (BASE, ev))
        ms = d.get("markets") or (d.get("event") or {}).get("markets") or []
        if ms: return ms
    except urllib.error.HTTPError as e:
        if e.code not in (404, 400): raise
    return _get("%s/markets?event_ticker=%s&limit=50" % (BASE, ev)).get("markets", [])

def quote(markets):
    m, flip = pick(markets)
    if not m: raise ValueError("no Republican or Democratic market in event")
    bid, ask, last = _cents(m, "yes_bid"), _cents(m, "yes_ask"), _cents(m, "last_price")
    if flip:
        bid, ask, last = (None if ask is None else 100 - ask), (None if bid is None else 100 - bid), (None if last is None else 100 - last)
    mid = (bid + ask) / 2 if bid is not None and ask is not None and (bid > 0 or ask < 100) else last
    return {"bid": bid, "ask": ask, "last": last, "mid": mid, "ticker": m.get("ticker"), "flipped": flip}

def refresh(races, workers=8):
    def one(r):
        ev = (r.get("kalshi") or {}).get("ev")
        if not ev: return r["id"], None, "no event ticker"
        try: return r["id"], quote(fetch_event(ev)), None
        except Exception as e: return r["id"], None, str(e)[:100]
    with ThreadPoolExecutor(workers) as ex: res = list(ex.map(one, races))
    now = time.time()
    with LOCK:
        for rid, q, err in res:
            if err: ERRORS[rid] = err; continue
            ERRORS.pop(rid, None)
            old = PRICES.get(rid)
            q["t"] = now
            q["prev_mid"] = old["mid"] if old else q["mid"]
            q["change"] = None if q["mid"] is None or q["prev_mid"] is None else round(q["mid"] - q["prev_mid"], 1)
            if old and q["mid"] == old["mid"]: q["change"] = old.get("change"); q["prev_mid"] = old.get("prev_mid", q["mid"])
            PRICES[rid] = q

def snapshot():
    with LOCK: return {"updated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "prices": dict(PRICES), "errors": dict(ERRORS)}

def loop(races):
    while True:
        t = time.time()
        try: refresh(races)
        except Exception as e: print("kalshi refresh failed:", e, flush=True)
        time.sleep(max(2, POLL - (time.time() - t)))
