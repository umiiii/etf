"""利安资金（Lion Global Investors）官网：旗下新加坡 ETF 的每日基金净值。

新兴亚洲 ETF 招商（520580）持有 EAU.SI（利安招商新兴亚洲精选 ETF 美元柜台），按其当日净值估值，
没有净值的日子（印度等成分市场休市）沿用最近一日净值。

接口是官网 Historical Prices 下载用的 GET，返回 CSV：第一行基金名，第二行表头 "NAV Price","Date"，
之后每行形如 0.8930,"14-Oct-26"。官网代码：LCME（美元类，对应 EAU）、LCMS（新元类，对应 EAA）。
净值只公布到 3 位小数（约 11bp 一档），数据从 2025-01-02 起；最新一天要到第二个新加坡交易日下午以后才上线。
"""

from __future__ import annotations

from datetime import date

from ..net import fetch

URL = "https://api.lionglobalinvestors.com/fhistorical?fcode={fcode}&fstart={start}&fend={end}"
FCODES = {"EAU": "LCME", "EAA": "LCMS"}  # 新交所柜台代码 -> 官网基金代码
_MON = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def download(code: str, start: date, end: date) -> dict[date, float]:
    """日期 -> 每份净值。code 为新交所柜台代码（EAU/EAA）或官网基金代码。"""
    raw = fetch(URL.format(fcode=FCODES.get(code, code), start=start.isoformat(), end=end.isoformat()))
    out = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        nav, _, day = line.partition(",")
        dd, _, rest = day.strip().strip('"').partition("-")
        mon, _, yy = rest.partition("-")
        try:
            out[date(2000 + int(yy), _MON[mon], int(dd))] = float(nav)
        except (ValueError, KeyError):
            continue  # 标题行、表头
    return dict(sorted(out.items()))
