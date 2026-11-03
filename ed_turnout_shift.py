"""Florida Election Day baseline from VoteHub's projection, shifted by today's Election Day registration.

    python ed_turnout_shift.py freshtake.json votehub_components.json votehub_estimates.json            # dry run
    python ed_turnout_shift.py freshtake.json votehub_components.json votehub_estimates.json --write    # writes data.json
    options: --base data.before_ed_shift.json   pre-shift baseline (created on the first --write, reused after, so a
                                                rerun later in the day replaces the update instead of stacking it)
             --k 0.9      vote-margin points per point of R-minus-D registration gap (party loyalty ~90%)
             --half 500   Election Day ballots at which a county's registration read gets half weight

Per county (the countywide race, FL-Gov):
  VoteHub projected Election Day margin  = the "remaining" candidate margin in VoteHub's FL-Gov county estimates
  VoteHub projected Election Day registration gap = R share - D share of VoteHub's "remaining" voters (components file)
  today's Election Day registration gap = R share - D share of Election Day ballots so far (FreshTake)
  Election Day margin = VoteHub margin + k * (today's gap - VoteHub's gap) * w,   w = n / (n + half)
The county's Election Day margin in the model is replaced by that, and the county and race toplines move with it.
House races hold only part of some counties, so each takes the same per-county change the countywide race got.
Vote totals and method shares do not change.

freshtake.json: {county name: [[voters R,D,O,All], [mail R,D,O,All], [early R,D,O,All], [eday R,D,O,All], updated]}
"""
import json, os, re, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GOV = "G2026FL00"

def norm(s): return re.sub(r"[^a-z]", "", s.lower().replace("saint", "st"))

def county_targets(ft, comp, est, k, half):
    cb = {norm(v["name"]): (f, v) for f, v in comp["features"].items()}
    eb = {f: v for f, v in est["features"].items()}
    out = {}
    for name, (vot, mail, early, ed, upd) in ft.items():
        hit = cb.get(norm(name))
        if not hit: raise ValueError("FreshTake county %r not in the VoteHub components file" % name)
        f, v = hit
        g = v["components"]["party_registration"]["toplines"]["remaining"]["groups"]
        rem = [g["republican"]["count"], g["democratic"]["count"], g["other"]["count"]]
        vh_gap = 100 * (rem[0] - rem[1]) / (sum(rem) or 1)
        e = (eb.get(f) or {}).get("toplines", {}).get("remaining")
        if not e: continue
        mm = e["map_margin"]; vh_m = mm["value"] * (1 if mm["candidate_id"].endswith("R") else -1)
        n = sum(ed[:3])
        if n <= 0: continue
        gap = 100 * (ed[0] - ed[1]) / n
        w = n / (n + half)
        out[f] = dict(name=name, n=n, vh_gap=vh_gap, gap=gap, vh_m=vh_m, w=w, target=vh_m + k * (gap - vh_gap) * w, updated=upd,
                      early_gap=100 * (early[0] - early[1]) / max(sum(early[:3]), 1))
    return out

def apply(r, change):
    """change: {fips: Election Day margin change}. Returns (old topline, new topline, new statewide ED margin)."""
    keys = [b["key"] for b in r.get("buckets") or []]
    if "ed" not in keys: return None
    j = keys.index("ed"); dflt = [r["shares"][x] for x in keys]; sh_all = r.get("county_shares") or {}
    D0 = sum(c[2] for c in r["counties"]); R0 = sum(c[3] for c in r["counties"]); T = D0 + R0
    num = den = 0.0
    for c in r["counties"]:
        d = change.get(c[0]); tot = c[2] + c[3]
        if d is None or tot <= 0 or len(c) <= 4 + j or c[4 + j] is None: continue
        sh = sh_all.get(c[0]) or dflt; w = sh[j] / (sum(sh) or 1)
        new = max(-100, min(100, c[4 + j] + d)); d = new - c[4 + j]; c[4 + j] = round(new, 2)
        m = max(-100, min(100, 100 * (c[3] - c[2]) / tot + w * d))
        c[3] = int(round(tot * (1 + m / 100) / 2)); c[2] = tot - c[3]
        num += tot * w * d; den += tot * w
    D1 = sum(c[2] for c in r["counties"]); R1 = sum(c[3] for c in r["counties"])
    old = r["headline"]; r["headline"] = r["margin"] = round(old + 100 * (R1 - D1) / T - 100 * (R0 - D0) / T, 2)
    if den: r["bucket_margins"]["ed"] = round(r["bucket_margins"]["ed"] + num / den, 2)
    return old, r["headline"], r["bucket_margins"]["ed"]

