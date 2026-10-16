"""输出：终端摘要、CSV 明细、Markdown 摘要、HTML 图表报告。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .config import FundConfig
from .estimator import Estimator
from .history import Row


def _pct(x: float | None, nd: int = 2) -> str:
    return "" if x is None else f"{x * 100:.{nd}f}%"


def _f(x: float | None, nd: int) -> str:
    return "" if x is None else f"{x:.{nd}f}"


FEEDER_SOURCES = ("csop", "lionglobal", "b3")  # 这些数据源给的是被投资 ETF 的净值或收盘价，不是指数


def is_feeder(fund: FundConfig) -> bool:
    """联接基金：持仓只有一只境外上市的 ETF，用它的收盘价估值。"""
    sr = fund.index_series
    return fund.holdings == "index" and sr is not None and (sr.source in FEEDER_SOURCES or sr.symbol.startswith("hk"))


def method_label(fund: FundConfig) -> str:
    """主估算方法的简称。"""
    if fund.holdings == "index":
        return "持仓ETF法" if is_feeder(fund) else "指数法"
    return "篓子法"


def _ccy_name(ccy: str) -> str:
    return {"USD": "美元", "EUR": "欧元", "HKD": "港元", "BRL": "雷亚尔", "CNY": "人民币", "SGD": "新加坡元",
            "SAR": "沙特里亚尔"}.get(ccy, ccy)


def method_lines(fund: FundConfig) -> list[str]:
    fee = f"{fund.annual_fee:.2%}"
    if fund.holdings == "index":
        sr = fund.index_series
        ccy = _ccy_name(sr.currency)
        if is_feeder(fund) and sr.source == "b3":
            first = (f"基金是联接型 ETF，持仓只有一只巴西上市的 ETF：{sr.name}。基金合同规定按它在 B3 的收盘价估值，"
                     "所以用 B3 官方行情文件里的收盘价从 Dp 到 D 的涨跌代表持仓涨跌。")
        elif is_feeder(fund):
            first = (f"基金是联接型 ETF，持仓只有一只境外 ETF。基金合同规定按被投资 ETF 的份额净值估值，"
                     f"所以用{sr.name}从 Dp 到 D 的涨跌代表持仓涨跌；它不估值的日子沿用最近一日的净值。"
                     "被投资 ETF 在场内几乎没有成交，用收盘价误差会大几十倍。")
        elif sr.source == "nasdaq_etf":
            first = (f"跟踪的标普行业指数没有免费日线，用跟踪同一指数的美国 ETF：{sr.name}的收盘价代替，"
                     "两端各取在该日或之前最近的收盘。")
        else:
            first = f"用跟踪指数{sr.name}从 Dp 到 D 的涨跌代表持仓涨跌，两端各取指数在该日或之前最近的收盘。"
        if sr.published:
            first += "被投资 ETF 的净值要到下一个港股交易日才公布，碰上港股假期，基金当天的净值用不到它，要顺延到之后的净值日。"
        fx = ("指数以人民币计价，不做汇率换算。" if sr.currency == "CNY"
              else f"汇率用外汇交易中心{ccy}中间价，取净值日 D 与前一净值日 Dp 当天的值。")
        if fund.fx_pair.endswith("@16"):
            fx = (f"汇率用外汇交易中心北京时间 16:00 {ccy}兑人民币参考汇率，取 D 与 Dp 当天的值。华夏基金按这个汇率估值，"
                  "用 9:15 的中间价误差会大十倍。")
        if sr.currency == "BRL":
            rule = "北京时间 22:00（巴西利亚 11:00）" if fund.fx_pair.endswith("BJ22") else "伦敦 16:00（巴西利亚 12:00 或 13:00）"
            fx = ("外汇交易中心不公布雷亚尔中间价，按基金合同以美元套算：美元兑人民币用外汇交易中心 16:00 参考汇率，"
                  f"美元兑雷亚尔用{rule}的报价，取巴西央行同一时点的询价公告；巴西外汇休市的日子用离岸报价补。")
        if sr.currency == "SAR":
            fx = "汇率用外汇交易中心人民币对沙特里亚尔中间价（取倒数）。美元或港元中间价套算都对不上，二者每天差几个基点。"
        timing = {
            "csop": "所持 ETF 的净值由南方东英在 T 日白天公布，T 日收盘时已经可用，开盘前不一定拿得到。",
            "lionglobal": "利安要到 D 之后第二个新加坡交易日下午才公布净值，T 日实时估算时只能沿用更早的净值，误差会比回测大。",
            "b3": "B3 的行情文件在北京时间 T 日早上 8 点前发布，开盘前可用。",
        }.get(sr.source, "整个过程只用 T 日开盘前已公开的数据。")
        return [
            first,
            fx,
            "用过去 20 个净值日的官方净值回归出真实敞口，吸收现金仓位、分红和跟踪误差的平均影响。上市前的净值日不参与回归。",
            f"估算净值 = NAV(Dp) × (1 + 敞口 × 涨跌) × 汇率变动 − 年费率 {fee} 的按日计提。",
            "溢价率 = 收盘价 / 估算净值 − 1。" + timing,
        ]
    if fund.holdings == "reports":
        return [
            "基金没有每日申赎清单。持仓取价格日 T 之前已发布的最新一份定期报告里的基金投资明细，按发布日生效，不按报告期末。",
            "按报告里各持仓占净值的比例固定权重、每日再平衡。实测比“份额不变、权重漂移”更贴近净值，说明基金经理会频繁调回目标权重。",
            "美国 ETF 用 Nasdaq 收盘价并计入扣除 10% 预提税后的分红，A 股 ETF 用腾讯后复权价以消除份额拆分，东京 ETF 用株探收盘价，各自按当日中间价折成人民币。",
            "海外与 A 股两部分的敞口分别用过去 20 个净值日的官方净值回归，两部分系数都接近 1，即与报告比例基本一致。",
            f"估算净值 = 前一净值日官方净值 × (1 + Σ 敞口系数 × 各部分贡献) − 年费率 {fee} 的按日计提。",
            "溢价率 = 收盘价 / 估算净值 − 1。整个过程只用 T 日开盘前已公开的数据。",
        ]
    lines = [
        "价格日 T 开盘前，深交所申赎清单 PCF(T) 给出每个申赎单位对应的成分股股数，以及前两个交易日的官方净值 NAV(Dp)。",
        "清单股数按拆股换算到复权口径：清单给出申购替代金额时用它推算原始价格；没有时比较前后两份清单的股数，"
        "再用未复权价核对，指数调仓造成的整数倍变化不算拆股。拆股当天清单把该股写成 0 股，此时沿用前一份清单。",
        "用 Nasdaq 收盘价算出成分篓子从 Dp 到 D 的美元收益，计入扣除 10% 预提税后的分红。",
        "用过去 20 个净值日回归出真实股票敞口，约 95% 到 98%，清单篓子本身按净值的 97.8% 到 100% 编制，二者不一致。",
        f"估算净值 = NAV(Dp) × (1 + 敞口 × 篓子收益) × 中间价变动 − 年费率 {fee} 的按日计提。",
        "溢价率 = 收盘价 / 估算净值 − 1。整个过程只用 T 日开盘前已公开的数据。",
    ]
    if fund.pcf_fund:
        lines[0] = (f"上交所不提供历史申赎清单。基金跟踪的指数没有免费的日线，所以借用跟踪同一指数的深市 ETF {fund.pcf_fund} "
                    "的每日申赎清单作成分篓子，只用其中各股票的股数比例；锚定净值和回归都用本基金自己的公布净值。")
    return lines


# ------------------------------------------------------------------ 终端

def jisilu_note(s: dict) -> str:
    """说明公布净值口径与集思录的差异来源。"""
    beyond, mismatch = s.get("jisilu_beyond_rounding", []), set(s.get("jisilu_price_mismatch", []))
    other = s.get("jisilu_other_nav_date", [])
    tail = ""
    if other:
        tail = "；另有 " + "、".join(f"{t}（集思录用 {jd} 的净值，本表用 {d}）" for t, jd, d in other) + \
               " 集思录沿用了更早的净值，口径不同，不参与比较"
    if not beyond:
        return "全部在两位小数舍入以内" + tail
    days = "、".join(str(d) for d in beyond)
    if set(beyond) <= mismatch:
        return f"{days} 超出舍入，当天两边的收盘价不同，其余为舍入" + tail
    return f"{days} 超出舍入，其余为舍入" + tail


def console_summary(fund: FundConfig, s: dict) -> str:
    p, ne, ni, pe = s["premium"], s["nav_err_bp"], s["nav_err_index_bp"], s["premium_err_pp"]
    latest_d, latest_v, latest_is_est = p["latest"]
    lines = [
        f"{fund.fund_id} {fund.name}  {s['first']} ~ {s['last']}"
        f"（{s['days']} 个交易日，{s['days_with_official']} 天已有公布净值）",
        "溢价率 = 收盘价 / 前一交易日净值 − 1",
        f"  最新 {latest_d}  {_pct(latest_v)}" + ("（净值未公布，为估算值）" if latest_is_est else ""),
        f"  均值 {_pct(p['mean'])}  中位数 {_pct(p['median'])}  "
        f"最高 {_pct(p['max'][1])}（{p['max'][0]}）  最低 {_pct(p['min'][1])}（{p['min'][0]}）",
        "估算精度（与公布净值比）",
        f"  净值误差  {method_label(fund)} 平均绝对 {ne['mae']:.2f}bp  均方根 {ne['rms']:.2f}bp  最大 {ne['max']:.2f}bp  "
        + "  ".join(f"{k}bp 内 {v:.0%}" for k, v in s["nav_within_bp"].items()),
        *([f"            指数法 平均绝对 {ni['mae']:.2f}bp  均方根 {ni['rms']:.2f}bp  最大 {ni['max']:.2f}bp"] if ni else []),
        f"  溢价率误差 平均绝对 {pe['mae']:.3f}pp  最大 {pe['max']:.3f}pp  "
        + "  ".join(f"{k}pp 内 {v:.0%}" for k, v in s["premium_within_pp"].items()),
    ]
    if s["jisilu_days"]:
        jo, je = s["jisilu_vs_official_pp"], s["jisilu_vs_est_pp"]
        lines += [
            f"与集思录历史表对照（{s['jisilu_days']} 天）",
            f"  公布净值口径 vs 集思录  最大差 {jo['max']:.3f}pp，{jisilu_note(s)}",
            f"  {method_label(fund)}估算 vs 集思录  平均绝对 {je['mae']:.3f}pp  最大 {je['max']:.3f}pp",
        ]
    lines.append("月度溢价率（均值 / 最低 / 最高）")
    for m, v in s["monthly"].items():
        lines.append(f"  {m}  {_pct(v['mean']):>7} {_pct(v['min']):>7} {_pct(v['max']):>7}  {v['n']:>2} 天")
    return "\n".join(lines)


# ------------------------------------------------------------------ CSV

def csv_columns(label: str) -> list:
    """逐日明细的列。label 为主估算方法的简称，例如“篓子法”“指数法”。"""
    cols = [(name.replace("篓子法", label).replace("篓子估算", label.removesuffix("法") + "估算")
             .replace("篓子收益", label.removesuffix("法") + "涨跌"), fn) for name, fn in CSV_COLUMNS]
    if label != "篓子法":
        cols = [("境外收盘日" if name == "持仓日期" else name, fn) for name, fn in cols]
    return cols


CSV_COLUMNS = [
    ("日期", lambda r: r.date.isoformat()),
    ("收盘价", lambda r: _f(r.close, 3)),
    ("参考净值日", lambda r: r.nav_date.isoformat()),
    ("公布净值", lambda r: _f(r.nav_official, 4)),
    ("估算净值_篓子法", lambda r: _f(r.nav_est, 6)),
    ("估算净值_指数法", lambda r: _f(r.nav_est_index, 6)),
    ("净值误差_篓子法_bp", lambda r: _f(r.nav_err_bp, 3)),
    ("净值误差_指数法_bp", lambda r: _f(r.nav_err_index_bp, 3)),
    ("溢价率_公布净值_%", lambda r: _f(r.premium_official and r.premium_official * 100, 4)),
    ("溢价率_篓子估算_%", lambda r: _f(r.premium_est * 100, 4)),
    ("溢价率_指数估算_%", lambda r: _f(r.premium_est_index and r.premium_est_index * 100, 4)),
    ("溢价率差_估算减公布_pp", lambda r: _f(r.premium_err_pp, 4)),
    ("集思录溢价率_%", lambda r: _f(r.jisilu_premium and r.jisilu_premium * 100, 2)),
    ("长假补算境外交易日", lambda r: str(r.gap_sessions)),
    ("开盘公允价值", lambda r: _f(r.fair_value, 6)),
    ("溢价率_公允价值_%", lambda r: _f(r.premium_fv * 100, 4)),
    ("有效敞口", lambda r: _f(r.exposure, 4)),
    ("篓子收益_%", lambda r: _f(r.basket_ret_usd * 100, 4)),
    ("指数美元收益_%", lambda r: _f(r.index_ret_usd and r.index_ret_usd * 100, 4)),
    ("中间价", lambda r: _f(r.fx, 4)),
    ("持仓日期", lambda r: r.holdings_date.isoformat()),
]


def write_csv(rows: list[Row], path: Path, label: str = "篓子法") -> None:
    cols = csv_columns(label)
    table = [[fn(r) for _, fn in cols] for r in rows]
    keep = [i for i in range(len(cols)) if any(row[i] for row in table)]  # 去掉整列为空的列（如无指数）
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([cols[i][0] for i in keep])
        for row in table:
            w.writerow([row[i] for i in keep])


# ------------------------------------------------------------------ Markdown

def write_markdown(fund: FundConfig, rows: list[Row], s: dict, est: Estimator, path: Path, csv_name: str) -> None:
    p, ne, ni, pe = s["premium"], s["nav_err_bp"], s["nav_err_index_bp"], s["premium_err_pp"]
    latest_d, latest_v, latest_is_est = p["latest"]
    gaps = [r for r in rows if r.gap_sessions]
    out = [
        f"# {fund.fund_id} {fund.name} 溢价率（{s['first']} 至 {s['last']}）",
        "",
        f"共 {s['days']} 个交易日，其中 {s['days_with_official']} 天的参考净值已公布。逐日明细见 `{csv_name}`。",
        "",
        "## 结果",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 最新（{latest_d}） | {_pct(latest_v)}{'，净值未公布，为估算值' if latest_is_est else ''} |",
        f"| 均值 | {_pct(p['mean'])} |",
        f"| 中位数 | {_pct(p['median'])} |",
        f"| 最高 | {_pct(p['max'][1])}（{p['max'][0]}） |",
        f"| 最低 | {_pct(p['min'][1])}（{p['min'][0]}） |",
        "",
        "溢价率口径与集思录一致：收盘价除以前一个 A 股交易日的基金净值再减 1。"
        + ("已公布净值的日子用公布净值，最新一天净值未公布，用估算净值。" if latest_is_est else "所有日子的净值都已公布，表中用公布净值。"),
        "",
        "## 估算精度",
        "",
        "| 方法 | 平均偏差 | 平均绝对误差 | 均方根 | 最大 |",
        "|---|---|---|---|---|",
        f"| 净值，{method_label(fund)} | {ne['mean']:+.2f}bp | {ne['mae']:.2f}bp | {ne['rms']:.2f}bp | {ne['max']:.2f}bp |",
        *([f"| 净值，指数法（对照） | {ni['mean']:+.2f}bp | {ni['mae']:.2f}bp | {ni['rms']:.2f}bp | {ni['max']:.2f}bp |"] if ni else []),
        f"| 溢价率，{method_label(fund)} | {pe['mean']:+.4f}pp | {pe['mae']:.4f}pp | {pe['rms']:.4f}pp | {pe['max']:.4f}pp |",
        "",
        "净值误差落在 1、2、5 个基点以内的比例："
        + "、".join(f"{v:.0%}" for v in s["nav_within_bp"].values())
        + "。溢价率误差落在 0.01、0.02、0.05 个百分点以内的比例："
        + "、".join(f"{v:.0%}" for v in s["premium_within_pp"].values())
        + "。",
        "",
    ]
    if s["jisilu_days"]:
        jo, je = s["jisilu_vs_official_pp"], s["jisilu_vs_est_pp"]
        out += [
            f"与集思录历史表重叠 {s['jisilu_days']} 天。用公布净值算出的溢价率与集思录最大相差 {jo['max']:.3f} 个百分点，"
            f"{jisilu_note(s)}。用{method_label(fund)}估算净值算出的溢价率与集思录平均相差 {je['mae']:.3f} 个百分点，最大 {je['max']:.3f}。",
            "",
        ]
    out += [
        "## 月度",
        "",
        "| 月份 | 交易日 | 均值 | 最低 | 最高 | 月末收盘 | 净值误差平均绝对值 |",
        "|---|---|---|---|---|---|---|",
    ]
    for m, v in s["monthly"].items():
        err = f"{v['nav_err_mae_bp']:.2f}bp" if v["nav_err_mae_bp"] is not None else "—"
        out.append(
            f"| {m} | {v['n']} | {_pct(v['mean'])} | {_pct(v['min'])} | {_pct(v['max'])} | {v['close_last']:.3f} | {err} |"
        )
    if gaps:
        out += [
            "",
            "## 长假后的首个交易日",
            "",
            "这些日子参考净值之后境外市场又交易了几天，按集思录口径算出的溢价率偏离真实水平。"
            "“开盘公允价值”把这几天的海外涨跌和当天中间价补进来。",
            "",
            "| 日期 | 补算境外交易日 | 集思录口径溢价率 | 公允价值溢价率 | 集思录历史表 |",
            "|---|---|---|---|---|",
        ]
        js_dates = [r.date for r in rows if r.jisilu_premium is not None]
        for r in gaps:
            if r.jisilu_premium is not None:
                js = _pct(r.jisilu_premium)
            elif js_dates and min(js_dates) <= r.date <= max(js_dates):
                js = "无此行"
            else:
                js = "不在历史表范围内"
            out.append(f"| {r.date} | {r.gap_sessions} | {_pct(r.premium_official or r.premium_est)} | {_pct(r.premium_fv)} | {js} |")
    out += ["", "## 方法", ""] + [f"{i}. {m}" for i, m in enumerate(method_lines(fund), 1)] + [""]
    notes = sorted(set(est.notes))
    if notes:
        out += ["## 数据备注", ""] + [f"- {n}" for n in notes] + [""]
    path.write_text("\n".join(out), encoding="utf-8")


# ------------------------------------------------------------------ HTML

_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --context: #c3c2b7;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --context: #52514e;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --context: #52514e;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.55 system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
main { max-width: 1080px; margin: 0 auto; padding: 28px 16px 48px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 16px; font-weight: 600; margin: 0 0 4px; }
.sub { color: var(--ink-2); margin: 0 0 20px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile, .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }
.tile { padding: 12px 14px; }
.tile .label { color: var(--ink-2); font-size: 13px; }
.tile .value { font-size: 24px; font-weight: 600; margin-top: 2px; }
.tile .note { color: var(--muted); font-size: 12px; }
.card { padding: 16px 16px 8px; margin-bottom: 16px; }
.card p.desc { color: var(--ink-2); margin: 0 0 8px; font-size: 13px; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; color: var(--ink-2); font-size: 13px; margin: 4px 0 6px; }
.key { display: inline-block; width: 16px; height: 2px; vertical-align: middle; margin-right: 6px; border-radius: 1px; }
.chart { position: relative; width: 100%; outline: none; }
.chart:focus-visible { box-shadow: 0 0 0 2px var(--series-1); border-radius: 6px; }
.chart svg { display: block; width: 100%; overflow: visible; }
.chart text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.12);
  min-width: 170px; display: none; z-index: 2; }
.tip .d { color: var(--ink-2); margin-bottom: 4px; }
.tip .row { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
.tip .row b { font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }
.tip .row span { color: var(--ink-2); }
table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--ink-2); font-weight: 500; }
.scroll { overflow-x: auto; }
details summary { cursor: pointer; color: var(--ink-2); padding: 8px 0; }
.method { color: var(--ink-2); font-size: 13px; }
.method li { margin: 2px 0; }
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <p class="sub">__SUBTITLE__</p>
  <section class="kpis" id="kpis"></section>

  <section class="card">
    <h2>每日溢价率</h2>
    <p class="desc">收盘价除以前一交易日净值再减 1。蓝线用__BASKET__估算的净值，灰线用基金公布的净值，两线基本重合。</p>
    <div class="legend"><span><i class="key" style="background:var(--series-1)"></i>__BASKET__估算</span>
      <span><i class="key" style="background:var(--context)"></i>公布净值</span></div>
    <div class="chart" id="premium" tabindex="0" aria-label="每日溢价率折线图，可用左右方向键逐日查看"></div>
  </section>

  <section class="card">
    <h2>净值估算误差</h2>
    <p class="desc">估算净值相对公布净值的偏差，单位为基点（0.01%）。正值表示估算偏高。</p>
    <div class="chart" id="error" tabindex="0" aria-label="每日净值估算误差柱状图，可用左右方向键逐日查看"></div>
  </section>

  <section class="card">
    <h2>月度汇总</h2>
    <div class="scroll"><table id="monthly"></table></div>
    <details><summary>逐日明细</summary><div class="scroll"><table id="daily"></table></div></details>
  </section>

  <section class="card method">
    <h2>方法</h2>
    <ol>__METHOD__</ol>
  </section>
</main>
<script>
const DATA = __DATA__;
const SUMMARY = __SUMMARY__;
const pct = (v, nd = 2) => v == null ? '—' : (v * 100).toFixed(nd) + '%';
const num = (v, nd) => v == null ? '—' : v.toFixed(nd);
const NS = 'http://www.w3.org/2000/svg';
function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function text(parent, x, y, s, anchor) {
  const t = el('text', { x, y, 'text-anchor': anchor || 'middle' }, parent);
  t.textContent = s;
  return t;
}
function niceStep(range, target) {
  const raw = range / target, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// KPI 卡片
(function () {
  const k = document.getElementById('kpis');
  const p = SUMMARY.premium;
  const tiles = [
    ['最新溢价率', pct(p.latest[1]), p.latest[0] + (p.latest[2] ? '，净值未公布，为估算' : '')],
    ['近一年均值', pct(p.mean), '中位数 ' + pct(p.median)],
    ['最高', pct(p.max[1]), p.max[0]],
    ['最低', pct(p.min[1]), p.min[0]],
    ['净值估算误差', SUMMARY.nav_err_bp.mae.toFixed(2) + ' bp', '平均绝对值，最大 ' + SUMMARY.nav_err_bp.max.toFixed(1) + ' bp'],
  ];
  for (const [label, value, note] of tiles) {
    const d = document.createElement('div'); d.className = 'tile';
    for (const [cls, s] of [['label', label], ['value', value], ['note', note]]) {
      const x = document.createElement('div'); x.className = cls; x.textContent = s; d.appendChild(x);
    }
    k.appendChild(d);
  }
})();

// 通用：时间轴 + 十字线/最近点悬停
function chart(host, opts) {
  const tip = document.createElement('div'); tip.className = 'tip'; host.appendChild(tip);
  let svg = null, xs = [], active = -1, geom = null;
  function draw() {
    if (svg) svg.remove();
    const W = host.clientWidth, H = opts.height, m = Object.assign({ l: 46, r: 16, t: 10, b: 26 }, opts.margin || {});
    const pw = W - m.l - m.r, ph = H - m.t - m.b, n = DATA.length;
    svg = el('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img' });
    host.insertBefore(svg, tip);
    const [lo, hi] = opts.domain();
    const step = niceStep(hi - lo, opts.ticks || 5);
    const y0 = Math.floor(lo / step) * step, y1 = Math.ceil(hi / step) * step;
    const X = i => m.l + (n === 1 ? pw / 2 : (i + 0.5) * pw / n);
    const Y = v => m.t + ph - (v - y0) / (y1 - y0) * ph;
    xs = DATA.map((_, i) => X(i));
    const g = el('g', {}, svg);
    for (let v = y0; v <= y1 + step / 2; v += step) {
      el('line', { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: cssVar(Math.abs(v) < 1e-12 ? '--axis' : '--grid'), 'stroke-width': 1 }, g);
      text(g, m.l - 6, Y(v) + 4, opts.tick(v), 'end');
    }
    let lastMonth = '', lastLabelX = -Infinity;
    DATA.forEach((r, i) => {
      const mo = r.d.slice(0, 7);
      if (mo !== lastMonth) {
        lastMonth = mo;
        el('line', { x1: X(i), x2: X(i), y1: m.t + ph, y2: m.t + ph + 4, stroke: cssVar('--axis'), 'stroke-width': 1 }, g);
        // 标签之间至少留 44px，避免短月份（如春节所在的 2 月）挤在一起
        if (X(i) - lastLabelX >= 44) { text(g, X(i), H - 6, mo.slice(2).replace('-', '/')); lastLabelX = X(i); }
      }
    });
    opts.render(svg, { X, Y, m, pw, ph, W, H, n });
    const cross = el('line', { y1: m.t, y2: m.t + ph, stroke: cssVar('--axis'), 'stroke-width': 1, visibility: 'hidden' }, svg);
    const hit = el('rect', { x: m.l, y: 0, width: pw, height: H, fill: 'transparent' }, svg);
    geom = { cross, m, W };
    hit.addEventListener('pointermove', e => {
      const r = svg.getBoundingClientRect();
      const x = (e.clientX - r.left) * (W / r.width);
      let best = 0, bd = Infinity;
      xs.forEach((v, i) => { const d = Math.abs(v - x); if (d < bd) { bd = d; best = i; } });
      show(best);
    });
    hit.addEventListener('pointerleave', hide);
    if (active >= 0) show(active);
  }
  function show(i) {
    active = i;
    const { cross, W } = geom;
    cross.setAttribute('x1', xs[i]); cross.setAttribute('x2', xs[i]); cross.setAttribute('visibility', 'visible');
    opts.highlight && opts.highlight(svg, i);
    tip.replaceChildren();
    const d = document.createElement('div'); d.className = 'd'; d.textContent = DATA[i].d; tip.appendChild(d);
    for (const [label, value, color] of opts.rows(DATA[i])) {
      const row = document.createElement('div'); row.className = 'row';
      const s = document.createElement('span');
      if (color) { const key = document.createElement('i'); key.className = 'key'; key.style.background = color; s.appendChild(key); }
      s.appendChild(document.createTextNode(label));
      const b = document.createElement('b'); b.textContent = value;
      row.appendChild(s); row.appendChild(b); tip.appendChild(row);
    }
    tip.style.display = 'block';
    const scale = host.clientWidth / W, tw = tip.offsetWidth;
    let left = xs[i] * scale + 12;
    if (left + tw > host.clientWidth) left = xs[i] * scale - tw - 12;
    tip.style.left = Math.max(0, left) + 'px'; tip.style.top = '8px';
  }
  function hide() {
    active = -1; tip.style.display = 'none';
    if (geom) geom.cross.setAttribute('visibility', 'hidden');
    opts.highlight && opts.highlight(svg, -1);
  }
  host.addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const i = active < 0 ? DATA.length - 1 : Math.max(0, Math.min(DATA.length - 1, active + (e.key === 'ArrowRight' ? 1 : -1)));
    show(i);
  });
  host.addEventListener('focus', () => { if (active < 0) show(DATA.length - 1); });
  host.addEventListener('blur', hide);
  new ResizeObserver(draw).observe(host);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', draw);
  draw();
}

function path(points) {
  let s = '', pen = false;
  for (const p of points) {
    if (p == null) { pen = false; continue; }
    s += (pen ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1);
    pen = true;
  }
  return s;
}

chart(document.getElementById('premium'), {
  height: 300,
  margin: { r: 58 },
  domain: () => {
    const v = DATA.flatMap(r => [r.pe, r.po]).filter(x => x != null);
    return [Math.min(0, ...v), Math.max(...v)];
  },
  tick: v => (v * 100).toFixed(0) + '%',
  render(svg, { X, Y }) {
    const ctx = cssVar('--context'), s1 = cssVar('--series-1');
    el('path', { d: path(DATA.map((r, i) => r.po == null ? null : [X(i), Y(r.po)])), fill: 'none', stroke: ctx,
      'stroke-width': 4, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
    el('path', { d: path(DATA.map((r, i) => [X(i), Y(r.pe)])), fill: 'none', stroke: s1,
      'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, svg);
    const last = DATA.length - 1;
    el('circle', { cx: X(last), cy: Y(DATA[last].pe), r: 4, fill: s1, stroke: cssVar('--surface'), 'stroke-width': 2 }, svg);
    const t = text(svg, X(last) + 8, Y(DATA[last].pe) + 4, pct(DATA[last].pe), 'start');
    t.style.fill = cssVar('--ink'); t.style.fontWeight = 600;
    this._dot = el('circle', { r: 4, fill: s1, stroke: cssVar('--surface'), 'stroke-width': 2, visibility: 'hidden' }, svg);
    this._Y = Y; this._X = X;
  },
  highlight(svg, i) {
    if (!this._dot) return;
    if (i < 0) { this._dot.setAttribute('visibility', 'hidden'); return; }
    this._dot.setAttribute('cx', this._X(i)); this._dot.setAttribute('cy', this._Y(DATA[i].pe));
    this._dot.setAttribute('visibility', 'visible');
  },
  rows: r => [
    ['__BASKET__估算', pct(r.pe), cssVar('--series-1')],
    ['公布净值口径', r.po == null ? '净值未公布' : pct(r.po), cssVar('--context')],
    ['收盘价', num(r.c, 3)],
    ['估算净值', num(r.ne, 4)],
    ['公布净值', num(r.no, 4)],
  ],
});

chart(document.getElementById('error'), {
  height: 200,
  ticks: 4,
  domain: () => {
    const v = DATA.map(r => r.err).filter(x => x != null);
    const a = Math.max(...v.map(Math.abs));
    return [-a, a];
  },
  tick: v => (v > 0 ? '+' : '') + v.toFixed(0),
  render(svg, { X, Y, pw, n }) {
    const s1 = cssVar('--series-1');
    const w = Math.max(1, Math.min(24, pw / n - 2));
    this._bars = DATA.map((r, i) => {
      if (r.err == null) return null;
      const y = Math.min(Y(0), Y(r.err)), h = Math.max(1, Math.abs(Y(r.err) - Y(0)));
      return el('rect', { x: X(i) - w / 2, y, width: w, height: h, fill: s1, rx: Math.min(2, w / 2) }, svg);
    });
  },
  highlight(svg, i) {
    (this._bars || []).forEach((b, j) => b && b.setAttribute('opacity', i < 0 || i === j ? 1 : 0.35));
  },
  rows: r => [
    ['净值误差', r.err == null ? '净值未公布' : (r.err > 0 ? '+' : '') + r.err.toFixed(2) + ' bp', cssVar('--series-1')],
    ['溢价率差', r.dpp == null ? '—' : (r.dpp > 0 ? '+' : '') + r.dpp.toFixed(3) + ' pp'],
    ['有效敞口', num(r.ex, 4)],
  ],
});

// 表格视图
(function () {
  const t = document.getElementById('monthly');
  const head = ['月份', '交易日', '均值', '最低', '最高', '月末收盘', '净值误差（平均绝对值）'];
  const tr = document.createElement('tr');
  head.forEach(h => { const th = document.createElement('th'); th.textContent = h; tr.appendChild(th); });
  t.appendChild(tr);
  for (const [m, v] of Object.entries(SUMMARY.monthly)) {
    const row = document.createElement('tr');
    [m, v.n, pct(v.mean), pct(v.min), pct(v.max), v.close_last.toFixed(3),
      v.nav_err_mae_bp == null ? '—' : v.nav_err_mae_bp.toFixed(2) + ' bp'].forEach(s => {
      const td = document.createElement('td'); td.textContent = s; row.appendChild(td);
    });
    t.appendChild(row);
  }
  const d = document.getElementById('daily');
  const dh = ['日期', '收盘价', '参考净值日', '公布净值', '估算净值', '误差 bp', '溢价率（公布）', '溢价率（估算）', '集思录'];
  const r0 = document.createElement('tr');
  dh.forEach(h => { const th = document.createElement('th'); th.textContent = h; r0.appendChild(th); });
  d.appendChild(r0);
  for (const r of DATA.slice().reverse()) {
    const row = document.createElement('tr');
    [r.d, num(r.c, 3), r.nd, num(r.no, 4), num(r.ne, 4), r.err == null ? '—' : r.err.toFixed(2),
      pct(r.po), pct(r.pe), pct(r.js)].forEach(s => {
      const td = document.createElement('td'); td.textContent = s; row.appendChild(td);
    });
    d.appendChild(row);
  }
})();
</script>
</body>
</html>
"""


