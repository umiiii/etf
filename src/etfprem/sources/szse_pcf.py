"""深交所 ETF 申购赎回清单（PCF）。

文件按发布日 T 命名。清单里有两个“信息内容”区块：
第一个是最近一个已公布净值的日期（对 QDII 来说是 T 的前两个交易日），
第二个是 T 当天的申赎参数。组合信息里给出每 100 万份对应的成分股股数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from ..net import fetch

URL = "https://reportdocs.static.szse.cn/files/text/etf/ETF{fund}{ymd}.txt"

_NUM = r"-?[\d,]+(?:\.\d+)?"
_ROW = re.compile(
    r"^\s*(?P<code>\S+)\s+(?P<name>\S+)\s+(?P<qty>\d+)\s+(?P<flag>\S+)\s+"
    r"(?P<sub_margin>[\d.]+)%\s+(?:(?P<red_margin>[\d.]+)%\s+)?"
    r"(?P<sub_amt>[\d,]+\.\d+)(?:\s+(?P<red_amt>[\d,]+\.\d+))?(?:\s+(?P<market>\S+))?"
)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


@dataclass(frozen=True)
class Holding:
    code: str
    qty: int
    flag: str  # 现金替代标志
    sub_margin: float  # 申购现金替代保证金率
    sub_amount: float  # 申购替代金额（人民币，含保证金）
    market: str


@dataclass
class PCF:
    fund_id: str
    trade_date: date
    nav_date: date | None
    unit_nav: float | None  # 最小申赎单位资产净值
    nav4: float | None  # 基金份额净值（4 位小数）
    cash_diff: float | None
    est_cash_diff: float | None
    unit_size: int
    cash_component: float | None  # 159900 申赎现金
    holdings: dict[str, Holding]

    @property
    def nav(self) -> float | None:
        """高精度份额净值 = 单位资产净值 / 单位份额。"""
        if self.unit_nav is not None and self.unit_size:
            return self.unit_nav / self.unit_size
        return self.nav4


def download(fund_id: str, day: date) -> bytes:
    return fetch(URL.format(fund=fund_id, ymd=day.strftime("%Y%m%d")))


def parse(raw: bytes | str, fund_id: str) -> PCF:
    text = raw.decode("gb18030") if isinstance(raw, bytes) else raw

    def grab(pattern: str) -> float | None:
        m = re.search(pattern, text)
        return _num(m.group(1)) if m else None

    title = re.search(r"\(\s*(\d{4}-\d{2}-\d{2})\s*\)", text)
    blocks = re.findall(r"(\d{4}-\d{2}-\d{2})日\s*信息内容", text)
    trade_date = date.fromisoformat(title.group(1) if title else blocks[-1])
    nav_date = date.fromisoformat(blocks[0]) if len(blocks) >= 2 else None

    holdings: dict[str, Holding] = {}
    cash_component = None
    _, _, body = text.partition("组合信息内容")
    for line in body.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        h = Holding(
            code=m["code"],
            qty=int(m["qty"]),
            flag=m["flag"],
            sub_margin=float(m["sub_margin"]) / 100,
            sub_amount=_num(m["sub_amt"]),
            market=m["market"] or "",
        )
        if m["name"] == "申赎现金":
            cash_component = h.sub_amount
        else:
            holdings[h.code] = h

    unit_size = grab(r"最小申购、赎回单位：\s*([\d,]+)份")
    return PCF(
        fund_id=fund_id,
        trade_date=trade_date,
        nav_date=nav_date,
        unit_nav=grab(rf"最小申购、赎回单位资产净值：\s*({_NUM})元"),
        nav4=grab(rf"基金份额净值：\s*({_NUM})元"),
        cash_diff=grab(rf"(?<!预估)现金差额：\s*({_NUM})元"),
        est_cash_diff=grab(rf"预估现金差额：\s*({_NUM})元"),
        unit_size=int(unit_size) if unit_size else 1_000_000,
        cash_component=cash_component,
        holdings=holdings,
    )
