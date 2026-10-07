"""County vote-method parsers. Each parse_* takes raw text/bytes and returns normalized rows:
   {"contest": str, "cand": str, "party": "D"|"R"|other, "method": lowercase label, "votes": int}
Use rows_for(fips, rows, contest_ok) to get method_feeds rows for one race."""
import json, re, csv, io
import xml.etree.ElementTree as ET
from html import unescape

PARTY = {"DEM": "D", "D": "D", "DEMOCRATIC": "D", "REP": "R", "R": "R", "REPUBLICAN": "R"}
def _p(x): return PARTY.get(str(x or "").strip().upper(), str(x or "").strip().upper())
def _n(x):
    s = str(x).replace(",", "").strip()
    return 0 if s in ("", "-") else int(float(s))
def _row(contest, cand, party, method, votes):
    return {"contest": contest, "cand": cand, "party": _p(party), "method": method.strip().lower(), "votes": _n(votes)}

def rows_for(fips, rows, contest_ok):
    return [{"fips": fips, "method": r["method"], "party": r["party"], "votes": r["votes"]}
            for r in rows if contest_ok(r["contest"]) and r["party"] in ("D", "R")]

# Clarity: summary.json lists contests (K, C, CH, P); vt.json gives per-type votes by candidate
def parse_clarity(summary, vt):
    summary = json.loads(summary) if isinstance(summary, (str, bytes)) else summary
    vt = json.loads(vt) if isinstance(vt, (str, bytes)) else vt
    types = vt["Types"]; byk = {c["K"]: c for c in vt["Contests"]}
    out = []
    for c in summary:
        v = byk.get(c["K"])
        if not v: continue
        for i, cand in enumerate(c["CH"]):
            for t, votes in zip(types, v["Votes"][i]):
                out.append(_row(c["C"], cand, c["P"][i], t, votes))
    return out

# Enhanced Voting (Cochise-style): ballotItems[].summaryResults.ballotOptions[].groupResults[]
def _txt(l): return (l or [{}])[0].get("text", "") if isinstance(l, list) else str(l or "")
def parse_enhanced(data):
    d = json.loads(data) if isinstance(data, (str, bytes)) else data
    out = []
    for it in d["ballotItems"]:
        name = _txt(it["name"])
        for o in (it.get("summaryResults") or {}).get("ballotOptions", []):
            party = (o.get("party") or {}).get("abbreviation", "")
            for g in o.get("groupResults", []):
                out.append(_row(name, _txt(o["name"]), party, _txt(g["groupName"]), g["voteCount"]))
    return out

# ES&S ENR summary XML: one <Table> per candidate
def parse_ess(xml):
    root = ET.fromstring(xml)
    out = []
    for t in root.iter("Table"):
        g = lambda k: (t.findtext(k) or "")
        for tag, m in (("EarlyVotes", "early"), ("AbsenteeVotes", "absentee"),
                       ("ElectionDayVotes", "election day"), ("ProvisionalVotes", "provisional")):
            out.append(_row(g("RaceName"), g("ContestantName"), g("Party"), m, g(tag) or 0))
    return out

# VR Systems candidate-summary CSV (Mail / Early / Election Day)
def parse_vr_csv(text):
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        for col, m in (("Mail Votes", "mail"), ("Early Votes", "early"), ("Election Day Votes", "election day")):
            if col in r:
                out.append(_row(r["Contest"], r["Candidate Issue"], r["Party"], m, r[col]))
    return out

# VR Systems summary HTML ("Detailed" table: Choice | Election Day | Early Votes | Vote By Mail | Total)
def parse_vr_html(html):
    out = []
    parts = re.split(r'(?=<h\d[^>]*>)', html)
    blocks = re.findall(r'<table[^>]*>.*?</table>', html, flags=re.S)
    for b in blocks:
        rows = [[unescape(re.sub(r'<[^>]+>', ' ', c)).replace("\xa0", " ").strip() for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r, flags=re.S)]
                for r in re.findall(r'<tr[^>]*>.*?</tr>', b, flags=re.S)]
        rows = [[re.sub(r'\s+', ' ', c) for c in r] for r in rows if r]
        if not rows or "Election Day" not in rows[0] or "Vote By Mail" not in rows[0]: continue
        h = rows[0]; idx = {m: h.index(m) for m in ("Election Day", "Early Votes", "Vote By Mail") if m in h}
        pos = b and html.find(b)
        labs = re.findall(r'<label[^>]*>\s*([^<]+?)\s*</label>', html[max(0, pos-6000):pos])
        contest = unescape(labs[-1]) if labs else ""
        for r in rows[1:]:
            m = re.match(r'(.*?)\s*\((\w+)\)\s*$', r[0])
            cand, party = (m.group(1), m.group(2)) if m else (r[0], "")
            for k, i in idx.items():
                if i < len(r):
                    out.append(_row(contest, cand, party, {"Election Day": "election day", "Early Votes": "early", "Vote By Mail": "mail"}[k], r[i]))
    return out

