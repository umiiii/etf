"""新浪行情：场内 ETF 日线与实时报价。"""

from __future__ import annotations

import json
from datetime import date

from ..net import fetch

KLINE = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}"
)
QUOTE = "https://hq.sinajs.cn/list={sym}"


def download_kline(code: str, market: str, n: int = 600) -> bytes:
    return fetch(KLINE.format(sym=f"{market}{code}", n=n))


def parse_kline(raw: bytes) -> dict[date, dict[str, float]]:
    out = {}
    for r in json.loads(raw) or []:
        out[date.fromisoformat(r["day"][:10])] = {
            k: float(r[k]) for k in ("open", "high", "low", "close", "volume") if k in r
        }
    return dict(sorted(out.items()))


def realtime(code: str, market: str) -> dict[str, object]:
    raw = fetch(QUOTE.format(sym=f"{market}{code}"), headers={"Referer": "https://finance.sina.com.cn/"})
    text = raw.decode("gb18030")
    body = text.split('"')[1] if '"' in text else ""
    f = body.split(",")
    if len(f) < 32:
        raise RuntimeError(f"新浪实时行情解析失败：{text[:120]}")
    return {
        "name": f[0],
        "open": float(f[1]),
        "prev_close": float(f[2]),
        "price": float(f[3]),
        "high": float(f[4]),
        "low": float(f[5]),
        "date": date.fromisoformat(f[30]),
        "time": f[31],
    }
