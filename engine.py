# SoCal Strategies 2026 midterm engine.
# Two-party margin is R minus D, in points, everywhere.
# One race at a time: prep(race) once, then project(prep, counted) every cycle.
import json, math, os
import numpy as np

_CENT = None
def _centroids():
    global _CENT
    if _CENT is None:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'centroids.json')
        _CENT = json.load(open(p)) if os.path.exists(p) else {}
    return _CENT


# ---- tuning constants (all margin points unless noted) ----
PRE_SD = {"House": 7.33, "Senate": 6.74, "Governor": 9.0}   # backed out of VoteHub win prob vs margin
TAU_FLOOR = 2.0            # county-to-statewide swing SD
HET_BASE, HET_SCALE, HET_MAX = 2.0, 8.0, 8.0   # within-county heterogeneity: base + scale*sqrt(share), capped
OUTLIER_LAMBDA = 3.0
FULL_TRUST_PCT = 0.25      # turnout recalibration credibility ramp (fraction of precincts)
TURNOUT_CLAMP = (0.40, 2.50)
TURNOUT_NOISE = 0.06       # SD of remaining-vote volume (log), scaled by share still out
# County swing is a correlated field: shared geography + shared partisan profile + county-specific noise.
GEO_SD, GEO_KM = 3.52, 93.0     # regional swing: SD (pts) and length scale (km); fitted by fit_swing.py on 2016-2024 county swings
PROF_SD, PROF_PTS = 0.0, 12.0   # profile kernel did not fit (collapsed to a constant the statewide shift already absorbs), so it is off
IDIO_SD = 2.77                  # county-specific swing, fitted
IDIO_C = 0.97                   # extra variance for small counties: IDIO_C / sqrt(votes/1000)
TAU_BASE = (GEO_SD**2 + PROF_SD**2 + IDIO_SD**2 + IDIO_C) ** 0.5
BUCKET_SD = 3.0            # prior SD of a method's statewide gap vs our baseline (mail, early in-person, Election Day)
OBS_FLOOR = 0.25           # variance floor on observed method-level margins (reporting error)
REGIME_PRIOR = (0.55, 0.35, 0.10)   # early-first, proportional, late-first (from the MI mixture prior)
ORDER = {"em": 0, "mail": 0, "early": 0, "eip": 1, "ed": 2, "lm": 3}
CALL_PROB = 0.995          # a race is only flagged call-ready above this (or below 1 minus this)
METHOD_REGIME_STATE = 0.5   # weight every method county gets on a regime county's counting-order prior (rest is by distance)
METHOD_REGIME_TEMPER = 0.6  # discount: counting order is shared within a state but not identical county to county
METHOD_REGIME_CAP = 5.0     # max log-odds the method counties can move the regime prior
N_SIMS = 4000


def _norm_logpdf(x, mu, var):
    return -0.5 * (np.log(2 * np.pi * var) + (x - mu) ** 2 / var)