# Texas SoS County.json: {fips: {Races: {oid: {ON, C: {id: {N,P,V,EV}}}}}}; ED = V - EV
def parse_tx(data):
    d = json.loads(data) if isinstance(data, (str, bytes)) else data
    out = {}
    for fips, c in d.items():
        rs = []
        for r in c["Races"].values():
            for x in r["C"].values():
                rs.append(_row(r["ON"], x["N"], x["P"], "early", x["EV"]))
                rs.append(_row(r["ON"], x["N"], x["P"], "election day", x["V"] - x["EV"]))
        out[fips] = rs
    return out

# NCSBE results_N.txt (JSON array): evc ED, ovc one-stop, avc absentee mail, pvc provisional
def parse_nc(data):
    d = json.loads(data) if isinstance(data, (str, bytes)) else data
    out = []
    for r in d:
        for k, m in (("evc", "election day"), ("ovc", "one-stop"), ("avc", "absentee mail"), ("pvc", "provisional")):
            out.append(_row(r["cnm"], r["bnm"], r["pty"], m, r.get(k) or 0))
    return out

# Maricopa-style tab-delimited report; sums precincts. Methods from "Votes_<method>" columns.
def parse_maricopa(text):
    rd = csv.reader(io.StringIO(text), delimiter="\t")
    hdr = next(rd); ix = {h: i for i, h in enumerate(hdr)}
    mcols = [(h[6:], i) for h, i in ix.items() if h.startswith("Votes_")]
    tot = {}
    for r in rd:
        if len(r) < len(hdr): continue
        key = (r[ix["ContestName"]], r[ix["CandidateName"]], r[ix["CandidateAffiliation"]])
        t = tot.setdefault(key, {})
        for m, i in mcols: t[m] = t.get(m, 0) + _n(r[i] or 0)
    return [_row(c, n, p, m, v) for (c, n, p), t in tot.items() for m, v in t.items()]

# Dauphin-style HTML: contest heading, then per-precinct blocks "Machine|Mail-in|Provisional|Total" + candidate rows.
# Party isn't on the page, so pass parties={"ROBERT P CASEY JR": "D", ...}.
def parse_dauphin(html, parties=None):
    parties = parties or {}
    t = re.sub(r'<script.*?</script>|<style.*?</style>', '', html, flags=re.S)
    t = unescape(re.sub(r'<[^>]+>', '|', t))
    toks = [x.strip() for x in t.split('|') if x.strip() and x.strip() != '-->']
    isnum = lambda x: re.fullmatch(r'[\d,]+', x) is not None
    out = []; contest = ""; i = 0
    while i < len(toks):
        if toks[i] == "Machine":
            j = i; heads = []
            while toks[j] != "Total": heads.append(toks[j]); j += 1
            n = len(heads) + 1; j += 1
            while j + n <= len(toks) and all(isnum(x) for x in toks[j + 1:j + 1 + n]):
                for m, v in zip(heads, toks[j + 1:j + n]):
                    out.append(_row(contest, toks[j], parties.get(toks[j], ""), m, v))
                j += n + 1
            i = j
        elif i + 2 < len(toks) and toks[i + 2] == "Machine":
            contest = toks[i]; i += 1       # contest heading, then precinct title
        else: i += 1
    return out


# Clarity detail.xml (inside detailxml.zip): Contest > Choice > VoteType (name, votes). Used for counties whose
# vt.json is missing. Structure taken from the Michigan primary feed notes; NOT yet verified against a live file.
def parse_clarity_detail(xml):
    root = ET.fromstring(xml)
    out = []
    for contest in root.iter():
        if not contest.tag.lower().endswith("contest"):
            continue
        cname = contest.get("text") or contest.get("name") or ""
        for choice in contest:
            if not choice.tag.lower().endswith("choice"):
                continue
            cand = choice.get("text") or choice.get("name") or ""
            party = choice.get("party") or choice.get("partyName") or ""
            for vt in choice:
                if not vt.tag.lower().endswith("votetype"):
                    continue
                v = vt.get("votes")
                if v is None:
                    v = sum(int(p.get("votes") or 0) for p in vt)
                out.append(_row(cname, cand, party, vt.get("name") or "", v))
    return out
