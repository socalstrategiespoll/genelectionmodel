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
            if ("2026-11-03" in it or "11/03/2026" in it or "11/3/2026" in it) and (e := re.search(r'"(?:eid|id|electionId)"\s*:\s*"?(\d{4,8})', it, re.I)):
                return f"{m_.group(1)}/{e.group(1)}"
    raise ValueError("no Nov 2026 Clarity election id yet for " + c["county"])
def clarity_is_general(es):
    """Clarity's electionsettings "electiondate" is sometimes stale (Iowa's Nov 3 sites say 8/13/2026, copied from the template).
    Accept the page if electiondate is 11/3/2026, or if the site was last updated on or after Oct 15, 2026 (only the
    general is being built then; the 2026 primaries were last touched in the summer)."""
    if "11/3/2026" in es or "11/03/2026" in es: return True
    m = re.search(r'"websiteupdatedat"\s*:\s*"(\d{1,2})/(\d{1,2})/(\d{4})', es)
    return bool(m) and int(m.group(3)) == 2026 and (int(m.group(1)), int(m.group(2))) >= (10, 15)

def fetch_clarity(c):
    b = clarity_base(c); ver = get(b + "/current_ver.txt").strip()
    try: es = get(f"{b}/{ver}/json/en/electionsettings.json")
    except Exception: es = ""
    if re.search(r"\d{1,2}/\d{1,2}/\d{4}", es): check_date("clarity", clarity_is_general(es))
    try:
        try: sm = get(f"{b}/{ver}/json/summary.json")
        except Exception: sm = get(f"{b}/{ver}/json/en/summary.json")      # newer Clarity sites (Iowa) keep it under en/
        return adapters.parse_clarity(sm, get(f"{b}/{ver}/json/vt.json"))
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
            ts = [x.strip() for x in re.findall(r'election-title[^>]*>([^<]*)<', h) if x.strip()]   # the first match is often an empty element
            if ts: check_date("ess", any("November 3, 2026" in x or ("2026" in x and "general" in x.lower() and "primary" not in x.lower()) for x in ts))
        _ess_ok[u] = True
    return adapters.parse_ess(get(u))
VR_SCAN = (4040, 4260)    # Florida VR election ids: the Nov 3 2026 ones published so far are 4055-4130 (ids are shared across counties)
_vr_found = {}; _vr_tried = {}
def vr_discover(code):
    """A Florida VR county whose Nov 3 page was not published when configured: look for it by id, at most every 10 minutes."""
    if code in _vr_found: return _vr_found[code]
    import time
    if time.time() - _vr_tried.get(code, 0) < 600: return None
    _vr_tried[code] = time.time()
    from concurrent.futures import ThreadPoolExecutor
    def one(i):
        try:
            h = get(f"https://enr.electionsfl.org/{code}/{i}/Summary/", 10)
            return i if re.search(r"\b11/0?3/2026\b", h[:20000]) else None
        except Exception: return None
    with ThreadPoolExecutor(max_workers=12) as ex:
        hits = [i for i in ex.map(one, range(*VR_SCAN)) if i]
    if hits: _vr_found[code] = f"https://enr.electionsfl.org/{code}/{hits[0]}/Summary/"; print("vr: found", _vr_found[code], flush=True)
    return _vr_found.get(code)

