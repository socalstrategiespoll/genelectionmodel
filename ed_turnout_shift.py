"""Shift Florida's Election Day baseline using today's Election Day turnout by party (FreshTake.Vote county pages).

    python ed_turnout_shift.py freshtake.json votehub_components.json                 # dry run: per-county and per-race report
    python ed_turnout_shift.py freshtake.json votehub_components.json --write         # writes data.json
    options: --base data.before_ed_shift.json   (pre-shift baseline; created on the first --write and reused after,
                                                 so re-running later in the day replaces the shift instead of stacking it)
             --k 0.9        margin points per point of R-minus-D registration gap (party loyalty: R ~+90, D ~-88)
             --weight 1.0   fraction of the measured shift to apply

How the shift is measured, per county:
  expected ED electorate = VoteHub's projected remaining voters by party, minus the early and mail ballots cast after
      VoteHub's snapshot (FreshTake's final early + mail by party, less VoteHub's early + mail counts)
  actual ED electorate so far = FreshTake's Election Day ballots by party
  gap change = (R share - D share) actual - (R share - D share) expected
  Election Day margin shift = k * gap change * weight
Every Florida race moves: each county's Election Day margin by its county shift (the House districts' portions of a split
county use the county's shift), and so the county topline and the race topline. Vote totals and method shares do not change.

freshtake.json: {county name: [[voters R,D,O,All], [mail R,D,O,All], [early R,D,O,All], [eday R,D,O,All], updated]}
"""
import json, os, re, shutil, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))

def norm(s): return re.sub(r"[^a-z]", "", s.lower().replace("saint", "st"))

def county_shifts(ft, vh, k, weight):
    byname = {norm(v["name"]): (f, v) for f, v in vh["features"].items()}
    out = {}
    for name, (vot, mail, early, ed, upd) in ft.items():
        hit = byname.get(norm(name))
        if not hit: raise ValueError("FreshTake county %r not in the VoteHub file" % name)
        f, v = hit
        pr = v["components"]["party_registration"]["toplines"]
        P = ("republican", "democratic", "other")
        vh_cast = [pr["early"]["groups"][p]["count"] + pr["mail_returns"]["groups"][p]["count"] for p in P]
        ft_cast = [early[i] + mail[i] for i in range(3)]
        late = [max(0, ft_cast[i] - vh_cast[i]) for i in range(3)]
        rem = [pr["remaining"]["groups"][p]["count"] for p in P]
        pool = [max(rem[i] - late[i], 0.05 * rem[i]) for i in range(3)]
        sp = sum(pool) or 1; exp_gap = 100 * (pool[0] - pool[1]) / sp
        se = sum(ed[:3])
        if se <= 0: continue
        act_gap = 100 * (ed[0] - ed[1]) / se
        out[f] = dict(name=name, ed_so_far=se, expected_pool=round(sp), expected_gap=round(exp_gap, 1), actual_gap=round(act_gap, 1),
                      shift=round(k * (act_gap - exp_gap) * weight, 2), updated=upd)
    return out

def shift_race(r, cs):
    keys = [b["key"] for b in r.get("buckets") or []]
    if "ed" not in keys: return None
    j = keys.index("ed"); dflt = [r["shares"][x] for x in keys]; sh_all = r.get("county_shares") or {}
    D0 = sum(c[2] for c in r["counties"]); R0 = sum(c[3] for c in r["counties"]); T = D0 + R0
    num = den = 0.0
    for c in r["counties"]:
        s = cs.get(c[0])
        if not s: continue
        tot = c[2] + c[3]
        if tot <= 0: continue
        sh = sh_all.get(c[0]) or dflt; w = sh[j] / (sum(sh) or 1)
        old_ed = c[4 + j] if len(c) > 4 + j and c[4 + j] is not None else 100 * (c[3] - c[2]) / tot
        new_ed = max(-100, min(100, old_ed + s["shift"])); d = new_ed - old_ed
        if len(c) > 4 + j and c[4 + j] is not None: c[4 + j] = round(new_ed, 2)
        m = max(-100, min(100, 100 * (c[3] - c[2]) / tot + w * d))
        c[3] = int(round(tot * (1 + m / 100) / 2)); c[2] = tot - c[3]
        num += tot * w * d; den += tot * w
    D1 = sum(c[2] for c in r["counties"]); R1 = sum(c[3] for c in r["counties"])
    move = 100 * (R1 - D1) / T - 100 * (R0 - D0) / T
    old = r["headline"]; r["headline"] = r["margin"] = round(old + move, 2)
    if den: r["bucket_margins"]["ed"] = round(r["bucket_margins"]["ed"] + num / den, 2)
    return old, r["headline"], (num / den if den else 0.0)

def main():
    a = sys.argv[1:]
    if len(a) < 2: print(__doc__); sys.exit(1)
    opt = lambda n, d: a[a.index(n) + 1] if n in a else d
    data = opt("--data", os.path.join(HERE, "data.json"))
    base = opt("--base", os.path.join(os.path.dirname(data), "data.before_ed_shift.json"))
    k = float(opt("--k", "0.9")); weight = float(opt("--weight", "1.0")); write = "--write" in a
    ft = json.load(open(a[0])); vh = json.load(open(a[1]))
    src = base if os.path.exists(base) else data
    d = json.load(open(src))
    cs = county_shifts(ft, vh, k, weight)
    fm = lambda m: ("R+%.1f" % m) if m > 0 else ("D+%.1f" % -m) if m < 0 else "EVEN"
    big = sorted(cs.values(), key=lambda s: -s["ed_so_far"])[:12]
    print("baseline from", os.path.basename(src))
    print("county            ED so far  expected gap  actual gap  ED margin shift")
    for s in big: print(f"{s['name']:16s} {s['ed_so_far']:>9,}   {fm(s['expected_gap']):>8s}    {fm(s['actual_gap']):>8s}    {s['shift']:+.1f}")
    tot = sum(s["ed_so_far"] for s in cs.values())
    print(f"all counties: {tot:,} ED ballots; ED-ballot-weighted shift {sum(s['shift']*s['ed_so_far'] for s in cs.values())/tot:+.2f}\n")
    for r in d["races"]:
        if r["state"] != "FL": continue
        res = shift_race(r, cs)
        if res: print(f"{r['label']:8s} {fm(res[0]):>7s} -> {fm(res[1]):<7s}  (Election Day {res[2]:+.1f})")
    if not write: print("\nDry run. Add --write to save."); return
    if not os.path.exists(base): shutil.copy(data, base); print("saved pre-shift baseline:", base)
    d["updated"] = re.sub(r"; FL Election Day shifted to turnout.*$", "", d.get("updated", "")) + "; FL Election Day shifted to turnout as of " + max(s["updated"] for s in cs.values())
    json.dump(d, open(data, "w"), separators=(",", ":"))
    print("wrote", data)

if __name__ == "__main__":
    main()
