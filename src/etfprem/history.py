"""逐个价格日计算溢价率，并汇总估算误差。"""

from __future__ import annotations

import math
import statistics as st
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .estimator import make_estimator
from .store import MarketData


@dataclass
class Row:
    date: date  # 价格日 T
    close: float  # 场内收盘价
    nav_date: date  # 参考净值日 D（T 的前一个 A 股交易日）
    nav_official: float | None  # 公布净值（4 位小数），尚未公布时为空
    nav_official_precise: float | None  # PCF 中的高精度净值
    nav_est: float  # 成分篓子法估算净值
    nav_est_index: float | None  # 指数法估算净值（对照）
    exposure: float  # 滚动回归的股票敞口
    basket_ret_usd: float  # 成分篓子美元收益
    index_ret_usd: float | None  # 指数美元收益
    fx: float  # FX(D)
    holdings_date: date  # 使用的持仓：申赎清单日期，或定期报告的报告期末
    gap_sessions: int  # D 之后到 T 开盘前又多出的美股交易日（长假）
    fair_value: float  # T 开盘时公允价值
    premium_official: float | None  # 收盘价 / 公布净值 − 1（集思录口径）
    premium_est: float  # 收盘价 / 篓子估算净值 − 1
    premium_est_index: float | None
    premium_fv: float  # 收盘价 / 公允价值 − 1
    jisilu_premium: float | None  # 集思录历史表 discount_rt
    jisilu_price: float | None = None  # 集思录历史表 price

    @property
    def nav_err_bp(self) -> float | None:
        if self.nav_official_precise is None:
            return None
        return (self.nav_est / self.nav_official_precise - 1) * 1e4

    @property
    def nav_err_index_bp(self) -> float | None:
        if self.nav_official_precise is None or self.nav_est_index is None:
            return None
        return (self.nav_est_index / self.nav_official_precise - 1) * 1e4

    @property
    def premium_err_pp(self) -> float | None:
        if self.premium_official is None:
            return None
        return (self.premium_est - self.premium_official) * 100


def compute(md: MarketData, start: date, end: date, est=None) -> list[Row]:
    est = est or make_estimator(md)
    rows: list[Row] = []
    for T in est.cal.cn:
        if not (start <= T <= end) or T not in md.etf:
            continue
        D = est.cal.prev_cn(T)
        b = est.estimate(D, "basket") if D else None
        if b is None:
            est.notes.append(f"{T}：缺少数据，无法估算")
            continue
        i = est.estimate(D, "index")
        close = md.etf[T]["close"]
        nav4 = md.nav4.get(D)
        fv, gap = est.fair_value(T, b)
        jrow = md.jisilu.get(T, {})
        js, jprice = jrow.get("discount_rt"), jrow.get("price")
        rows.append(
            Row(
                date=T,
                close=close,
                nav_date=D,
                nav_official=nav4,
                nav_official_precise=md.nav.get(D),
                nav_est=b.nav,
                nav_est_index=i.nav if i else None,
                exposure=b.exposure,
                basket_ret_usd=b.r,
                index_ret_usd=b.step.r_index,
                fx=b.step.fx1,
                holdings_date=b.step.holdings_date,
                gap_sessions=gap,
                fair_value=fv,
                premium_official=close / nav4 - 1 if nav4 else None,
                premium_est=close / b.nav - 1,
                premium_est_index=close / i.nav - 1 if i else None,
                premium_fv=close / fv - 1,
                jisilu_premium=float(js) / 100 if js not in (None, "-", "") else None,
                jisilu_price=float(jprice) if jprice not in (None, "-", "") else None,
            )
        )
    return rows


def _stats(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {}
    a = [abs(x) for x in xs]
    return {
        "n": len(xs),
        "mean": st.mean(xs),
        "mae": st.mean(a),
        "rms": math.sqrt(st.mean([x * x for x in xs])),
        "max": max(a),
    }


def summarize(rows: list[Row]) -> dict:
    done = [r for r in rows if r.nav_official_precise is not None]
    nav_err = [r.nav_err_bp for r in done]
    nav_err_idx = [r.nav_err_index_bp for r in done if r.nav_err_index_bp is not None]
    prem_err = [r.premium_err_pp for r in done if r.premium_err_pp is not None]
    js = [r for r in rows if r.jisilu_premium is not None]
    prem = [r.premium_official if r.premium_official is not None else r.premium_est for r in rows]

    monthly = defaultdict(list)
    for r, p in zip(rows, prem):
        monthly[r.date.strftime("%Y-%m")].append((r, p))

    hi = max(zip(rows, prem), key=lambda x: x[1])
    lo = min(zip(rows, prem), key=lambda x: x[1])
    return {
        "first": rows[0].date,
        "last": rows[-1].date,
        "days": len(rows),
        "days_with_official": len(done),
        "nav_err_bp": _stats(nav_err),
        "nav_err_index_bp": _stats(nav_err_idx),
        "nav_within_bp": {k: sum(abs(x) <= k for x in nav_err) / len(nav_err) for k in (1, 2, 5)} if nav_err else {},
        "premium_err_pp": _stats(prem_err),
        "premium_within_pp": {k: sum(abs(x) <= k for x in prem_err) / len(prem_err) for k in (0.01, 0.02, 0.05)}
        if prem_err
        else {},
        "jisilu_days": len(js),
        "jisilu_vs_official_pp": _stats(
            [(r.premium_official - r.jisilu_premium) * 100 for r in js if r.premium_official is not None]
        ),
        "jisilu_vs_est_pp": _stats([(r.premium_est - r.jisilu_premium) * 100 for r in js]),
        # 超出两位小数舍入（0.006 个百分点）的日子，以及其中收盘价两边不一致的日子
        "jisilu_beyond_rounding": [r.date for r in js if r.premium_official is not None
                                   and abs(r.premium_official - r.jisilu_premium) * 100 > 0.006],
        "jisilu_price_mismatch": [r.date for r in js if r.jisilu_price is not None and abs(r.jisilu_price - r.close) > 1e-9],
        "premium": {
            "mean": st.mean(prem),
            "median": st.median(prem),
            "max": (hi[0].date, hi[1]),
            "min": (lo[0].date, lo[1]),
            "latest": (rows[-1].date, prem[-1], rows[-1].premium_official is None),
        },
        "monthly": {
            m: {
                "n": len(v),
                "mean": st.mean(p for _, p in v),
                "min": min(p for _, p in v),
                "max": max(p for _, p in v),
                "close_last": v[-1][0].close,
                "nav_err_mae_bp": st.mean(abs(r.nav_err_bp) for r, _ in v if r.nav_err_bp is not None)
                if any(r.nav_err_bp is not None for r, _ in v)
                else None,
            }
            for m, v in monthly.items()
        },
    }