def prep(race):
    """Baseline arrays for one race, recentred so the county aggregate equals VoteHub's headline margin."""
    rows = race["counties"]
    fips = [r[0] for r in rows]
    names = [r[1] for r in rows]
    D = np.array([r[2] for r in rows], float)
    R = np.array([r[3] for r in rows], float)
    T = np.maximum(D + R, 1.0)
    marg = (R - D) / T * 100.0
    agg = (R.sum() - D.sum()) / max(T.sum(), 1.0) * 100.0
    b = np.clip(marg + (race["headline"] - agg), -100, 100)
    bks = sorted(race.get("buckets") or [], key=lambda x: ORDER.get(x["key"], 9))
    if bks:
        idx = {x["key"]: j for j, x in enumerate(race["buckets"])}
        sh = np.array([race["shares"][x["key"]] for x in bks], float)
        sh = sh / sh.sum()
        M = np.zeros((len(rows), len(bks)))
        for k, x in enumerate(bks):
            col = [r[4 + idx[x["key"]]] if len(r) > 4 + idx[x["key"]] else None for r in rows]
            M[:, k] = [b[i] if v is None else v for i, v in enumerate(col)]
        M += (b - M @ sh)[:, None]          # reconcile buckets to the county baseline
        M = np.clip(M, -100, 100)
        keys = [x["key"] for x in bks]
    else:
        sh = np.array([1.0]); M = b[:, None].copy(); keys = ["all"]
    share = T / T.sum()
    het = np.minimum(HET_MAX, HET_BASE + HET_SCALE * np.sqrt(share))
    cen = _centroids()
    have = all(f in cen for f in fips)
    if have:
        ll = np.array([cen[f] for f in fips])
        kx = (ll[:, 0] * 111.32 * math.cos(math.radians(ll[:, 1].mean())))
        ky = ll[:, 1] * 110.57
        d2 = (kx[:, None] - kx[None, :]) ** 2 + (ky[:, None] - ky[None, :]) ** 2
        Kg = GEO_SD ** 2 * np.exp(-d2 / (2 * GEO_KM ** 2))
    else:
        Kg = np.zeros((len(fips), len(fips)))
    db = (b[:, None] - b[None, :]) ** 2
    idio = IDIO_SD ** 2 + IDIO_C / np.sqrt(T / 1000.0)
    Kbase = Kg + PROF_SD ** 2 * np.exp(-db / (2 * PROF_PTS ** 2)) + np.diag(idio)
    if not have:
        Kbase = Kbase + GEO_SD ** 2 * np.eye(len(fips))
    return dict(id=race["id"], label=race["label"], type=race["type"], fips=fips, names=names, T=T, b=b,
                sh=sh, M=M, keys=keys, share=share, het=het, K=Kbase, headline=race["headline"],
                pre_sd=PRE_SD.get(race["type"], 7.5))


def _regime_fill(n, V, reverse):
    """Counted votes n (per county) fill the buckets V (counties x K) in reporting order."""
    Vo = V[:, ::-1] if reverse else V
    cum = np.cumsum(Vo, axis=1) - Vo
    fill = np.clip(n[:, None] - cum, 0, Vo)
    return fill[:, ::-1] if reverse else fill


def _wmedian(x, w):
    o = np.argsort(x); x, w = x[o], w[o]
    c = np.cumsum(w)
    return float(x[np.searchsorted(c, c[-1] / 2)])


