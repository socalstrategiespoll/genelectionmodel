"""Move race baselines to exit-poll toplines before results come in.

    python exit_poll_update.py exit_polls.csv              # dry run: prints old -> new for every race, changes nothing
    python exit_poll_update.py exit_polls.csv --write      # writes data.json (old copy saved as data.before_exit_polls_<time>.json)
    python exit_poll_update.py exit_polls.csv --write --data path/to/data.json

exit_polls.csv (see exit_polls_template.csv). One row per race; blank cells are ignored.
    race     race id (S2026TX02) or label (TX-Sen, MI-Gov, FL-07; "TX Sen" and "tx-sen" work too)
    margin   two-party margin, R minus D: "R+3.2", "D+1.5", "3.2" (R) or "-1.5" (D)
    dem,rep  or the two candidates' percentages instead of margin (margin = rep - dem, renormalised to the two-party vote)
    early, ed, mail, eip, em, lm    optional: statewide margin for that vote method, when the exit poll breaks it out
    absorb   optional: which methods take the topline shift, e.g. "ed" or "all" (default below)

What changes, and what does not:
  - Each county's margin moves; its vote total, method shares and turnout do not. Only margins change.
  - Default (same rule as the VoteHub topline updates): ballots already cast (mail, early in-person, early) stay as they are,
    and Election Day plus late ballots (lm) take the whole shift. Texas: every method shifts together.
  - A method given its own exit-poll margin is set to it (the same amount in every county); the remaining methods then take
    whatever shift is still needed to hit the topline. Give only method margins and no topline, and the topline follows from them.
  - Hand-set counties stay where they are (HOLD below: Denton in TX-Sen); the other counties make up the difference.
  - Maine and Vermont town rows move with their race. Safe / not-modeled races in data["safe"] just get the new margin.
  - Win probability is not edited: the engine recomputes it from the new baseline. vh_win (VoteHub, shown for reference) is left alone.
"""
import csv, json, re, sys, time, os, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
HOLD = {"S2026TX02": {"48121"}}              # counties frozen by hand (Denton at D+1 in TX-Sen)
CAST = {"mail", "eip", "early", "em"}        # methods already cast before Election Day (left alone by default)
ALL_SHIFT_STATES = {"TX"}                    # states where every method shifts together

def parse_margin(s):
    s = (s or "").strip().upper().replace(" ", "")
    if not s: return None
    m = re.fullmatch(r"([RD])\+?(-?\d+(?:\.\d+)?)", s)
    if m: return float(m.group(2)) * (1 if m.group(1) == "R" else -1)
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)([RD])", s)
    if m: return float(m.group(1)) * (1 if m.group(2) == "R" else -1)
    if s in ("EVEN", "TIE", "0"): return 0.0
    return float(s)

def find_race(key, races, safe):
    k = re.sub(r"[^a-z0-9]", "", key.lower())
    for r in races:
        if k in (re.sub(r"[^a-z0-9]", "", r["id"].lower()), re.sub(r"[^a-z0-9]", "", r["label"].lower())): return r, False
    for s in safe:
        lab = s["state"] + ("-" + s["id"][-2:] if s["type"] == "House" else "-Sen" if s["type"] == "Senate" else "-Gov")
        if k in (re.sub(r"[^a-z0-9]", "", s["id"].lower()), re.sub(r"[^a-z0-9]", "", lab.lower())): return s, True
    return None, None

def county_state(r):
    """Per county: votes T, margin m, bucket shares SH, bucket margins M (None filled with the county margin)."""
    keys = [b["key"] for b in r.get("buckets") or []]
    dflt = [r["shares"][k] for k in keys]
    out = []
    for c in r["counties"]:
        T = c[2] + c[3]; m = (c[3] - c[2]) / T * 100 if T else 0.0
        sh = (r.get("county_shares") or {}).get(c[0]) or dflt
        s = sum(sh) or 1; sh = [x / s for x in sh]
        M = [c[4 + j] if len(c) > 4 + j and c[4 + j] is not None else m for j in range(len(keys))]
        out.append((T, m, sh, M))
    return keys, out

