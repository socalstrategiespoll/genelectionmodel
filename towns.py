# Town-level units for Maine and Vermont. A race that carries a "towns" list is projected with every town as its own unit
# (pure deduction: the statewide swing, turnout and regional pattern are learned from the towns that have reported).
# The site still draws county tables and maps, so the town-level result is rolled up to counties here.
def engine_race(r):
    """The race dict the engine should prep: towns as units when the race has them, otherwise the race unchanged."""
    if not r.get("towns"): return r
    e = dict(r)
    e["counties"] = [[t[0], t[1], t[2], t[3]] for t in r["towns"]]
    e["buckets"] = []; e["shares"] = {}; e.pop("county_shares", None)
    return e

def wrap(res, r, live_towns):
    """Roll a town-level engine result up to the race's counties (same 12-field rows the site already reads) and attach the live town rows."""
    if not r.get("towns"): return res
    cty = {t[0]: t[4] for t in r["towns"]}
    agg = {}
    for row in res["counties"]:
        f = row[0]; c = cty.get(f)
        if c is None: continue
        pct, n, rem, pm, _, sw, cD, cR, rmm = row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10]
        Tp = n + rem
        a = agg.setdefault(c, dict(pw=0.0, n=0, rem=0, Tp=0.0, fin=0.0, sw=0.0, cD=0, cR=0, rm=0.0, rmv=0.0))
        a["pw"] += (pct or 0) * Tp; a["n"] += n; a["rem"] += rem; a["Tp"] += Tp
        if pm is not None: a["fin"] += pm * Tp / 100.0
        a["sw"] += (sw or 0) * Tp; a["cD"] += cD; a["cR"] += cR
        if rmm is not None: a["rm"] += rmm * rem / 100.0
    rows = []
    for c in r["counties"]:
        a = agg.get(c[0])
        if not a or a["Tp"] <= 0:
            rows.append([c[0], c[1], 0.0, 0, 0, None, False, 0.0, 0, 0, None, None]); continue
        rows.append([c[0], c[1], round(a["pw"] / a["Tp"], 3), a["n"], a["rem"], round(100 * a["fin"] / a["Tp"], 2), False,
                     round(a["sw"] / a["Tp"], 2), a["cD"], a["cR"],
                     round(100 * a["rm"] / a["rem"], 2) if a["rem"] > 1 else None,
                     round(100 * (a["cR"] - a["cD"]) / a["n"], 2) if a["n"] > 0 else None])
    out = dict(res); out["counties"] = rows; out["towns"] = live_towns or {}
    return out
