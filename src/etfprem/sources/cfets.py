"""中国外汇交易中心人民币汇率中间价。接口单次只能查一年内的数据，因此分段请求。"""

from __future__ import annotations

import json
from datetime import date, timedelta

from ..net import fetch

URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
_HDR = {"Referer": "https://www.chinamoney.com.cn/chinese/bkccpr/"}


def download(pair: str, start: date, end: date, chunk_days: int = 300) -> dict[date, float]:
    out: dict[date, float] = {}
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=chunk_days))
        raw = fetch(
            URL,
            data={
                "startDate": cur.isoformat(),
                "endDate": stop.isoformat(),
                "currency": pair,
                "pageNum": 1,
                "pageSize": 400,
            },
            headers=_HDR,
        )
        j = json.loads(raw)
        records = j.get("records") or []
        if not records and (j.get("data") or {}).get("flagMessage"):
            raise RuntimeError(f"中间价接口拒绝 {cur}~{stop}：{j['data']['flagMessage']}")
        for r in records:
            out[date.fromisoformat(r["date"])] = float(r["values"][0])
        cur = stop + timedelta(days=1)
    return dict(sorted(out.items()))
