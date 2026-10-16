"""净值估算模型（成分篓子法，指数法作对照）。

记号：净值日 D 为 A 股交易日，Dp 为其前一个净值日，价格日 T 为 D 的下一个 A 股交易日。
T 日开盘前已知：PCF(T)（持仓股数 + 官方净值 NAV(Dp)）、美股 D 日收盘价、中间价 FX(D)。

    q      = PCF(T) 股数，按拆股换算到复权口径
    r      = Σ q·(p(s1) + 税后分红) / Σ q·p(s0) − 1              # 美元篓子收益，s0/s1 为 Dp/D 对应的美股收盘日
    E      = 过去 N 个净值日“去汇率、去费用的净值收益”对 r 的过原点回归斜率（真实股票敞口）
    estNAV = NAV(Dp) · (1 + E·r) · FX(D)/FX(Dp) − NAV(Dp) · 年费率 · 自然日/365

E 只用 Dp 及更早的官方净值估计，因此整个流程在 T 日开盘前即可完成，没有用到未来数据。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

from bisect import bisect_right

from .config import LOCAL_MARKETS as CN_MARKETS
from .store import MarketData

LOCAL_MARKETS = ("深圳市场", "上海市场")


@dataclass(frozen=True)
class Params:
    window: int = 20  # 敞口回归窗口（净值日个数）
    min_obs: int = 10  # 少于该观测数时敞口取 1
    max_lookback: int = 60  # 向前最多找多少个 A 股交易日
    annual_fee: float = 0.01
    dividend_wht: float = 0.10
    model: str = "single"  # 定期报告基金：single 单一敞口 / split 海外与 A 股分别回归 / fixed 直接用报告比例


@dataclass(frozen=True)
class Step:
    """净值日 D 相对 Dp 的市场变动。"""

    D: date
    Dp: date
    holdings_date: date  # PCF 日期，或定期报告的报告期末
    s0: date
    s1: date
    fx0: float
    fx1: float
    r_basket: float
    r_index: float | None
    weight: float | None  # 篓子市值 / NAV(Dp)
    n_holdings: int
    parts: dict[str, float] | None = None  # 定期报告基金：各部分对总收益的贡献（foreign / cn）


@dataclass(frozen=True)
class NavEstimate:
    D: date
    method: str
    nav_prev: float
    r: float
    exposure: float
    n_obs: int
    fee: float
    nav: float
    step: Step
    betas: dict[str, float] | None = None  # split 模型的分部分敞口


def snap_split(ratio: float) -> float:
    """把“清单隐含原始价 / 复权价”吸附到常见拆并股比例。"""
    if abs(math.log(ratio)) < 0.03:
        return 1.0
    f = Fraction(ratio).limit_denominator(20)
    return float(f) if abs(float(f) / ratio - 1) < 0.01 else ratio


class PcfEstimator:
    """每日申赎清单（深交所 ETF）。"""

    def __init__(self, md: MarketData, params: Params | None = None):
        self.md = md
        self.p = params or Params(annual_fee=md.fund.annual_fee, dividend_wht=md.fund.dividend_wht)
        self.cal = md.calendar
        self._hold: dict[date, dict[str, float]] = {}
        self._steps: dict[date, Step | None] = {}
        self.notes: list[str] = []

    # ------------------------------------------------------------ 持仓与价格

    def pcf_for(self, T: date) -> date | None:
        """价格日 T 应使用的清单；当天清单缺失时退回最近一份。"""
        if T in self.md.pcf:
            return T
        earlier = [d for d in self.md.pcf if d < T]
        if earlier:
            self.notes.append(f"{T} 无当日 PCF，改用 {max(earlier)}")
            return max(earlier)
        return None

    def holdings(self, T: date) -> dict[str, float]:
        """PCF(T) 的复权股数。拆股当天清单把股数写成 0，此时沿用前一份清单。"""
        if T in self._hold:
            return self._hold[T]
        pcf = self.md.pcf[T]
        P = self.cal.prev_cn(T)
        priced_us = self.cal.us_on_or_before(P - timedelta(days=1)) if P else None
        fx = self.md.fx.get(P) if P else None
        out: dict[str, float] = {}
        for code, h in pcf.holdings.items():
            if h.market in LOCAL_MARKETS:
                continue
            if h.qty == 0:
                prev = self._previous_qty(code, T)
                if prev:
                    out[code] = prev
                continue
            k = 1.0
            series = self.md.px.get(code, {})
            if priced_us in series and fx:
                implied = h.sub_amount / (h.qty * (1 + h.sub_margin) * fx)
                k = snap_split(implied / series[priced_us])
                if k != 1.0 and Fraction(k).limit_denominator(20) != Fraction(k):
                    self.notes.append(f"{T} {code} 清单价格与行情不符，比例 {k:.4f}")
            out[code] = h.qty * k
        self._hold[T] = out
        return out

    def _previous_qty(self, code: str, T: date) -> float | None:
        d = self.cal.prev_cn(T)
        for _ in range(5):
            if d is None:
                return None
            h = self.md.pcf.get(d, None)
            if h and code in h.holdings and h.holdings[code].qty > 0:
                return self.holdings(d)[code]
            d = self.cal.prev_cn(d)
        return None

    def price(self, code: str, s: date) -> float | None:
        series = self.md.px.get(code)
        if not series:
            return None
        if s in series:
            return series[s]
        prior = [d for d in series if d <= s]
        return series[max(prior)] if prior else None

    def basket_return(self, q: dict[str, float], s0: date, s1: date) -> tuple[float, float]:
        """返回（美元篓子收益，s0 时的篓子美元市值）。"""
        v0 = v1 = 0.0
        for code, n in q.items():
            p0, p1 = self.price(code, s0), self.price(code, s1)
            if p0 is None or p1 is None:
                continue
            div = sum(a for d, a in self.md.divs.get(code, {}).items() if s0 < d <= s1)
            v0 += n * p0
            v1 += n * (p1 + div * (1 - self.p.dividend_wht))
        return (v1 / v0 - 1 if v0 else 0.0), v0

    # ------------------------------------------------------------ 单日变动

    def step(self, D: date) -> Step | None:
        if D in self._steps:
            return self._steps[D]
        result = None
        Dp, T = self.cal.prev_cn(D), self.cal.next_cn(D)
        pcf_date = self.pcf_for(T) if T else None
        fx = self.md.fx
        if Dp and pcf_date and D in fx and Dp in fx and pcf_date > Dp:
            s0, s1 = self.cal.us_on_or_before(Dp), self.cal.us_on_or_before(D)
            if s0 and s1:
                q = self.holdings(pcf_date)
                r, v0 = self.basket_return(q, s0, s1)
                idx = self.md.index
                r_idx = idx[s1] / idx[s0] - 1 if s0 in idx and s1 in idx else None
                nav_prev = self.md.nav.get(Dp)
                unit = self.md.pcf[pcf_date].unit_size
                weight = v0 * fx[Dp] / unit / nav_prev if nav_prev else None
                result = Step(D, Dp, pcf_date, s0, s1, fx[Dp], fx[D], r, r_idx, weight, len(q))
        self._steps[D] = result
        return result

    # ------------------------------------------------------------ 敞口与估值

    def exposure(self, D: date, method: str = "basket") -> tuple[float, int]:
        """用 D 之前（含 Dp）的官方净值回归真实敞口。"""
        nav = self.md.nav
        xs: list[float] = []
        ys: list[float] = []
        k = self.cal.prev_cn(D)
        for _ in range(self.p.max_lookback):
            if k is None or len(xs) >= self.p.window:
                break
            st = self.step(k)
            if st and k in nav and st.Dp in nav:
                r = st.r_basket if method == "basket" else st.r_index
                if r is not None and abs(r) > 1e-6:
                    nav0, nav1 = nav[st.Dp], nav[k]
                    days = (st.D - st.Dp).days
                    y = (nav1 + nav0 * self.p.annual_fee * days / 365) / nav0 / (st.fx1 / st.fx0) - 1
                    xs.append(r)
                    ys.append(y)
            k = self.cal.prev_cn(k)
        if len(xs) < self.p.min_obs:
            return 1.0, len(xs)
        return sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs), len(xs)

    def estimate(self, D: date, method: str = "basket", nav_prev: float | None = None) -> NavEstimate | None:
        st = self.step(D)
        if st is None:
            return None
        nav_prev = nav_prev if nav_prev is not None else self.md.nav.get(st.Dp)
        r = st.r_basket if method == "basket" else st.r_index
        if nav_prev is None or r is None:
            return None
        E, n = self.exposure(D, method)
        fee = nav_prev * self.p.annual_fee * (st.D - st.Dp).days / 365
        nav = nav_prev * (1 + E * r) * st.fx1 / st.fx0 - fee
        return NavEstimate(D, method, nav_prev, r, E, n, fee, nav, st)

    def fair_value(self, T: date, est: NavEstimate) -> tuple[float, int]:
        """T 日开盘时的公允价值：补上 D 之后、T 之前的美股交易日（长假后），汇率换成 FX(T)。"""
        st = est.step
        latest = self.cal.us_on_or_before(T - timedelta(days=1))
        gap = self.cal.us_between(st.s1, latest) if latest else []
        r_gap = 0.0
        if gap:
            r_gap, _ = self.basket_return(self.holdings(st.holdings_date), st.s1, latest)
        fx_t = self.md.fx.get(T, st.fx1)
        return est.nav * (1 + est.exposure * r_gap) * fx_t / st.fx1, len(gap)


Estimator = PcfEstimator  # 兼容旧名


def _solve2(a11: float, a12: float, a22: float, b1: float, b2: float) -> tuple[float, float] | None:
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-18 * max(1.0, a11 * a22):
        return None
    return (b1 * a22 - b2 * a12) / det, (a11 * b2 - a12 * b1) / det


class ReportEstimator:
    """定期报告持仓（QDII-LOF 等没有每日申赎清单的基金）。

    篓子取价格日 T 之前已发布的最新一份报告，按报告里各持仓占净值的比例固定权重，每天再平衡。
    501225 的实测表明基金经理会频繁把权重拉回目标，固定权重比“份额不变、权重漂移”误差小三成。
    每个持仓用本市场收盘价和本币中间价折成人民币收益，A 股持仓不受汇率影响。
    海外与 A 股两部分的真实敞口分别用过去的官方净值滚动回归（model="split"）。
    """

    PARTS = ("foreign", "cn")

    def __init__(self, md: MarketData, params: Params | None = None):
        self.md = md
        self.p = params or Params(annual_fee=md.fund.annual_fee, dividend_wht=md.fund.dividend_wht, model="split")
        self.cal = md.calendar
        self.notes: list[str] = []
        self.assets = {a.code: a for a in md.fund.assets}
        self._keys = {c: sorted(s) for c, s in md.px.items()}
        self._fx_keys = {c: sorted(s) for c, s in md.fx_all.items()}
        self._steps: dict[date, Step | None] = {}

    # ------------------------------------------------------------ 基础取值

    @staticmethod
    def _on_or_before(keys: list[date], series: dict, d: date):
        i = bisect_right(keys, d)
        return series[keys[i - 1]] if i else None

    def price(self, code: str, d: date) -> float | None:
        return self._on_or_before(self._keys.get(code, []), self.md.px.get(code, {}), d)

    def fx(self, ccy: str, d: date) -> float | None:
        if ccy == "CNY":
            return 1.0
        return self._on_or_before(self._fx_keys.get(ccy, []), self.md.fx_all.get(ccy, {}), d)

    def report_for(self, T: date):
        """T 日开盘前已发布的最新报告（按发布日，不按报告期末）。"""
        usable = [r for r in self.md.reports if r.holdings and r.publish_date < T]
        return max(usable, key=lambda r: (r.period_end, r.publish_date)) if usable else None

    def weights(self, rep) -> dict[str, float]:
        w = {c: v for c, v in rep.holdings.items() if c in self.assets}
        for c in set(rep.holdings) - set(w):
            self.notes.append(f"{rep.title}：持仓 {c} 未配置行情，未计入篓子")
        return w

    def asset_return(self, code: str, d0: date, d1: date, fx_day1: date | None = None) -> float | None:
        """人民币收益，计入 (d0, d1] 内除息的税后分红。"""
        a = self.assets[code]
        p0, p1 = self.price(code, d0), self.price(code, d1)
        f0, f1 = self.fx(a.currency, d0), self.fx(a.currency, fx_day1 or d1)
        if None in (p0, p1, f0, f1):
            return None
        div = sum(x for e, x in self.md.divs.get(code, {}).items() if d0 < e <= d1)
        return (p1 + div * (1 - self.p.dividend_wht)) * f1 / (p0 * f0) - 1

    def contributions(self, rep, d0: date, d1: date, fx_day1: date | None = None) -> dict[str, float]:
        """海外、A 股两部分对净值收益的贡献：Σ 报告权重 × 人民币收益。"""
        out = dict.fromkeys(self.PARTS, 0.0)
        for code, w in self.weights(rep).items():
            r = self.asset_return(code, d0, d1, fx_day1)
            if r is None:
                self.notes.append(f"{code} 缺少 {d0}~{d1} 的价格或汇率，当日未计入")
                continue
            out["cn" if self.assets[code].market in CN_MARKETS else "foreign"] += w * r
        return out

    def part_weights(self, rep) -> dict[str, float]:
        out = dict.fromkeys(self.PARTS, 0.0)
        for code, w in self.weights(rep).items():
            out["cn" if self.assets[code].market in CN_MARKETS else "foreign"] += w
        return out

    # ------------------------------------------------------------ 单日变动

    def step(self, D: date) -> Step | None:
        if D in self._steps:
            return self._steps[D]
        result = None
        Dp, T = self.cal.prev_cn(D), self.cal.next_cn(D)
        rep = self.report_for(T) if T else None
        if Dp and rep:
            parts = self.contributions(rep, Dp, D)
            W = sum(self.part_weights(rep).values())
            if W > 0:
                result = Step(
                    D, Dp, rep.period_end, self.cal.us_on_or_before(Dp), self.cal.us_on_or_before(D),
                    self.fx("USD", Dp), self.fx("USD", D), sum(parts.values()) / W, None, W,
                    len(self.weights(rep)), parts,
                )
        self._steps[D] = result
        return result

    # ------------------------------------------------------------ 敞口与估值

    def _observations(self, D: date) -> list[tuple[Step, float]]:
        """D 之前（含 Dp）的观测：(当日变动, 去费用后的官方净值收益)。"""
        nav = self.md.nav
        obs = []
        k = self.cal.prev_cn(D)
        for _ in range(self.p.max_lookback):
            if k is None or len(obs) >= self.p.window:
                break
            st = self.step(k)
            if st and k in nav and st.Dp in nav and abs(st.r_basket) > 1e-6:
                nav0 = nav[st.Dp]
                y = (nav[k] + nav0 * self.p.annual_fee * (st.D - st.Dp).days / 365) / nav0 - 1
                obs.append((st, y))
            k = self.cal.prev_cn(k)
        return obs

    def betas(self, D: date) -> tuple[dict[str, float], int]:
        """各部分敞口系数。1 表示与报告比例一致；观测不足时按报告比例。"""
        st = self.step(D)
        obs = self._observations(D)
        if self.p.model == "fixed" or len(obs) < self.p.min_obs:
            return dict.fromkeys(self.PARTS, 1.0), len(obs)
        if self.p.model == "single":
            xs = [o.r_basket * o.weight for o, _ in obs]
            k = sum(x * y for x, (_, y) in zip(xs, obs)) / sum(x * x for x in xs)
            return dict.fromkeys(self.PARTS, k), len(obs)
        f = [o.parts["foreign"] for o, _ in obs]
        c = [o.parts["cn"] for o, _ in obs]
        y = [v for _, v in obs]
        sol = _solve2(sum(x * x for x in f), sum(a * b for a, b in zip(f, c)), sum(x * x for x in c),
                      sum(a * b for a, b in zip(f, y)), sum(a * b for a, b in zip(c, y)))
        if sol is None:
            return dict.fromkeys(self.PARTS, 1.0), len(obs)
        return {"foreign": sol[0], "cn": sol[1]}, len(obs)

    def exposure(self, D: date, method: str = "basket") -> tuple[float, int]:
        """有效股票敞口（占净值）= Σ 部分敞口系数 × 报告比例。"""
        st = self.step(D)
        b, n = self.betas(D)
        rep = self.report_for(self.cal.next_cn(D))
        pw = self.part_weights(rep) if rep else dict.fromkeys(self.PARTS, 0.0)
        return sum(b[k] * pw[k] for k in self.PARTS), n

    def estimate(self, D: date, method: str = "basket", nav_prev: float | None = None) -> NavEstimate | None:
        if method != "basket":
            return None  # 没有跟踪指数，不做指数法对照
        st = self.step(D)
        if st is None:
            return None
        nav_prev = nav_prev if nav_prev is not None else self.md.nav.get(st.Dp)
        if nav_prev is None:
            return None
        betas, n = self.betas(D)
        E, _ = self.exposure(D)
        move = sum(betas[k] * st.parts[k] for k in self.PARTS)
        fee = nav_prev * self.p.annual_fee * (st.D - st.Dp).days / 365
        return NavEstimate(D, method, nav_prev, st.r_basket, E, n, fee, nav_prev * (1 + move) - fee, st, betas)

    def fair_value(self, T: date, est: NavEstimate) -> tuple[float, int]:
        """T 日开盘时的公允价值：补上 D 之后的海外交易日（长假），海外部分汇率换成 FX(T)。"""
        st = est.step
        rep = self.report_for(T)
        latest = T - timedelta(days=1)
        parts = self.contributions(rep, st.D, latest, fx_day1=T)
        gap = len(self.cal.us_between(st.s1, self.cal.us_on_or_before(latest))) if st.s1 else 0
        move = sum(est.betas[k] * parts[k] for k in self.PARTS)
        return est.nav * (1 + move), gap


def make_estimator(md: MarketData, params: Params | None = None):
    if md.fund.holdings == "reports":
        return ReportEstimator(md, params)
    return PcfEstimator(md, params)
