"""腾讯行情：A 股 / 场内基金日线，支持后复权（hfq）。

场内 ETF 会做份额拆分，未复权价格在拆分日直接腰斩。后复权价格连续，且历史值不会因为以后的
拆分或分红而改写，适合增量缓存。
"""

from __future__ import annotations

import json
from datetime import date

from ..net import fetch

URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,{start},{end},{count},{fq}"


def download_kline(code: str, market: str, start: date, end: date, fq: str = "hfq") -> dict[date, float]:
    """日期 -> 收盘价（复权）。"""
    sym = f"{market}{code}"
    count = (end - start).days + 10
    j = json.loads(fetch(URL.format(sym=sym, start=start, end=end, count=count, fq=fq)))
    data = (j.get("data") or {}).get(sym) or {}
    rows = data.get(f"{fq}day") or data.get("day") or []
    return {date.fromisoformat(r[0]): float(r[2]) for r in rows if len(r) >= 3}
