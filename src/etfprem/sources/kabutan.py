"""株探（kabutan.jp）东京证券交易所日线，按页翻取，每页约 30 个交易日。"""

from __future__ import annotations

import re
import time
from datetime import date

from ..net import fetch

URL = "https://kabutan.jp/stock/kabuka?code={code}&ashi=day&page={page}"
_ROW = re.compile(
    r'<time datetime="(\d{4}-\d{2}-\d{2})">[^<]*</time>\s*</th>\s*'
    r"<td>([\d,.]+)</td>\s*<td>([\d,.]+)</td>\s*<td>([\d,.]+)</td>\s*<td>([\d,.]+)</td>"
)


def parse_page(html: str) -> dict[date, float]:
    """日期 -> 收盘价（日元）。"""
    return {date.fromisoformat(d): float(c.replace(",", "")) for d, _o, _h, _l, c in _ROW.findall(html)}


def download_daily(code: str, since: date, max_pages: int = 20, pause: float = 0.6) -> dict[date, float]:
    out: dict[date, float] = {}
    for page in range(1, max_pages + 1):
        rows = parse_page(fetch(URL.format(code=code, page=page)).decode("utf-8", "replace"))
        if not rows:
            break
        out.update(rows)
        if min(rows) <= since:
            break
        time.sleep(pause)
    return dict(sorted(out.items()))
