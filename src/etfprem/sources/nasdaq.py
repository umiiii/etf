"""Nasdaq：美股日线、分红记录、指数历史（xlsx）与指数成分。"""

from __future__ import annotations

import json
from datetime import date, datetime

from ..net import fetch
from ..xlsx import excel_date, read_rows

_HDR = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
STOCK_HIST = (
    "https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={cls}"
    "&fromdate={start}&limit=9999&todate={end}"
)
DIVIDENDS = "https://api.nasdaq.com/api/quote/{sym}/dividends?assetclass={cls}"
INDEX_HIST = (
    "https://indexes.nasdaqomx.com/Index/ExportHistory/{idx}"
    "?startDate={start}T00:00:00.000&endDate={end}T00:00:00.000&timeOfDay=EOD"
)
INDEX_MEMBERS = "https://indexes.nasdaqomx.com/Index/WeightingData"


def _money(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    if s in ("", "N/A", "--"):
        return None
    return float(s)


def _mdy(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def download_stock(sym: str, start: date, end: date, assetclass: str = "stocks") -> bytes:
    """assetclass 取 stocks 或 etf。"""
    return fetch(STOCK_HIST.format(sym=sym, cls=assetclass, start=start, end=end), headers=_HDR)


def parse_stock(raw: bytes) -> dict[date, float]:
    j = json.loads(raw)
    data = j.get("data") or {}
    rows = ((data.get("tradesTable") or {}).get("rows")) or []
    out: dict[date, float] = {}
    for r in rows:
        d, c = _mdy(r.get("date", "")), _money(r.get("close"))
        if d and c is not None:
            out[d] = c
    return dict(sorted(out.items()))


def download_dividends(sym: str, assetclass: str = "stocks") -> bytes:
    return fetch(DIVIDENDS.format(sym=sym, cls=assetclass), headers=_HDR)


def parse_dividends(raw: bytes) -> dict[date, float]:
    """除息日 -> 每股现金分红（美元）。"""
    j = json.loads(raw)
    data = j.get("data") or {}
    rows = ((data.get("dividends") or {}).get("rows")) or []
    out: dict[date, float] = {}
    for r in rows:
        if (r.get("type") or "Cash").lower() != "cash":
            continue
        d, a = _mdy(r.get("exOrEffDate", "")), _money(r.get("amount"))
        if d and a:
            out[d] = out.get(d, 0.0) + a
    return dict(sorted(out.items()))


def download_index_history(idx: str, start: date, end: date) -> bytes:
    return fetch(INDEX_HIST.format(idx=idx, start=start, end=end))


def parse_index_history(raw: bytes) -> dict[date, float]:
    out: dict[date, float] = {}
    for row in read_rows(raw):
        if len(row) >= 2 and isinstance(row[0], float) and isinstance(row[1], float) and row[1] > 0:
            out[excel_date(row[0])] = row[1]
    return dict(sorted(out.items()))


def index_members(idx: str, trade_date: date) -> list[str]:
    raw = fetch(
        INDEX_MEMBERS,
        data={"id": idx, "tradeDate": f"{trade_date}T00:00:00.000", "timeOfDay": "EOD"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    return [r["Symbol"] for r in json.loads(raw).get("aaData", [])]
