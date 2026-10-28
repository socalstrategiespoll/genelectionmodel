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
    try: es = get(f"{b}/{ver}/json/en/electionsettings.json")
    except Exception: es = ""
    if re.search(r"\d{1,2}/\d{1,2}/\d{4}", es): check_date("clarity", "11/3/2026" in es or "11/03/2026" in es)
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
_ess_ok = {}
def fetch_ess(c):     # summary_N.xml; the XML has no election date, so the Index_N.html title is checked once per county
    u = _ov(c, "url")
    if u not in _ess_ok:
        m = re.match(r"(.*/(\d+))/summary_\d+\.xml", u)
        if m:
            h = get(f"{m.group(1)}/en/Index_{m.group(2)}.html")
            t = re.search(r'election-title[^>]*>([^<]*)<', h)
            if t: check_date("ess", "November 3, 2026" in t.group(1))
        _ess_ok[u] = True
    return adapters.parse_ess(get(u))
def fetch_vr(c):      # Summary page; hidden "DetailResults" table carries the by-method counts
    h = get(_ov(c, "url"))
    m = re.search(r"Election Date:\s*([0-9/]+)", h)
    if m: check_date("vr", m.group(1) in ("11/3/2026", "11/03/2026"))
    return adapters.parse_vr_html(h)
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
def _state_ov(st, k):
    try: return json.load(open(_ov_path)).get(st, {}).get(k)
    except Exception: return None
def check_date(kind, ok):
    """Refuse a page that is not the Nov 3, 2026 General (old primaries and 2024 pages must never be read as live)."""
    if not ok: raise ValueError(kind + ": page is not the 2026-11-03 General")
def fetch_enhanced(c):
    u = _ov(c, "data_url")
    if not u:
        slug = ev_slug(c)
        if not slug: raise ValueError("no Enhanced Voting slug for " + c["county"])
        eid = _ov(c, "election_id") or _state_ov(c["state"], "election_id") or ev_find_election(slug)
        u = f"{EV_API}/elections/{slug}/{eid}/data"
    raw = get(u)
    d = json.loads(raw)
    ed = (d.get("election") or {}).get("electionDate") if isinstance(d, dict) else None
    if ed: check_date("enhanced", str(ed).startswith(ELECTION_DATE))
    return adapters.parse_enhanced(d)

GA_API = "https://results.sos.ga.gov/results/public/api"
_ga_eid = {}
def ga_election_id():
    """Georgia's statewide portal lists elections at /api/jurisdictions/Georgia. Override with election_overrides.json {"GA": {"election_id": ...}};
    otherwise pick the 2026-11-03 election from the listing, falling back to 2026NovGen (the 2022 and 2024 ids were 2022NovGen and 2024NovGen)."""
    try: o = json.load(open(_ov_path)).get("GA", {}).get("election_id")
    except Exception: o = None
    if o: return o
    if "id" in _ga_eid: return _ga_eid["id"]
    try:
        raw = get(GA_API + "/jurisdictions/Georgia")
        for m in re.finditer(r'\{[^{}]*\}', raw):
            it = m.group(0)
            if "2026-11-03" in it and (e := re.search(r'"id"\s*:\s*"([^"]+)"', it)):
                _ga_eid["id"] = e.group(1); return _ga_eid["id"]
    except Exception: pass
    return "2026NovGen"
def fetch_ga(c):      # one county's contests by vote method from the Georgia SoS portal (Enhanced Voting engine)
    slug = re.sub(r"[^A-Za-z]+", "-", c["county"].strip()).strip("-") + "-County-GA"
    u = _ov(c, "data_url") or f"{GA_API}/elections/{slug}/{ga_election_id()}/ballot-items"
    return adapters.parse_enhanced(get(u, 30))
_tx = {}
TX_API = "https://goelect.txelections.civixapps.com/api-ivis-system/api/s3/enr/election/countyInfo/"
def tx_url(c):
    u = _ov(c, "data_url")
    if u: return u
    try: eid = json.load(open(_ov_path)).get("TX", {}).get("election_id")
    except Exception: eid = None
    if not eid: raise ValueError("no Texas election id yet (set election_overrides.json {\"TX\": {\"election_id\": ...}})")
    return TX_API + str(eid)
def fetch_tx(c):      # one statewide countyInfo file serves every TX county; cache for the poll cycle
    import time
    u = tx_url(c)
    hit = _tx.get(u)
    if not hit or time.time() - hit[0] > 30:
        hit = _tx[u] = (time.time(), adapters.parse_tx(get(u, 60)))
    rows = hit[1].get(adapters._cn(c["county"]))
    if rows is None: raise ValueError("county not in Texas portal file: " + c["county"])
    return rows
def fetch_turnout_pdf(c):
    """Counts-only feed from a PDF with a turnout-by-method summary (Maricopa). The PDF's address changes with each posting, so
    set it in election_overrides.json {fips: {"url": ...}} or fetch it from a stable page."""
    import subprocess, tempfile, urllib.request
    u = _ov(c, "url")
    raw = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=60).read()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(raw); f.flush()
        txt = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True, timeout=60).stdout
    rows = adapters.parse_turnout_text(txt)
    if not rows: raise ValueError("no turnout-by-method rows in PDF for " + c["county"])
    return rows
HARRIS_URL = "https://appfiles.harrisvotes.com/harrisvotes/prd/Data/1126/resultsbyballot.json"
def fetch_harris(c):
    return adapters.parse_harris(get(_ov(c, "url") or HARRIS_URL))
FETCH = {"turnout_pdf": fetch_turnout_pdf, "harris": fetch_harris, "clarity": fetch_clarity, "nc": fetch_nc, "ess": fetch_ess, "vr": fetch_vr, "enhanced": fetch_enhanced, "tx": fetch_tx, "ga": fetch_ga}

# feed label -> bucket key, by the race's bucket structure
EARLY = ("advance voting", "early", "early voting", "early votes", "one-stop", "in-person early", "ev", "early voting - regional", "early voting - central", "ev-regional", "ev-central")
MAIL = ("av ed", "av counting boards", "absentee - local", "absentee - county", "absentee-local", "absentee-county", "absentee votes", "pre-process absentee", "absentee election day", "avcb election day", "avcb", "absentee voting", "mail", "absentee", "absentee mail", "absentee by mail", "vote by mail", "mail-in", "absentee mail-in", "mailed absentee")
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
