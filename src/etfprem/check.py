"""用集思录列表的当前快照逐只核对：收盘价、最新公布净值、按公布净值算的溢价率。

集思录对部分联接型基金（沙特、巴西、亚太低碳）不保存历史溢价，历史表对照覆盖不到，靠这里补一天的核对。
快照应在收盘后取，盘中的价格是最新成交价，和收盘价对不上是正常的。
"""

from __future__ import annotations

import json
from datetime import date

from . import store
from .config import DATA, FundConfig
from .sources import jisilu


def snapshot(save: bool = True) -> dict[str, dict]:
    """欧美市场 ETF 与 LOF 两张列表合并。"""
    out: dict[str, dict] = {}
    raws = {}
    for only in ("only_etf", "only_lof"):
        raws[only] = jisilu.download_list("E", only)
        out.update(jisilu.parse_list(raws[only]))
    if save:
        rows = [{"id": k, "cell": v} for k, v in sorted(out.items())]
        (DATA / "qdii_etf_list.json").write_text(json.dumps({"page": 1, "rows": rows}, ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
    return out


def _num(x) -> float | None:
    try:
        return float(str(x).rstrip("%"))
    except (TypeError, ValueError):
        return None


def compare(fund: FundConfig, cell: dict) -> dict:
    """返回一行核对结果。溢价率单位为百分比。"""
    md = store.load(fund)
    price_dt, nav_dt = date.fromisoformat(cell["price_dt"]), date.fromisoformat(cell["nav_dt"])
    close = md.etf.get(price_dt, {}).get("close")
    nav = md.nav4.get(nav_dt)
    js_price, js_nav, js_prem = _num(cell.get("price")), _num(cell.get("fund_nav")), _num(cell.get("nav_discount_rt"))
    ours = (close / nav - 1) * 100 if close and nav else None
    return {
        "fund": fund.fund_id, "name": fund.name, "price_dt": price_dt, "nav_dt": nav_dt,
        "close": close, "js_price": js_price, "nav": nav, "js_nav": js_nav, "premium": ours, "js_premium": js_prem,
        "price_ok": close is not None and js_price is not None and abs(close - js_price) < 1e-9,
        "nav_ok": nav is not None and js_nav is not None and abs(nav - js_nav) < 1e-9,
        "premium_ok": ours is not None and js_prem is not None and abs(ours - js_prem) <= 0.006,
    }
