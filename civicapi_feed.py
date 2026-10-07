# civicAPI client. Server-side only (no CORS). No auth.
# Payload shape (from the WI primary race 85787): top-level percent_reporting, last_updated, candidates,
# region_results[] with type, fips, percent_reporting (0-100, precincts) and candidates[] {name, party, votes}.
# UNVERIFIED against a general-election House/Senate/Governor payload: check one real race before election night.
import json, re, urllib.request

URL = "https://civicapi.org/api/v2/race/{}"

def fetch(race_id, timeout=15):
    req = urllib.request.Request(URL.format(race_id), headers={"User-Agent": "socal-strategies-model/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def _last(name):
    name = re.sub(r"<[^>]*>", " ", name or "")          # civicAPI has a stray <br/> in at least one candidate name
    name = re.sub(r"\(.*?\)", "", name).strip()
    return name.split()[-1].lower() if name else ""

def side_of(cand, race):
    """'R', 'D' or None for a candidate in a civicAPI payload, using party first then last name."""
    party = (cand.get("party") or "").lower()
    if party.startswith("republican"): return "R"
    if party.startswith("democrat"): return "D"
    last = _last(cand.get("name"))
    if last and last == _last(race.get("rep")): return "R"
    if last and last == _last(race.get("other")): return "D"
    return None

def parse_counties(payload, race):
    """{fips: (D_votes, R_votes, pct_reporting 0-1)}. Two-party only; others dropped. County-type regions only."""
    out = {}
    rr = payload.get("region_results") or []
    for reg in (rr.values() if isinstance(rr, dict) else rr):
        fips = str(reg.get("fips") or "")
        if len(fips) != 5 or (reg.get("type") and "count" not in str(reg["type"]).lower()):
            continue
        D = R = 0
        for c in reg.get("candidates") or []:
            s = side_of(c, race)
            if s == "R": R += c.get("votes") or 0
            elif s == "D": D += c.get("votes") or 0
        if D + R > 0:
            out[fips] = (D, R, max(0.0, min(1.0, (reg.get("percent_reporting") or 0) / 100.0)))
    return out


# National roll-ups: one entry per state or district, each carrying that race's own election_id.
NATIONAL = {"Senate": 87528, "Governor": 87535, "House": 87524}

def national_ids():
    """{our label: (election_id, [candidate last names])}, e.g. 'IA-Sen', 'IA-Gov', 'AL-02'."""
    out = {}
    for kind, nid in NATIONAL.items():
        rr = fetch(nid, timeout=60).get("region_results") or {}
        items = rr.items() if isinstance(rr, dict) else [(str(r.get("name")), r) for r in rr]
        for key, reg in items:
            eid = reg.get("election_id")
            if not eid: continue
            k = str(key).lower()
            if kind == "House":
                if "-" not in k: continue
                st, d = k.split("-", 1)
                label = f"{st.upper()}-{d.upper()}"
            else:
                label = f"{k.upper()}-{'Sen' if kind == 'Senate' else 'Gov'}"
            out[label] = (int(eid), [_last(c.get("name")) for c in reg.get("candidates") or []])
    return out

def resolve(races, rmap):
    """Fill missing civicAPI ids in rmap from the national feeds. Returns (rmap, warnings)."""
    warns = []
    try:
        nat = national_ids()
    except Exception as e:
        return rmap, [f"national feeds unreachable: {e}"]
    for r in races:
        if rmap.get(r["id"]):
            continue
        hit = nat.get(r["label"])
        if not hit:
            warns.append(f"{r['label']}: not in national feed"); continue
        eid, lasts = hit
        if _last(r.get("rep")) not in lasts and _last(r.get("other")) not in lasts:
            warns.append(f"{r['label']}: candidates in feed {lasts} do not match ours; id {eid} used anyway")
        rmap[r["id"]] = eid
    return rmap, warns
