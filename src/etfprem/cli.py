"""命令行：etfprem update | history | estimate"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta

from . import history, store
from .config import REPORTS, get_fund, select_funds
from .estimator import make_estimator


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def cmd_update(a: argparse.Namespace) -> int:
    failed = []
    for fund in select_funds(a.fund):
        print(f"== {fund.fund_id} {fund.name}")
        end = _d(a.end) or date.today()
        start = _d(a.start) or date.fromisoformat(fund.first_date)
        try:
            store.update(fund, start, end, jisilu_hist=not a.no_jisilu)
        except Exception as e:  # noqa: BLE001 批量更新时一只失败不影响其他
            if "," not in a.fund and a.fund != "all":
                raise
            failed.append(fund.fund_id)
            print(f"   失败：{e!r}", file=sys.stderr)
    if failed:
        print(f"更新失败：{', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


def cmd_history(a: argparse.Namespace) -> int:
    funds = select_funds(a.fund)
    if len(funds) == 1:
        return _history_one(funds[0], a)[0]
    from . import report

    results = []
    for fund in funds:
        print(f"\n== {fund.fund_id} {fund.name}")
        code, summary = _history_one(fund, a)
        if summary:
            results.append((fund, summary))
    if results and not a.no_files:
        for p in report.write_overview(results, a.out or REPORTS):
            print(f"汇总：{p}")
    return 0


def _history_one(fund, a: argparse.Namespace) -> tuple[int, dict | None]:
    if a.update:
        end = _d(a.end) or date.today()
        start = (_d(a.start) or end - timedelta(days=365)) - timedelta(days=60)
        store.update(fund, start, end)
    md = store.load(fund)
    if not md.etf or not md.nav or (fund.holdings == "pcf" and not md.pcf) or (fund.holdings == "reports" and not md.reports) \
            or (fund.holdings == "index" and not md.index):
        print("本地没有数据，请先运行：etfprem update", file=sys.stderr)
        return 1, None
    end = _d(a.end) or max(md.etf)
    start = _d(a.start) or end - timedelta(days=365)
    est = make_estimator(md)
    rows = history.compute(md, start, end, est)
    if not rows:
        print("区间内没有可计算的交易日", file=sys.stderr)
        return 1, None
    summary = history.summarize(rows)
    from . import report

    print(report.console_summary(fund, summary))
    if not a.no_files:
        paths = report.write_all(fund, rows, summary, est, a.out or REPORTS)
        print("\n输出文件：")
        for p in paths:
            print(f"  {p}")
    for n in sorted(set(est.notes))[:20]:
        print(f"注：{n}")
    return 0, summary


def cmd_estimate(a: argparse.Namespace) -> int:
    from .sources import sina

    fund = get_fund(a.fund)
    today = date.today()
    if not a.no_update:
        store.update(fund, today - timedelta(days=75), today, log=lambda m: None, jisilu_hist=False)
    md = store.load(fund)
    try:
        q = sina.realtime(fund.fund_id, fund.market)
        price, when, T = q["price"], f"{q['date']} {q['time']}", q["date"]
        md.etf.setdefault(T, {"close": price})  # 让日历包含今天
    except Exception as e:  # noqa: BLE001 实时行情失败时退回日线
        T = max(md.etf)
        price, when = md.etf[T]["close"], f"{T} 收盘（实时行情失败：{e}）"
    est = make_estimator(md)
    D = est.cal.prev_cn(T)
    b = est.estimate(D, "basket") if D else None
    if b is None:
        print("数据不足，无法估算", file=sys.stderr)
        return 1
    i = est.estimate(D, "index")
    fv, gap = est.fair_value(T, b)
    st = b.step
    print(f"{fund.fund_id} {fund.name}")
    print(f"  价格日 T              {T}")
    print(f"  场内价格              {price:.3f}  @ {when}")
    print(f"  锚定净值 NAV({st.Dp})  {b.nav_prev:.6f}（官方）")
    if fund.holdings == "index":
        sr = fund.index_series
        print(f"  {sr.name}  {st.s0} → {st.s1}  {b.r:+.4%}")
        if sr.currency != "CNY":
            print(f"  {sr.currency} 汇率 {st.fx0:.4f} → {st.fx1:.4f}  敞口 {b.exposure:.4f}（{b.n_obs} 个观测）")
        else:
            print(f"  敞口 {b.exposure:.4f}（{b.n_obs} 个观测）")
    elif fund.holdings == "pcf":
        line = f"  美股 {st.s0} → {st.s1}  篓子 {b.r:+.4%}"
        print(line + (f"  指数 {st.r_index:+.4%}" if st.r_index is not None else ""))
        print(f"  清单 {st.holdings_date}  中间价 {st.fx0:.4f} → {st.fx1:.4f}  敞口 {b.exposure:.4f}（{b.n_obs} 个观测）")
    else:
        rep = est.report_for(T)
        m = re.search(r"\d{4}\s*年\S*?报告", rep.title)
        print(f"  持仓报告              {m.group(0) if m else rep.title}（报告期末 {rep.period_end}，{rep.publish_date} 发布）")
        for k, label in (("foreign", "海外"), ("cn", "A 股")):
            print(f"  {label}部分贡献 {st.parts[k]:+.4%}  敞口系数 {b.betas[k]:.3f}")
        print(f"  美元中间价 {st.fx0:.4f} → {st.fx1:.4f}  有效敞口 {b.exposure:.4f}（{b.n_obs} 个观测）")
    print(f"  费用                  {b.fee:.6f}")
    print(f"  估算净值 NAV({D})   {b.nav:.4f}" + (f"  指数法 {i.nav:.4f}" if i else ""))
    print(f"  溢价率                {price / b.nav - 1:+.2%}" + (f"  指数法 {price / i.nav - 1:+.2%}" if i else ""))
    if gap:
        print(f"  长假后公允价值        {fv:.4f}（补 {gap} 个境外交易日）  溢价 {price / fv - 1:+.2%}")
    if D in md.nav4:
        print(f"  官方净值 NAV({D})   {md.nav4[D]:.4f}  官方口径溢价 {price / md.nav4[D] - 1:+.2%}")
    for n in sorted(set(est.notes)):
        print(f"注：{n}")
    return 0


def cmd_model(a: argparse.Namespace) -> int:
    try:
        from . import analysis, model_report
    except ImportError as e:
        print(f"建模需要可选依赖：uv sync --extra analysis（{e}）", file=sys.stderr)
        return 1
    funds = [get_fund(f.strip()) for f in a.funds.split(",") if f.strip()]
    if a.update:
        for fund in funds:
            store.update_long_history(fund)
    results = [analysis.analyze(fund) for fund in funds]
    print(model_report.console_summary(results))
    if not a.no_files:
        print("\n输出文件：")
        for p in model_report.write_all(results, a.out or REPORTS):
            print(f"  {p}")
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    from . import check

    cells = check.snapshot()
    bad = 0
    print("基金    名称                价格日      收盘价  集思录  净值日      公布净值  集思录    溢价率   集思录")
    for fund in select_funds(a.fund):
        cell = cells.get(fund.fund_id)
        if not cell:
            print(f"{fund.fund_id}  集思录列表里没有这只基金")
            continue
        r = check.compare(fund, cell)
        ok = r["price_ok"] and r["nav_ok"] and r["premium_ok"]
        bad += not ok
        f = lambda x, n: "—" if x is None else f"{x:.{n}f}"  # noqa: E731
        print(f"{r['fund']}  {r['name']:<12}  {r['price_dt']}  {f(r['close'], 3):>6}  {f(r['js_price'], 3):>6}  "
              f"{r['nav_dt']}  {f(r['nav'], 4):>7}  {f(r['js_nav'], 4):>7}  {f(r['premium'], 3):>7}%  "
              f"{f(r['js_premium'], 2):>6}%  {'一致' if ok else '不一致'}")
    print(f"\n{bad} 只不一致" if bad else "\n全部一致")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="etfprem", description="QDII ETF 溢价率计算器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("update", help="下载或刷新数据到 data/")
    u.add_argument("--fund", default="159509", help="基金代码，逗号分隔多只，或 all")
    u.add_argument("--start", help="起始日期，默认取基金配置的 first_date")
    u.add_argument("--end", help="结束日期，默认今天")
    u.add_argument("--no-jisilu", action="store_true", help="不抓集思录历史溢价")
    u.set_defaults(func=cmd_update)

    h = sub.add_parser("history", help="计算区间内每日溢价率并输出报告")
    h.add_argument("--fund", default="159509", help="基金代码，逗号分隔多只，或 all（另写汇总表）")
    h.add_argument("--start", help="起始价格日，默认结束日前 365 天")
    h.add_argument("--end", help="结束价格日，默认最新交易日")
    h.add_argument("--out", help="报告目录，默认 reports/")
    h.add_argument("--update", action="store_true", help="计算前先刷新数据")
    h.add_argument("--no-files", action="store_true", help="只打印汇总，不写文件")
    h.set_defaults(func=cmd_history)

    e = sub.add_parser("estimate", help="估算最新交易日的净值与溢价率")
    e.add_argument("--fund", default="159509")
    e.add_argument("--no-update", action="store_true", help="不刷新数据，直接用缓存")
    e.set_defaults(func=cmd_estimate)

    c = sub.add_parser("check", help="用集思录列表的当前快照核对收盘价、公布净值和溢价率")
    c.add_argument("--fund", default="all", help="基金代码，逗号分隔多只，或 all")
    c.set_defaults(func=cmd_check)

    m = sub.add_parser("model", help="溢价率与后续价格走势的建模分析")
    m.add_argument("--funds", default="159509,501225", help="逗号分隔的基金代码")
    m.add_argument("--update", action="store_true", help="先下载成立以来的净值与日线")
    m.add_argument("--out", help="报告目录，默认 reports/")
    m.add_argument("--no-files", action="store_true", help="只打印摘要，不写文件")
    m.set_defaults(func=cmd_model)

    a = ap.parse_args(argv)
    return a.func(a)
