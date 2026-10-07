"""Wires county_config.json to method_feeds. Live fetchers use urllib; formats are verified against saved samples
(see test_adapters.py) but the live URL patterns are untested until a county posts a real file."""
import json, os, re, urllib.request, adapters, method_feeds

CFG = json.load(open(os.path.join(os.path.dirname(__file__), "county_config.json")))
UA = {"User-Agent": "Mozilla/5.0 SoCalStrategies-ENR"}
def get(url, timeout=20):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode("utf-8", "replace")

def clarity_base(c):
    m = re.match(r"(https?://[^#]*?/\d+)(?:/|$)", c["url"] or "")
    return m.group(1) if m else None
def fetch_clarity(c):
    b = clarity_base(c); ver = get(b + "/current_ver.txt").strip()
    return adapters.parse_clarity(get(f"{b}/{ver}/json/summary.json"), get(f"{b}/{ver}/json/vt.json"))
def fetch_nc(c, date="20261103"):
    cid = re.search(r"results_(\d+)", c["url"] or ""); cid = cid.group(1) if cid else None
    return adapters.parse_nc(get(f"https://er.ncsbe.gov/enr/{date}/data/results_{cid}.txt"))

# election_overrides.json: {fips: {"url": ..., "data_url": ...}} replaces the 2024 URLs in county_config with the 2026 ones.
_ov_path = os.path.join(os.path.dirname(__file__), "election_overrides.json")
def _ov(c, k):
    try: o = json.load(open(_ov_path)).get(c["fips"], {})
    except Exception: o = {}
    return o.get(k) or (c.get("url") if k == "url" else None)
def fetch_ess(c):     # summary_N.xml
    return adapters.parse_ess(get(_ov(c, "url")))
def fetch_vr(c):      # Summary page; hidden "DetailResults" table carries the by-method counts
    return adapters.parse_vr_html(get(_ov(c, "url")))
def fetch_enhanced(c):  # data_url must be the jurisdiction's JSON endpoint (see the Cochise sample); no guessing
    u = _ov(c, "data_url")
    if not u: raise ValueError("no data_url for " + c["county"])
    return adapters.parse_enhanced(get(u))
_tx = {}
def fetch_tx(c):      # one statewide County.json serves every TX county; cache for the poll cycle
    u = _ov(c, "data_url")
    if not u or "<version>" in u: raise ValueError("no data_url for " + c["county"])
    import time
    hit = _tx.get(u)
    if not hit or time.time() - hit[0] > 30:
        hit = _tx[u] = (time.time(), adapters.parse_tx(get(u, 60)))
    return hit[1][c["fips"]]
FETCH = {"clarity": fetch_clarity, "nc": fetch_nc, "ess": fetch_ess, "vr": fetch_vr, "enhanced": fetch_enhanced, "tx": fetch_tx}

# feed label -> bucket key, by the race's bucket structure
EARLY = ("early", "early voting", "early votes", "one-stop", "in-person early")
MAIL = ("mail", "absentee", "absentee mail", "absentee by mail", "vote by mail", "mail-in", "absentee mail-in")
def keymap(race):
    keys = [b["key"] for b in race.get("buckets", [])]
    km = {"election day": "ed", "machine": "ed"}
    if keys == ["em", "lm", "ed"] or keys == ["em", "ed", "lm"]:
        km.update({"early": "em", "early vote": "em", "early voting": "em", "early a.r.s. 16-579": "lm"})
    elif keys == ["mail", "eip", "ed"]:
        km.update({m: "eip" for m in EARLY}); km.update({m: "mail" for m in MAIL})
    elif keys == ["early", "ed"]:
        km.update({m: "early" for m in EARLY + MAIL})
    return km

def contest_ok(race):
    lab = race["label"]; st = race["state"]
    def ok(name):
        n = name.lower()
        if race["type"] == "House":
            d = re.sub(r"\D", "", lab.split("-")[-1]).lstrip("0")
            return bool(re.search(r"(?:rep|congress|district|dist\.?|cd)[^0-9]*0*%s\b" % (d or "at"), n)) and ("rep" in n or "congress" in n)
        if race["type"] == "Senate": return "senat" in n and "state sen" not in n
        return "governor" in n and "lieutenant" not in n
    return ok

def install(races):
    for r in races:
        fl = [f for f, c in CFG.items() if r["label"] in c["races"] and c["kind"] in FETCH and c["in_scope"]]
        if not fl: continue
        for f in fl:
            c = dict(CFG[f], fips=f)
            method_feeds.COUNTIES[f] = {"fetch": (lambda c=c: FETCH[c["kind"]](c)), "contest": lambda race, name: contest_ok(race)(name)}
        method_feeds.SOURCES[r["id"]] = method_feeds.make_source(fl)
        method_feeds.KEYMAP[r["id"]] = keymap(r)
    return len(method_feeds.SOURCES)
