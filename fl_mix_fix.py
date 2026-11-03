"""Florida: set each county's mail / early in-person vote to the actual counts (early voting is over; mail is nearly in).

    python fl_mix_fix.py freshtake.json data.json [more data files ...]    # edits each file in place (copies to <file>.before_mix)

The model had every Florida county on the statewide method split (~26% mail / 44% early / 30% Election Day); the real
county mixes differ a lot (Pinellas, Lee, Sarasota are mail-heavy; Duval, St. Johns early-heavy).
Per county, in FL-Gov (whole counties):
    early in-person = actual early ballots x 0.98 (two-party share)
    mail            = actual mail ballots x LATE_MAIL x 0.98 (ballots still arriving today)
    Election Day    = unchanged
House districts hold parts of some counties, so each of their county rows scales its mail and early votes by the same
ratio (actual / FL-Gov's old expectation) as the whole county. Mail and early margins are unchanged. With KEEP_TOPLINE
(default) each county keeps its topline and its Election Day margin absorbs the change (the rule used for every topline
update); otherwise county toplines are re-weighted from the method margins and the race headline moves with them.
"""
import json, re, shutil, sys

LATE_MAIL = 1.0       # no flat bump: today's arrivals come from the mail-today file (TODAY_MULT projects the rest of the day)
TODAY_MULT = 2.5      # mail arriving today, full day / so far (18.5k by late morning; drop-offs continue to 7 PM)
K = 0.9               # vote-margin points per point of R-minus-D registration gap
KEEP_TOPLINE = True   # county toplines stay; Election Day absorbs (set False to let toplines follow the new mix)
TWO_PARTY = 0.98

def norm(s): return re.sub(r"[^a-z]", "", s.lower().replace("saint", "st"))