def apply_uniform(r, change):
    """change: {fips: points}. Every method margin of that county row moves by the same amount."""
    keys = [b["key"] for b in r.get("buckets") or []]
    D0 = sum(c[2] for c in r["counties"]); R0 = sum(c[3] for c in r["counties"]); T0 = D0 + R0
    for c in r["counties"]:
        d = change.get(c[0]); tot = c[2] + c[3]
        if d is None or tot <= 0: continue
        for i in range(len(keys)):
            if len(c) > 4 + i and c[4 + i] is not None: c[4 + i] = round(max(-100, min(100, c[4 + i] + d)), 2)
        m = max(-100, min(100, 100 * (c[3] - c[2]) / tot + d))
        c[3] = int(round(tot * (1 + m / 100) / 2)); c[2] = tot - c[3]
    D1 = sum(c[2] for c in r["counties"]); R1 = sum(c[3] for c in r["counties"])
    old = r["headline"]; r["headline"] = r["margin"] = round(old + 100 * (R1 - D1) / T0 - 100 * (R0 - D0) / T0, 2)
    cs = r.get("county_shares") or {}
    for i, k in enumerate(keys):
        num = den = 0.0
        for c in r["counties"]:
            sh = cs.get(c[0]) or [r["shares"][x] for x in keys]; v = (c[2] + c[3]) * sh[i] / (sum(sh) or 1)
            if len(c) > 4 + i and c[4 + i] is not None: num += v * c[4 + i]; den += v
        if den: r["bucket_margins"][k] = round(num / den, 2)
    return old, r["headline"], r["bucket_margins"].get("ed")