def _json_default(o):
    return o.isoformat() if hasattr(o, "isoformat") else str(o)


def write_html(fund: FundConfig, rows: list[Row], s: dict, path: Path) -> None:
    data = [
        {
            "d": r.date.isoformat(),
            "c": r.close,
            "nd": r.nav_date.isoformat(),
            "no": r.nav_official,
            "ne": round(r.nav_est, 6),
            "pe": round(r.premium_est, 6),
            "po": round(r.premium_official, 6) if r.premium_official is not None else None,
            "js": r.jisilu_premium,
            "err": round(r.nav_err_bp, 3) if r.nav_err_bp is not None else None,
            "dpp": round(r.premium_err_pp, 4) if r.premium_err_pp is not None else None,
            "ex": round(r.exposure, 4),
        }
        for r in rows
    ]
    method = method_lines(fund)
    basket = {"reports": "持仓篓子", "index": "持仓ETF" if is_feeder(fund) else "指数"}.get(fund.holdings, "成分篓子")
    how = {
        "reports": "净值由定期报告披露的持仓价格独立估算",
        "index": ("净值由所持境外 ETF 的价格独立估算" if is_feeder(fund) else "净值由跟踪指数与汇率独立估算"),
    }.get(fund.holdings, "净值由申赎清单成分股价格独立估算")
    html = (
        _HTML.replace("__TITLE__", f"{fund.fund_id} {fund.name} 溢价率")
        .replace("__SUBTITLE__", f"{s['first']} 至 {s['last']}，{s['days']} 个交易日。{how}，并与基金公布净值逐日核对。")
        .replace("__BASKET__", basket)
        .replace("__METHOD__", "".join(f"<li>{m}</li>" for m in method))
        .replace("__DATA__", json.dumps(data, ensure_ascii=False))
        .replace("__SUMMARY__", json.dumps(s, ensure_ascii=False, default=_json_default))
    )
    path.write_text(html, encoding="utf-8")