def fix(d, ft, today=None):
    gov = next(r for r in d["races"] if r["id"] == "G2026FL00")
    keys = [b["key"] for b in gov["buckets"]]
    act = {norm(n): {"mail": m[3] * LATE_MAIL * TWO_PARTY, "eip": e[3] * TWO_PARTY} for n, (v, m, e, ed, u) in ft.items()}
    # mail arriving today is a different electorate (statewide D+0.8 by registration vs D+7.8 for all mail): its margin is
    # the county's mail margin moved by K x (today's registration gap - all mail's registration gap)
    late = {}
    if today:
        tn = {norm(n): v for n, v in today.items()}
        for n, (v, m, e, ed, u) in ft.items():
            t = tn.get(norm(n))
            if not t or sum(t) <= 0: continue
            so_far = sum(t); full = so_far * TODAY_MULT
            gap_t = 100 * (t[0] - t[1]) / so_far
            gap_m = 100 * (m[0] - m[1]) / max(sum(m[:3]), 1)
            act[norm(n)]["mail"] += (full - so_far) * TWO_PARTY          # the rest of today's arrivals
            late[norm(n)] = (full * TWO_PARTY, K * (gap_t - gap_m))
    ratio = {}
    for c in gov["counties"]:
        a = act.get(norm(c[1]))
        if not a: continue
        sh = (gov.get("county_shares") or {}).get(c[0]) or [gov["shares"][k] for k in keys]
        T = c[2] + c[3]; s = sum(sh)
        ratio[c[0]] = {k: a[k] / max(T * sh[keys.index(k)] / s, 1) for k in ("mail", "eip")}
    out = []
    for r in d["races"]:
        if r["state"] != "FL": continue
        ks = [b["key"] for b in r.get("buckets") or []]
        if set(ks) != {"mail", "eip", "ed"}: continue
        cs = r.setdefault("county_shares", {})
        D0 = sum(c[2] for c in r["counties"]); R0 = sum(c[3] for c in r["counties"]); old_agg = 100 * (R0 - D0) / (D0 + R0)
        for c in r["counties"]:
            q = ratio.get(c[0]); T = c[2] + c[3]
            if not q or T <= 0: continue
            sh = cs.get(c[0]) or [r["shares"][k] for k in ks]; s = sum(sh)
            vol = {k: T * sh[i] / s for i, k in enumerate(ks)}
            vol["mail"] *= q["mail"]; vol["eip"] *= q["eip"]
            M = {k: (c[4 + i] if len(c) > 4 + i and c[4 + i] is not None else 100 * (c[3] - c[2]) / T) for i, k in enumerate(ks)}
            lt = late.get(norm(c[1]))
            if lt and vol["mail"] > 0:     # today's arrivals (scaled to this race's share of the county) at their own margin
                q_ = vol["mail"] / max(act[norm(c[1])]["mail"], 1); lv = min(lt[0] * q_, vol["mail"])
                jm = ks.index("mail")
                M["mail"] = max(-100, min(100, M["mail"] + lt[1] * lv / vol["mail"]))
                if len(c) > 4 + jm and c[4 + jm] is not None: c[4 + jm] = round(M["mail"], 2)
            Tn = sum(vol.values())
            if KEEP_TOPLINE and r["type"] != "House" and "ed" in ks and vol["ed"] > 0:   # House parts get their county gap back in ed_turnout_shift, spread evenly over methods
                # the county's topline (VoteHub county forecast) and its mail / early margins (tracker) stay; Election Day
                # absorbs the difference, the same rule as the topline updates
                m = 100 * (c[3] - c[2]) / T
                jd = ks.index("ed")
                M["ed"] = max(-100, min(100, (Tn * m - sum(vol[k] * M[k] for k in ks if k != "ed")) / vol["ed"]))
                if len(c) > 4 + jd and c[4 + jd] is not None: c[4 + jd] = round(M["ed"], 2)
            m = sum(vol[k] * M[k] for k in ks) / Tn
            c[3] = int(round(Tn * (1 + m / 100) / 2)); c[2] = int(round(Tn)) - c[3]
            cs[c[0]] = [round(vol[k] / Tn, 4) for k in ks]
        D1 = sum(c[2] for c in r["counties"]); R1 = sum(c[3] for c in r["counties"]); T1 = D1 + R1
        new_agg = 100 * (R1 - D1) / T1
        tot = {k: sum((cs.get(c[0]) or [r["shares"][x] for x in ks])[i] * (c[2] + c[3]) for c in r["counties"]) for i, k in enumerate(ks)}
        r["shares"] = {k: round(tot[k] / T1, 3) for k in ks}
        for k in ks:      # statewide method margins: vote-weighted over counties
            j = ks.index(k)
            num = sum((cs.get(c[0]) or [r["shares"][x] for x in ks])[j] * (c[2] + c[3]) * c[4 + j] for c in r["counties"] if len(c) > 4 + j and c[4 + j] is not None)
            den = sum((cs.get(c[0]) or [r["shares"][x] for x in ks])[j] * (c[2] + c[3]) for c in r["counties"] if len(c) > 4 + j and c[4 + j] is not None)
            if den: r["bucket_margins"][k] = round(num / den, 2)
        old_h = r["headline"]; r["headline"] = r["margin"] = round(old_h + new_agg - old_agg, 2); r["votes"] = T1
        out.append((r["label"], old_h, r["headline"], D0 + R0, T1, dict(r["shares"])))
    return out

if __name__ == "__main__":
    a = sys.argv[1:]
    today = json.load(open(a[a.index("--today") + 1])) if "--today" in a else None
    if "--today" in a: i = a.index("--today"); a = a[:i] + a[i + 2:]
    ft = json.load(open(a[0]))
    for p in a[1:]:
        shutil.copy(p, p + ".before_mix")
        d = json.load(open(p))
        res = fix(d, ft, today)
        json.dump(d, open(p, "w"), separators=(",", ":"))
        print(p)
        for lab, o, n, t0, t1, sh in res: print(f"  {lab:8s} {o:+.2f} -> {n:+.2f}   votes {t0:,} -> {t1:,}   shares {sh}")