def apply(r, delta, hold):
    """delta: {bucket key: shift} (or {"all": shift} for races without method buckets). Returns each county's topline shift."""
    keys, cs = county_state(r); shifts = []
    for c, (T, m, sh, M) in zip(r["counties"], cs):
        if c[0] in hold or T <= 0: shifts.append(0.0); continue
        if keys:
            newM = [max(-100, min(100, M[j] + delta.get(k, 0.0))) for j, k in enumerate(keys)]
            dm = sum(sh[j] * (newM[j] - M[j]) for j in range(len(keys)))
            for j in range(len(keys)):
                if len(c) > 4 + j and c[4 + j] is not None: c[4 + j] = round(newM[j], 2)
        else:
            dm = delta.get("all", 0.0)
        mg = max(-100, min(100, m + dm)); dm = mg - m
        c[3] = int(round(T * (1 + mg / 100) / 2)); c[2] = T - c[3]
        shifts.append(dm)
    return shifts

def agg_margin(r):
    D = sum(c[2] for c in r["counties"]); R = sum(c[3] for c in r["counties"])
    return (R - D) / max(D + R, 1) * 100

def update_race(r, target, mtargets, absorb):
    keys = [b["key"] for b in r.get("buckets") or []]
    hold = HOLD.get(r["id"], set())
    bm = r.get("bucket_margins") or {}
    # "early" in a race without an early method (mail / eip, Arizona em / lm, Nevada lm) means every ballot not cast on
    # Election Day, moved together; any other method name must be one of the race's own
    groups = {}
    for k in mtargets:
        g = [k] if k in keys else ([x for x in keys if x != "ed"] if k == "early" and "ed" in keys else [])
        if not g: raise ValueError(f"no {k} vote method in this race (it has {', '.join(keys) or 'none'})")
        groups[k] = g
    targeted = {x for g in groups.values() for x in g}
    if absorb == "all": free = [k for k in keys if k not in targeted]
    elif absorb: free = [k for k in re.split(r"[ ,;/+]+", absorb) if k]
    elif r["state"] in ALL_SHIFT_STATES: free = [k for k in keys if k not in targeted]
    else: free = [k for k in keys if k not in CAST and k not in targeted]
    bad = [k for k in free if k not in keys or k in targeted]
    if bad: raise ValueError(f"absorb lists {bad}: must be methods of this race without their own exit-poll margin ({keys})")
    if keys and target is not None and not free:
        free = [k for k in keys if k not in targeted]       # Election Day has its own exit-poll margin: the other methods take the rest
        if not free: raise ValueError("every method has its own exit-poll margin, so nothing is left to hit the topline; drop one or the topline")
    old_head = r["headline"]; old_bm = dict(bm)
    a0 = agg_margin(r)
    fixed = {}                                                        # method targets: same shift in every county
    for k, g in groups.items():
        w = sum(r["shares"][x] for x in g) or 1
        cur = sum(r["shares"][x] * bm.get(x, 0.0) for x in g) / w
        for x in g: fixed[x] = mtargets[k] - cur
    want = None if target is None else target - old_head              # the county aggregate moves by exactly this much
    snap = json.loads(json.dumps(r["counties"])); x = 0.0
    for _ in range(6):                                                # linear apart from clipping at +/-100: a few passes settle it
        r["counties"] = json.loads(json.dumps(snap))
        d = dict(fixed); d.update({k: x for k in free}) if keys else d.update({"all": x})
        apply(r, d, hold)
        if want is None: break
        err = want - (agg_margin(r) - a0)
        if abs(err) < 0.005: break
        # how much the aggregate moves per point of x
        keys_, cs = county_state({**r, "counties": snap})
        W = sum(T for T, *_ in cs) or 1
        slope = sum(T * (sum(sh[j] for j, k in enumerate(keys_) if k in free) if keys_ else 1.0)
                    for c, (T, m, sh, M) in zip(snap, cs) if c[0] not in hold) / W
        if slope <= 1e-9: raise ValueError(f"{r['label']}: no counties / methods free to move")
        x += err / slope
    moved = agg_margin(r) - a0
    new_head = round(old_head + moved, 2) if target is None else round(target, 2)
    r["headline"] = r["margin"] = new_head
    for k in keys:                                                    # statewide method margins move by the vote-weighted county shift
        if k in bm:
            keys_, cs_old = county_state({**r, "counties": snap}); _, cs_new = county_state(r)
            j = keys_.index(k)
            num = sum(T * sh[j] * (Mn[j] - Mo[j]) for (T, _, sh, Mo), (_, _, _, Mn) in zip(cs_old, cs_new))
            den = sum(T * sh[j] for (T, _, sh, _) in cs_old) or 1
            bm[k] = round(old_bm[k] + num / den, 2)
    for t in r.get("towns") or []:                                    # Maine / Vermont towns: same shift as their race
        tt = t[2] + t[3]
        if tt > 0:
            mg = max(-100, min(100, (t[3] - t[2]) / tt * 100 + moved))
            t[3] = int(round(tt * (1 + mg / 100) / 2)); t[2] = tt - t[3]
    return old_head, new_head, old_bm, dict(bm), free

