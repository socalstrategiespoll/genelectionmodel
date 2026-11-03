"""Shift one state's Election Day margin by the same number of points in every county (registration-based update).

    python ed_margin_shift.py NV 1.94 [--scale 0.68] [--scales tx.json]   # dry run; --scale multiplies every county's
                                                 # Election Day votes, --scales {county: factor} overrides it per county
    python ed_margin_shift.py NV 1.94 --write    # writes data.json; first run saves data.before_ed_NV.json and later
                                                 # runs start from it, so a rerun replaces the shift instead of stacking

Each county's Election Day margin moves by DELTA points (positive = toward the Republican). With --scale, every
county's Election Day vote volume is multiplied by that factor (turnout update; mail / early volumes and all
margins other than the DELTA shift stay as they are). Otherwise vote volumes and method shares stay as they are; county toplines are re-weighted from the method margins and the race headline moves by the
same amount as the county sum.
"""
import json, os, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__))

def shift(d, st, delta, scale=1.0, scales=None):
    scales = scales or {}
    out = []
    for r in d["races"]:
        if r["state"] != st: continue
        ks = [b["key"] for b in r.get("buckets") or []]
        if "ed" not in ks: continue
        j = ks.index("ed"); cs = r.get("county_shares") or {}
        agg = lambda: 100 * sum(c[3] - c[2] for c in r["counties"]) / sum(c[2] + c[3] for c in r["counties"])
        old = agg(); ev = em = 0
        for c in r["counties"]:
            T = c[2] + c[3]
            if T <= 0 or len(c) <= 4 + j or c[4 + j] is None: continue
            sh = cs.get(c[0]) or [r["shares"][k] for k in ks]; s = sum(sh)
            M = [c[4 + i] if c[4 + i] is not None else 100 * (c[3] - c[2]) / T for i in range(len(ks))]
            vol = [T * sh[i] / s for i in range(len(ks))]; f_ = scales.get(c[1], scale); vol[j] *= f_
            c[4 + j] = M[j] = round(max(-100, min(100, M[j] + delta)), 2)
            Tn = sum(vol); m = sum(vol[i] * M[i] for i in range(len(ks))) / Tn
            c[3] = int(round(Tn * (1 + m / 100) / 2)); c[2] = int(round(Tn)) - c[3]
            if f_ != 1.0: cs[c[0]] = [round(v / Tn, 4) for v in vol]
            ev += vol[j]; em += vol[j] * c[4 + j]
        r["bucket_margins"]["ed"] = round(em / ev, 2)
        if scale != 1.0 or scales:
            r["county_shares"] = cs; Tt = sum(c[2] + c[3] for c in r["counties"]); r["votes"] = Tt
            tot = [sum((cs.get(c[0]) or [r["shares"][k] for k in ks])[i] * (c[2] + c[3]) for c in r["counties"]) for i in range(len(ks))]
            r["shares"] = {k: round(tot[i] / Tt, 3) for i, k in enumerate(ks)}
        oh = r["headline"]; r["headline"] = r["margin"] = round(oh + agg() - old, 2)
        out.append((r["label"], oh, r["headline"], r["bucket_margins"]["ed"]))
    return out

if __name__ == "__main__":
    st, delta = sys.argv[1], float(sys.argv[2]); write = "--write" in sys.argv
    scale = float(sys.argv[sys.argv.index("--scale") + 1]) if "--scale" in sys.argv else 1.0
    scales = json.load(open(sys.argv[sys.argv.index("--scales") + 1])) if "--scales" in sys.argv else {}   # {county name: factor}; others get --scale
    data = os.path.join(HERE, "data.json"); base = os.path.join(HERE, f"data.before_ed_{st}.json")
    src = base if os.path.exists(base) else data
    d = json.load(open(src))
    for lab, o, n, e in shift(d, st, delta, scale, scales):
        fm = lambda m: ("R+%.1f" % m) if m > 0 else ("D+%.1f" % -m) if m < 0 else "Even"
        print(f"{lab:8s} {fm(o)} -> {fm(n)}   Election Day now {fm(e)}")
    if write:
        if not os.path.exists(base): shutil.copy(data, base)
        cur = json.load(open(data)); mine = {r["id"]: r for r in d["races"] if r["state"] == st}   # other states untouched
        cur["races"] = [mine.get(r["id"], r) for r in cur["races"]]
        json.dump(cur, open(data, "w"), separators=(",", ":")); print("wrote", data)
