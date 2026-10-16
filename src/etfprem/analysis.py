"""溢价率与后续价格走势的关系。需要可选依赖：uv sync --extra analysis。

核心恒等式（对数）：价格收益 = 净值收益 + 溢价变化
    p[t+h] − p[t] = (n[t+h] − n[t]) + (z[t+h] − z[t]),   z = ln(1 + 溢价率)

结构模型（状态空间，卡尔曼滤波 + 极大似然）：
    z[t]  = m[t] + x[t] + u[t]           溢价 = 体制锚 + 错误定价 + 信息领先
    m[t]  = m[t−1] + η[t]                锚：随机游走（申购额度、暂停申购等体制）
    x[t]  = φ·x[t−1] + ε[t]              错误定价：AR(1)，会自行衰减
    u[t]  ~ iid                          场内价格已反映、参考净值次日才反映的信息
    Δn[t] = μ + λ·u[t−1] + v[t]          当日净值收益兑现前一日的信息领先
推论：E_t[价格 h 日收益] = h·μ + (φ^h − 1)·x[t] + (λ − 1)·u[t]
"""

from __future__ import annotations

import math
import warnings
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
import statsmodels.api as sm
from statsmodels.tsa.statespace.mlemodel import MLEModel
from statsmodels.tsa.stattools import adfuller

from . import store
from .config import DATA, FundConfig

HORIZONS = (1, 5, 10, 20, 60)
PREDICTORS = {"z": "溢价水平", "d20": "偏离 20 日均值", "dz1": "当日溢价变化"}
TARGETS = {"rP": "价格", "rN": "净值", "dZ": "溢价变化"}
HIGH_REGIME = 0.03  # 过去 60 日平均溢价超过 3% 视为套利受限的高溢价体制

warnings.filterwarnings("ignore", module="statsmodels")


# ------------------------------------------------------------------ 数据面板

def build_panel(fund: FundConfig) -> pd.DataFrame:
    """逐个场内交易日：收盘价、参考净值（前一 A 股交易日官方净值）、对数溢价及各期限前瞻收益。

    剔除：停牌日（成交量为 0，收盘价不是成交价）、长假后首个交易日（参考净值之后美股又交易过，溢价失真）。
    """
    md = store.load(fund)
    us = sorted(store._read_series(DATA / "index" / "NDXTMC.json"))  # 美股交易日历
    if not us:
        raise RuntimeError("缺少美股交易日历，请先运行 etfprem model --update")
    cn = sorted(set(md.nav) | set(md.etf))
    rows = []
    for t in sorted(md.etf):
        i = bisect_left(cn, t)
        if i == 0:
            continue
        D = cn[i - 1]
        bar = md.etf[t]
        gap = bisect_right(us, t - timedelta(days=1)) - bisect_right(us, D) > 0
        rows.append({"date": t, "D": D, "P": bar["close"], "vol": bar.get("volume", np.nan),
                     "nav": md.nav.get(D), "gap": gap})
    df = pd.DataFrame(rows).set_index("date")
    df["valid"] = df["nav"].notna() & (df["vol"] > 0) & ~df["gap"]
    df["p"] = np.where(df["valid"], np.log(df["P"]), np.nan)
    df["n"] = np.where(df["valid"], np.log(df["nav"].astype(float)), np.nan)
    df["z"] = df["p"] - df["n"]
    df["prem"] = np.expm1(df["z"])
    df["d20"] = df["z"] - df["z"].shift(1).rolling(20, min_periods=15).mean()
    df["dz1"] = df["z"] - df["z"].shift(1)
    for h in HORIZONS:
        df[f"rP{h}"] = df["p"].shift(-h) - df["p"]
        df[f"rN{h}"] = df["n"].shift(-h) - df["n"]
        df[f"dZ{h}"] = df["z"].shift(-h) - df["z"]
    avg60 = np.expm1(df["z"].shift(1).rolling(60, min_periods=40).mean())
    df["regime_hi"] = avg60 > HIGH_REGIME
    return df


# ------------------------------------------------------------------ 回归工具

