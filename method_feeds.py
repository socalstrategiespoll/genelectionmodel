# Vote-method counts from county sites. Plug a fetcher in per race: SOURCES[race_id] = callable(race) -> rows
# rows: [{"fips": "13121", "method": "Early Voting", "party": "D" or "R", "votes": 12345}, ...]
# KEYMAP[race_id] maps each feed method name (lowercase) to one of the race's bucket keys, for example
#   3-bucket states: {"absentee by mail": "mail", "early voting": "eip", "election day": "ed"}
#   2-bucket states: {"absentee by mail": "early", "early voting": "early", "election day": "ed"}
#   Arizona: {"early": "em", "election day": "ed", "late early": "lm"}
# Methods not in the map are ignored. If a county's method totals disagree with civicAPI's total by more than
# TOLERANCE, the county falls back to inferring the counting order (the two feeds describe different ballots).
SOURCES = {}
KEYMAP = {}
TOLERANCE = 0.03
STATUS = {}   # race_id -> {fips: 'ok' | 'mismatch x%' | 'no civicAPI row' | 'fetch error: ...'}

PARENT, CHILD = "26163", "2622000"      # Wayne County and the city of Detroit, which reports on its own feed
SPLIT_TOL = 0.01                        # Detroit may lead civicAPI's Wayne total by this much (refresh jitter)

def _split_detroit(counted, by, race, st):
    """civicAPI reports Wayne as one county (Detroit included). The engine models Detroit and the rest of Wayne separately,
    so rest-of-Wayne = civicAPI Wayne - Detroit feed. Without a usable Detroit feed Wayne is held back (treated as not
    reporting) rather than being credited to the wrong half."""
    out = dict(counted); P = counted.get(PARENT)
    base = {c[0]: c[2] + c[3] for c in race["counties"]}
    det = by.get(CHILD)
    dD = sum(v[0] for v in det.values()) if det else 0
    dR = sum(v[1] for v in det.values()) if det else 0
    if P is None and not (dD + dR):
        return out
    if not (dD + dR):
        out.pop(PARENT, None)
        if P and P[0] + P[1] > 0: st[CHILD] = "no Detroit feed: Wayne held back"
        return out
    pD, pR = (P[0], P[1]) if P else (0, 0)
    pp = (P[2] if P else 0) or 0
    if (dD + dR) > (pD + pR) * (1 + SPLIT_TOL) + 200:
        # Detroit is ahead of civicAPI's Wayne: use Detroit, keep the rest of Wayne unreported until civicAPI catches up
        out.pop(PARENT, None); rD = rR = 0
        st[CHILD] = "Detroit ahead of civicAPI Wayne"
    else:
        rD, rR = max(pD - dD, 0), max(pR - dR, 0)
        st[CHILD] = "ok"
    done = pp >= 0.995
    pdet = 1.0 if done else min(0.9, (dD + dR) / max(base.get(CHILD, 1), 1))
    prest = 1.0 if done else min(0.9, (rD + rR) / max(base.get(PARENT, 1), 1))
    out[CHILD] = (dD, dR, pdet, {k: (v[0], v[1]) for k, v in det.items()})
    if rD + rR > 0: out[PARENT] = (rD, rR, prest)
    return out

def attach(counted, race, engine_keys=None):
    split = any(c[0] == CHILD for c in race.get("counties", []))
    src = SOURCES.get(race["id"]); km = KEYMAP.get(race["id"])
    if not src or not km:
        return _split_detroit(counted, {}, race, STATUS.setdefault(race["id"], {})) if split else counted
    try:
        rows = src(race)
    except Exception as e:
        STATUS[race["id"]] = {"_": "source error: %s" % str(e)[:80]}
        return _split_detroit(counted, {}, race, STATUS[race["id"]]) if split else counted
    st = STATUS[race["id"]] = {}
    by = {}
    for r in rows:
        key = km.get(str(r.get("method", "")).strip().lower())
        if key is None or (engine_keys and key not in engine_keys):
            continue
        d = by.setdefault(r["fips"], {}).setdefault(key, [0, 0])
        d[0 if r["party"] == "D" else 1] += r["votes"]
    if split:
        counted = _split_detroit(counted, by, race, st)
        by = {f: b for f, b in by.items() if f not in (CHILD, PARENT)}   # Wayne's own method rows would be a PDF; Detroit handled above
    out = dict(counted)
    for fips, b in by.items():
        c = counted.get(fips)
        if not c:
            st[fips] = "no civicAPI row"; continue
        tot = sum(v[0] + v[1] for v in b.values()); civ = c[0] + c[1]
        if civ and abs(tot - civ) / civ > TOLERANCE:
            st[fips] = "mismatch %.1f%% (method %d vs civicAPI %d)" % (100 * (tot - civ) / civ, tot, civ); continue
        st[fips] = "ok"
        out[fips] = (c[0], c[1], c[2], {k: (v[0], v[1]) for k, v in b.items()})
    return out

# ---- County adapters -------------------------------------------------------
# COUNTIES[fips] = {"fetch": callable()->rows (adapters.parse_*), "contest": callable(race, contest_name)->bool}
# make_source(race_id_fips_list) builds a SOURCES entry that merges the counties a race touches.
import adapters
COUNTIES = {}
ERRORS = {}   # fips -> last fetch/parse error

def make_source(fips_list):
    def src(race):
        rows = []
        for f in fips_list:
            c = COUNTIES.get(f)
            if not c: continue
            try:
                rows += adapters.rows_for(f, c["fetch"](), lambda name, c=c: c["contest"](race, name))
            except Exception as e:
                ERRORS[f] = str(e)[:100]; continue   # a broken county feed just falls back to inference
            ERRORS.pop(f, None)
        return rows
    return src
