"""Wires county_config.json to method_feeds. Live fetchers use urllib; formats are verified against saved samples
(see test_adapters.py) but the live URL patterns are untested until a county posts a real file."""
import json, os, re, urllib.request, adapters, method_feeds

CFG = json.load(open(os.path.join(os.path.dirname(__file__), "county_config.json")))
UA = {"User-Agent": "Mozilla/5.0 SoCalStrategies-ENR"}
def get(url, timeout=20):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode("utf-8", "replace")

def clarity_base(c):
    url = c["url"] or ""
    eid = _ov(c, "eid")
    m = re.match(r"(https?://[^#]*?/[A-Z]{2}/[^/]+)/(?:(\d+)(?:/|$))?", url)
    if eid and m:                                   # election id supplied (or discovered) for 2026-11-03
        return f"{m.group(1)}/{eid}"
    m = re.match(r"(https?://[^#]*?/\d+)(?:/|$)", url)
    if m and not c.get("stale_eid"): return m.group(1)
    if m_ := re.match(r"(https?://[^#]*?/[A-Z]{2}/[^/]+)/", url):
        raw = get(m_.group(1) + "/elections.json")      # Clarity's per-county election list; unverified shape
        for it in re.findall(r"\{[^{}]*\}", raw):
            if ("2026-11-03" in it or "11/03/2026" in it or "11/3/2026" in it) and (e := re.search(r'"(?:eid|id|electionId)"\s*:\s*"?(\d{4,8})', it)):
                return f"{m_.group(1)}/{e.group(1)}"
    raise ValueError("no Nov 2026 Clarity election id yet for " + c["county"])
def fetch_clarity(c):
    b = clarity_base(c); ver = get(b + "/current_ver.txt").strip()
    try:
        return adapters.parse_clarity(get(f"{b}/{ver}/json/summary.json"), get(f"{b}/{ver}/json/vt.json"))
    except Exception:       # no vt.json: fall back to detail.xml (Michigan counties)
        import io, zipfile, urllib.request
        raw = urllib.request.urlopen(urllib.request.Request(f"{b}/{ver}/reports/detailxml.zip", headers=UA), timeout=30).read()
        z = zipfile.ZipFile(io.BytesIO(raw))
        return adapters.parse_clarity_detail(z.read(next(n for n in z.namelist() if n.lower().endswith(".xml"))))
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
EV_API = "https://app.enhancedvoting.com/results/public/api"
ELECTION_DATE = "2026-11-03"
def ev_slug(c):
    m = re.search(r"/public/([^/#?]+)", c.get("url") or "")
    return m.group(1) if m else None
def ev_find_election(slug):
    """Enhanced Voting lists a jurisdiction's elections at /api/jurisdictions/<slug>. Pick the 11/3/2026 one by its date,
    or by an id shaped like 11032026_<timestamp> (how Kent's was named). Unverified against a live listing."""
    raw = get(f"{EV_API}/jurisdictions/{slug}")
    m = re.search(r"\b11032026_\d+", raw)
    if m: return m.group(0)
    found = []
    def walk(o):
        if isinstance(o, dict):
            if any(isinstance(v, str) and v.startswith(ELECTION_DATE) for k, v in o.items() if "date" in k.lower()):
                for k in ("publicElectionId", "electionId", "id", "slug"):
                    if isinstance(o.get(k), str): found.append(o[k]); break
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(json.loads(raw))
    if not found: raise ValueError("no 2026-11-03 election listed for " + slug)
    return found[0]
def fetch_enhanced(c):
    u = _ov(c, "data_url")
    if not u:
        slug = ev_slug(c)
        if not slug: raise ValueError("no Enhanced Voting slug for " + c["county"])
        u = f"{EV_API}/elections/{slug}/{_ov(c, 'election_id') or ev_find_election(slug)}/data"
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
EARLY = ("early", "early voting", "early votes", "one-stop", "in-person early", "ev", "early voting - regional", "early voting - central", "ev-regional", "ev-central")
MAIL = ("av ed", "av counting boards", "absentee - local", "absentee - county", "absentee-local", "absentee-county", "absentee votes", "pre-process absentee", "absentee election day", "avcb election day", "avcb", "absentee voting", "mail", "absentee", "absentee mail", "absentee by mail", "vote by mail", "mail-in", "absentee mail-in")
def keymap(race):
    keys = [b["key"] for b in race.get("buckets", [])]
    km = {m: "ed" for m in ("election day", "machine", "election", "precinct", "precinct voting", "precinct votes", "election day voting", "election day votes")}
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
            return bool(re.search(r"(?:rep|congress|district|dist\.?|cd)[^0-9]*0*%s(?!\d)" % (d or "at"), n)) and ("rep" in n or "congress" in n)
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
