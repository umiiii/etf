"""南方东英（CSOP）官网：旗下香港、新加坡 ETF 的每日基金净值。

沙特 ETF 南方（159329）、华泰柏瑞（520830）是 2830.HK 的联接基金，实际按 2830 的沙特里亚尔净值乘人民币对
里亚尔中间价估值。2830 在港股几乎没有成交，收盘价经常几天不变，不能当代理。
2830 只在港股与沙特同时开市的日子（周一至周四）计算净值，沙特周日的行情并入周一的净值。

亚太精选 ETF 南方（159687）持有 LCU.SI，东南亚科技 ETF 华泰柏瑞（513730）持有 SQU.SI，两者都按被投资 ETF
当日的美元净值乘美元中间价估值；被投资 ETF 不估值的日子（日本、香港、泰国、印尼、美国等成分市场休市）
沿用最近一日净值。新加坡柜台：LCU SP（美元）、LCS SP（新元）、SQU SP（美元）、SQQ SP（新元）。

接口是 POST JSON，必须带 Content-Type: application/json 和浏览器 UA（fetch 默认带），否则返回 403 或 500；
日期写成 YYYYMMDD。各柜台分别给出净值：2830 HK（港元）、72830 HK（里亚尔）、82830 HK（人民币）、2830 HK - A（美元）。
历史接口偶尔漏掉个别交易日（例如 2026-07-27），每次下载再并入“最新净值”接口的一条，按日缓存就能补齐。
已经漏掉的日子，用同一接口里按月更新（只到上月末）的复权净值表 IndexPriceAndAdjNav 的日涨跌补上
（SQU 2026-05-29、07-27；2830 的里亚尔、美元柜台 2026-07-27）。
"""

from __future__ import annotations

import json
import re
from datetime import date

from ..net import fetch

URL = "https://website-api.csopasset.com/cmsApi/{path}"
PRODUCTS = {  # 产品代码 -> 官网产品名
    "2830": "CSOP Saudi Arabia ETF",
    "LCU": "CSOP FTSE Asia Pacific Select Index ETF",
    "SQU": "CSOP iEdge Southeast Asia+ TECH Index ETF",
    "3167": "ICBC CSOP S&P New China Sectors ETF",
}
_SG_ALIAS = {"LCS": "LCU", "SQQ": "SQU"}  # 新元柜台 -> 产品代码


def _post(path: str, body: dict):
    raw = fetch(URL.format(path=path), data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(raw)


def _cn_date(text: str) -> date:
    """'2025年01月02日' -> date。"""
    y, m, d = re.findall(r"\d+", text)[:3]
    return date(int(y), int(m), int(d))


def _product(ticker: str) -> tuple[str, str]:
    """柜台 -> (产品代码, 请求用的主柜台)。'72830 HK' -> ('2830', '2830 HK')，'LCS SP' -> ('LCU', 'LCU SP')。"""
    if ticker.endswith(" SP"):
        code = ticker.split()[0]
        code = _SG_ALIAS.get(code, code)
        return code, f"{code} SP"
    code = re.search(r"\d+", ticker).group(0)[-4:]
    return code, f"{code} HK"


def _fill_from_adjusted(out: dict[date, float], adj: dict[date, float], start: date, end: date) -> None:
    """复权净值表按 FundCode 分组，标签不可靠（LCU 的美元净值记在 'LCS' 名下），只采用与本柜台净值之比恒定的那组。

    没分过红的产品两者相等；分过红的（如 3167）复权净值是净值乘一个常数。别的币种柜台的比值随汇率变动，会被排除。
    """
    common = [d for d in out if d in adj]
    if len(common) < 5:
        return
    ratios = sorted(out[d] / adj[d] for d in common)
    mid = ratios[len(ratios) // 2]
    if sum(abs(r / mid - 1) < 1e-5 for r in ratios) < 0.9 * len(ratios):
        return
    anchors = sorted(common)
    for d in sorted(set(adj) - set(out)):
        prev = [a for a in anchors if a < d]
        if prev and start <= d <= end:
            out[d] = out[prev[-1]] * adj[d] / adj[prev[-1]]


def download(ticker: str, start: date, end: date) -> dict[date, float]:
    """日期 -> 该柜台的每份净值。ticker 例如 '72830 HK'（里亚尔）、'2830 HK'（港元）、'LCU SP'（美元）。"""
    code, main = _product(ticker)
    product = PRODUCTS[code]
    j = _post("performanceView/product", {"productName": product, "ticker": main,
                                          "beginDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d")})
    data = j.get("data") or {}
    out = {}
    for rows in data.get("NAV") or []:
        for r in rows:
            if r.get("Ticker") == ticker and r.get("NAV") is not None:
                out[_cn_date(r["HstDate2"])] = float(r["NAV"])
    for rows in data.get("IndexPriceAndAdjNav") or []:
        _fill_from_adjusted(out, {_cn_date(r["HstDate2"]): float(r["AdjustedNAV"]) for r in rows if r.get("AdjustedNAV")},
                            start, end)
    for r in _post("NAV/product", {"productName": product}) or []:
        if r.get("Ticker") == ticker and r.get("NAV") is not None:
            d = date.fromisoformat(r["HstDateFormat"])
            if start <= d <= end:
                out[d] = float(r["NAV"])
    return dict(sorted(out.items()))
