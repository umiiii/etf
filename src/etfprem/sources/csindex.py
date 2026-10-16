"""中证指数官网：指数日线（含中证海外中国互联网等跨境指数）。

跨境指数的基础代码（H30533、930604、930796、931456）都是人民币计价；美元版、港元版、全收益版是另外的代码，
derivatives() 可以查到。全收益版在除息日按税前股息再投资。D 日收盘 = 港股（A 股）D 日收盘 + 美股 D 日收盘。

折算汇率与基金不同，这是用中证指数估值的主要误差来源：
- H30533 系列的人民币版按中间价折算（港股用港元中间价、美股用美元中间价），与 QDII 净值口径一致，直接用；
- 930604、930796、931456 系列的人民币版用的是市场汇率（接近离岸人民币），和中间价每天差几个到几十个基点。
  这几只用港元版全收益指数再乘港元中间价：持仓以港股为主，基金按港元中间价折算。930604 没有港元版，
  用美元版乘中证自己的美元兑港元（另一对港元版、美元版指数之比）换成港元版。
- 指数里有 A 股时（930796、931456），港元版把 A 股也按市场汇率折成了港元，乘港元中间价后 A 股部分会多出
  “市场汇率与中间价之差”，按 A 股权重扣掉（a_share_no_fx）。
"""

from __future__ import annotations

import json
from bisect import bisect_right
from datetime import date

from ..net import fetch

BASE = "https://www.csindex.com.cn/csindex-home"
URL = BASE + "/perf/index-perf?indexCode={code}&startDate={start}&endDate={end}"
_HDR = {"Referer": "https://www.csindex.com.cn/"}
USDHKD_PAIR = ("H21072", "H20797")  # 中证全球中国互联网全收益 港元版 / 美元版：两者之比就是中证用的美元兑港元


def download(code: str, start: date, end: date) -> dict[date, float]:
    raw = fetch(URL.format(code=code, start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d")), headers=_HDR)
    out = {}
    for r in json.loads(raw).get("data") or []:
        if r.get("close") is not None:
            t = r["tradeDate"]
            out[date(int(t[:4]), int(t[4:6]), int(t[6:8]))] = float(r["close"])
    return dict(sorted(out.items()))


def derivatives(code: str) -> dict[str, str]:
    """同一指数的其他币种、全收益版本：代码 -> 简称，例如 930604 -> {"930605": "中国互联网30USD", "H20605": ...}。"""
    j = json.loads(fetch(f"{BASE}/perf/get-derivative-index?indexCode={code}", headers=_HDR))
    return {r["indexCode"]: r["indexNameCn"] for r in j.get("data") or []}


def usd_to_hkd(usd: dict[date, float], start: date, end: date) -> dict[date, float]:
    """美元版指数按中证自己的美元兑港元换成港元版（差一个常数，不影响涨跌）。"""
    hkd_ref, usd_ref = (download(c, start, end) for c in USDHKD_PAIR)
    return {d: v * hkd_ref[d] / usd_ref[d] for d, v in usd.items() if d in hkd_ref and d in usd_ref}


def a_share_no_fx(hkd: dict[date, float], cny: dict[date, float], hkd_cny: dict[date, float], w_a: float) -> dict[date, float]:
    """港元版 × (中证的港元兑人民币 / 港元中间价)^w_a：再按港元中间价折算时，A 股那部分就相当于没换汇。

    hkd、cny 是同一指数的港元版和人民币版，两者之比就是中证用的港元兑人民币；中间价缺的日子沿用前一个。
    """
    keys = sorted(hkd_cny)
    out = {}
    for d, v in hkd.items():
        i = bisect_right(keys, d)
        if d in cny and i:
            out[d] = v * (cny[d] / v / hkd_cny[keys[i - 1]]) ** w_a
    return out


# 合成序列：名称 -> 计算函数 (起, 止, 港元中间价)。A 股权重是中证官网 2026-10-14 的上交所加深交所权重
# （930796 为 0.1196，931456 为 0.5557），误差对它不敏感：偏 0.02 到 0.05，平均误差只变 0.05 到 0.3bp。
RECIPES = {
    "930604-HKD-TR": lambda s, e, fx: usd_to_hkd(download("H20605", s, e), s, e),
    "930796-HKD-TR-A": lambda s, e, fx: a_share_no_fx(download("H21072", s, e), download("H20796", s, e), fx, 0.12),
    "931456-HKD-TR-A": lambda s, e, fx: a_share_no_fx(download("921457", s, e), download("921456", s, e), fx, 0.556),
}


def download_recipe(name: str, start: date, end: date, hkd_cny: dict[date, float]) -> dict[date, float]:
    return dict(sorted(RECIPES[name](start, end, hkd_cny).items()))
