"""腾讯行情：A 股 / 场内基金 / 港股 / 美股指数日线。

场内 ETF 会做份额拆分，未复权价格在拆分日直接腰斩。后复权价格连续，且历史值不会因为以后的
拆分或分红而改写，适合增量缓存。港股、美股指数用不复权价（fq=""）。
美股个股要带交易所后缀（AAPL.OQ / XOM.N / ... ），返回未复权价，用来核对清单里的拆股。
"""

from __future__ import annotations

import json
from datetime import date

from ..net import fetch

URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,{start},{end},{count},{fq}"
US_SUFFIXES = (".OQ", ".N", ".AM", ".PS")


def download_kline(code: str, market: str, start: date, end: date, fq: str = "hfq") -> dict[date, float]:
    """日期 -> 收盘价。market 取 sz / sh / hk / us，美股指数 code 写成 .INX、.DJI、.NDX。"""
    sym = f"{market}{code}"
    count = (end - start).days + 10
    j = json.loads(fetch(URL.format(sym=sym, start=start, end=end, count=count, fq=fq)))
    data = (j.get("data") or {})
    data = data.get(sym) if isinstance(data, dict) else None
    rows = (data or {}).get(f"{fq}day") or (data or {}).get("day") or []
    return {date.fromisoformat(r[0]): float(r[2]) for r in rows if len(r) >= 3}


def us_raw_close(symbol: str, start: date, end: date) -> dict[date, float]:
    """美股个股未复权收盘价。依次尝试交易所后缀，都没有数据时返回空。"""
    for suffix in US_SUFFIXES:
        series = download_kline(f"{symbol.replace('/', '.')}{suffix}", "us", start, end, fq="")
        if series:
            return series
    return {}
