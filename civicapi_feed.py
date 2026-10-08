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
    toks = set(re.sub(r"[^a-z ]", " ", (cand.get("name") or "").lower()).split())
    if toks & set(race.get("minor_tokens") or []): return None      # Alaska: a second Republican named Sullivan (Jr.) is a minor candidate
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
    byname = {re.sub(r"[^a-z]", "", str(c[1]).lower()): c[0] for c in race.get("counties", [])} if race.get("name_match") else {}
    for reg in (rr.values() if isinstance(rr, dict) else rr):
        fips = str(reg.get("fips") or "")
        if len(fips) != 5 and byname:          # Alaska: rows may carry only a borough / census area name
            fips = byname.get(re.sub(r"[^a-z]", "", str(reg.get("name") or "").lower()), "")
            if len(fips) != 5: continue
        elif len(fips) != 5 or (reg.get("type") and "count" not in str(reg["type"]).lower()):
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


def add_statewide(counted, payload, race, fips_keys):
    """Races whose only unit is one non-county key (NE-Sen's '31') get it from the payload's top-level candidates,
    or from the sum of the county rows when the top level has no votes."""
    odd = [k for k in fips_keys if len(str(k)) != 5 and k not in counted and k != "2622000"]
    if len(odd) != 1 or len(fips_keys) != 1:
        return counted
    D = R = 0
    for c in payload.get("candidates") or []:
        s = side_of(c, race)
        if s == "R": R += c.get("votes") or 0
        elif s == "D": D += c.get("votes") or 0
    pct = (payload.get("percent_reporting") or 0) / 100.0
    if D + R <= 0 and counted:
        D = sum(v[0] for v in counted.values()); R = sum(v[1] for v in counted.values())
        w = D + R
        pct = sum((v[0] + v[1]) * v[2] for v in counted.values()) / w if w else 0
    out = dict(counted)
    if D + R > 0:
        out[odd[0]] = (D, R, max(0.0, min(1.0, pct)))
    return out

OVER_X = 2.5       # a county counting more than this multiple of its expected two-party vote is a feed error
DROP_TOL = 0.02    # a county total falling more than 2% below the last good one is held back ...
DROP_CYCLES = 3    # ... until it persists this many cycles in a row (then it is treated as a real correction)

def sanitize(counted, race, mem):
    """Guards against bad feed data. mem is a per-race dict carried across cycles. Returns (clean, warnings).
    - over-count: county votes above OVER_X times its baseline vote are ignored (last good value kept)
    - drop: a county total that falls (or goes to zero) is held at the last good value for DROP_CYCLES cycles
    - percent reporting is clipped to 0-1"""
    base = {c[0]: c[2] + c[3] for c in race["counties"]}
    good = mem.setdefault("good", {}); strikes = mem.setdefault("strikes", {})
    out, warns = {}, []
    for f, v in counted.items():
        tot = v[0] + v[1]
        if not all(isinstance(x, (int, float)) and x == x and x >= 0 for x in v[:3]):
            warns.append(f"{f}: bad number, ignored"); continue
        v = (v[0], v[1], max(0.0, min(1.0, v[2]))) + tuple(v[3:])
        b = base.get(f)
        if b and tot > OVER_X * b and tot > 2000:
            warns.append(f"{f}: {tot:.0f} votes is over {OVER_X}x expected {b:.0f}, ignored")
            if f in good: out[f] = good[f]
            continue
        old = good.get(f)
        if old and tot < (old[0] + old[1]) * (1 - DROP_TOL):
            strikes[f] = strikes.get(f, 0) + 1
            if strikes[f] < DROP_CYCLES:
                warns.append(f"{f}: total fell {old[0]+old[1]:.0f} -> {tot:.0f}, holding last good ({strikes[f]}/{DROP_CYCLES})")
                out[f] = old; continue
        strikes.pop(f, None)
        good[f] = v; out[f] = v
    for f in good:                      # a county that vanishes from the feed keeps its last good value for the same grace period
        if f not in out and f not in counted:
            strikes[f] = strikes.get(f, 0) + 1
            if strikes[f] < DROP_CYCLES:
                out[f] = good[f]; warns.append(f"{f}: missing from feed, holding last good")
            else:
                good.pop(f, None); strikes.pop(f, None)
    return out, warns


def add_residual(counted, payload, race):
    """Races with a 'residual' unit (Alaska: votes not assigned to a borough): its counted votes are the payload's statewide
    candidate totals minus the sum of the unit rows. Stays unreported if the payload carries no statewide totals."""
    res = race.get("residual")
    if not res: return counted
    D = R = 0
    for c in payload.get("candidates") or []:
        s = side_of(c, race)
        if s == "R": R += c.get("votes") or 0
        elif s == "D": D += c.get("votes") or 0
    if D + R <= 0: return counted
    others = [v for k, v in counted.items() if k != res["key"]]
    rD, rR = D - sum(v[0] for v in others), R - sum(v[1] for v in others)
    out = dict(counted)
    if rD + rR > 0 and rD >= 0 and rR >= 0: out[res["key"]] = (rD, rR, 0.0)
    else: out.pop(res["key"], None)
    return out

def pct_from_votes(counted, race, cap=0.97):
    """Races flagged pct_from_votes (Alaska: the feed's percent reporting reads 0% even with votes in): a unit with votes and 0%
    reporting gets counted / expected, capped at 97%, so the engine does not treat it as barely started."""
    if not race.get("pct_from_votes"): return counted
    base = {c[0]: c[2] + c[3] for c in race["counties"]}
    out = {}
    for f, v in counted.items():
        if (v[0] + v[1]) > 0 and not v[2] and base.get(f):
            v = (v[0], v[1], max(0.02, min(cap, (v[0] + v[1]) / base[f]))) + tuple(v[3:])
        out[f] = v
    return out


def minor_share(payload, race):
    """Combined share of every candidate other than the two majors, from the payload's statewide totals; None unless the feed lists more
    than two candidates with at least 1,000 votes in all."""
    cs = payload.get("candidates") or []
    tot = sum((c.get("votes") or 0) for c in cs)
    if len(cs) <= 2 or tot < 1000: return None
    major = sum((c.get("votes") or 0) for c in cs if side_of(c, race))
    return max(0.0, 1.0 - major / tot)