def main():
    a = sys.argv[1:]
    if len(a) < 3: print(__doc__); sys.exit(1)
    opt = lambda n, d: a[a.index(n) + 1] if n in a else d
    data = opt("--data", os.path.join(HERE, "data.json"))
    base = opt("--base", os.path.join(os.path.dirname(data), "data.before_ed_shift.json"))
    k = float(opt("--k", "0.9")); half = float(opt("--half", "500")); write = "--write" in a
    tg = county_targets(json.load(open(a[0])), json.load(open(a[1])), json.load(open(a[2])), k, half)
    src = base if os.path.exists(base) else data
    d = json.load(open(src))
    fm = lambda m: ("R+%.1f" % m) if m > 0 else ("D+%.1f" % -m) if m < 0 else "EVEN"
    gov = next(r for r in d["races"] if r["id"] == GOV)
    j = [b["key"] for b in gov["buckets"]].index("ed")
    base_ed = {c[0]: c[4 + j] for c in gov["counties"]}
    # --from votehub: start from VoteHub's projected Election Day margin. Default (--from model): keep the model's own
    # Election Day baseline (VoteHub's projection runs ~6 points left of registration statewide) and apply only the
    # registration change vs what VoteHub expected.
    mode = opt("--from", "model")
    if mode == "turnout":
        # Election Day projected from who is voting today: the county's early in-person vote shows how its registration
        # turns into votes (lean = early in-person margin - k * early registration gap); apply that to today's
        # Election Day registration gap. Small counties blend toward their baseline until enough ballots are in.
        je = [b["key"] for b in gov["buckets"]].index("eip")
        eip = {c[0]: c[4 + je] for c in gov["counties"]}
        change = {}
        for f, t in tg.items():
            if f not in base_ed: continue
            lean = eip[f] - k * t["early_gap"]
            lvl = k * t["gap"] + lean
            t["vh_m"] = base_ed[f]; t["target"] = base_ed[f] + t["w"] * (lvl - base_ed[f])
            change[f] = t["target"] - base_ed[f]
    elif mode == "votehub":
        change = {f: t["target"] - base_ed[f] for f, t in tg.items() if f in base_ed}
    else:
        change = {f: t["target"] - t["vh_m"] for f, t in tg.items() if f in base_ed}
        for f, t in tg.items(): t["vh_m"] = base_ed.get(f, 0); t["target"] = t["vh_m"] + change.get(f, 0)
        # Floor: a county's Election Day voters should not vote bluer than its early in-person voters would at the same
        # registration mix (early in-person margin + k x (ED gap - early gap)). Miami-Dade broke this: VoteHub's county
        # forecast implied a soft Election Day, then today's Election Day voters came in less Republican than VoteHub
        # expected, stacking to D+4.5 Election Day against R+3.6 early on almost the same registration.
        je_ = [b["key"] for b in gov["buckets"]].index("eip")
        eipm = {c[0]: c[4 + je_] for c in gov["counties"]}
        for f, t in tg.items():
            if f not in base_ed or f not in eipm: continue
            floor = eipm[f] + k * (t["gap"] - t["early_gap"])
            floor = base_ed[f] + t["w"] * (floor - base_ed[f])        # small counties: only as far as their ballots support
            if t["target"] < floor:
                t["floored"] = True; t["target"] = floor; change[f] = floor - base_ed[f]
    print("baseline from", os.path.basename(src))
    print("county          ED so far  VoteHub reg  today reg   VoteHub ED  -> new ED   (model baseline ED)")
    for f, t in sorted(tg.items(), key=lambda x: -x[1]["n"])[:20]:
        print(f"{t['name']:14s} {t['n']:>9,}   {fm(t['vh_gap']):>8s}   {fm(t['gap']):>8s}    {fm(t['vh_m']):>8s} -> {fm(t['target']):>7s}   ({fm(base_ed.get(f, 0))}){'  floored at early-vote level' if t.get('floored') else ''}")
    print()
    ref_path = opt("--ref", os.path.join(os.path.dirname(data), "data.votehub_baseline.json"))
    ref = {r["id"]: r for r in json.load(open(ref_path))["races"]} if os.path.exists(ref_path) else {}
    top = lambda c: 100 * (c[3] - c[2]) / max(c[2] + c[3], 1)
    def race_change(r):
        """House districts: registration is only known countywide, so a district keeps the gap VoteHub had between its
        part of each county and the whole county (FL-13's part of Pinellas ran ~5 points redder than all of Pinellas).
        District part topline = the county's new FL-Gov topline + that gap; its Election Day margin is solved to hit it
        (mail and early stay at their actual volumes and tracker margins)."""
        if mode not in ("turnout", "model") or r is gov: return change
        rr, rg = ref.get(r["id"]), ref.get(GOV)
        ks = [b["key"] for b in r.get("buckets") or []]
        if not rr or not rg or "ed" not in ks: return change
        og = {c[0]: c for c in rg["counties"]}; orr = {c[0]: c for c in rr["counties"]}; ng = {c[0]: c for c in gov["counties"]}
        jd = ks.index("ed"); cs = r.get("county_shares") or {}; out = {}
        for c in r["counties"]:
            f = c[0]; T = c[2] + c[3]
            if f not in tg or f not in og or f not in orr or f not in ng or T <= 0 or len(c) <= 4 + jd or c[4 + jd] is None: continue
            # the district part is assumed to differ from the whole county by the same amount in every vote method
            out[f] = top(ng[f]) + (top(orr[f]) - top(og[f])) - top(c)
        return ("uniform", out)
    for r in [gov] + [r for r in d["races"] if r["state"] == "FL" and r["id"] != GOV]:
        ch = race_change(r)
        res = apply_uniform(r, ch[1]) if isinstance(ch, tuple) else apply(r, ch)
        if res: print(f"{r['label']:8s} {fm(res[0]):>7s} -> {fm(res[1]):<7s}  Election Day now {fm(res[2])}")
    if not write: print("\nDry run. Add --write to save."); return
    if not os.path.exists(base): shutil.copy(data, base); print("saved pre-shift baseline:", base)
    d["updated"] = re.sub(r"; FL Election Day (shifted|set).*$", "", d.get("updated", "")) + \
        "; FL Election Day projected from Election Day turnout by party (" + mode + ") as of " + max(t["updated"] for t in tg.values())
    json.dump(d, open(data, "w"), separators=(",", ":"))
    print("wrote", data)

if __name__ == "__main__":
    main()