def write_all(fund: FundConfig, rows: list[Row], s: dict, est: Estimator, out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{fund.fund_id}_premium_{rows[0].date}_{rows[-1].date}"
    csv_path, md_path, html_path = (out_dir / f"{stem}.{ext}" for ext in ("csv", "md", "html"))
    write_csv(rows, csv_path, method_label(fund))
    write_markdown(fund, rows, s, est, md_path, csv_path.name)
    write_html(fund, rows, s, html_path)
    return [csv_path, md_path, html_path]


# ------------------------------------------------------------------ 多只基金汇总

def write_overview(results: list[tuple[FundConfig, dict]], out_dir: Path) -> list[Path]:
    """每只基金一行：区间、方法、溢价率统计、估算精度、与集思录的对照。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    head = ["基金", "名称", "方法", "区间", "交易日", "已公布净值", "溢价率均值", "中位数", "最高", "最低", "最新",
            "净值误差平均绝对bp", "均方根bp", "最大bp", "溢价率误差平均绝对pp", "集思录重叠天数", "公布净值口径与集思录最大差pp", "超出舍入的日子"]
    table = []
    for fund, s in results:
        p, ne, pe, jo = s["premium"], s["nav_err_bp"], s["premium_err_pp"], s["jisilu_vs_official_pp"]
        latest_d, latest_v, latest_is_est = p["latest"]
        table.append([
            fund.fund_id, fund.name, method_label(fund), f"{s['first']}~{s['last']}", str(s["days"]), str(s["days_with_official"]),
            _pct(p["mean"]), _pct(p["median"]), f"{_pct(p['max'][1])}（{p['max'][0]}）", f"{_pct(p['min'][1])}（{p['min'][0]}）",
            f"{_pct(latest_v)}（{latest_d}{'，估算' if latest_is_est else ''}）",
            _f(ne.get("mae"), 2), _f(ne.get("rms"), 2), _f(ne.get("max"), 1), _f(pe.get("mae"), 3),
            str(s["jisilu_days"]), _f(jo.get("max"), 3) if jo else "", "、".join(str(d) for d in s["jisilu_beyond_rounding"]),
        ])
    csv_path, md_path = out_dir / "overview.csv", out_dir / "overview.md"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(table)
    md = ["# 全部基金汇总", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    md += ["| " + " | ".join(row) + " |" for row in table]
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return [csv_path, md_path]