def ols_hac(df: pd.DataFrame, y: str, xs: list[str], lags: int) -> dict | None:
    d = df[[y, *xs]].dropna()
    if len(d) < 40:
        return None
    m = sm.OLS(d[y], sm.add_constant(d[xs])).fit(cov_type="HAC", cov_kwds={"maxlags": max(1, lags)})
    return {"b": float(m.params[xs[0]]), "t": float(m.tvalues[xs[0]]), "r2": float(m.rsquared), "n": int(len(d)),
            "params": {k: float(v) for k, v in m.params.items()}, "tvalues": {k: float(v) for k, v in m.tvalues.items()}}


def block_bootstrap(df: pd.DataFrame, y: str, x: str, B: int = 2000, L: int = 20, seed: int = 7) -> tuple[float, float]:
    """移动区块自助法的斜率 95% 区间，保留重叠收益与溢价的序列相关。"""
    d = df[[y, x]].dropna()
    X, Y = d[x].to_numpy(), d[y].to_numpy()
    n = len(d)
    rng = np.random.default_rng(seed)
    out = np.empty(B)
    for k in range(B):
        starts = rng.integers(0, n - L + 1, size=math.ceil(n / L))
        idx = (starts[:, None] + np.arange(L)).ravel()[:n]
        xx, yy = X[idx], Y[idx]
        xm = xx - xx.mean()
        out[k] = (xm * (yy - yy.mean())).sum() / (xm * xm).sum()
    lo, hi = np.percentile(out, [2.5, 97.5])
    return float(lo), float(hi)


def nonoverlap(df: pd.DataFrame, y: str, x: str, h: int) -> dict | None:
    d = df[[y, x]].dropna().iloc[::h]
    if len(d) < 15:
        return None
    m = sm.OLS(d[y], sm.add_constant(d[x])).fit()
    return {"b": float(m.params[x]), "t": float(m.tvalues[x]), "n": int(len(d))}


# ------------------------------------------------------------------ 溢价本身的动态

def premium_dynamics(df: pd.DataFrame) -> dict:
    zz = pd.DataFrame({"z": df["z"], "z1": df["z"].shift(1)}).dropna()
    m = sm.OLS(zz["z"], sm.add_constant(zz["z1"])).fit()
    rho = float(m.params["z1"])
    out = {"rho": rho, "half_life": math.log(0.5) / math.log(rho) if 0 < rho < 1 else math.inf,
           "adf_p": float(adfuller(df["z"].dropna(), regression="c", autolag="AIC", result_object=False)[1])}
    for name, mask in (("low", ~df["regime_hi"]), ("high", df["regime_hi"])):
        sub = zz.loc[zz.index.isin(df.index[mask])]
        if len(sub) > 30:
            r = float(sm.OLS(sub["z"], sm.add_constant(sub["z1"])).fit().params["z1"])
            out[f"rho_{name}"] = r
            out[f"half_life_{name}"] = math.log(0.5) / math.log(r) if 0 < r < 1 else math.inf
            out[f"n_{name}"] = int(len(sub))
    return out


# ------------------------------------------------------------------ 结构模型

class PremiumStateSpace(MLEModel):
    """观测 [z_t, Δn_t]；状态 [m_t, x_t, u_t, u_{t−1}]。t 期只用到 t 期及以前可见的数据。"""

    PARAMS = ("sigma_anchor", "sigma_mispricing", "sigma_info", "sigma_nav", "phi", "lam", "mu")

    def __init__(self, endog):
        super().__init__(endog, k_states=4, k_posdef=3, initialization="approximate_diffuse")
        self["transition"] = np.array([[1.0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 1.0, 0]])
        self["selection"] = np.array([[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0], [0, 0, 0]])
        self["design"] = np.array([[1.0, 1.0, 1.0, 0], [0, 0, 0, 0]])
        self["obs_cov"] = np.diag([1e-10, 1.0])
        self["obs_intercept"] = np.zeros((2, 1))

    @property
    def param_names(self):
        return list(self.PARAMS)

    @property
    def start_params(self):
        return np.array([0.007, 0.008, 0.008, 0.013, 0.4, 1.0, 0.001])

    def transform_params(self, u):
        p = np.array(u, dtype=float)
        p[:4] = np.abs(p[:4]) + 1e-8
        p[4] = np.tanh(u[4])
        return p

    def untransform_params(self, p):
        u = np.array(p, dtype=float)
        u[4] = np.arctanh(np.clip(p[4], -0.999, 0.999))
        return u

    def update(self, params, **kw):
        params = super().update(params, **kw)
        s_eta, s_eps, s_u, s_v, phi, lam, mu = params
        self["state_cov"] = np.diag([s_eta**2, s_eps**2, s_u**2])
        self["transition", 1, 1] = phi
        self["design", 1, 3] = lam
        self["obs_cov", 1, 1] = s_v**2
        self["obs_intercept", 1, 0] = mu