def fetch_vr(c):      # Summary page; hidden "DetailResults" table carries the by-method counts
    u = _ov(c, "url"); h = None
    try:
        h = get(u)
        m = re.search(r"Election Date:\s*([0-9/]+)", h)
        if m: check_date("vr", m.group(1) in ("11/3/2026", "11/03/2026"))
    except Exception as e:
        cm = re.search(r"electionsfl\.org/([A-Z]{3})/", u or "")
        nu = vr_discover(cm.group(1)) if cm else None
        if not nu: raise
        h = get(nu)
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
_mc_txt = {}          # results-file URL -> (total votes, parsed rows); each posting gets a new asset URL, so a parsed file never changes
def fetch_maricopa_txt(c):
    """Maricopa's results page lists tab-delimited results exports (Votes_Early Vote / Votes_Election Day /
    Votes_Early A.R.S. 16-579 / Votes_Provisional), each update under a new /asset/jcr:... URL. Read every .txt link on the
    page once, keep only general-election files (US House contests with no party affiliation), and use the one with the
    most votes, i.e. the latest drop."""
    base = "https://elections.maricopa.gov"
    page = get(_ov(c, "url"), 30)
    links = {(base + u if u.startswith("/") else u) for u in re.findall(r'href="([^"]+?\.txt)"', page)}
    if not links: raise ValueError("no results .txt link on the Maricopa results page yet")
    for u in links:
        if u in _mc_txt: continue
        try: txt = get(u, 90)
        except Exception: continue
        hdr = txt.split("\n", 1)[0].split("\t")
        if "ContestPartyAffiliation" not in hdr or not any(h.startswith("Votes_") for h in hdr):
            _mc_txt[u] = (-1, None); continue
        ix = {h: i for i, h in enumerate(hdr)}; tot = 0; general = False
        for line in txt.split("\n")[1:]:
            r = line.split("\t")
            if len(r) < len(hdr): continue
            if r[ix["ContestName"]].startswith("US Rep") and not r[ix["ContestPartyAffiliation"]].strip(): general = True
            tot += sum(adapters._n(r[i]) for h, i in ix.items() if h.startswith("Votes_"))
        _mc_txt[u] = (tot, adapters.parse_maricopa(txt)) if general else (-1, None)
    best = max(_mc_txt.items(), key=lambda kv: kv[1][0])
    if best[1][0] < 0: raise ValueError("no general-election results file on the Maricopa page yet")
    return best[1][1]
PARTY_BY_SURNAME = {}     # filled by install(): state -> {candidate surname: D / R}, for result pages that print no party
def fetch_washtenaw(c):
    u = _ov(c, "url"); h = get(u, 30)
    if "November 3, 2026" not in h and "11/3/2026" not in h and "11/03/2026" not in h and "General Election" not in h:
        check_date("washtenaw", False)
    rows = adapters.parse_washtenaw(h, PARTY_BY_SURNAME.get(c["state"], {}))
    if not rows: raise ValueError("no Washtenaw rows with a matched candidate yet")
    return rows
KY_LIVE = "https://vrsws.sos.ky.gov/liveresults/CumulativePdf/{}"
def fetch_ky(c):
    """Kentucky SOS live results: one cumulative Electionware PDF per county, split Absentee Mail-In / Absentee Walk-In /
    Early Voting / Election Day. Counties are numbered alphabetically, which is (FIPS county code + 1) / 2."""
    u = _ov(c, "data_url") or KY_LIVE.format((int(c["fips"][2:]) + 1) // 2)
    raw = urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30).read()
    if not raw.startswith(b"%PDF"): raise ValueError("Kentucky live results PDF not posted yet")
    text, rows = adapters.parse_ky_pdf(raw)
    check_date("ky", ("11/3/2026" in text or "11/03/2026" in text) and "GENERAL" in text.upper())
    return rows
HARRIS_URL = "https://appfiles.harrisvotes.com/harrisvotes/prd/Data/1126/resultsbyballot.json"
def fetch_harris(c):
    return adapters.parse_harris(get(_ov(c, "url") or HARRIS_URL))