def main():
    args = sys.argv[1:]
    if not args or args[0].startswith("-"): print(__doc__); sys.exit(1)
    src = args[0]; write = "--write" in args
    dpath = args[args.index("--data") + 1] if "--data" in args else os.path.join(HERE, "data.json")
    d = json.load(open(dpath)); races = d["races"]; safe = d.get("safe") or []
    rows = list(csv.DictReader(open(src, newline="", encoding="utf-8-sig")))
    report, errors = [], []
    for row in rows:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        key = row.get("race", "")
        if not key or key.startswith("#"): continue
        r, is_safe = find_race(key, races, safe)
        if r is None: errors.append(f"{key}: not a race in data.json"); continue
        try:
            target = parse_margin(row.get("margin"))
            if target is None and row.get("dem") and row.get("rep"):
                dm_, rp = float(row["dem"]), float(row["rep"]); target = (rp - dm_) / (rp + dm_) * 100
            mt = {k: parse_margin(row[k]) for k in ("mail", "eip", "early", "ed", "em", "lm") if row.get(k)}
            if is_safe:
                if target is None: raise ValueError("needs a margin")
                report.append((r["id"], r["m"], round(target, 2), "", "safe race (margin only)")); r["m"] = round(target, 2); continue
            if target is None and not mt: raise ValueError("no margin given")
            old, new, obm, nbm, free = update_race(r, target, mt, row.get("absorb", "").lower())
            meth = "  ".join(f"{k} {obm.get(k, 0):+.1f}->{nbm.get(k, 0):+.1f}" for k in nbm)
            report.append((r["label"], old, new, meth, ("shift on: " + (", ".join(free) or "county margins")) if target is not None else "set by method margins"))
        except Exception as e:
            errors.append(f"{key}: {e}")
    fmt = lambda m: ("R+%.1f" % m) if m > 0 else ("D+%.1f" % -m) if m < 0 else "EVEN"
    for lab, o, n, meth, note in report:
        print(f"{lab:10s} {fmt(o):>7s} -> {fmt(n):<7s} {note:28s} {meth}")
    for e in errors: print("ERROR", e)
    if errors: print("\nNothing written: fix the rows above first."); sys.exit(2)
    if not write: print("\nDry run. Add --write to save."); return
    bak = os.path.join(os.path.dirname(dpath), "data.before_exit_polls_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy(dpath, bak)
    d["updated"] = (d.get("updated", "") + "; " if d.get("updated") else "") + "baselines moved to exit polls " + time.strftime("%Y-%m-%d %H:%M")
    json.dump(d, open(dpath, "w"), separators=(",", ":"))
    print(f"\nWrote {dpath} ({len(report)} races). Previous copy: {bak}")

if __name__ == "__main__":
    main()