def project(P, counted, n_sims=N_SIMS, seed=7):
    """counted: {fips: (D, R, pct_reporting)} or {fips: (D, R, pct_reporting, {bucket_key: (D, R)})}.
    A county with the 4th element has observed counts by vote method: the engine reads the buckets directly.
    A county without it falls back to inferring which methods have been counted (early first / proportional / late first)."""
    rng = np.random.default_rng(seed)
    nC = len(P["fips"]); T = P["T"]; K = len(P["keys"]); keys = P["keys"]
    cD = np.zeros(nC); cR = np.zeros(nC); p = np.zeros(nC)
    nk = np.zeros((nC, K)); mk_R = np.zeros((nC, K)); mk_D = np.zeros((nC, K)); has_m = np.zeros(nC, bool)
    for i, f in enumerate(P["fips"]):
        v = counted.get(f)
        if not v: continue
        if len(v) > 3 and v[3] and K > 1:
            for j, key in enumerate(keys):
                if key in v[3]:
                    mk_D[i, j], mk_R[i, j] = v[3][key]
            nk[i] = mk_D[i] + mk_R[i]
            if nk[i].sum() > 0:
                has_m[i] = True
                cD[i], cR[i] = mk_D[i].sum(), mk_R[i].sum()
                p[i] = v[2] if v[2] else 0.0
                continue
        if (v[0] + v[1]) > 0:
            cD[i], cR[i], p[i] = v[0], v[1], v[2]
    n = cD + cR
    rep = n > 0
    p_eff = np.where(rep, np.clip(np.where(p > 0, p, 0.05), 0.02, 1.0), 0.0)
    done = rep & (p_eff >= 0.995)

    # --- turnout recalibration ---
    ratio = np.ones(nC); w = np.minimum(1.0, p_eff / FULL_TRUST_PCT)
    g = 1.0
    if rep.any():
        impl = np.where(rep, n / np.maximum(p_eff, 1e-6), 0.0)
        r_c = np.clip(impl / T, *TURNOUT_CLAMP)
        g = _wmedian(r_c[rep], (T * w)[rep] + 1e-9)
        ratio = np.where(rep, w * r_c + (1 - w) * g, g)
    Tp = np.maximum(T * ratio, n)
    Tp = np.where(done, n, Tp)
    V = Tp[:, None] * P["sh"][None, :]                      # expected votes per bucket
    V = np.where(has_m[:, None], np.maximum(V, nk), V)      # a bucket can't have fewer votes than already counted
    Tp = np.where(has_m, V.sum(1), Tp)
    Tp = np.where(done, n, Tp)
    rem = np.maximum(Tp - n, 0.0)
    Mm = P["M"] / 100.0
    pk = np.clip(nk / np.maximum(V, 1.0), 0.02, 1.0)        # share of each bucket counted (observed counties)
    rem_k = np.where(has_m[:, None], np.where(done[:, None], 0.0, np.maximum(V - nk, 0.0)), 0.0)

    # --- regime-inferred counties: counted ballots drawn in order (early first), proportional, or reversed ---
    fills = [_regime_fill(n, V, False),
             V * np.where(Tp > 0, np.minimum(n / np.maximum(Tp, 1), 1), 0)[:, None],
             _regime_fill(n, V, True)]
    pred = np.stack([100 * (f * Mm).sum(1) / np.maximum(n, 1) for f in fills])
    RM = np.stack([((V - f) * Mm).sum(1) for f in fills])
    pri = np.array([1.0, 0.0, 0.0]) if K == 1 else np.array(REGIME_PRIOR)
    prop_rem = V * (1 - np.where(Tp > 0, np.minimum(n / np.maximum(Tp, 1), 1), 0))[:, None]   # remaining votes if proportional

    # --- mixed coverage: what the method counties reveal about counting order, applied to the counties without method data ---
    # Each method county shows its true early/Election-Day mix at its current % counted. Compare that with what each
    # regime (early-first / proportional / late-first) would have produced; pool across method counties (statewide
    # rules plus nearer-neighbour weight) and use the result as the regime prior for counties that report only totals.
    pri_c = np.tile(pri[:, None], (1, nC)); mcov = 0
    if K > 1 and has_m.any() and (rep & ~has_m).any():
        ok = has_m & (p_eff < 0.97) & (n > 0)
        if ok.any():
            f_obs = nk[:, 0] / np.maximum(n, 1)
            f_r = np.stack([fl[:, 0] / np.maximum(n, 1) for fl in fills])
            sdc = np.sqrt(np.maximum(f_obs * (1 - f_obs), 0.02) / np.maximum(n, 1) + 0.06 ** 2)
            Lj = -0.5 * ((f_obs[None, :] - f_r) / sdc[None, :]) ** 2
            Lj = np.where(ok[None, :], Lj, 0.0)
            Kg_ = P["K"] - np.diag(np.diag(P["K"]))
            prox = np.clip(Kg_ / max(GEO_SD ** 2, 1e-9), 0, 1)
            W = METHOD_REGIME_STATE + (1 - METHOD_REGIME_STATE) * prox
            W[:, ~ok] = 0.0
            pooled = METHOD_REGIME_TEMPER * np.einsum('rj,ij->ri', Lj, W)
            pooled = np.clip(pooled - pooled.max(0), -METHOD_REGIME_CAP, 0)
            pc_ = np.log(np.maximum(pri, 1e-12))[:, None] + pooled
            pc_ -= pc_.max(0); pc_ = np.exp(pc_); pc_ /= pc_.sum(0)
            pri_c = pc_; mcov = int(ok.sum())

    # --- observations ---
    q = P["het"] ** 2
    pcap = np.clip(p_eff, 0.02, 0.995)
    mo = np.where(rep, 100 * (cR - cD) / np.maximum(n, 1), 0.0)
    samp = 1e4 * (1 - (mo / 100) ** 2) / np.maximum(n, 1)
    v_reg = np.where(rep & ~has_m, samp + q * (1 - pcap) / pcap, 1e9)
    y_r = mo[None, :] - pred
    mob = np.where(nk > 0, 100 * (mk_R - mk_D) / np.maximum(nk, 1), 0.0)
    sampb = 1e4 * (1 - (mob / 100) ** 2) / np.maximum(nk, 1)
    pkb = np.minimum(pk, 0.995)
    vb = np.where(nk > 0, np.maximum(sampb + q[:, None] * (1 - pkb) / pkb, OBS_FLOOR), np.inf)
    yb = np.where(nk > 0, mob - P["M"], 0.0)
    ob_mask = nk > 0
    wn = np.where(ob_mask, nk, 0.0)
    y_obs_c = (wn * yb).sum(1) / np.maximum(wn.sum(1), 1)
    v_obs_c = np.where(has_m, (wn ** 2 * np.where(ob_mask, vb, 0.0)).sum(1) / np.maximum(wn.sum(1), 1) ** 2, 1e9)

    Kb = P["K"]
    sd0_sq = max(P["pre_sd"] ** 2 - float(P["share"] @ Kb @ P["share"]), (0.5 * P["pre_sd"]) ** 2)
    nth = 1 + (K if K > 1 else 0)
    prior_prec = np.array([1 / sd0_sq] + ([1 / BUCKET_SD ** 2] * K if K > 1 else []))
    tau = TAU_BASE
    ksc = 1.0
    theta = np.zeros(nth); Vth = np.diag(1 / prior_prec)
    wr = np.tile(pri[:, None], (1, nC)); yc = y_r[0].copy(); vc = v_reg.copy()
    reg_ok = rep & ~has_m
    # observation units: one per regime county, one per (method county, bucket)
    u_c, u_X, u_y, u_v = [], [], [], []
    def build_units(yc_, vc_, damp_):
        cs, Xs, ys, vs = [], [], [], []
        for i in np.nonzero(reg_ok)[0]:
            x = np.zeros(nth); x[0] = 1
            cs.append(i); Xs.append(x); ys.append(yc_[i]); vs.append(vc_[i] / damp_[i])
        for i in np.nonzero(has_m)[0]:
            for j in np.nonzero(ob_mask[i])[0]:
                x = np.zeros(nth); x[0] = 1
                if K > 1: x[1 + j] = 1
                cs.append(i); Xs.append(x); ys.append(yb[i, j]); vs.append(vb[i, j] / damp_[i])
        return np.array(cs, int), np.array(Xs), np.array(ys), np.array(vs)
    damp = np.ones(nC)
    cs = np.zeros(0, int); Xs = np.zeros((0, nth)); ys = np.zeros(0); vs = np.zeros(0)
    for _ in range(4):
        s_mu, s_var = theta[0], Vth[0, 0]
        if not rep.any(): break
        Kc = ksc * Kb
        kdiag = np.diag(Kc)
        # the method counties' learned bucket gaps B explain part of a regime county's margin: remove it under each regime
        if K > 1:
            B_mu = theta[1:]; B_var = np.diag(Vth)[1:]
            fr = [f_ / np.maximum(n, 1)[:, None] for f_ in fills]
            gadj = np.stack([(a_ * B_mu[None, :]).sum(1) for a_ in fr]); gvar = np.stack([(a_ ** 2 * B_var[None, :]).sum(1) for a_ in fr])
        else:
            gadj = np.zeros((3, nC)); gvar = np.zeros((3, nC))
        y_a = y_r - gadj
        ll = np.stack([np.log(np.maximum(pri_c[r], 1e-12)) + _norm_logpdf(y_a[r], s_mu, v_reg + gvar[r] + kdiag + s_var) for r in range(3)])
        ll -= ll.max(0)
        wr = np.exp(ll); wr /= wr.sum(0)
        yc = (wr * y_a).sum(0)
        vc = v_reg + (wr * gvar).sum(0) + (wr * (y_a - yc) ** 2).sum(0)
        yagg = np.where(has_m, y_obs_c, yc); vagg = np.where(has_m, v_obs_c, vc)
        z = np.abs(yagg - s_mu) / np.sqrt(vagg + kdiag)
        damp = np.where(z > OUTLIER_LAMBDA, (OUTLIER_LAMBDA / np.maximum(z, 1e-9)) ** 2, 1.0)
        cs, Xs, ys, vs = build_units(yc, vc, damp)
        Sig = Kc[np.ix_(cs, cs)] + np.diag(vs)
        SiX = np.linalg.solve(Sig, Xs); Siy = np.linalg.solve(Sig, ys)
        A = np.diag(prior_prec).astype(float) + Xs.T @ SiX
        Vth = np.linalg.inv(A); theta = Vth @ (Xs.T @ Siy)
        k_rep = int(rep.sum())
        if k_rep >= 3:   # DerSimonian-Laird heterogeneity across reporting counties (total county swing, regional part included)
            wi = np.where(rep, 1 / np.maximum(vagg, 1e-6), 0)
            ybar = (wi * yagg).sum() / wi.sum()
            Q = (wi * (yagg - ybar) ** 2).sum()
            den = wi.sum() - (wi ** 2).sum() / wi.sum()
            tau_dl = math.sqrt(max(0.0, (Q - (k_rep - 1)) / den)) if den > 0 else 0.0
            ksc = max(1.0, (tau_dl / TAU_BASE) ** 2)
            tau = TAU_BASE * math.sqrt(ksc)
    s_mu, s_var = float(theta[0]), float(Vth[0, 0])
    Kc = ksc * Kb

    # --- Monte Carlo ---
    L = np.linalg.cholesky(Vth + 1e-9 * np.eye(nth))
    TH = theta[None, :] + rng.normal(0, 1, (n_sims, nth)) @ L.T
    S = TH[:, :1]
    B = TH[:, 1:] if K > 1 else np.zeros((n_sims, K))
    # correlated county swing field, conditioned on the observations and on the sampled statewide shift / method gaps
    if len(cs):
        Sig = Kc[np.ix_(cs, cs)] + np.diag(vs)
        KS = Kc[:, cs]
        G = np.linalg.solve(Sig, KS.T).T                       # C x m
        Sig_u = Kc - G @ KS.T
        R_ = ys[None, :] - TH @ Xs.T                          # sims x m
        mu_u = R_ @ G.T                                       # sims x C
    else:
        Sig_u = Kc; mu_u = np.zeros((n_sims, nC))
    Lu = np.linalg.cholesky(Sig_u + 1e-8 * np.eye(nC))
    U = mu_u + rng.normal(0, 1, (n_sims, nC)) @ Lu.T
    Uc_mean = mu_u.mean(0)
    # regime counties: uncounted precincts in a partly counted county are a different mix than the counted ones
    mixv = np.where(reg_ok, q * pcap / (1 - pcap), 0.0)
    Sc = S + U + rng.normal(0, 1, (n_sims, nC)) * np.sqrt(mixv)[None, :]
    u = rng.random((n_sims, nC))
    cw = np.cumsum(wr, axis=0)
    reg = (u > cw[0][None, :]).astype(int) + (u > cw[1][None, :]).astype(int)
    RMs = np.take_along_axis(np.broadcast_to(RM.T[None], (n_sims, nC, 3)), reg[:, :, None], 2)[:, :, 0]
    bterm = (prop_rem[None, :, :] * B[:, None, :]).sum(2) / 100 if K > 1 else 0.0
    f = np.exp(rng.normal(0, TURNOUT_NOISE, (n_sims, nC)) * (1 - p_eff)[None, :])
    f = np.where(done[None, :], 1.0, f)
    reg_part = f * (RMs + rem[None, :] * Sc / 100 + bterm)
    obs_part = 0.0
    if has_m.any():
        mix_sd = np.sqrt(q[:, None] * np.minimum(pk, 0.9) / (1 - np.minimum(pk, 0.9)))
        E_k = rng.normal(0, 1, (n_sims, nC, K)) * mix_sd[None]
        shift_k = S[:, :, None] + B[:, None, :] + U[:, :, None] + E_k
        rem_votes_m = (rem_k[None] * (P["M"][None] + shift_k) / 100).sum(2)
        obs_part = np.where(has_m[None, :], f * rem_votes_m, 0.0)
        reg_part = np.where(has_m[None, :], 0.0, reg_part)
    fm = (cR - cD).sum() + (reg_part + obs_part).sum(1)
    ft = n.sum() + (f * rem[None, :]).sum(1)
    final = 100 * fm / np.maximum(ft, 1)
    if not rep.any():   # nothing counted: center the forecast exactly on the baseline margin (removes Monte Carlo and turnout-weight drift)
        final = final + (P["headline"] - np.median(final))
    win = float(((final > 0).mean() + (final == 0).mean() / 2))
    pc = np.percentile(final, [5, 25, 50, 75, 95])
    exp_total = float(Tp.sum())
    frac = float(n.sum() / exp_total) if exp_total else 0.0
    counties = []
    for i in range(nC):
        pm = None
        if Tp[i] > 0:
            if has_m[i]:
                rm = (rem_k[i] * (P["M"][i] + theta[0] + (theta[1:] if K > 1 else 0) + Uc_mean[i])).sum() / 100
                pm = float(100 * ((cR[i] - cD[i]) + rm) / Tp[i])
            else:
                pm = float(100 * ((cR[i] - cD[i]) + (wr[:, i] * RM[:, i]).sum() + rem[i] * (theta[0] + Uc_mean[i]) / 100) / Tp[i])
        counties.append([P["fips"][i], P["names"][i], round(float(p_eff[i]), 3), int(n[i]), int(rem[i]),
                         None if pm is None else round(pm, 2), bool(has_m[i]), round(float(Uc_mean[i]), 2)])
    state = "complete" if (rep.sum() == nC and done.all()) else ("counting" if rep.any() else "pre")
    n_reg = rep & ~has_m
    return dict(id=P["id"], label=P["label"], state=state, win_prob_R=round(win, 4),
                margin=round(float(pc[2]), 2), p05=round(float(pc[0]), 2), p25=round(float(pc[1]), 2),
                p75=round(float(pc[3]), 2), p95=round(float(pc[4]), 2), reporting=round(frac, 4),
                shift=round(s_mu, 2), shift_sd=round(math.sqrt(s_var), 2), tau=round(tau, 2),
                bucket_gap=({k: round(float(theta[1 + j]), 2) for j, k in enumerate(keys)} if K > 1 else {}),
                counties_with_method_data=int(has_m.sum()),
                turnout_ratio=round(float(g), 3),
                regimes=[round(float(x), 3) for x in (wr[:, n_reg].mean(1) if n_reg.any() else pri)],
                regime_prior_from_method=[round(float(x), 3) for x in (pri_c[:, n_reg].mean(1) if n_reg.any() else pri)],
                method_counties_informing=mcov,
                call_ready=bool(win >= CALL_PROB or win <= 1 - CALL_PROB) and state != "pre",
                counties=counties,
                percentiles=[round(float(x), 2) for x in np.percentile(final, np.linspace(2, 98, 25))])