WAYNE_SITE = "https://waynecountyresults.azurewebsites.us/Wayne/ResultsSW.aspx?type={}&cid=05&map="
WAYNE_SVC = "https://phillyresws.azurewebsites.us/ResultsAjax.svc/GetMapData?type={}&category=PREC&raceID={}&osn={}&county=05&party=0&ward=0&LanguageID=1"
_wayne = {}
def wayne_detroit_totals(race):
    """Detroit's D/R totals for a race from Wayne County's precinct-level results (Detroit precincts + absentee counting
    boards). Fallback for when Detroit's own Enhanced Voting feed is empty."""
    import time
    if "races" not in _wayne:
        found = []
        for ty in ("CON", "ST"):
            h = get(WAYNE_SITE.format(ty), 30)
            if "11/3/2026" not in h: raise ValueError("Wayne site is not on the 11/3/2026 election")
            found += re.findall(r"BuildPrecincts\('([A-Z]+)',\s*'(\d+)',\s*'([^']*)'", h)
        _wayne["races"] = list(dict.fromkeys(found)); _wayne["names"] = {}
    ok = contest_ok(race)
    c = _wayne.get(race["id"])
    if c and time.time() - c[0] < 45: return c[1]
    for ty, rid, osn in _wayne["races"]:
        nm = _wayne["names"].get(rid)
        if nm is not None and not ok(nm): continue
        rows = json.loads(get(WAYNE_SVC.format(ty, rid, osn.strip()), 40) or "[]")
        if not rows: continue
        nm = _wayne["names"][rid] = re.sub(r"<br\s*/?>.*", "", rows[0].get("RaceName") or "")
        if not ok(nm): continue
        D = sum(r.get("calcCandidateVotes") or 0 for r in rows if (r.get("PrecinctName") or "").startswith("City of Detroit") and (r.get("PartyCode") or "").strip() == "DEM")
        R = sum(r.get("calcCandidateVotes") or 0 for r in rows if (r.get("PrecinctName") or "").startswith("City of Detroit") and (r.get("PartyCode") or "").strip() == "REP")
        _wayne[race["id"]] = (time.time(), (D, R)); return D, R
    return None
method_feeds.DETROIT_TOTALS = wayne_detroit_totals

FETCH = {"washtenaw": fetch_washtenaw, "ky": fetch_ky, "maricopa_txt": fetch_maricopa_txt, "turnout_pdf": fetch_turnout_pdf, "harris": fetch_harris, "clarity": fetch_clarity, "nc": fetch_nc, "ess": fetch_ess, "vr": fetch_vr, "enhanced": fetch_enhanced, "tx": fetch_tx, "ga": fetch_ga}

# feed label -> bucket key, by the race's bucket structure
EARLY = ("absentee walk-in", "ev excused", "ev unexcused 3-day", "advance voting", "early", "early voting", "early votes", "one-stop", "in-person early", "ev", "early voting - regional", "early voting - central", "ev-regional", "ev-central")
MAIL = ("absentee mail-in", "mail in", "av ed", "av counting boards", "absentee - local", "absentee - county", "absentee-local", "absentee-county", "absentee votes", "pre-process absentee", "absentee election day", "avcb election day", "avcb", "absentee voting", "mail", "absentee", "absentee mail", "absentee by mail", "vote by mail", "mail-in", "absentee mail-in", "mailed absentee")
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
        # "Governor and Lieutenant Governor" (joint ticket, MI/OH/FL/IA/KS...) is the governor race; a standalone
        # "Lieutenant Governor" / "Lt. Governor" contest is not
        return "governor" in n and not re.match(r"\W*(lieutenant|lt\.?)\s+gov", n)
    return ok

def install(races):
    for r in races:                       # surname -> party for pages without party labels (rep is R, other is D unless marked (I))
        for nm, p in ((r.get("rep") or "", "R"), (re.sub(r"\s*\([A-Z]\)\s*$", "", r.get("other") or ""), "D")):
            parts = [w for w in nm.split() if w.strip(".,").upper() not in ("JR", "SR", "II", "III", "IV")]
            if parts: PARTY_BY_SURNAME.setdefault(r["state"], {})[parts[-1]] = p
    for r in races:
        fl = [f for f, c in CFG.items() if r["label"] in c["races"] and c["kind"] in FETCH and c["in_scope"]]
        if not fl: continue
        for f in fl:
            c = dict(CFG[f], fips=f)
            ent = method_feeds.COUNTIES.get(f)
            if ent is None:
                ent = method_feeds.COUNTIES[f] = {"fetch": (lambda c=c: FETCH[c["kind"]](c)), "contest": lambda race, name: contest_ok(race)(name), "races": []}
            ent["races"].append(r)
        method_feeds.SOURCES[r["id"]] = method_feeds.make_source(fl)
        method_feeds.KEYMAP[r["id"]] = keymap(r)
    return len(method_feeds.SOURCES)
