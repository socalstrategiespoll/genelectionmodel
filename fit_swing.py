# Fit the county-swing covariance (regional + profile + county noise) to presidential county swings 2016->2020 and 2020->2024.
# Run once offline; paste the printed constants into engine.py. Needs scipy and the three tonmcg county CSVs.
import csv, json, math, sys
import numpy as np
from scipy.optimize import minimize
D = sys.argv[1] if len(sys.argv) > 1 else "."
cent = json.load(open("centroids.json"))
def load(y):
    out = {}
    with open(f"{D}/{y}_US_County_Level_Presidential_Results.csv", newline="") as f:
        for r in csv.DictReader(f):
            fp = (r.get("county_fips") or r.get("combined_fips") or "").zfill(5)
            try: d = float(r["votes_dem"]); g = float(r["votes_gop"])
            except: continue
            if d + g > 0: out[fp] = (d, g)
    return out
Y = {y: load(y) for y in (2016, 2020, 2024)}
STATES = ["AL","AZ","CO","FL","GA","IA","KS","KY","ME","MI","MN","MO","MT","NC","NE","NJ","NV","NY","OH","OR","PA","SC","TX","VA","VT","WA","WI"]
FIPS = {"AL":"01","AZ":"04","CO":"08","FL":"12","GA":"13","IA":"19","KS":"20","KY":"21","ME":"23","MI":"26","MN":"27","MO":"29","MT":"30","NC":"37","NE":"31","NJ":"34","NV":"32","NY":"36","OH":"39","OR":"41","PA":"42","SC":"45","TX":"48","VA":"51","VT":"50","WA":"53","WI":"55"}
def marg(y, f): d, g = Y[y][f]; return 100 * (g - d) / (g + d)
blocks = []
for st in STATES:
    for a, b in ((2016, 2020), (2020, 2024)):
        fs = [f for f in Y[a] if f.startswith(FIPS[st]) and f in Y[b] and f in cent]
        if len(fs) < 8: continue
        xy = np.array([cent[f] for f in fs]); kx = xy[:, 0] * 111.32 * math.cos(math.radians(xy[:, 1].mean())); ky = xy[:, 1] * 110.57
        d2 = (kx[:, None] - kx[None]) ** 2 + (ky[:, None] - ky[None]) ** 2
        m0 = np.array([marg(a, f) for f in fs]); sw = np.array([marg(b, f) - marg(a, f) for f in fs])
        n = np.array([sum(Y[b][f]) for f in fs])
        blocks.append((st, a, d2, (m0[:, None] - m0[None]) ** 2, sw, n))
def nll(th):
    g, l, p, w, i, c = np.exp(th)
    tot = 0.0
    for st, a, d2, db, sw, n in blocks:
        K = g**2 * np.exp(-d2 / (2 * l**2)) + p**2 * np.exp(-db / (2 * w**2)) + np.diag(i**2 + c / np.sqrt(n / 1000.0))
        C = K + 1e4 * np.ones_like(K)          # free statewide shift
        s, ld = np.linalg.slogdet(C)
        tot += 0.5 * (ld + sw @ np.linalg.solve(C, sw))
    return tot
x0 = np.log([1.2, 120, 1.2, 12, 1.2, 1.0])
res = minimize(nll, x0, method="Nelder-Mead", options={"maxiter": 600, "xatol": 1e-2, "fatol": 1e-2})
g, l, p, w, i, c = np.exp(res.x)
print("blocks", len(blocks), "nll", round(res.fun, 1), "start", round(nll(x0), 1))
print(f"GEO_SD, GEO_KM = {g:.2f}, {l:.0f}\nPROF_SD, PROF_PTS = {p:.2f}, {w:.1f}\nIDIO_SD = {i:.2f}   small-county term c = {c:.2f} (var += c/sqrt(votes/1000))")
