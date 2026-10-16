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


KLINE = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2,f3"
    "&fields2=f51,f53&klt=101&fqt=0&beg={start}&end={end}"
)


def download_kline(secid: str, start: date, end: date) -> dict[date, float]:
    """东方财富行情日线收盘价，用于海外指数（例如德国 DAX 100.GDAXI）。

    注意 100.NDX 实为纳斯达克综合指数，纳斯达克 100 用 Nasdaq 官方导出。该接口连续请求过快会断开，调用方需要间隔。
    """
    raw = fetch(KLINE.format(secid=secid, start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d")),
                headers={"Referer": "https://quote.eastmoney.com/"})
    lines = ((json.loads(raw).get("data") or {}).get("klines")) or []
    out = {}
    for line in lines:
        d, c = line.split(",")[:2]
        out[date.fromisoformat(d)] = float(c)
    return dict(sorted(out.items()))


KLINE_BARS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2,f3"
    "&fields2=f51,f52,f53,f54,f55&klt=101&fqt=0&beg={start}&end={end}"
)


def download_kline_bars(secid: str, start: date, end: date) -> dict[date, dict[str, float]]:
    """日线开高低收。外汇（119.USDBRL）日线在北京时间 05:00 切日：开盘约为前一日纽约 17:00，收盘约为当日纽约 17:00。"""
    raw = fetch(KLINE_BARS.format(secid=secid, start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d")),
                headers={"Referer": "https://quote.eastmoney.com/"})
    out = {}
    for line in ((json.loads(raw).get("data") or {}).get("klines")) or []:
        d, o, c, h, lo = line.split(",")[:5]
        out[date.fromisoformat(d)] = {"open": float(o), "close": float(c), "high": float(h), "low": float(lo)}
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
