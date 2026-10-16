"""集思录 QDII 列表与单只基金历史溢价（需要登录 cookie，放在 .env 的 JISILU_COOKIE）。"""

from __future__ import annotations

import json
import time
from datetime import date

from ..config import load_env
from ..net import fetch

LIST = "https://www.jisilu.cn/data/qdii/qdii_list/{market}?___jsl=LST___t={ts}&{only}=y&rp=22"
HIST = "https://www.jisilu.cn/data/qdii/detail_hists/?___jsl=LST___t={ts}"


def _headers() -> dict[str, str]:
    cookie = load_env().get("JISILU_COOKIE")
    if not cookie:
        raise RuntimeError("缺少 JISILU_COOKIE，请写入项目根目录 .env")
    return {
        "Cookie": cookie,
        "Referer": "https://www.jisilu.cn/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }


def download_list(market: str = "E", only: str = "only_etf") -> bytes:
    """QDII 列表当前快照。market：E 欧美、A 亚洲、C 商品；only：only_etf 或 only_lof。"""
    return fetch(LIST.format(market=market, ts=int(time.time() * 1000), only=only), headers=_headers())


def parse_list(raw: bytes) -> dict[str, dict]:
    return {r["id"]: r["cell"] for r in json.loads(raw).get("rows", [])}


def download_hist(fund_id: str) -> bytes:
    return fetch(
        HIST.format(ts=int(time.time() * 1000)),
        data={"is_search": 1, "fund_id": fund_id, "rp": 50, "page": 1},
        headers=_headers(),
    )


def parse_hist(raw: bytes) -> dict[date, dict]:
    out = {}
    for r in json.loads(raw).get("rows", []):
        c = r["cell"]
        out[date.fromisoformat(c["price_dt"])] = c
    return dict(sorted(out.items()))
