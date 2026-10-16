"""中国外汇交易中心人民币汇率：中间价，以及北京时间 16:00 人民币参考汇率。接口单次只能查一年内的数据，因此分段请求。

多数基金公司用当天 9:15 公布的中间价折算外币资产；华夏基金用 16:00 参考汇率（实测两只华夏 ETF 改用它后
净值误差从 8bp 多降到 1bp 以内，其他公司的基金改用它反而变差）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from ..net import fetch

URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
# 这些货币的中间价报价方向是 CNY/XXX（1 元人民币兑多少外币），其余是 XXX/CNY
CNY_BASE = ("MYR", "RUB", "ZAR", "KRW", "AED", "SAR", "HUF", "PLN", "DKK", "SEK", "NOK", "TRY", "MXN", "THB")
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


REF_URL = (
    "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-fx/RefRateHis?lang=cn"
    "&startDateTool={start}&endDateTool={end}&currencyCode={ccy}"
)
_REF_HDR = {"Referer": "https://www.chinamoney.com.cn/chinese/homefxrrm/index.html"}


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_REF_CCY = {"100JPY/CNY": "JPY.CNY"}  # 参考汇率接口里日元写作 JPY.CNY（仍是每 100 日元）


def download_ref16(pair: str, start: date, end: date, chunk_days: int = 300) -> dict[date, float]:
    """北京时间 16:00 人民币参考汇率。pair 写成 USD/CNY，接口的币种代码是 USD.CNY，日期格式为 “01 Jun 2025”。

    接口出错时不报错：跨度超过一年会在 data.message 里提示并返回空表，日期格式没被识别时会返回最近一个月。
    这里逐段检查，避免拿到空数据或错位的数据。
    """
    fmt = lambda d: f"{d.day:02d}%20{_MONTHS[d.month - 1]}%20{d.year}"  # noqa: E731  不依赖 locale
    ccy = _REF_CCY.get(pair, pair.replace("/", "."))
    out: dict[date, float] = {}
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=chunk_days))
        j = json.loads(fetch(REF_URL.format(start=fmt(cur), end=fmt(stop), ccy=ccy), data=b"", headers=_REF_HDR))
        msg = (j.get("data") or {}).get("message")
        if msg and not j.get("records"):
            raise RuntimeError(f"16:00 参考汇率接口拒绝 {pair} {cur}~{stop}：{msg}")
        for r in j.get("records") or []:
            v, d = r.get("rateOf16hour"), date.fromisoformat(r["dealDate"])
            if v and v not in ("---", "/") and cur <= d <= stop:
                out[d] = float(v)
        cur = stop + timedelta(days=1)
    return dict(sorted(out.items()))