def _endog(df: pd.DataFrame) -> np.ndarray:
    return np.column_stack([df["z"].to_numpy(), (df["n"] - df["n"].shift(1)).to_numpy()])


def _fit(endog: np.ndarray, start=None):
    mod = PremiumStateSpace(endog)
    res = mod.fit(start if start is not None else mod.start_params, disp=False, maxiter=3000, method="lbfgs")
    res = mod.fit(res.params, disp=False, maxiter=3000, method="nm")
    res = mod.fit(res.params, disp=False, maxiter=3000, method="lbfgs")
    return mod, res


def model_forecast(states: np.ndarray, params, h: int) -> dict[str, np.ndarray]:
    """由滤波状态给出 h 日预期中与溢价相关的部分：价格、净值、溢价变化。

    漂移项 h·μ 对所有日子相同，检验时交给回归常数项；否则滚动重估的 μ 会把收益率的块间波动混进来。
    """
    _, _, _, _, phi, lam, _ = params
    x, u = states[1], states[2]
    return {"rP": (phi**h - 1) * x + (lam - 1) * u, "rN": lam * u, "dZ": (phi**h - 1) * x - u}


def structural(df: pd.DataFrame, oos_start: int = 300, oos_step: int = 60) -> dict:
    endog = _endog(df)
    mod, res = _fit(endog)
    p = dict(zip(mod.param_names, map(float, res.params)))
    se = dict(zip(mod.param_names, map(float, res.bse)))
    phi = p["phi"]
    var_x = p["sigma_mispricing"] ** 2 / (1 - phi**2)
    dz_var = {"锚": p["sigma_anchor"] ** 2, "错误定价": 2 * p["sigma_mispricing"] ** 2 / (1 + phi),
              "信息领先": 2 * p["sigma_info"] ** 2}
    tot = sum(dz_var.values())
    st = res.filtered_state
    states = pd.DataFrame({"anchor": st[0], "mispricing": st[1], "info": st[2]}, index=df.index)

    def score(F: dict[int, dict[str, np.ndarray]]) -> dict:
        out = {}
        for h in HORIZONS:
            for k in TARGETS:
                d = pd.DataFrame({"y": df[f"{k}{h}"].to_numpy(), "f": F[h][k]}).dropna()
                if len(d) < 40 or d["f"].std() == 0:
                    continue
                m = sm.OLS(d["y"], sm.add_constant(d["f"])).fit(cov_type="HAC", cov_kwds={"maxlags": h})
                out[f"{k}{h}"] = {"slope": float(m.params["f"]), "t": float(m.tvalues["f"]), "r2": float(m.rsquared),
                                  "n": int(len(d))}
        return out

    in_sample = score({h: model_forecast(st, res.params, h) for h in HORIZONS})

    # 严格样本外：每 oos_step 天用截至当时的数据重估参数，对之后 oos_step 天做实时滤波预测
    n = len(df)
    Fo = {h: {k: np.full(n, np.nan) for k in TARGETS} for h in HORIZONS}
    for cut in range(oos_start, n, oos_step):
        past = endog.copy()
        past[cut:] = np.nan
        _, r_cut = _fit(past, res.params)
        filt = PremiumStateSpace(endog).filter(r_cut.params)   # 滤波只用到 t 及以前的观测
        for h in HORIZONS:
            fc = model_forecast(filt.filtered_state, r_cut.params, h)
            for k in TARGETS:
                Fo[h][k][cut:cut + oos_step] = fc[k][cut:cut + oos_step]
    oos = score(Fo)

    # 价格可预测性的理论上限：假设能完全看清错误定价与信息领先成分
    ceiling = {}
    for h in HORIZONS:
        explained = (phi**h - 1) ** 2 * var_x + (p["lam"] - 1) ** 2 * p["sigma_info"] ** 2
        total = float(df[f"rP{h}"].var())
        ceiling[h] = explained / total if total > 0 else float("nan")

    return {
        "params": p, "se": se, "loglik": float(res.llf),
        "half_life": math.log(0.5) / math.log(phi) if 0 < phi < 1 else math.inf,
        "sd_mispricing": math.sqrt(var_x),
        "dz_share": {k: v / tot for k, v in dz_var.items()},
        "states": states, "in_sample": in_sample, "oos": oos, "ceiling": ceiling,
        "oos_from": df.index[oos_start] if n > oos_start else None,
    }


