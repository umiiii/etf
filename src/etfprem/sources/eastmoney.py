"""天天基金（东方财富）基金历史净值，每页最多 20 条。"""

from __future__ import annotations

import json
import time
from datetime import date

from ..net import fetch

NAV = (
    "https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex={page}"
    "&pageSize=20&startDate={start}&endDate={end}"
)


def download_nav(code: str, start: date, end: date, pause: float = 0.3) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        raw = fetch(
            NAV.format(code=code, page=page, start=start, end=end),
            headers={"Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html"},
        )
        j = json.loads(raw)
        batch = ((j.get("Data") or {}).get("LSJZList")) or []
        rows.extend(batch)
        total = j.get("TotalCount") or 0
        if not batch or len(rows) >= total:
            return rows
        page += 1
        time.sleep(pause)


def parse_nav(rows: list[dict]) -> dict[date, float]:
    out = {}
    for r in rows:
        if r.get("DWJZ"):
            out[date.fromisoformat(r["FSRQ"])] = float(r["DWJZ"])
    return dict(sorted(out.items()))


PINGZHONG = "https://fund.eastmoney.com/pingzhongdata/{code}.js"


def download_nav_full(code: str) -> dict[date, float]:
    """成立以来全部单位净值（一次请求）。时间戳为北京时间零点。"""
    import re
    from datetime import datetime, timedelta, timezone

    js = fetch(PINGZHONG.format(code=code), headers={"Referer": "https://fund.eastmoney.com/"}).decode("utf-8", "replace")
    m = re.search(r"var Data_netWorthTrend\s*=\s*(\[.*?\]);", js, re.S)
    if not m:
        raise RuntimeError(f"{code} 没有净值走势数据")
    cst = timezone(timedelta(hours=8))
    return {datetime.fromtimestamp(p["x"] / 1000, cst).date(): float(p["y"]) for p in json.loads(m.group(1))}
