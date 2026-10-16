"""MSCI 官网指数日线（app2.msci.com）。

index_variant：STRD 价格指数、NETR 净收益（按非居民最高预提税扣税后再投资）、GRTR 全收益。
MSCI 在美股休市日也给出数值（与前一天相同）。
"""

from __future__ import annotations

import json
from datetime import date

from ..net import fetch

URL = (
    "https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph?currency_symbol={ccy}"
    "&index_variant={variant}&start_date={start}&end_date={end}&data_frequency=DAILY&index_codes={code}"
)


def download(code: str, start: date, end: date, variant: str = "NETR", ccy: str = "USD") -> dict[date, float]:
    raw = fetch(URL.format(code=code, variant=variant, ccy=ccy, start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d")))
    rows = ((json.loads(raw).get("indexes") or {}).get("INDEX_LEVELS")) or []
    out = {}
    for r in rows:
        d = int(r["calc_date"])
        out[date(d // 10000, d // 100 % 100, d % 100)] = float(r["level_eod"])
    return dict(sorted(out.items()))