# ------------------------------------------------------------------ 汇总

@dataclass
class FundAnalysis:
    fund: FundConfig
    panel: pd.DataFrame
    dynamics: dict
    regressions: dict  # (predictor, target, h) -> 结果
    robust: dict
    regimes: dict
    quintiles: list[dict]
    variance: dict
    nav_day1: dict
    structural: dict
    notes: list[str] = field(default_factory=list)


def quintile_table(df: pd.DataFrame, x: str = "d20", h: int = 5) -> list[dict]:
    d = df[[x, f"rP{h}", f"rN{h}", f"dZ{h}"]].dropna()
    d["q"] = pd.qcut(d[x], 5, labels=False)
    out = []
    for q, g in d.groupby("q"):
        out.append({"q": int(q) + 1, "n": int(len(g)), "x": float(g[x].mean()),
                    **{k: float(g[f"{k}{h}"].mean()) for k in TARGETS},
                    **{f"{k}_se": float(g[f"{k}{h}"].std() / math.sqrt(len(g) / max(1, h))) for k in TARGETS}})
    return out


def variance_split(df: pd.DataFrame) -> dict:
    out = {}
    for name, mask in (("low", ~df["regime_hi"]), ("high", df["regime_hi"])):
        d = pd.DataFrame({"rP": df["p"].diff(), "rN": df["n"].diff(), "dZ": df["z"].diff()})[mask].dropna()
        if len(d) < 30:
            continue
        vP, vN, vZ = d["rP"].var(), d["rN"].var(), d["dZ"].var()
        c = d[["rN", "dZ"]].cov().iloc[0, 1]
        out[name] = {"n": int(len(d)), "sd_price": math.sqrt(vP), "sd_nav": math.sqrt(vN), "sd_premium": math.sqrt(vZ),
                     "corr": c / math.sqrt(vN * vZ), "premium_share": (vZ + 2 * c) / vP}
    return out


def analyze(fund: FundConfig) -> FundAnalysis:
    df = build_panel(fund)
    regs = {}
    for x in PREDICTORS:
        for k in TARGETS:
            for h in HORIZONS:
                regs[(x, k, h)] = ols_hac(df, f"{k}{h}", [x], h)
    robust = {}
    for x, h in (("z", 5), ("z", 20), ("d20", 5), ("d20", 20), ("dz1", 5), ("dz1", 10)):
        robust[(x, h)] = {"ci": block_bootstrap(df, f"rP{h}", x), "nonoverlap": nonoverlap(df, f"rP{h}", x, h)}
    regimes = {}
    for name, mask in (("low", ~df["regime_hi"]), ("high", df["regime_hi"])):
        sub = df[mask]
        for x in ("z", "d20"):
            for k in ("rP", "dZ"):
                for h in (5, 20):
                    regimes[(name, x, k, h)] = ols_hac(sub, f"{k}{h}", [x], h)
        regimes[(name, "n")] = int(mask.sum())
    day1 = {}
    for x in ("d20", "dz1"):
        day1[(x, 1)] = ols_hac(df, "rN1", [x], 1)
        for h in (5, 10, 20):
            later = df.assign(later=df["n"].shift(-h) - df["n"].shift(-1))
            day1[(x, h)] = ols_hac(later, "later", [x], h)
    mom = df.assign(mom20=df["n"] - df["n"].shift(20))
    day1["momentum"] = {h: ols_hac(mom, f"rN{h}", ["d20", "mom20"], h) for h in (1, 5, 10, 20)}
    return FundAnalysis(
        fund=fund, panel=df, dynamics=premium_dynamics(df), regressions=regs, robust=robust, regimes=regimes,
        quintiles=quintile_table(df), variance=variance_split(df), nav_day1=day1, structural=structural(df),
    )
