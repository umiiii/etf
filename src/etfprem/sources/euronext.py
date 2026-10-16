"""泛欧交易所（Euronext）指数日线，CSV 下载，约两年。

CAC 40 价格指数不含分红，法国公司集中在 4 到 5 月除息，用价格指数估值会在分红季系统性偏低。
净收益指数 CAC 40 NR（QS0011131826）按扣税后的分红再投资，与基金实际收到的分红一致。
"""

from __future__ import annotations

from datetime import date, datetime

from ..net import fetch

URL = (
    "https://live.euronext.com/en/ajax/AwlHistoricalPrice/getFullDownloadAjax/{code}"
    "?format=csv&decimal_separator=.&date_form=d/m/Y&op=&adjusted=&base100=&startDate={start}&endDate={end}"
)


def download(code: str, start: date, end: date) -> dict[date, float]:
    """code 形如 QS0011131826-XPAR（ISIN-市场）。返回 日期 -> 收盘点位。"""
    text = fetch(URL.format(code=code, start=start, end=end)).decode("utf-8-sig", "replace")
    out = {}
    for line in text.splitlines():
        f = line.split(";")
        if len(f) > 5 and f[0][:1].isdigit() and f[5]:
            out[datetime.strptime(f[0], "%d/%m/%Y").date()] = float(f[5])
    return dict(sorted(out.items()))
