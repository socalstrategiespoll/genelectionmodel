# Synthetic election-night replay: draws a "true" night, feeds it to the engine in stages, scores the engine.
import json, sys, math
import numpy as np
import engine as E

def true_night(P, rng, sd_true=None, tau_true=2.5, shift_extra=0.0, beta_sd=None, corr_true=True):
    nC = len(P["fips"]); sd = sd_true if sd_true is not None else P["pre_sd"]
    s = rng.normal(0, math.sqrt(max(sd**2 - float((P["share"]**2).sum())*tau_true**2, 4))) + shift_extra
    beta = rng.normal(0, E.BUCKET_SD if beta_sd is None else beta_sd, len(P["keys"])) if len(P["keys"]) > 1 else np.zeros(1)   # our method baselines are off statewide
    if corr_true:      # county swings share a regional / similar-county component, as the engine's kernel assumes
        Lk = np.linalg.cholesky(P["K"] + 1e-8 * np.eye(nC)); sc = s + Lk @ rng.normal(0, 1, nC)
    else:
        sc = s + rng.normal(0, tau_true, nC)
    Ttrue = P["T"] * math.exp(rng.normal(0, 0.08)) * np.exp(rng.normal(0, 0.05, nC))
    reg = rng.choice(3, nC, p=np.array(E.REGIME_PRIOR)) if len(P["keys"]) > 1 else np.zeros(nC, int)
    t0 = rng.uniform(0, 0.55, nC); dur = rng.uniform(0.15, 0.45, nC)
    Mt = np.clip(P["M"] + sc[:, None] + beta[None, :], -100, 100)
    V = Ttrue[:, None] * P["sh"][None, :]
    fm_final = (V * Mt / 100).sum()
    final = 100 * fm_final / Ttrue.sum()
    NP = 50                                   # equal-size precincts per county, random reporting order
    sig = P["het"] * math.sqrt(NP)
    off = rng.normal(0, 1, (nC, NP)) * sig[:, None]
    off -= off.mean(1, keepdims=True)
    return dict(sc=sc, Ttrue=Ttrue, reg=reg, t0=t0, dur=dur, Mt=Mt, V=V, final=final, off=off, NP=NP, s=s)

def snapshot(P, N, t, methods=False):
    p = np.clip((t - N["t0"]) / N["dur"], 0, 1)
    p = np.where(p > 0.97, 1.0, np.floor(p * N["NP"]) / N["NP"])
    K = len(P["keys"]); jp = P["keys"].index("ed") if "ed" in P["keys"] else 0   # the precinct-counted bucket carries the mix noise
    out = {}
    for i, f in enumerate(P["fips"]):
        if p[i] <= 0: continue
        V = N["V"][i:i+1]; n = np.array([p[i] * N["Ttrue"][i]])
        r = N["reg"][i]
        fill = V * p[i] if r == 1 else E._regime_fill(n, V, r == 2)
        fk = fill[0]
        mk = fk * N["Mt"][i] / 100                                   # counted margin votes per bucket
        pk = fk[jp] / max(N["V"][i][jp], 1e-9)
        k = int(round(pk * N["NP"])) if 0 < pk < 1 else 0
        if k > 0:
            mk[jp] += N["off"][i, :k].mean() * fk[jp] / 100          # counted precincts are not a random-mix sample
        mv = mk.sum(); nn = fk.sum()
        Rv = (nn + mv) / 2; Dv = (nn - mv) / 2
        if methods and K > 1:
            out[f] = (max(Dv, 0), max(Rv, 0), float(p[i]),
                      {key: (max((fk[j] - mk[j]) / 2, 0), max((fk[j] + mk[j]) / 2, 0)) for j, key in enumerate(P["keys"])})
        else:
            out[f] = (max(Dv, 0), max(Rv, 0), float(p[i]))
    return out

def run(races, trials, stages=(0.25, 0.5, 0.75, 1.0), methods=False, **kw):
    rng = np.random.default_rng(11)
    rows = {t: [] for t in stages}
    for r in races:
        P = E.prep(r)
        for k in range(trials):
            N = true_night(P, rng, **kw)
            for t in stages:
                o = E.project(P, snapshot(P, N, t, methods), n_sims=1500, seed=k)
                rows[t].append((o["p05"] <= N["final"] <= o["p95"], o["p25"] <= N["final"] <= o["p75"],
                                abs(o["margin"] - N["final"]), (o["win_prob_R"] - (N["final"] > 0)) ** 2,
                                o["win_prob_R"], N["final"] > 0, o["reporting"]))
    return rows

if __name__ == "__main__":
    d = json.load(open("data.json"))
    pick = sys.argv[1].split(",") if len(sys.argv) > 1 else ["NC-Sen", "GA-Gov", "AZ-06", "AL-02", "PA-07", "MI-Sen", "TX-35", "OH-10"]
    races = [r for r in d["races"] if r["label"] in pick]
    kw = {}
    if len(sys.argv) > 2: kw["shift_extra"] = float(sys.argv[2])
    if len(sys.argv) > 4: kw["corr_true"] = sys.argv[4] == "corr"
    for meth in (False, True):
        rows = run(races, int(sys.argv[3]) if len(sys.argv) > 3 else 25, methods=meth, **kw)
        print("races:", [r["label"] for r in races], "| method data:", meth)
        print("stage  rpt   cov90  cov50  MAE   Brier")
        for t, v in rows.items():
            a = np.array([[x[0], x[1], x[2], x[3], x[6]] for x in v], float).mean(0)
            print(f"{t:4.2f}  {a[4]:.2f}  {a[0]:.2f}   {a[1]:.2f}   {a[2]:.2f}  {a[3]:.3f}")
