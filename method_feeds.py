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

def attach(counted, race, engine_keys=None):
    src = SOURCES.get(race["id"]); km = KEYMAP.get(race["id"])
    if not src or not km:
        return counted
    try:
        rows = src(race)
    except Exception as e:
        STATUS[race["id"]] = {"_": "source error: %s" % str(e)[:80]}
        return counted
    st = STATUS[race["id"]] = {}
    by = {}
    for r in rows:
        key = km.get(str(r.get("method", "")).strip().lower())
        if key is None or (engine_keys and key not in engine_keys):
            continue
        d = by.setdefault(r["fips"], {}).setdefault(key, [0, 0])
        d[0 if r["party"] == "D" else 1] += r["votes"]
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
